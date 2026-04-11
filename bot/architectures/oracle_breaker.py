"""
Architecture: oracle_breaker (Adaptive Self-Protection)

HYPOTHESIS
==========
Oracle's underlying logic is fine. The problem is it has no awareness of its OWN
recent performance. When it's bleeding, it keeps firing instead of slowing down.
When it's printing, it keeps the same size instead of pressing the advantage.

A simple adaptive layer can fix both: track rolling WR over the last 20 trades.
- If rolling WR drops below BREAKER_HALT_WR, halt for HALT_MIN minutes
- If rolling WR is between HALT and NORMAL, size at 0.5x (slow down)
- If rolling WR is between NORMAL and HOT, size at 1.0x (normal)
- If rolling WR is above HOT, size at 1.3x (press the edge)

This is a circuit breaker AND a momentum amplifier in one. It self-corrects to
whatever regime we're in without needing to know what the regime is.

LOGIC
=====
1. Same fire conditions as oracle.py (edge table + 8pp threshold)
2. Track rolling WR of last N trades from this session's own state
3. Apply size multiplier based on rolling WR
4. If WR < HALT threshold, refuse to fire entirely for cooldown period

The key insight: we don't need to predict regimes. We just need to NOTICE when
something is going wrong and react fast enough.
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

# Adaptive sizing parameters (NEW)
BREAKER_WINDOW = 20            # rolling window of N trades
BREAKER_HALT_WR = 0.45         # halt if rolling WR drops below 45%
BREAKER_SLOW_WR = 0.55         # 0.5x size below 55%
BREAKER_NORMAL_WR = 0.62       # 1.0x size 55-62%
BREAKER_HOT_WR = 0.70          # 1.3x size above 62%, full press above 70%
BREAKER_HALT_MIN = 30          # minutes to halt after triggering

# Internal state
_recent_results = deque(maxlen=BREAKER_WINDOW)  # 1 = win, 0 = loss
_halt_until = [0.0]
_last_signal_time = [0.0]
_last_status = [0.0]
_last_seen_trade_count = [0]
_edge_table_local = None


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [OB] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
    else:
        print("  [OB] WARNING: No edge table found!")
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


def _update_recent_results(state):
    """Pull recent settled trade outcomes from state.combos."""
    # Count trades from the breaker combo
    if not state.combos:
        return
    combo = state.combos[0]
    all_trades = combo.trades
    if len(all_trades) <= _last_seen_trade_count[0]:
        return
    new_trades = all_trades[_last_seen_trade_count[0]:]
    for t in new_trades:
        result = t.get("result", "")
        if result == "WIN":
            _recent_results.append(1)
        elif result == "LOSS":
            _recent_results.append(0)
    _last_seen_trade_count[0] = len(all_trades)


def _get_rolling_wr():
    """Compute rolling WR from recent results."""
    if len(_recent_results) < 5:
        return None
    return sum(_recent_results) / len(_recent_results)


def _get_size_multiplier(rolling_wr):
    """Determine size multiplier based on rolling WR."""
    if rolling_wr is None:
        return 1.0
    if rolling_wr >= BREAKER_HOT_WR:
        return 1.5
    if rolling_wr >= BREAKER_NORMAL_WR:
        return 1.0
    if rolling_wr >= BREAKER_SLOW_WR:
        return 0.5
    return 0.0  # halt


def on_tick(state, price, ts):
    _load_table()


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return

    # Check halt status
    now = time.time()
    if now < _halt_until[0]:
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

    if now - _last_signal_time[0] < getattr(engine, 'OR_COOLDOWN_SEC', OR_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    # Update rolling results from settled trades
    _update_recent_results(state)
    rolling_wr = _get_rolling_wr()

    # Check breaker
    halt_wr = getattr(engine, 'BREAKER_HALT_WR', BREAKER_HALT_WR)
    halt_min = getattr(engine, 'BREAKER_HALT_MIN', BREAKER_HALT_MIN)

    if rolling_wr is not None and rolling_wr < halt_wr and len(_recent_results) >= BREAKER_WINDOW:
        _halt_until[0] = now + halt_min * 60
        print("  {}[OB] HALT TRIGGERED — rolling WR {:.0%} < {:.0%}, pausing {} min{}".format(
            engine.R, rolling_wr, halt_wr, halt_min, engine.RST))
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
    else:
        min_edge = getattr(engine, 'OR_PHASE2_MIN_EDGE', OR_PHASE2_MIN_EDGE)

    # Status
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        edge_str = "{:+.0%}".format(edge) if edge is not None else "n/a"
        rwr_str = "{:.0%}({})".format(rolling_wr, len(_recent_results)) if rolling_wr is not None else "n/a"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | rolling_wr={} | {} @{:.0f}c | edge={}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, rwr_str, direction, entry * 100, edge_str))
        _last_status[0] = now

    if edge is None or edge < min_edge:
        return

    combo = state.combos[0]
    if combo.has_position_in_window(state.window_start):
        return

    base_dollars = getattr(engine, 'OR_BASE_DOLLARS', OR_BASE_DOLLARS)

    # Edge-based base sizing
    if edge >= 0.10:
        dollars = int(base_dollars * 1.3)
    elif edge >= 0.05:
        dollars = base_dollars
    else:
        dollars = int(base_dollars * 0.7)

    # ═══ NEW: Adaptive multiplier from rolling WR ═══
    size_mult = _get_size_multiplier(rolling_wr)
    if size_mult <= 0:
        return  # halt
    dollars = int(dollars * size_mult)

    # Vol-adjusted sizing
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    print("  {}[OB] FIRE {} edge={:.0%} rwr={} mult={:.1f}x @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, edge,
        "{:.0%}".format(rolling_wr) if rolling_wr is not None else "n/a",
        size_mult, entry * 100, dollars,
        level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_breaker",
    "combo_params": [{"name": "OB_break", "btc_threshold_bp": 0, "lookback_s": 0}],
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
        "BREAKER_HALT_WR": BREAKER_HALT_WR,
        "BREAKER_SLOW_WR": BREAKER_SLOW_WR,
        "BREAKER_NORMAL_WR": BREAKER_NORMAL_WR,
        "BREAKER_HOT_WR": BREAKER_HOT_WR,
        "BREAKER_HALT_MIN": BREAKER_HALT_MIN,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
