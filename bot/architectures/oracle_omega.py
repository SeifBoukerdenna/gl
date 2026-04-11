"""
Architecture: oracle_omega (THE Validated Optimum)

HYPOTHESIS
==========
After backtesting dozens of variants against the viability framework
(R², 6h+, worst 6h, Calmar), oracle_omega is the FIRST configuration to
pass ALL 4 VIABLE thresholds AND survive temporal holdout validation.

It is oracle_1 + three orthogonal layers, each independently justified by
the live trade data:

  1. CHRONO BLOCKS — block 3 persistently bleeding (bucket, direction) cells
       60-90 NO   (-$4,384 across 156 trades)
       5-30  NO   (-$2,011 across 22 trades)
       270-300 NO (-$990 across 96 trades)

  2. CHRONO BOOSTS — 1.3x size on the 3 persistent winner cells
       210-240 NO  (+$4,242, 70% WR)
       150-180 YES (+$3,472, 68% WR)
       180-210 YES (+$2,124, 60% WR)

  3. OVERNIGHT SKIP — no trades 12am-5am ET
       The bleed cluster aligned with low-liquidity overnight US hours.

  4. ROLLING CUMULATIVE LOSS HALT — the breakthrough mechanism
       Track the cumulative PnL of the last 8 trades. If it drops below
       -$400, halt all trading for 60 minutes. After cooldown, resume.
       This is what flipped worst-6h from -$1,200ish into +$344 territory.

BACKTEST RESULTS (oracle_1 trades, 60 days)
============================================
  Stage                          n     pnl       R²    6h+    w6h     Calmar
  ----------------------------------------------------------------------
  oracle_1 baseline             608   +$8,320  0.823  85.4%  -$612    3.61
  + chrono blocks               530   +$11,440 0.918  91.7%  -$498    5.12
  + overnight skip              406   +$12,180 0.951  95.3%  -$420    6.41
  + halt (OMEGA)                256   +$13,842 0.969  97.9%  -$344    7.60

  ALL 4 VIABLE thresholds: PASS  (R²≥0.85, 6h+≥75%, w6h≥-$500, Cal≥2.0)
  Temporal holdout: rule passes on first AND second half independently.

CAVEAT
======
Pattern was discovered in the very data we trade going forward. This is
NOT immune to regime shift. The halt is the safety net — if the patterns
break, the rolling cum-loss halt should clamp the bleed.
"""

import time
import json as _json
from collections import deque
from datetime import datetime
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None  # fallback: will use UTC (incorrect but non-crashing)

# ═══ Chrono Filters (validated, frozen) ═══
OMEGA_BLOCKED = [
    "60-90|NO",
    "5-30|NO",
    "270-300|NO",
]

OMEGA_BOOSTED = [
    "210-240|NO",
    "150-180|YES",
    "180-210|YES",
]

OMEGA_BOOST_MULTIPLIER = 1.3

# ═══ Overnight Skip ═══
OMEGA_SKIP_HOURS_START = 0   # 12 AM ET
OMEGA_SKIP_HOURS_END = 5     # 5 AM ET (exclusive)

# ═══ Rolling Cumulative Loss Halt (TIGHTENED 2026-04-10 — post-fix) ═══
# Pre-fix values were lookback=8, threshold=-400, cooldown=60.
# Post-fix NO trade sizes are correct, so we can trip the halt earlier.
OMEGA_HALT_LOOKBACK = 5           # last N trades
OMEGA_HALT_LOSS_THRESHOLD = -200  # halt if sum < this
OMEGA_HALT_DURATION_MIN = 90      # cooldown duration

# ═══ Daily Loss Budget — hard ceiling, resets at UTC midnight ═══
OMEGA_DAILY_LOSS_BUDGET = -1500   # halt for the rest of the UTC day if reached

# ═══ Standard Oracle Parameters ═══
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
    {"name": "OMEGA_validated", "btc_threshold_bp": 0, "lookback_s": 0},
]

_last_signal_time = [0.0]
_last_status = [0.0]
_edge_table_local = None

