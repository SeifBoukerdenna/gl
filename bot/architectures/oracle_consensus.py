"""
Architecture: oracle_consensus (Edge Table + Multi-Exchange Confirmation)

HYPOTHESIS
==========
The architecture that survived April 9's bloodbath best was test_ic_wide
(impulse_confirmed). Its key feature: it requires Coinbase AND Bybit momentum
in the same direction before firing. Single-exchange Binance moves can be
artifacts (one large order, an internal liquidation, a spike from a single
trader). Multi-exchange consensus moves are real institutional flow.

What if we gave oracle the same protection? Edge table picks the setup, then
multi-exchange confirmation acts as a "is this move actually happening across
the market?" gate.

This is structurally different from oracle_pulse (which uses Binance impulse
only). oracle_pulse confirms with the SAME data source the bot already trades
on. oracle_consensus confirms with INDEPENDENT data sources — Coinbase and
Bybit are separate venues with separate market makers, separate liquidity, and
separate price discovery.

LOGIC
=====
Fire only when ALL of:
  1. Edge table edge >= OR_PHASE1_MIN_EDGE (8pp default, same as base oracle)
  2. At least IC_MIN_CONFIRM_EXCHANGES other exchanges show >= IC_CONFIRM_MIN_BPS
     momentum in the same direction over the last IC_CONFIRM_WINDOW_SEC seconds
  3. Standard book/spread/timing gates from oracle

With 1 confirmation: normal size
With 2 confirmations (both Coinbase AND Bybit): 1.3x size — high conviction

Imports the exchange feed module from impulse_confirmed to avoid code
duplication. Each session runs in its own process so the feeds get started
fresh per session.
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# Reuse the existing exchange feed infrastructure from impulse_confirmed.
# When this architecture loads in its own process, the module-level state
# in impulse_confirmed gets initialized fresh and the feeds start.
from bot.architectures.impulse_confirmed import (
    _start_feeds as _start_exchange_feeds,
    _get_exchange_momentum,
)

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

# Multi-exchange confirmation parameters (from impulse_confirmed)
IC_MIN_CONFIRM_EXCHANGES = 1
IC_CONFIRM_WINDOW_SEC = 5
IC_CONFIRM_MIN_BPS = 1.0

COMBO_PARAMS = [
    {"name": "OC_early", "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "OC_late",  "btc_threshold_bp": 0, "lookback_s": 0},
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
        print("  [OC] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
    else:
        print("  [OC] WARNING: No edge table found!")
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


def _check_consensus(direction, window_sec, min_bps):
    """Check how many independent exchanges confirm the direction.
    Returns (count, sessions_list)."""
    confirms = 0
    sessions = []

    mom_cb = _get_exchange_momentum("coinbase", window_sec)
    if mom_cb is not None:
        if direction == "YES" and mom_cb >= min_bps:
            confirms += 1
            sessions.append("CB{:+.1f}".format(mom_cb))
        elif direction == "NO" and mom_cb <= -min_bps:
            confirms += 1
            sessions.append("CB{:+.1f}".format(mom_cb))

    mom_by = _get_exchange_momentum("bybit", window_sec)
    if mom_by is not None:
        if direction == "YES" and mom_by >= min_bps:
            confirms += 1
            sessions.append("BY{:+.1f}".format(mom_by))
        elif direction == "NO" and mom_by <= -min_bps:
            confirms += 1
            sessions.append("BY{:+.1f}".format(mom_by))

    return confirms, sessions


def on_tick(state, price, ts):
    _load_table()
    _start_exchange_feeds()  # idempotent — only starts once


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
        combo_name = "OC_early"
    else:
        min_edge = getattr(engine, 'OR_PHASE2_MIN_EDGE', OR_PHASE2_MIN_EDGE)
        combo_name = "OC_late"

    # Check exchange consensus regardless (for status display)
    window_sec = getattr(engine, 'IC_CONFIRM_WINDOW_SEC', IC_CONFIRM_WINDOW_SEC)
    min_bps = getattr(engine, 'IC_CONFIRM_MIN_BPS', IC_CONFIRM_MIN_BPS)
    confirm_count, confirm_sessions = _check_consensus(direction, window_sec, min_bps)

    # Status
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        edge_str = "{:+.0%}".format(edge) if edge is not None else "n/a"
        confirm_str = "{}({})".format("+".join(confirm_sessions), confirm_count) if confirm_sessions else "0/2"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | edge={} | confirms={}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, direction, entry * 100, edge_str, confirm_str))
        _last_status[0] = now

    if edge is None or edge < min_edge:
        return

    # ═══ NEW GATE: Multi-exchange consensus ═══
    min_confirms = getattr(engine, 'IC_MIN_CONFIRM_EXCHANGES', IC_MIN_CONFIRM_EXCHANGES)
    if confirm_count < min_confirms:
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

    # Bonus: if BOTH exchanges confirm, size up
    if confirm_count >= 2:
        dollars = int(dollars * 1.3)

    # Vol-adjusted sizing
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    print("  {}[OC] FIRE {} {} edge={:.0%} confirms={} [{}] @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        combo_name, direction, edge, confirm_count,
        "+".join(confirm_sessions),
        entry * 100, dollars,
        level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_consensus",
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
        "IC_MIN_CONFIRM_EXCHANGES": IC_MIN_CONFIRM_EXCHANGES,
        "IC_CONFIRM_WINDOW_SEC": IC_CONFIRM_WINDOW_SEC,
        "IC_CONFIRM_MIN_BPS": IC_CONFIRM_MIN_BPS,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
