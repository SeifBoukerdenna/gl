"""
Architecture: oracle_lazy (Signal Persistence Test)

HYPOTHESIS
==========
Today's bloodbath had a specific failure pattern:
  21:50:30 → BTC $72,266, +5bp → fires YES @ 59c → LOSS
  21:51:18 → BTC $72,323, +13bp → fires YES @ 74c → LOSS
  21:51:32 → BTC $72,310, +12bp → fires YES @ 62c → LOSS
  21:52:14 → BTC $72,285, +8bp → fires YES @ 47c → LOSS

In each case BTC spiked, oracle saw an edge, fired immediately, and BTC reverted
within ~30 seconds. The trades were technically correct at fire time but wrong
by settlement.

What if oracle waited LAZY_WAIT_SEC seconds (default 10) after detecting an edge,
THEN re-checked, and only fired if the edge was still present?

  Real momentum:    edge persists 10s later → fire (slightly worse entry)
  Whipsaw spike:    edge dropped or flipped 10s later → skip
  Cost:             10s of "first mover" speed
  Benefit:          filters out spike-and-revert patterns specifically

This is the only architecture testing FORWARD-LOOKING signal persistence.
Every other architecture filters on past momentum, peer agreement, or
historical patterns. None ask "is this signal still here in 10 seconds?"

LOGIC
=====
1. On each tick, compute current setup (delta, direction, edge)
2. If no pending signal AND current edge >= min_edge → queue it
3. If pending signal exists AND >= LAZY_WAIT_SEC seconds elapsed:
     a. Re-compute edge for the SAME DIRECTION at current entry
     b. If still >= min_edge → FIRE
     c. If not → drop pending (signal didn't persist)
4. If window changes while pending → drop pending (invalidated)
5. If a different direction now has edge → drop pending and queue the new one

The lazy buffer means we miss the first 10s of any move, but skip every move
that doesn't last 10s. That's the entire trade-off being tested.
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# ═══ Lazy parameters ═══
LAZY_WAIT_SEC = 10        # wait this long before confirming the signal
LAZY_MAX_DEGRADATION = 0.02  # signal can degrade by up to 2pp and still count as persistent

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
}

_last_signal_time = [0.0]
_last_status = [0.0]
_edge_table_local = None

# Stats tracking
_stats = {"queued": 0, "fired_persistent": 0, "dropped_degraded": 0, "dropped_flipped": 0, "dropped_window": 0}


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [OL] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
        print("  [OL] Lazy wait: {}s, max degradation: {:.0%}".format(LAZY_WAIT_SEC, LAZY_MAX_DEGRADATION))
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
    if abs(delta_bps) < 0.5:
        return None, None, None

    d_bucket = _get_delta_bucket(delta_bps)
    t_bucket = _get_time_bucket(time_remaining)

    mom_type = "flat"
    if len(state.price_buffer) >= 5:
        target_ts = time.time() - 30
        price_30ago = None
        for ts, px in state.price_buffer:
            if ts >= target_ts:
                price_30ago = px
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

    lazy_wait = getattr(engine, 'LAZY_WAIT_SEC', LAZY_WAIT_SEC)
    lazy_max_deg = getattr(engine, 'LAZY_MAX_DEGRADATION', LAZY_MAX_DEGRADATION)

    # Status (every 12s)
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        edge_str = "{:+.0%}".format(current_edge) if current_edge is not None else "n/a"
        if _pending["active"]:
            elapsed = now - _pending["queued_at"]
            pend_str = "pending {} {:.0f}s/{:.0f}s".format(_pending["direction"], elapsed, lazy_wait)
        else:
            pend_str = "idle"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | edge={} | {} (q{} ✓{} ✗{}/{}/{})".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, current_direction, current_entry * 100, edge_str, pend_str,
            _stats.get("queued", 0), _stats.get("fired_persistent", 0),
            _stats.get("dropped_degraded", 0), _stats.get("dropped_flipped", 0), _stats.get("dropped_window", 0)))
        _last_status[0] = now

    # ═══ State machine ═══
    # Case 1: We have a pending signal
    if _pending["active"]:
        # Check if window changed
        if state.window_start != _pending["window_start"]:
            _drop_pending("window")
        # Check if direction flipped (BTC crossed strike)
        elif current_direction != _pending["direction"]:
            _drop_pending("flipped")
        # Check if we've waited long enough
        elif now - _pending["queued_at"] >= lazy_wait:
            # Re-check the edge at current state
            if current_edge is None:
                _drop_pending("degraded")
            else:
                # Allow some degradation: signal persists if still within max_degradation of original
                # OR still above the absolute min_edge threshold
                degradation = _pending["original_edge"] - current_edge
                if degradation > lazy_max_deg or current_edge < min_edge:
                    _drop_pending("degraded")
                else:
                    # FIRE — signal persisted!
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

                        wait_elapsed = now - _pending["queued_at"]
                        print("  {}[OL] FIRE-PERSISTENT {} edge {:+.0%}->{:+.0%} (held {:.1f}s) @{:.0f}c ${} {} T-{:.0f}s{}".format(
                            engine.G if current_direction == "YES" else engine.R,
                            current_direction, _pending["original_edge"], current_edge,
                            wait_elapsed, current_entry * 100, dollars,
                            level, time_remaining, engine.RST))
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

    # Queue this signal for verification
    _pending["active"] = True
    _pending["direction"] = current_direction
    _pending["queued_at"] = now
    _pending["original_edge"] = current_edge
    _pending["original_entry"] = current_entry
    _pending["window_start"] = state.window_start
    _stats["queued"] += 1

    print("  {}[OL] QUEUE {} edge={:+.0%} @{:.0f}c — re-checking in {}s{}".format(
        engine.DIM, current_direction, current_edge, current_entry * 100, lazy_wait, engine.RST))


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
        "LAZY_WAIT_SEC": LAZY_WAIT_SEC,
        "LAZY_MAX_DEGRADATION": LAZY_MAX_DEGRADATION,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
