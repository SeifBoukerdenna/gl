"""
Architecture registry — maps architecture names to modules.
Each architecture provides an ARCH_SPEC dict with:
  - name: str
  - combo_params: list of dicts (each must have "name" key)
  - check_signals: callable(state, now_s) — called once per Binance tick
  - extra_globals: dict of default config values (optional)
  - on_window_start: callable(state) (optional)
  - on_window_end: callable(state) (optional)
  - on_tick: callable(state, price, ts) (optional)
"""

import importlib
from pathlib import Path

_REGISTRY = {}


def _discover():
    """Find all architecture modules in this directory."""
    arch_dir = Path(__file__).parent
    for f in arch_dir.glob("*.py"):
        if f.name.startswith("_"):
            continue
        name = f.stem
        _REGISTRY[name] = "bot.architectures.{}".format(name)


_discover()


def load_architecture(name):
    """Load an architecture by name. Returns the ARCH_SPEC dict."""
    if name not in _REGISTRY:
        raise ValueError("Unknown architecture: '{}'. Available: {}".format(
            name, ", ".join(sorted(_REGISTRY.keys()))))
    module = importlib.import_module(_REGISTRY[name])
    spec = module.ARCH_SPEC
    assert spec["name"] == name, "ARCH_SPEC.name '{}' != '{}'".format(spec["name"], name)
    assert callable(spec["check_signals"]), "check_signals must be callable"
    assert isinstance(spec["combo_params"], list), "combo_params must be a list"
    return spec


def list_architectures():
    """Return list of available architecture names."""
    return sorted(_REGISTRY.keys())