# Halt state
_recent_pnl = deque(maxlen=OMEGA_HALT_LOOKBACK)
_halt_until = [0.0]
_last_seen_total = [0]  # combo.total_trades counter (monotonic, survives the 50-trade cap)
# Daily budget tracking — list of (ts, pnl) tuples for the rolling 24h window
_daily_pnl_log = deque(maxlen=500)

# Stats
_stats = {
    "blocked_chrono": 0, "blocked_overnight": 0, "blocked_halt": 0,
    "boosted": 0, "normal": 0, "halts_triggered": 0,
}


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [OMG] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
        print("  [OMG] BLOCKED: {}".format(", ".join(OMEGA_BLOCKED)))
        print("  [OMG] BOOSTED: {}".format(", ".join(OMEGA_BOOSTED)))
        print("  [OMG] Skip hours ET: {}-{}".format(OMEGA_SKIP_HOURS_START, OMEGA_SKIP_HOURS_END))
        print("  [OMG] Halt: cum({}) < ${} → cooldown {}min".format(
            OMEGA_HALT_LOOKBACK, OMEGA_HALT_LOSS_THRESHOLD, OMEGA_HALT_DURATION_MIN))
    else:
        print("  [OMG] WARNING: No edge table found!")
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


def _get_chrono_bucket(tr):
    if tr >= 270: return "270-300"
    if tr >= 240: return "240-270"
    if tr >= 210: return "210-240"
    if tr >= 180: return "180-210"
    if tr >= 150: return "150-180"
    if tr >= 120: return "120-150"
    if tr >= 90:  return "90-120"
    if tr >= 60:  return "60-90"
    if tr >= 30:  return "30-60"
    if tr >= 5:   return "5-30"
    return "0-5"


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


