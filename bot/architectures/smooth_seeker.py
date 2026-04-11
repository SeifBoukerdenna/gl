"""
Architecture: smooth_seeker (Optimized for Temporal Smoothness)

HYPOTHESIS
==========
Total PnL is a vanity metric. The metric that determines whether you survive
going live is TEMPORAL MONOTONICITY: does the equity curve trend up smoothly,
or does it spike and crash?

By that lens, our existing sessions look very different:
  - blitz_1:   R²=0.82, 73% of 6h windows positive, worst 6h -$1,569
  - oracle_1:  R²=0.80, 62% positive, worst 6h -$2,889
  - oracle_safe: R²=0.38, 73% positive, worst 6h -$7,077 ← catastrophic
  - oracle_2:  R²=0.44, only 48% positive ← coin flip 6h windows

The "boring" multi-confirmation sessions cluster at the top of the smoothness
ranking. The oracle variants look "better" by total PnL but their worst stretches
are 3-7x larger.

smooth_seeker is built EXPLICITLY to maximize smoothness, not PnL. It accepts
lower absolute returns in exchange for a near-linear equity curve and capped
worst-case stretches. This is the architecture profile that survives going live
on real capital.

LOGIC
=====
Five layers of protection:

1. MULTI-CONFIRMATION GATE (3+ peers must agree)
   Only fires when at least 3 of [blitz_1, test_ic_wide, edge_hunter, test_xp]
   have unsettled positions in the same direction in the current/recent window.

2. EDGE TABLE SANITY CHECK
   The table must also show edge ≥ 5pp in the same direction. This filters out
   peer cascades during regime shifts where everyone is wrong simultaneously.

3. SMALL POSITIONS
   $75 base (vs typical $200) — variance reduction. Wins are smaller but losses
   are smaller too. The R² improves dramatically.

4. SELF-IMPOSED 6H DRAWDOWN HALT
   Tracks rolling 6-hour PnL. If it drops below -$500, halt for 2 hours. This
   is the architecture's "stop loss" — if 6h is going badly, we stop adding to
   the bleed.

5. ONE TRADE PER WINDOW MAX
   No compounding. Limits exposure per 5-minute window. If something goes wrong
   in this window, we lose at most one trade's worth.

The combined effect: lower trade volume, lower per-trade upside, but the
equity curve should be much smoother than any single-source strategy.
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# ═══ Smooth Seeker parameters ═══
SS_MIN_CONFIRMS = 3                  # need 3+ peer architectures agreeing
SS_PEER_WINDOW_SEC = 900             # look at peer fires over this window
SS_PEER_SESSIONS = ["blitz_1", "test_ic_wide", "edge_hunter", "test_xp"]
SS_BASE_DOLLARS = 75                 # smaller positions for variance reduction
SS_MIN_EDGE = 0.05                   # edge table sanity check
SS_COOLDOWN_SEC = 30                 # longer cooldown between fires
SS_DD_HALT_THRESHOLD = -500.0        # halt if rolling 6h PnL drops below this
SS_DD_HALT_DURATION_MIN = 120        # halt for 2 hours after trigger
SS_DD_LOOKBACK_SEC = 21600           # 6h rolling window for drawdown check

# Standard book filters
OR_MIN_ENTRY = 0.10
OR_MAX_ENTRY = 0.90
OR_MAX_SPREAD = 0.04
OR_MIN_BOOK_LEVELS = 2

COMBO_PARAMS = [
    {"name": "SS_smooth", "btc_threshold_bp": 0, "lookback_s": 0},
]

# State
_last_signal_time = [0.0]
_last_status = [0.0]
_last_peer_check = [0.0]
_peer_cache = []  # list of (ts, session, direction)
_halt_until = [0.0]
_recent_pnl = deque()  # (ts, pnl) tuples for rolling 6h drawdown tracking
_last_seen_trade_count = [0]
_edge_table_local = None

# Stats
_stats = {
    "queued_no_peer_agreement": 0,
    "queued_no_table_edge": 0,
    "fired": 0,
    "halts_triggered": 0,
}


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [SS] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
        print("  [SS] Min confirms: {}, peer window: {}s, sessions: {}".format(
            SS_MIN_CONFIRMS, SS_PEER_WINDOW_SEC, ", ".join(SS_PEER_SESSIONS)))
        print("  [SS] Base size: ${}, DD halt: <${} over 6h, halt duration: {}min".format(
            SS_BASE_DOLLARS, SS_DD_HALT_THRESHOLD, SS_DD_HALT_DURATION_MIN))
    else:
        print("  [SS] WARNING: No edge table found!")
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
        return None, None

    wr = cell["wr"]
    table_dir = "YES" if delta_bps > 0 else "NO"
    if direction != table_dir:
        wr = 1.0 - wr
    return wr, level


def _refresh_peers():
    """Read recent peer fires from stats.json files."""
    global _peer_cache
    now = time.time()
    if now - _last_peer_check[0] < 5.0:  # don't spam disk, 5s is fine for 900s window
        return
    _last_peer_check[0] = now

    cutoff = now - SS_PEER_WINDOW_SEC
    new_cache = [(ts, s, d) for ts, s, d in _peer_cache if ts >= cutoff]

    for sess in SS_PEER_SESSIONS:
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
                if key not in new_cache:
                    new_cache.append(key)
        except Exception:
            pass

    _peer_cache = new_cache


def _check_peer_consensus():
    """Returns (direction, count, sessions_set) of peer agreement, or (None, 0, set())."""
    now = time.time()
    cutoff = now - SS_PEER_WINDOW_SEC
    recent = [(ts, s, d) for ts, s, d in _peer_cache if ts >= cutoff]

    yes_sessions = set(s for _, s, d in recent if d == "YES")
    no_sessions = set(s for _, s, d in recent if d == "NO")

    if len(yes_sessions) >= SS_MIN_CONFIRMS and len(yes_sessions) > len(no_sessions):
        return "YES", len(yes_sessions), yes_sessions
    if len(no_sessions) >= SS_MIN_CONFIRMS and len(no_sessions) > len(yes_sessions):
        return "NO", len(no_sessions), no_sessions
    return None, 0, set()


def _update_recent_pnl(state):
    """Track our own recent trade outcomes for the rolling 6h drawdown check."""
    if not state.combos:
        return
    combo = state.combos[0]
    all_trades = combo.trades
    if len(all_trades) <= _last_seen_trade_count[0]:
        return
    new_trades = all_trades[_last_seen_trade_count[0]:]
    for t in new_trades:
        pnl = t.get("pnl_taker", 0)
        ts = t.get("timestamp", time.time())
        _recent_pnl.append((ts, pnl))
    _last_seen_trade_count[0] = len(all_trades)

    # Drop entries older than the lookback window
    cutoff = time.time() - SS_DD_LOOKBACK_SEC
    while _recent_pnl and _recent_pnl[0][0] < cutoff:
        _recent_pnl.popleft()


def _get_rolling_6h_pnl():
    """Sum of PnL over the last SS_DD_LOOKBACK_SEC seconds."""
    return sum(p for _, p in _recent_pnl)


def on_tick(state, price, ts):
    _load_table()


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return

    now = time.time()
    # Check halt status
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
    if time_remaining < 5 or time_remaining > 295:
        return

    if state.binance_price is None or not state.window_open or state.window_open <= 0:
        return

    if now - _last_signal_time[0] < getattr(engine, 'SS_COOLDOWN_SEC', SS_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    # Update PnL tracking and check 6h drawdown halt
    _update_recent_pnl(state)
    rolling_pnl = _get_rolling_6h_pnl()
    halt_threshold = getattr(engine, 'SS_DD_HALT_THRESHOLD', SS_DD_HALT_THRESHOLD)
    halt_duration_min = getattr(engine, 'SS_DD_HALT_DURATION_MIN', SS_DD_HALT_DURATION_MIN)

    if rolling_pnl < halt_threshold and len(_recent_pnl) >= 5:
        _halt_until[0] = now + halt_duration_min * 60
        _stats["halts_triggered"] += 1
        print("  {}[SS] DD HALT — rolling 6h PnL ${:+.0f} < ${:.0f}, pausing {} min{}".format(
            engine.R, rolling_pnl, halt_threshold, halt_duration_min, engine.RST))
        return

    # Refresh peer cache
    _refresh_peers()
    direction, confirm_count, confirm_sessions = _check_peer_consensus()

    btc_corrected = state.binance_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # Status (every 12s)
    if now - _last_status[0] >= 12:
        peer_str = "{}({}: {})".format(direction, confirm_count, ",".join(sorted(confirm_sessions))) if direction else "no consensus ({} confirms)".format(confirm_count)
        rwr_str = "${:+.0f}".format(rolling_pnl) if _recent_pnl else "n/a"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {:+.1f}bp | peers: {} | 6h PnL: {} | (q✗p:{} q✗t:{} f:{} h:{})".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            delta_bps, peer_str, rwr_str,
            _stats["queued_no_peer_agreement"], _stats["queued_no_table_edge"],
            _stats["fired"], _stats["halts_triggered"]))
        _last_status[0] = now

    # Layer 1: Need peer consensus
    if direction is None:
        _stats["queued_no_peer_agreement"] += 1
        return

    # Get entry for the consensus direction
    entry = state.book.best_ask if direction == "YES" else state.book.best_bid
    min_entry = getattr(engine, 'OR_MIN_ENTRY', OR_MIN_ENTRY)
    max_entry = getattr(engine, 'OR_MAX_ENTRY', OR_MAX_ENTRY)
    if entry < min_entry or entry > max_entry:
        return

    # Layer 2: Edge table sanity check
    table_wr, level = _get_table_wr(state, direction, time_remaining)
    if table_wr is None:
        return

    fee = entry * (1 - entry) * 0.072
    if direction == "YES":
        breakeven = entry + fee
    else:
        breakeven = 1.0 - (entry - fee)
    edge = table_wr - breakeven

    min_edge = getattr(engine, 'SS_MIN_EDGE', SS_MIN_EDGE)
    if edge < min_edge:
        _stats["queued_no_table_edge"] += 1
        return

    # Layer 5: One trade per window
    combo = state.combos[0]
    if combo.has_position_in_window(state.window_start):
        return

    # Layer 3: Small positions
    base_dollars = getattr(engine, 'SS_BASE_DOLLARS', SS_BASE_DOLLARS)
    dollars = base_dollars

    # More confirmations = slight boost (but capped to not break smoothness)
    if confirm_count >= 4:
        dollars = int(base_dollars * 1.2)

    # Vol-adjusted sizing
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(20, int(dollars * vol_mult))

    print("  {}[SS] FIRE {} confirms={}({}) edge={:.0%} 6h={:+.0f} @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, confirm_count, ",".join(sorted(confirm_sessions)),
        edge, rolling_pnl, entry * 100, dollars, level, time_remaining, engine.RST))

    _stats["fired"] += 1
    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "smooth_seeker",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "OR_MIN_ENTRY": OR_MIN_ENTRY,
        "OR_MAX_ENTRY": OR_MAX_ENTRY,
        "OR_MAX_SPREAD": OR_MAX_SPREAD,
        "OR_MIN_BOOK_LEVELS": OR_MIN_BOOK_LEVELS,
        "SS_MIN_CONFIRMS": SS_MIN_CONFIRMS,
        "SS_PEER_WINDOW_SEC": SS_PEER_WINDOW_SEC,
        "SS_BASE_DOLLARS": SS_BASE_DOLLARS,
        "SS_MIN_EDGE": SS_MIN_EDGE,
        "SS_COOLDOWN_SEC": SS_COOLDOWN_SEC,
        "SS_DD_HALT_THRESHOLD": SS_DD_HALT_THRESHOLD,
        "SS_DD_HALT_DURATION_MIN": SS_DD_HALT_DURATION_MIN,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
