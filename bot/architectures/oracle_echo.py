"""
Architecture: oracle_echo (Multi-Session Confirmation Piggyback)

HYPOTHESIS
==========
The architectures that survived the April 9 bloodbath best (blitz_1, test_ic_wide,
test_xp, edge_hunter) all share one trait: they require multi-source confirmation
before firing. They wait for impulses, cross-exchange agreement, or volume signals.

What if oracle didn't try to filter chop itself, but instead piggybacked on those
already-running architectures? When blitz_1 OR test_ic_wide fires NO in the last
30 seconds, that's a strong signal that the move is real (not a chop spike). Oracle
joins those trades with edge-table-derived sizing.

Oracle becomes a "leverage layer" on top of validated signals: it can make the
trade bigger than the original session would (because the edge table tells it the
expected WR), but it never fires alone.

LOGIC
=====
Periodically (every check) read the recent_trades from confirmation sessions'
stats.json files. If any of them fired in the last ECHO_WINDOW_SEC seconds:
  1. Determine the consensus direction (which way did they bet?)
  2. Look up edge table WR for that direction
  3. If table also says edge >= ECHO_MIN_EDGE → fire same direction
  4. Size based on edge table strength

This trades fewer times than oracle but only on confirmed signals. Should
dramatically reduce chop bleeding.
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# ═══ Parameters ═══
ECHO_WINDOW_SEC = 30           # look at confirmation trades from last N seconds
ECHO_MIN_EDGE = 0.05           # require at least 5pp edge from table on top
ECHO_BASE_DOLLARS = 200
ECHO_COOLDOWN_SEC = 10

# Sessions whose trades we treat as confirmation signals
CONFIRMATION_SESSIONS = ["blitz_1", "test_ic_wide", "test_xp", "edge_hunter"]

OR_MIN_ENTRY = 0.10
OR_MAX_ENTRY = 0.90
OR_MAX_SPREAD = 0.04
OR_MIN_BOOK_LEVELS = 2

COMBO_PARAMS = [
    {"name": "OE_echo", "btc_threshold_bp": 0, "lookback_s": 0},
]

_last_signal_time = [0.0]
_last_status = [0.0]
_last_stats_check = [0.0]
_recent_confirmations = []  # cache: list of (ts, session, direction)
_edge_table_local = None


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [OE] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
    else:
        print("  [OE] WARNING: No edge table found!")
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


def _get_table_wr(state, direction, time_remaining):
    if not _edge_table_local:
        return None, None
    btc = state.binance_price
    if btc is None or not state.window_open or state.window_open <= 0:
        return None, None
    btc_corrected = btc - state.offset
    strike = state.window_open
    delta_bps = (btc_corrected - strike) / strike * 10000
    if abs(delta_bps) < 0.5:
        return None, None

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
        return None, None

    wr = cell["wr"]
    table_dir = "YES" if delta_bps > 0 else "NO"
    if direction != table_dir:
        wr = 1.0 - wr
    return wr, level


def _refresh_confirmations():
    """Read recent_trades from confirmation sessions' stats.json files."""
    global _recent_confirmations
    now = time.time()
    if now - _last_stats_check[0] < 1.0:  # don't spam disk
        return
    _last_stats_check[0] = now

    cutoff = now - 60  # keep last 60s of confirmations
    new_confirmations = [(ts, sess, d) for ts, sess, d in _recent_confirmations if ts >= cutoff]

    for sess in CONFIRMATION_SESSIONS:
        stats_path = Path("data/{}/stats.json".format(sess))
        if not stats_path.exists():
            continue
        try:
            with open(stats_path) as f:
                data = _json.load(f)
            recent = data.get("recent_trades", [])
            for t in recent:
                ts = t.get("timestamp", 0)
                if ts < cutoff:
                    continue
                direction = t.get("direction")
                if direction not in ("YES", "NO"):
                    continue
                key = (ts, sess, direction)
                if key not in new_confirmations:
                    new_confirmations.append(key)
        except Exception:
            pass

    _recent_confirmations = new_confirmations


