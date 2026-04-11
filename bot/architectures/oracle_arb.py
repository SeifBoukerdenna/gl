"""
Architecture: oracle_arb (Edge Table vs Market Mispricing)

HYPOTHESIS
==========
The edge table tells us "this setup wins X% of the time historically." But the
PM order book ALSO tells us "the market thinks this setup wins Y% of the time"
(via the implied probability = entry price for YES, or 1-entry for NO).

Real edge only exists when the table sees something the market DOESN'T. When
both agree, there's no mispricing to capture — the market already prices in
whatever historical pattern oracle is detecting.

This explains why entry price doesn't filter directly: when the table says 65%
and you're paying 60c, you're capturing 5pp of edge. When the table says 65% and
you're paying 65c, the market already priced it in — no edge, just risk.

LOGIC
=====
For each potential trade:
  1. Table predicted WR  →  table_wr
  2. Market implied WR   →  market_wr (= entry_price for YES, 1-entry for NO)
  3. Mispricing          →  table_wr - market_wr
  4. Fire only if mispricing >= MISPRICING_MIN_BPS (default 5pp)

This REPLACES the edge threshold logic. We don't care about absolute edge
anymore — we care about the GAP between table and market.

When the market is calm and the table is well-calibrated, almost everything will
have small gaps and we won't fire much. When the market is whipsawing and the
book is lagging, gaps will appear and we'll fire on those specifically.
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# ═══ Parameters ═══
OR_PHASE1_END = 90
OR_MIN_ENTRY = 0.10
OR_MAX_ENTRY = 0.95
OR_MAX_SPREAD = 0.04
OR_MIN_BOOK_LEVELS = 2
OR_COOLDOWN_SEC = 10
OR_BASE_DOLLARS = 200

# Mispricing parameters (NEW — replaces edge threshold)
MISPRICING_MIN_PP = 0.05    # 5pp minimum gap between table and market
MISPRICING_STRONG_PP = 0.10 # 10pp = strong mispricing → 1.5x size
MISPRICING_MAX_PP = 0.30    # 30pp+ = probably stale data, skip

# ═══ Halt parameters (added 2026-04-11) ═══
# Conservative config matching the already-battle-tested omega/titan halts.
# Rationale: oracle_arb's worst-6h of -$5,449 during strike-pinning chop
# represents a real failure mode, not noise. A circuit breaker catches it.
OA_HALT_LOOKBACK = 8              # last N trades
OA_HALT_LOSS_THRESHOLD = -200     # halt if sum < this
OA_HALT_DURATION_MIN = 90         # cooldown duration
OA_DAILY_LOSS_BUDGET = -1500      # daily hard ceiling

COMBO_PARAMS = [
    {"name": "OA_arb", "btc_threshold_bp": 0, "lookback_s": 0},
]

_last_signal_time = [0.0]
_last_status = [0.0]
_edge_table_local = None

# ═══ Halt state ═══
_recent_pnl = deque(maxlen=OA_HALT_LOOKBACK)
_halt_until = [0.0]
_daily_pnl_log = deque(maxlen=1000)  # (ts, pnl) tuples
_last_seen_total = [0]


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [OA] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
    else:
        print("  [OA] WARNING: No edge table found!")
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
    """Get the historical WR from the edge table for this setup, in the bet direction."""
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


def on_tick(state, price, ts):
    _load_table()


def _ingest_settled_pnl(state):
    """Pick up newly settled trades into the rolling halt window + daily log.
    Mirrors oracle_omega.py pattern — combo.trades is a list of dicts,
    combo.total_trades is a monotonic counter that survives the 50-trade cap."""
    if not state.combos:
        return
    combo = state.combos[0]
    cur = combo.total_trades
    if cur <= _last_seen_total[0]:
        return
    new_count = cur - _last_seen_total[0]
    new_trades = combo.trades[-new_count:] if new_count <= len(combo.trades) else combo.trades
    for t in new_trades:
        pnl = float(t.get("pnl_taker", 0.0))
        ts = float(t.get("timestamp", time.time()))
        _recent_pnl.append(pnl)
        _daily_pnl_log.append((ts, pnl))
    _last_seen_total[0] = cur


def _today_pnl():
    today_start = int(time.time() // 86400) * 86400
    return sum(p for ts, p in _daily_pnl_log if ts >= today_start)


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine

    # Always ingest any new settled trades into the rolling halt window
    _ingest_settled_pnl(state)

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

    # ═══ Halt cooldown check ═══
    if now < _halt_until[0]:
        return

    # ═══ Daily loss budget — hard ceiling, resets at UTC midnight ═══
    daily_budget = getattr(engine, 'OA_DAILY_LOSS_BUDGET', OA_DAILY_LOSS_BUDGET)
    today = _today_pnl()
    if today < daily_budget:
        next_midnight = (int(now // 86400) + 1) * 86400
        _halt_until[0] = next_midnight
        print("  {}[OA] DAILY BUDGET HIT  today=${:+.0f} < ${} → halt until UTC midnight{}".format(
            engine.R, today, daily_budget, engine.RST))
        return

    # ═══ Rolling cum-loss halt trigger ═══
    halt_lookback = getattr(engine, 'OA_HALT_LOOKBACK', OA_HALT_LOOKBACK)
    halt_thresh = getattr(engine, 'OA_HALT_LOSS_THRESHOLD', OA_HALT_LOSS_THRESHOLD)
    halt_dur_min = getattr(engine, 'OA_HALT_DURATION_MIN', OA_HALT_DURATION_MIN)
    if len(_recent_pnl) >= halt_lookback and sum(_recent_pnl) < halt_thresh:
        _halt_until[0] = now + halt_dur_min * 60
        cum = sum(_recent_pnl)
        print("  {}[OA] HALT TRIGGERED  cum({})=${:+.0f} < ${} → cooldown {}min{}".format(
            engine.R, halt_lookback, cum, halt_thresh, halt_dur_min, engine.RST))
        _recent_pnl.clear()
        return

    btc_corrected = state.binance_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # ═══ The Arbitrage Logic: try BOTH directions, pick the bigger mispricing ═══
    candidates = []

    # Try YES
    yes_entry = state.book.best_ask
    if yes_entry > 0.01 and yes_entry < 0.99:
        yes_table_wr, yes_level = _get_table_wr(state, "YES", time_remaining)
        if yes_table_wr is not None:
            yes_market_wr = yes_entry  # market's implied YES WR = ask price
            yes_mispricing = yes_table_wr - yes_market_wr
            candidates.append(("YES", yes_entry, yes_table_wr, yes_market_wr, yes_mispricing, yes_level))

    # Try NO
    no_entry_yes_side = state.book.best_bid  # we'd "sell YES at bid" = buy NO at (1-bid)
    if no_entry_yes_side > 0.01 and no_entry_yes_side < 0.99:
        no_table_wr, no_level = _get_table_wr(state, "NO", time_remaining)
        if no_table_wr is not None:
            no_market_wr = 1.0 - no_entry_yes_side  # market's implied NO WR
            no_mispricing = no_table_wr - no_market_wr
            candidates.append(("NO", no_entry_yes_side, no_table_wr, no_market_wr, no_mispricing, no_level))

    if not candidates:
        return

    # Pick the candidate with the biggest mispricing
    candidates.sort(key=lambda c: c[4], reverse=True)
    direction, entry, table_wr, market_wr, mispricing, level = candidates[0]

    # Status
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | best:{} table_wr={:.0%} mkt_wr={:.0%} gap={:+.0%}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, direction, table_wr, market_wr, mispricing))
        _last_status[0] = now

    min_pp = getattr(engine, 'MISPRICING_MIN_PP', MISPRICING_MIN_PP)
    max_pp = getattr(engine, 'MISPRICING_MAX_PP', MISPRICING_MAX_PP)
    strong_pp = getattr(engine, 'MISPRICING_STRONG_PP', MISPRICING_STRONG_PP)

    # Filter: must be a real mispricing in our favor
    if mispricing < min_pp:
        return
    # Filter: too-extreme mispricings are usually stale data
    if mispricing > max_pp:
        return

    min_entry = getattr(engine, 'OR_MIN_ENTRY', OR_MIN_ENTRY)
    max_entry = getattr(engine, 'OR_MAX_ENTRY', OR_MAX_ENTRY)
    if entry < min_entry or entry > max_entry:
        return

    combo = state.combos[0]
    if combo.has_position_in_window(state.window_start):
        return

    base_dollars = getattr(engine, 'OR_BASE_DOLLARS', OR_BASE_DOLLARS)
    if mispricing >= strong_pp:
        dollars = int(base_dollars * 1.5)
    else:
        dollars = base_dollars

    # Vol-adjusted sizing
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    print("  {}[OA] FIRE {} table={:.0%} mkt={:.0%} gap={:+.0%} @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, table_wr, market_wr, mispricing, entry * 100, dollars,
        level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_arb",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "OR_MIN_ENTRY": OR_MIN_ENTRY,
        "OR_MAX_ENTRY": OR_MAX_ENTRY,
        "OR_MAX_SPREAD": OR_MAX_SPREAD,
        "OR_MIN_BOOK_LEVELS": OR_MIN_BOOK_LEVELS,
        "OR_COOLDOWN_SEC": OR_COOLDOWN_SEC,
        "OR_BASE_DOLLARS": OR_BASE_DOLLARS,
        "OR_MAX_TIME": 295,
        "OR_MIN_TIME": 5,
        "MISPRICING_MIN_PP": MISPRICING_MIN_PP,
        "MISPRICING_STRONG_PP": MISPRICING_STRONG_PP,
        "MISPRICING_MAX_PP": MISPRICING_MAX_PP,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
        "OA_HALT_LOOKBACK": OA_HALT_LOOKBACK,
        "OA_HALT_LOSS_THRESHOLD": OA_HALT_LOSS_THRESHOLD,
        "OA_HALT_DURATION_MIN": OA_HALT_DURATION_MIN,
        "OA_DAILY_LOSS_BUDGET": OA_DAILY_LOSS_BUDGET,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