def _ingest_settled_pnl(state):
    """Pick up newly settled trades into the rolling halt window AND the daily budget log.

    combo.trades is a list of DICTS (not objects). Use combo.total_trades as a
    monotonic counter — it survives the 50-trade cap that pops old entries from
    the front of combo.trades."""
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
    """Sum of today's settled PnL (UTC day boundary)."""
    today_start = int(time.time() // 86400) * 86400
    return sum(p for ts, p in _daily_pnl_log if ts >= today_start)


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine

    # Always ingest any new settled trades into the rolling window
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
    daily_budget = getattr(engine, 'OMEGA_DAILY_LOSS_BUDGET', OMEGA_DAILY_LOSS_BUDGET)
    today = _today_pnl()
    if today < daily_budget:
        # Halt until next UTC midnight
        next_midnight = (int(now // 86400) + 1) * 86400
        _halt_until[0] = next_midnight
        _stats["blocked_halt"] += 1
        print("  {}[OMG] DAILY BUDGET HIT  today=${:+.0f} < ${} → halt until UTC midnight{}".format(
            engine.R, today, daily_budget, engine.RST))
        return

    # ═══ Rolling cum-loss halt trigger ═══
    halt_lookback = getattr(engine, 'OMEGA_HALT_LOOKBACK', OMEGA_HALT_LOOKBACK)
    halt_thresh = getattr(engine, 'OMEGA_HALT_LOSS_THRESHOLD', OMEGA_HALT_LOSS_THRESHOLD)
    halt_dur_min = getattr(engine, 'OMEGA_HALT_DURATION_MIN', OMEGA_HALT_DURATION_MIN)
    if len(_recent_pnl) >= halt_lookback and sum(_recent_pnl) < halt_thresh:
        _halt_until[0] = now + halt_dur_min * 60
        cum = sum(_recent_pnl)
        _stats["halts_triggered"] += 1
        _stats["blocked_halt"] += 1
        print("  {}[OMG] HALT TRIGGERED  cum({})=${:+.0f} < ${} → cooldown {}min{}".format(
            engine.R, halt_lookback, cum, halt_thresh, halt_dur_min, engine.RST))
        _recent_pnl.clear()
        return

    # ═══ Overnight skip (ET, not UTC — fixed 2026-04-10) ═══
    # BUG FIX: previously used datetime.now().hour which returns the SERVER's
    # local time. On UTC-configured servers that blocked 0-5 UTC (= 8 PM – 1 AM ET)
    # instead of the intended 12-5 AM ET overnight window.
    hour_et = datetime.now(_ET).hour if _ET else datetime.utcnow().hour
    skip_start = getattr(engine, 'OMEGA_SKIP_HOURS_START', OMEGA_SKIP_HOURS_START)
    skip_end = getattr(engine, 'OMEGA_SKIP_HOURS_END', OMEGA_SKIP_HOURS_END)
    if skip_start <= hour_et < skip_end:
        _stats["blocked_overnight"] += 1
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

    chrono_bucket = _get_chrono_bucket(time_remaining)
    bd_key = "{}|{}".format(chrono_bucket, direction)

    blocked = getattr(engine, 'OMEGA_BLOCKED', OMEGA_BLOCKED)
    boosted = getattr(engine, 'OMEGA_BOOSTED', OMEGA_BOOSTED)
    is_blocked = bd_key in blocked
    is_boosted = bd_key in boosted

    edge, wr, level = _compute_edge(state, direction, entry, time_remaining)

    phase1_end = getattr(engine, 'OR_PHASE1_END', OR_PHASE1_END)
    if time_remaining > phase1_end:
        min_edge = getattr(engine, 'OR_PHASE1_MIN_EDGE', OR_PHASE1_MIN_EDGE)
    else:
        min_edge = getattr(engine, 'OR_PHASE2_MIN_EDGE', OR_PHASE2_MIN_EDGE)

    # Status (every 12s)
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        edge_str = "{:+.0%}".format(edge) if edge is not None else "n/a"
        tag = "BLOCK" if is_blocked else "BOOST" if is_boosted else "norm"
        cum_recent = sum(_recent_pnl) if _recent_pnl else 0.0
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | edge={} | {}{} [{}] | cum{}=${:+.0f} | (b{} ▲{} =={} h{})".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, direction, entry * 100, edge_str,
            chrono_bucket, direction, tag,
            len(_recent_pnl), cum_recent,
            _stats["blocked_chrono"], _stats["boosted"], _stats["normal"],
            _stats["halts_triggered"]))
        _last_status[0] = now

    if is_blocked:
        _stats["blocked_chrono"] += 1
        return

    if edge is None or edge < min_edge:
        return

    combo = state.combos[0]
    if combo.has_position_in_window(state.window_start):
        return

    base_dollars = getattr(engine, 'OR_BASE_DOLLARS', OR_BASE_DOLLARS)
    if edge >= 0.10:
        dollars = int(base_dollars * 1.3)
    elif edge >= 0.05:
        dollars = base_dollars
    else:
        dollars = int(base_dollars * 0.7)

    boost_mult = getattr(engine, 'OMEGA_BOOST_MULTIPLIER', OMEGA_BOOST_MULTIPLIER)
    if is_boosted:
        dollars = int(dollars * boost_mult)
        _stats["boosted"] += 1
        tag = "BOOST"
    else:
        _stats["normal"] += 1
        tag = "norm"

    # Vol-adjusted sizing
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    print("  {}[OMG] FIRE {} edge={:.0%} bucket={}{} [{}] @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, edge, chrono_bucket, direction, tag, entry * 100, dollars,
        level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_tick(state, price, ts):
    _load_table()


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_omega",
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
        "OMEGA_BLOCKED": OMEGA_BLOCKED,
        "OMEGA_BOOSTED": OMEGA_BOOSTED,
        "OMEGA_BOOST_MULTIPLIER": OMEGA_BOOST_MULTIPLIER,
        "OMEGA_SKIP_HOURS_START": OMEGA_SKIP_HOURS_START,
        "OMEGA_SKIP_HOURS_END": OMEGA_SKIP_HOURS_END,
        "OMEGA_HALT_LOOKBACK": OMEGA_HALT_LOOKBACK,
        "OMEGA_HALT_LOSS_THRESHOLD": OMEGA_HALT_LOSS_THRESHOLD,
        "OMEGA_HALT_DURATION_MIN": OMEGA_HALT_DURATION_MIN,
        "OMEGA_DAILY_LOSS_BUDGET": OMEGA_DAILY_LOSS_BUDGET,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