def _check_confirmation(window_sec):
    """Return (direction, count, sessions) of confirmations in last N seconds, or (None, 0, [])."""
    now = time.time()
    cutoff = now - window_sec
    recent = [(ts, s, d) for ts, s, d in _recent_confirmations if ts >= cutoff]

    yes_sessions = set(s for _, s, d in recent if d == "YES")
    no_sessions = set(s for _, s, d in recent if d == "NO")

    if len(yes_sessions) >= 1 and len(yes_sessions) > len(no_sessions):
        return "YES", len(yes_sessions), sorted(yes_sessions)
    if len(no_sessions) >= 1 and len(no_sessions) > len(yes_sessions):
        return "NO", len(no_sessions), sorted(no_sessions)
    return None, 0, []


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
    if time_remaining < 5 or time_remaining > 295:
        return

    if state.binance_price is None or not state.window_open or state.window_open <= 0:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'ECHO_COOLDOWN_SEC', ECHO_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    # Refresh confirmation cache
    _refresh_confirmations()

    # Check for confirmation in the last N seconds
    window_sec = getattr(engine, 'ECHO_WINDOW_SEC', ECHO_WINDOW_SEC)
    direction, confirm_count, confirm_sessions = _check_confirmation(window_sec)

    btc_corrected = state.binance_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # Status
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        confirm_str = "{}({}) by {}".format(direction, confirm_count, ",".join(confirm_sessions)) if direction else "none"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | confirm: {}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, confirm_str))
        _last_status[0] = now

    if direction is None:
        return

    # Get entry price for the confirmed direction
    entry = state.book.best_ask if direction == "YES" else state.book.best_bid
    min_entry = getattr(engine, 'OR_MIN_ENTRY', OR_MIN_ENTRY)
    max_entry = getattr(engine, 'OR_MAX_ENTRY', OR_MAX_ENTRY)
    if entry < min_entry or entry > max_entry:
        return

    # Edge table sanity check: don't fire if table actively disagrees
    table_wr, level = _get_table_wr(state, direction, time_remaining)
    if table_wr is None:
        return

    fee = entry * (1 - entry) * 0.072
    if direction == "YES":
        breakeven = entry + fee
    else:
        breakeven = 1.0 - (entry - fee)
    edge = table_wr - breakeven

    min_edge = getattr(engine, 'ECHO_MIN_EDGE', ECHO_MIN_EDGE)
    if edge < min_edge:
        return

    combo = state.combos[0]
    if combo.has_position_in_window(state.window_start):
        return

    base_dollars = getattr(engine, 'ECHO_BASE_DOLLARS', ECHO_BASE_DOLLARS)
    # More confirmations = bigger size
    if confirm_count >= 3:
        dollars = int(base_dollars * 1.5)
    elif confirm_count >= 2:
        dollars = int(base_dollars * 1.2)
    else:
        dollars = base_dollars

    # Vol-adjusted sizing
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    print("  {}[OE] FIRE {} confirmed by {} [{}] edge={:.0%} @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, confirm_count, ",".join(confirm_sessions), edge, entry * 100, dollars,
        level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_echo",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "OR_MIN_ENTRY": OR_MIN_ENTRY,
        "OR_MAX_ENTRY": OR_MAX_ENTRY,
        "OR_MAX_SPREAD": OR_MAX_SPREAD,
        "OR_MIN_BOOK_LEVELS": OR_MIN_BOOK_LEVELS,
        "ECHO_WINDOW_SEC": ECHO_WINDOW_SEC,
        "ECHO_MIN_EDGE": ECHO_MIN_EDGE,
        "ECHO_BASE_DOLLARS": ECHO_BASE_DOLLARS,
        "ECHO_COOLDOWN_SEC": ECHO_COOLDOWN_SEC,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
