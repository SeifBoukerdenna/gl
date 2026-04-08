"""
Architecture: oracle (Pure Edge Table Trader)

Every other architecture detects a signal, then the edge table filters it.
Oracle INVERTS this: the edge table IS the signal.

Every 200ms, oracle asks: "Based on 13,861 historical PM windows, is the
current setup (delta, time, momentum) profitable at the current entry price?"
If yes → trade. If no → wait.

No impulse detection. No cross-exchange momentum. No z-score.
Just: "historically, this exact situation won X% of the time, and I can
enter at a price where I only need Y% — the gap is my edge."

Why this catches trades other architectures miss:
  - BTC moved 8bp from strike 3 minutes ago, but impulse_confirmed's
    lookback window expired. Oracle still sees the 8bp delta and trades it.
  - BTC is 5bp below strike with momentum away. No architecture fires because
    the move was gradual (no impulse). Oracle sees the delta and trades.

Two phases:
  Phase 1 (T-295 to T-90): Trade when edge > 8pp. One trade max.
  Phase 2 (T-90 to T-5): Trade when edge > 5pp (lower bar near expiry). One trade max.

Both directions. Dynamic sizing from edge score.
"""

import time
import math
import json as _json
from collections import deque
from pathlib import Path

# ═══ Parameters ═══
OR_PHASE1_END = 90          # Phase 1: T-295 to T-90
OR_PHASE1_MIN_EDGE = 0.08   # 8pp edge required early (more time = more risk)
OR_PHASE2_MIN_EDGE = 0.05   # 5pp edge near expiry (outcome more certain)
OR_MIN_ENTRY = 0.15
OR_MAX_ENTRY = 0.88
OR_MAX_SPREAD = 0.04
OR_MIN_BOOK_LEVELS = 2
OR_COOLDOWN_SEC = 10
OR_BASE_DOLLARS = 200

COMBO_PARAMS = [
    {"name": "OR_early",  "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "OR_late",   "btc_threshold_bp": 0, "lookback_s": 0},
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
        print("  [OR] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
    else:
        print("  [OR] WARNING: No edge table found!")
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
    """Compute edge from the hierarchical edge table.
    Returns (edge_pp, wr, level) or (None, None, None) if no data."""
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

    # Momentum
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

    # Hierarchical lookup
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

    # Breakeven with fees
    fee = entry_price * (1 - entry_price) * 0.072
    if direction == "YES":
        breakeven = entry_price + fee
    else:
        breakeven = 1.0 - (entry_price - fee)

    edge = wr - breakeven
    return edge, wr, level


def on_tick(state, price, ts):
    _load_table()


def check_signals(state, now_s):
    """Trade purely from edge table — no signal detection needed."""
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

    if state.binance_price is None:
        return
    if not state.window_open or state.window_open <= 0:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'OR_COOLDOWN_SEC', OR_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    btc_corrected = state.binance_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # Determine the natural direction (bet WITH the delta)
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

    # Compute edge from table
    edge, wr, level = _compute_edge(state, direction, entry, time_remaining)

    phase1_end = getattr(engine, 'OR_PHASE1_END', OR_PHASE1_END)

    # Status
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        phase = "P1" if time_remaining > phase1_end else "P2"
        edge_str = "{:+.0%}".format(edge) if edge is not None else "n/a"
        wr_str = "{:.0%}".format(wr) if wr is not None else "n/a"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | edge={} wr={} {} {}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, direction, entry * 100,
            edge_str, wr_str, level or "", phase))
        _last_status[0] = now

    if edge is None:
        return

    # Phase-based edge thresholds
    if time_remaining > phase1_end:
        # Phase 1: early window, need higher edge
        min_edge = getattr(engine, 'OR_PHASE1_MIN_EDGE', OR_PHASE1_MIN_EDGE)
        combo_name = "OR_early"
    else:
        # Phase 2: near expiry, lower bar
        min_edge = getattr(engine, 'OR_PHASE2_MIN_EDGE', OR_PHASE2_MIN_EDGE)
        combo_name = "OR_late"

    if edge < min_edge:
        return

    combo = next((c for c in state.combos if c.name == combo_name), None)
    if combo is None or combo.has_position_in_window(state.window_start):
        return

    base_dollars = getattr(engine, 'OR_BASE_DOLLARS', OR_BASE_DOLLARS)
    # Scale sizing by edge: 2-5pp → 0.7x, 5-10pp → 1.0x, 10pp+ → 1.3x
    if edge >= 0.10:
        dollars = int(base_dollars * 1.3)
    elif edge >= 0.05:
        dollars = base_dollars
    else:
        dollars = int(base_dollars * 0.7)

    print("  {}[OR] FIRE {} {} edge={:.0%} wr={:.0%} @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        combo_name, direction, edge, wr, entry * 100, dollars,
        level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle",
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
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
