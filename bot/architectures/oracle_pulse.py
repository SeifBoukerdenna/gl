"""
Architecture: oracle_pulse (Edge Table + Impulse Confirmation Gate)

HYPOTHESIS
==========
Oracle bleeds in chop because it fires on the edge table alone, with no awareness
of whether BTC is actually moving in a sustained direction. During chop, BTC
oscillates around the strike — the edge table sees +5bp delta and says "fire YES"
but BTC is already reversing.

If oracle ALSO required a fresh BTC impulse in the same direction (Binance moved
≥3bp in last 15s in the bet direction), it would naturally skip whipsaw spikes.
By the time you've checked the impulse, BTC has either confirmed the move (real
trend) or partially reverted (chop, no fire).

This is the same gate that lets blitz_1 and test_ic_wide survive chop. We give
oracle the same protection without rebuilding the edge table.

LOGIC
=====
Fire only when ALL of:
  1. Edge table edge >= OR_PHASE1_MIN_EDGE (8pp default)
  2. BTC impulse in last PULSE_LOOKBACK seconds is in the same direction
     and >= PULSE_MIN_BPS magnitude
  3. Standard book/spread/timing gates from the original oracle

This is "oracle.py" with one extra filter inserted before fire.
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# ═══ Parameters ═══
OR_PHASE1_END = 90
OR_PHASE1_MIN_EDGE = 0.08
OR_PHASE2_MIN_EDGE = 0.05
OR_MIN_ENTRY = 0.10
OR_MAX_ENTRY = 0.88
OR_MAX_SPREAD = 0.04
OR_MIN_BOOK_LEVELS = 2
OR_COOLDOWN_SEC = 10
OR_BASE_DOLLARS = 200

# Pulse confirmation parameters
PULSE_LOOKBACK_SEC = 15      # check BTC move over last N seconds
PULSE_MIN_BPS = 3.0          # FALLBACK only — used when sigma unavailable
PULSE_MAX_BPS = 25.0         # but not so much it's a blowoff (likely to revert)
PULSE_SIGMA_MULT = 2.0       # dynamic threshold = sigma_bps * this
PULSE_USE_DYNAMIC = True     # if True, use sigma-based threshold
# Vol regime gate
PULSE_MIN_VOL_PCT = 25.0     # skip if 1h annualized vol < this
PULSE_MAX_VOL_PCT = 80.0     # skip if 1h annualized vol > this (panic regime)

COMBO_PARAMS = [
    {"name": "OP_early", "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "OP_late",  "btc_threshold_bp": 0, "lookback_s": 0},
]

_last_signal_time = [0.0]
_last_status = [0.0]
_edge_table_local = None


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [OP] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
    else:
        print("  [OP] WARNING: No edge table found!")
        _edge_table_local = {}


def _get_delta_bucket(d):
    a = abs(d)
    if a >= 12: return "far_above" if d > 0 else "far_below"
    elif a >= 5: return "above" if d > 0 else "below"
    elif a >= 2: return "near_above" if d > 0 else "near_below"
    else: return "at_strike"


def _get_time_bucket(tr):
    if tr > 240: return "240-300"
    elif tr > 180: return "180-240"
    elif tr > 120: return "120-180"
    elif tr > 60: return "60-120"
    else: return "0-60"


def _compute_edge(state, direction, entry_price, time_remaining):
    if not _edge_table_local:
        return None, None, None
    btc = state.binance_price
    if btc is None or not state.window_open or state.window_open <= 0:
        return None, None, None
    btc_corrected = btc - state.offset
    strike = state.window_open
    delta_bps = (btc_corrected - strike) / strike * 10000
    if abs(delta_bps) < 0.5:
        return None, None, None

    d_bucket = _get_delta_bucket(delta_bps)
    t_bucket = _get_time_bucket(time_remaining)

    mom_type = "flat"
    if len(state.price_buffer) >= 5:
        target_ts = time.time() - 30
        price_30ago = None
        for ts, px in state.price_buffer:
            if ts <= target_ts:
                price_30ago = px
            else:
                break
        if price_30ago and price_30ago > 0:
            mom_bps = (btc - price_30ago) / price_30ago * 10000
            if delta_bps > 0:
                mom_type = "away" if mom_bps > 0.5 else "toward" if mom_bps < -0.5 else "flat"
            else:
                mom_type = "away" if mom_bps < -0.5 else "toward" if mom_bps > 0.5 else "flat"

    side = "above" if delta_bps > 0 else "below"
    k1 = "{}|{}".format(d_bucket, t_bucket)
    k2 = "{}|{}".format(k1, mom_type)
    k3 = "{}|{}".format(k2, side)

    L1 = _edge_table_local.get("L1", {})
    L2 = _edge_table_local.get("L2", {})
    L3 = _edge_table_local.get("L3", {})

    cell = None
    level = "none"
    if k3 in L3 and L3[k3].get("count", 0) >= 50:
        cell = L3[k3]; level = "L3"
    elif k2 in L2 and L2[k2].get("count", 0) >= 50:
        cell = L2[k2]; level = "L2"
    elif k1 in L1 and L1[k1].get("count", 0) >= 50:
        cell = L1[k1]; level = "L1"

    if cell is None:
        return None, None, None

    wr = cell["wr"]
    table_dir = "YES" if delta_bps > 0 else "NO"
    if direction != table_dir:
        wr = 1.0 - wr

    fee = entry_price * (1 - entry_price) * 0.072
    if direction == "YES":
        breakeven = entry_price + fee
    else:
        breakeven = 1.0 - (entry_price - fee)
    edge = wr - breakeven
    return edge, wr, level


def _compute_pulse(state, lookback_sec):
    """BTC impulse over last N seconds. Returns bps move."""
    if not state.binance_price or len(state.price_buffer) < 2:
        return None
    target_ts = time.time() - lookback_sec
    price_then = None
    for ts, px in state.price_buffer:
        if ts <= target_ts:
            price_then = px  # keep LATEST tick still ≥ lookback_sec old
        else:
            break
    if price_then is None or price_then <= 0:
        return None
    return (state.binance_price - price_then) / price_then * 10000


def on_tick(state, price, ts):
    _load_table()


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    min_levels = getattr(engine, 'OR_MIN_BOOK_LEVELS', OR_MIN_BOOK_LEVELS)
    if len(state.book.bids) < min_levels or len(state.book.asks) < min_levels:
        return
    if state.book.spread > getattr(engine, 'OR_MAX_SPREAD', OR_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    _or_max_time = getattr(engine, 'OR_MAX_TIME', 295)
    _or_min_time = getattr(engine, 'OR_MIN_TIME', 5)
    if time_remaining < _or_min_time or time_remaining > _or_max_time:
        return

    if state.binance_price is None or not state.window_open or state.window_open <= 0:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'OR_COOLDOWN_SEC', OR_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    btc_corrected = state.binance_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    if delta_bps > 0:
        direction = "YES"
        entry = state.book.best_ask
    else:
        direction = "NO"
        entry = state.book.best_bid

    min_entry = getattr(engine, 'OR_MIN_ENTRY', OR_MIN_ENTRY)
    max_entry = getattr(engine, 'OR_MAX_ENTRY', OR_MAX_ENTRY)
    if entry < min_entry or entry > max_entry:
        return

    edge, wr, level = _compute_edge(state, direction, entry, time_remaining)

    phase1_end = getattr(engine, 'OR_PHASE1_END', OR_PHASE1_END)
    if time_remaining > phase1_end:
        min_edge = getattr(engine, 'OR_PHASE1_MIN_EDGE', OR_PHASE1_MIN_EDGE)
        combo_name = "OP_early"
    else:
        min_edge = getattr(engine, 'OR_PHASE2_MIN_EDGE', OR_PHASE2_MIN_EDGE)
        combo_name = "OP_late"

    # Status
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        edge_str = "{:+.0%}".format(edge) if edge is not None else "n/a"
        pulse = _compute_pulse(state, getattr(engine, 'PULSE_LOOKBACK_SEC', PULSE_LOOKBACK_SEC))
        pulse_str = "{:+.1f}bp".format(pulse) if pulse is not None else "n/a"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | pulse={} | {} @{:.0f}c | edge={}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, pulse_str, direction, entry * 100, edge_str))
        _last_status[0] = now

    if edge is None or edge < min_edge:
        return

    # ═══ NEW GATE: Vol regime check ═══
    from bot.shared.volatility import vol_tracker as _vt
    use_dynamic = getattr(engine, 'PULSE_USE_DYNAMIC', PULSE_USE_DYNAMIC)
    min_vol = getattr(engine, 'PULSE_MIN_VOL_PCT', PULSE_MIN_VOL_PCT)
    max_vol = getattr(engine, 'PULSE_MAX_VOL_PCT', PULSE_MAX_VOL_PCT)
    realized_vol = _vt.get_realized_vol_pct()
    if realized_vol is not None:
        if realized_vol < min_vol or realized_vol > max_vol:
            return  # silently skip — wrong regime

    # ═══ NEW GATE: Pulse confirmation (dynamic or static) ═══
    pulse_lookback = getattr(engine, 'PULSE_LOOKBACK_SEC', PULSE_LOOKBACK_SEC)
    pulse_min_static = getattr(engine, 'PULSE_MIN_BPS', PULSE_MIN_BPS)
    pulse_max = getattr(engine, 'PULSE_MAX_BPS', PULSE_MAX_BPS)
    pulse_sigma_mult = getattr(engine, 'PULSE_SIGMA_MULT', PULSE_SIGMA_MULT)

    # Dynamic threshold = sigma_bps * multiplier, with static as floor
    if use_dynamic:
        sigma_bps = _vt.get_sigma_bps_over(pulse_lookback)
        if sigma_bps is not None:
            pulse_min = max(pulse_min_static, sigma_bps * pulse_sigma_mult)
        else:
            pulse_min = pulse_min_static
    else:
        pulse_min = pulse_min_static

    pulse_bps = _compute_pulse(state, pulse_lookback)
    if pulse_bps is None:
        return

    # Direction-aligned: YES needs positive pulse, NO needs negative pulse
    if direction == "YES":
        aligned = pulse_bps >= pulse_min
    else:
        aligned = pulse_bps <= -pulse_min

    if not aligned:
        return

    # Reject blowoff moves (likely reversals)
    if abs(pulse_bps) > pulse_max:
        return

    combo = next((c for c in state.combos if c.name == combo_name), None)
    if combo is None or combo.has_position_in_window(state.window_start):
        return

    base_dollars = getattr(engine, 'OR_BASE_DOLLARS', OR_BASE_DOLLARS)
    if edge >= 0.10:
        dollars = int(base_dollars * 1.3)
    elif edge >= 0.05:
        dollars = base_dollars
    else:
        dollars = int(base_dollars * 0.7)

    # Vol-adjusted sizing
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    print("  {}[OP] FIRE {} {} edge={:.0%} pulse={:+.1f}bp wr={:.0%} @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        combo_name, direction, edge, pulse_bps, wr, entry * 100, dollars,
        level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_pulse",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "OR_PHASE1_END": OR_PHASE1_END,
        "OR_PHASE1_MIN_EDGE": OR_PHASE1_MIN_EDGE,
        "OR_PHASE2_MIN_EDGE": OR_PHASE2_MIN_EDGE,
        "OR_MIN_ENTRY": OR_MIN_ENTRY,
        "OR_MAX_ENTRY": OR_MAX_ENTRY,
        "OR_MAX_SPREAD": OR_MAX_SPREAD,
        "OR_MIN_BOOK_LEVELS": OR_MIN_BOOK_LEVELS,
        "OR_COOLDOWN_SEC": OR_COOLDOWN_SEC,
        "OR_BASE_DOLLARS": OR_BASE_DOLLARS,
        "OR_MAX_TIME": 295,
        "OR_MIN_TIME": 5,
        "PULSE_LOOKBACK_SEC": PULSE_LOOKBACK_SEC,
        "PULSE_MIN_BPS": PULSE_MIN_BPS,
        "PULSE_MAX_BPS": PULSE_MAX_BPS,
        "PULSE_SIGMA_MULT": PULSE_SIGMA_MULT,
        "PULSE_USE_DYNAMIC": PULSE_USE_DYNAMIC,
        "PULSE_MIN_VOL_PCT": PULSE_MIN_VOL_PCT,
        "PULSE_MAX_VOL_PCT": PULSE_MAX_VOL_PCT,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
