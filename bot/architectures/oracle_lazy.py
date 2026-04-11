"""
Architecture: oracle_lazy (Vol-Adaptive Direction-Stable Persistence)

HYPOTHESIS
==========
The bloodbath showed a specific failure pattern: BTC spikes, oracle fires,
BTC reverts within 30s, trade loses. The edge was "correct" at fire time but
the market was actually in chop, not a real trend.

The fix isn't "wait a fixed 10s" — that's arbitrary. A signal in calm markets
can be confirmed in 3s; a signal in volatile markets needs 10+ seconds because
noise is bigger. And "wall-clock time" doesn't actually verify persistence —
only that time passed. A smarter approach tracks whether BTC STAYS on the
expected side during the wait.

V2 (vol-adaptive direction-stable persistence)
===============================================
Queue signal → wait → fire when THREE conditions all hold:
  1. Minimum wait has elapsed (scaled to vol: 3-12s based on realized vol%)
  2. THREE consecutive 1-second ticks show BTC on the same side of the strike
     as our queued direction (true persistence, not chop)
  3. Current edge is still >= 80% of the original edge (no catastrophic decay)

If any of these fail at any point, drop the signal:
  - Direction flips (BTC crosses strike): drop immediately
  - Edge collapses (drops below 80% of original): drop
  - Max wait of 25s exceeded without confirmation: drop (opportunity gone)

Why this is smarter than a fixed wait
======================================
  - In calm markets (vol 20%): min wait is 3s. We can confirm fast when the
    noise floor is low.
  - In volatile markets (vol 80%): min wait is 8-10s. Noise is bigger, so we
    need more time for "3 consecutive stable ticks" to mean anything.
  - Chop regimes: BTC crosses strike repeatedly → never accumulates 3 stable
    ticks → drop. This is the exact failure mode we want to filter.
  - Real trends: BTC stays on one side → 3 stable ticks accumulate quickly
    → fire with only a few seconds of delay.

The key insight: we're measuring REAL persistence (tick stability), not
WALL-CLOCK TIME. Time alone doesn't verify anything.
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# ═══ Vol-adaptive persistence parameters ═══
LAZY_MIN_WAIT_FLOOR_SEC = 3   # minimum wait time in calm markets
LAZY_MIN_WAIT_CEIL_SEC = 12   # maximum of the adaptive wait
LAZY_VOL_SCALE = 0.1          # wait_sec = realized_vol_pct × this (clamped to floor/ceil)
LAZY_STABLE_TICKS = 3         # consecutive same-side 1s ticks required to fire
LAZY_MAX_WAIT_SEC = 25        # hard cap — drop signal if not confirmed by then
LAZY_EDGE_DEGRADATION_MAX = 0.20  # final edge must be >= 80% of original
LAZY_WAIT_SEC = 10            # LEGACY — kept for config compatibility, no longer used

# ═══ Standard oracle parameters ═══
OR_PHASE1_END = 90
OR_PHASE1_MIN_EDGE = 0.08
OR_PHASE2_MIN_EDGE = 0.05
OR_MIN_ENTRY = 0.10
OR_MAX_ENTRY = 0.88
OR_MAX_SPREAD = 0.04
OR_MIN_BOOK_LEVELS = 2
OR_COOLDOWN_SEC = 10
OR_BASE_DOLLARS = 200

COMBO_PARAMS = [
    {"name": "OL_lazy", "btc_threshold_bp": 0, "lookback_s": 0},
]

# Pending signal state (module-level, single signal at a time)
_pending = {
    "active": False,
    "direction": None,
    "queued_at": 0.0,
    "original_edge": 0.0,
    "original_entry": 0.0,
    "window_start": 0,
    "min_wait_sec": 0.0,      # vol-adaptive minimum wait (frozen at queue time)
    "stable_count": 0,         # consecutive same-side 1s ticks
    "last_tick_sec": 0,        # last second we updated the counter (for 1s gating)
}

_last_signal_time = [0.0]
_last_status = [0.0]
_edge_table_local = None

# Stats tracking
_stats = {
    "queued": 0, "fired_persistent": 0,
    "dropped_degraded": 0, "dropped_flipped": 0,
    "dropped_window": 0, "dropped_timeout": 0,
}


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [OL] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
        print("  [OL] V2: vol-adaptive direction-stable persistence")
        print("  [OL]   min_wait = clamp({}s, vol% × {}, {}s)".format(
            LAZY_MIN_WAIT_FLOOR_SEC, LAZY_VOL_SCALE, LAZY_MIN_WAIT_CEIL_SEC))
        print("  [OL]   require {} consecutive same-side 1s ticks".format(LAZY_STABLE_TICKS))
        print("  [OL]   edge must stay >= {:.0%} of original".format(1 - LAZY_EDGE_DEGRADATION_MAX))
        print("  [OL]   max wait {}s before timeout".format(LAZY_MAX_WAIT_SEC))
    else:
        print("  [OL] WARNING: No edge table found!")
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
    """Returns (edge, wr, level) or (None, None, None) if no data."""
    if not _edge_table_local:
        return None, None, None
    btc = state.binance_price
    if btc is None or not state.window_open or state.window_open <= 0:
        return None, None, None
    btc_corrected = btc - state.offset
    strike = state.window_open
    delta_bps = (btc_corrected - strike) / strike * 10000
    if abs(delta_bps) < 0.1:
        return None, None, None

    d_bucket = _get_delta_bucket(delta_bps)
    t_bucket = _get_time_bucket(time_remaining)

    mom_type = "flat"
    if len(state.price_buffer) >= 5:
        target_ts = time.time() - 30
        price_30ago = None
        for ts, px in state.price_buffer:
            if ts <= target_ts:
                price_30ago = px  # keep the LATEST tick still ≥ 30s old
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


def _drop_pending(reason):
    """Clear the pending signal."""
    _pending["active"] = False
    _stats["dropped_" + reason] = _stats.get("dropped_" + reason, 0) + 1


def on_tick(state, price, ts):
    _load_table()


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        if _pending["active"]:
            _drop_pending("window")
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
        current_direction = "YES"
        current_entry = state.book.best_ask
    else:
        current_direction = "NO"
        current_entry = state.book.best_bid

    min_entry = getattr(engine, 'OR_MIN_ENTRY', OR_MIN_ENTRY)
    max_entry = getattr(engine, 'OR_MAX_ENTRY', OR_MAX_ENTRY)
    if current_entry < min_entry or current_entry > max_entry:
        return

    current_edge, current_wr, level = _compute_edge(state, current_direction, current_entry, time_remaining)

    phase1_end = getattr(engine, 'OR_PHASE1_END', OR_PHASE1_END)
    if time_remaining > phase1_end:
        min_edge = getattr(engine, 'OR_PHASE1_MIN_EDGE', OR_PHASE1_MIN_EDGE)
    else:
        min_edge = getattr(engine, 'OR_PHASE2_MIN_EDGE', OR_PHASE2_MIN_EDGE)

    # ═══ V2 parameters ═══
    min_wait_floor = getattr(engine, 'LAZY_MIN_WAIT_FLOOR_SEC', LAZY_MIN_WAIT_FLOOR_SEC)
    min_wait_ceil  = getattr(engine, 'LAZY_MIN_WAIT_CEIL_SEC',  LAZY_MIN_WAIT_CEIL_SEC)
    vol_scale      = getattr(engine, 'LAZY_VOL_SCALE',          LAZY_VOL_SCALE)
    stable_req     = getattr(engine, 'LAZY_STABLE_TICKS',       LAZY_STABLE_TICKS)
    max_wait       = getattr(engine, 'LAZY_MAX_WAIT_SEC',       LAZY_MAX_WAIT_SEC)
    deg_max        = getattr(engine, 'LAZY_EDGE_DEGRADATION_MAX', LAZY_EDGE_DEGRADATION_MAX)

    # Status (every 12s)
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        edge_str = "{:+.0%}".format(current_edge) if current_edge is not None else "n/a"
        if _pending["active"]:
            elapsed = now - _pending["queued_at"]
            pend_str = "pending {} {:.0f}s stable={}/{}".format(
                _pending["direction"], elapsed, _pending["stable_count"], stable_req)
        else:
            pend_str = "idle"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | edge={} | {} (q{} ✓{} deg{} flip{} to{})".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, current_direction, current_entry * 100, edge_str, pend_str,
            _stats["queued"], _stats["fired_persistent"],
            _stats["dropped_degraded"], _stats["dropped_flipped"], _stats.get("dropped_timeout", 0)))
        _last_status[0] = now

    # ═══ State machine V2 — vol-adaptive direction-stable persistence ═══
    # Case 1: We have a pending signal
    if _pending["active"]:
        elapsed = now - _pending["queued_at"]
        # 1a. Window rolled over → drop
        if state.window_start != _pending["window_start"]:
            _drop_pending("window")
            return
        # 1b. Direction flipped (BTC crossed strike) → drop immediately
        if current_direction != _pending["direction"]:
            _drop_pending("flipped")
            return
        # 1c. Hard timeout — opportunity passed
        if elapsed > max_wait:
            _stats["dropped_timeout"] += 1
            _pending["active"] = False
            return
        # 1d. Edge collapsed → drop
        if current_edge is None or current_edge < _pending["original_edge"] * (1.0 - deg_max):
            _drop_pending("degraded")
            return

        # 1e. Update stability counter (only advance once per second of wall time)
        cur_sec = int(now)
        if cur_sec > _pending["last_tick_sec"]:
            _pending["last_tick_sec"] = cur_sec
            # BTC is still on the queued side (we already checked direction match above)
            _pending["stable_count"] += 1

        # 1f. Fire condition: min_wait elapsed AND enough stable ticks AND edge OK
        if elapsed >= _pending["min_wait_sec"] and _pending["stable_count"] >= stable_req:
            combo = state.combos[0]
            if not combo.has_position_in_window(state.window_start):
                base_dollars = getattr(engine, 'OR_BASE_DOLLARS', OR_BASE_DOLLARS)
                if current_edge >= 0.10:
                    dollars = int(base_dollars * 1.3)
                elif current_edge >= 0.05:
                    dollars = base_dollars
                else:
                    dollars = int(base_dollars * 0.7)

                # Vol-adjusted sizing
                from bot.shared.volatility import vol_tracker as _vt
                _sigma = _vt.get_sigma()
                if _sigma and _sigma > 0:
                    vol_mult = min(1.0, 3.0 / _sigma)
                    dollars = max(25, int(dollars * vol_mult))

                print("  {}[OL] FIRE-STABLE {} edge {:+.0%}->{:+.0%} (held {:.1f}s, stable={}, min_wait={:.1f}s) @{:.0f}c ${} {} T-{:.0f}s{}".format(
                    engine.G if current_direction == "YES" else engine.R,
                    current_direction, _pending["original_edge"], current_edge,
                    elapsed, _pending["stable_count"], _pending["min_wait_sec"],
                    current_entry * 100, dollars, level, time_remaining, engine.RST))
                _stats["fired_persistent"] += 1
                _last_signal_time[0] = now
                _pending["active"] = False
                engine.execute_paper_trade(combo, current_direction, abs(delta_bps),
                                           time_remaining, current_entry,
                                           override_dollars=dollars)
        # Otherwise still waiting — do nothing this tick
        return

    # Case 2: No pending signal — check if current setup is queueable
    if current_edge is None or current_edge < min_edge:
        return

    # Compute vol-adaptive min_wait for this signal
    from bot.shared.volatility import vol_tracker as _vt
    realized_vol = _vt.get_realized_vol_pct()
    if realized_vol is not None and realized_vol > 0:
        min_wait_sec = max(min_wait_floor, min(min_wait_ceil, realized_vol * vol_scale))
    else:
        min_wait_sec = min_wait_floor  # fallback when vol tracker has no data yet

    # Queue this signal for verification
    _pending["active"] = True
    _pending["direction"] = current_direction
    _pending["queued_at"] = now
    _pending["original_edge"] = current_edge
    _pending["original_entry"] = current_entry
    _pending["window_start"] = state.window_start
    _pending["min_wait_sec"] = min_wait_sec
    _pending["stable_count"] = 0
    _pending["last_tick_sec"] = int(now)
    _stats["queued"] += 1

    print("  {}[OL] QUEUE {} edge={:+.0%} @{:.0f}c — min_wait={:.1f}s (vol={:.0f}%), need {} stable ticks{}".format(
        engine.DIM, current_direction, current_edge, current_entry * 100,
        min_wait_sec, realized_vol or 0, stable_req, engine.RST))


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0
    if _pending["active"]:
        _pending["active"] = False  # window changed, drop any pending


ARCH_SPEC = {
    "name": "oracle_lazy",
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
        "LAZY_WAIT_SEC": LAZY_WAIT_SEC,  # legacy, no longer used
        "LAZY_MIN_WAIT_FLOOR_SEC": LAZY_MIN_WAIT_FLOOR_SEC,
        "LAZY_MIN_WAIT_CEIL_SEC": LAZY_MIN_WAIT_CEIL_SEC,
        "LAZY_VOL_SCALE": LAZY_VOL_SCALE,
        "LAZY_STABLE_TICKS": LAZY_STABLE_TICKS,
        "LAZY_MAX_WAIT_SEC": LAZY_MAX_WAIT_SEC,
        "LAZY_EDGE_DEGRADATION_MAX": LAZY_EDGE_DEGRADATION_MAX,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
