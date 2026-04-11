"""
Architecture: oracle_titan (Defense-in-Depth Oracle — final form)

HYPOTHESIS
==========
Combine the proven advantages of every architecture we've tested into one
session:

  - oracle_1     → edge table V2 picks the SETUP
  - oracle_omega → chrono blocks + chrono boosts + overnight skip + rolling halt
  - oracle_consensus V2 → LEVEL-based cross-venue veto (no Binance-only artifacts)
  - test_ic_wide / blitz_1 → present-tense cross-venue circuit breaker
  - vol regime gate from oracle_chrono V2 → silent skip in dead/panic markets

The thesis from our analysis: oracle's edge is real but FRAGILE because it's
backward-looking. Multi-venue architectures (blitz, ic_wide, cross_pressure)
survived the April 9 bloodbath because their TRIGGER LOGIC naturally
self-silenced when consensus broke down. titan layers that same property on
top of oracle, so we get oracle's selectivity AND multi-venue's robustness.

DEFENSE LAYERS (in order of evaluation)
=======================================
  1. Engine guards (book ok, spread ok, time window valid)
  2. Vol regime gate (skip if 1h vol < 25% or > 80%)
  3. Halt cooldown check (60min after trip)
  4. Halt trigger check (rolling 8 trades < -$400)
  5. Overnight skip (0-5 ET)
  6. Chrono BLOCKED buckets (60-90 NO, 5-30 NO, 270-300 NO)
  7. Edge table lookup → edge >= TITAN_MIN_EDGE
  8. Cross-venue LEVEL consensus (≥1 venue agrees, hard veto on disagreement)

SIZING (compounded multipliers)
===============================
  base × edge_ladder × chrono_boost × consensus_bonus × vol_adjustment

  edge_ladder:
    edge ≥ 0.10 → 1.3x
    edge ≥ 0.05 → 1.0x
    else        → 0.7x
  chrono_boost: 1.3x on (210-240 NO, 150-180 YES, 180-210 YES)
  consensus_bonus: 1.2x if BOTH venues confirm, 1.0x if only 1
  vol_adjustment: capped by current realized sigma

WHY THIS IS DIFFERENT FROM oracle_omega
=======================================
omega has chrono + overnight + halt but is still BINANCE-ONLY for the trigger.
A bad Binance feed event would make omega bleed. titan adds cross-venue
LEVEL consensus, which is the property that let blitz / ic_wide / cross_pressure
survive the bloodbath. The fire rate is lower (~3-5/day) but each fire is
high-conviction.

EXPECTED FIRE RATE
==================
~3-5 trades per day after all filters. This is the conservative end of the
portfolio. Sized for high conviction, not volume.
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
    _ET = None

# Reuse the exchange feed infrastructure from impulse_confirmed.
from bot.architectures.impulse_confirmed import (
    _start_feeds as _start_exchange_feeds,
    _exchange_prices,
    _lock as _exchange_lock,
)

# ═══ Edge table thresholds ═══
TITAN_PHASE1_END = 90              # T_remaining > this → phase 1 (early)
TITAN_PHASE1_MIN_EDGE = 0.07       # phase 1 min edge (slightly relaxed from 0.08
                                    # to compensate for the consensus filter)
TITAN_PHASE2_MIN_EDGE = 0.05       # phase 2 min edge

# ═══ Chrono filters (validated, frozen) ═══
TITAN_BLOCKED = ["60-90|NO", "5-30|NO", "270-300|NO"]
TITAN_BOOSTED = ["210-240|NO", "150-180|YES", "180-210|YES"]
TITAN_BOOST_MULTIPLIER = 1.3

# ═══ Overnight skip (low-liquidity regime) ═══
TITAN_SKIP_HOURS_START = 0     # 12 AM ET
TITAN_SKIP_HOURS_END = 5       # 5 AM ET (exclusive)

# ═══ Vol regime gate ═══
TITAN_MIN_VOL_PCT = 25.0       # skip if 1h annualized vol < this (dead market)
TITAN_MAX_VOL_PCT = 80.0       # skip if vol > this (panic regime)

# ═══ Cross-venue consensus (V2 level-based) ═══
TITAN_REQUIRE_CONSENSUS = True
TITAN_MIN_CONFIRMS = 1               # at least N venues must agree on side
TITAN_MAX_VENUE_AGE_SEC = 3          # reject venue prices older than this
TITAN_LEVEL_TOLERANCE_BPS = 0.5      # venue within this of strike → abstain
TITAN_VETO_ON_DISAGREEMENT = True    # any opposite-side venue is a hard veto
TITAN_BOTH_VENUE_BONUS = 1.2         # size multiplier when both venues confirm

# ═══ Rolling cumulative loss halt (TIGHTENED 2026-04-10 — post-fix) ═══
TITAN_HALT_LOOKBACK = 5
TITAN_HALT_LOSS_THRESHOLD = -200
TITAN_HALT_DURATION_MIN = 90

# ═══ Daily Loss Budget — hard ceiling, resets at UTC midnight ═══
TITAN_DAILY_LOSS_BUDGET = -1500

# ═══ Standard oracle parameters ═══
OR_MIN_ENTRY = 0.10
OR_MAX_ENTRY = 0.88
OR_MAX_SPREAD = 0.04
OR_MIN_BOOK_LEVELS = 2
OR_COOLDOWN_SEC = 10
OR_BASE_DOLLARS = 200

COMBO_PARAMS = [
    {"name": "TITAN_core", "btc_threshold_bp": 0, "lookback_s": 0},
]

_last_signal_time = [0.0]
_last_status = [0.0]
_edge_table_local = None

# Halt state
_recent_pnl = deque(maxlen=TITAN_HALT_LOOKBACK)
_halt_until = [0.0]
_last_seen_total = [0]
_daily_pnl_log = deque(maxlen=500)

_stats = {
    "fires": 0,
    "skipped_vol": 0, "skipped_overnight": 0, "skipped_chrono": 0,
    "skipped_edge": 0, "skipped_no_consensus": 0, "skipped_venue_veto": 0,
    "skipped_halt": 0, "halts_triggered": 0, "boosted": 0, "both_venues": 0,
}


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [TITAN] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
        print("  [TITAN] BLOCKED: {}".format(", ".join(TITAN_BLOCKED)))
        print("  [TITAN] BOOSTED: {}".format(", ".join(TITAN_BOOSTED)))
        print("  [TITAN] Skip hours ET: {}-{}".format(TITAN_SKIP_HOURS_START, TITAN_SKIP_HOURS_END))
        print("  [TITAN] Vol regime gate: {:.0f}-{:.0f}%".format(TITAN_MIN_VOL_PCT, TITAN_MAX_VOL_PCT))
        print("  [TITAN] Cross-venue: ≥{} confirms, veto on disagreement".format(TITAN_MIN_CONFIRMS))
        print("  [TITAN] Halt: cum({}) < ${} → {}min cooldown".format(
            TITAN_HALT_LOOKBACK, TITAN_HALT_LOSS_THRESHOLD, TITAN_HALT_DURATION_MIN))
    else:
        print("  [TITAN] WARNING: No edge table found!")
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


def _latest_venue_price(name, max_age_sec):
    """Return (price, age_sec) for the latest tick from a venue."""
    with _exchange_lock:
        prices = _exchange_prices.get(name)
        if not prices:
            return None, None
        ts, px = prices[-1]
    age = time.time() - ts
    if age > max_age_sec:
        return None, None
    return px, age


def _check_level_consensus(strike, direction, max_age_sec, tolerance_bps):
    """LEVEL-BASED cross-venue consensus.

    Returns (confirms, rejects, tags). A venue confirms if its latest price
    is on the same side of strike as our bet direction. Rejects if opposite.
    Abstains if within `tolerance_bps` of strike or no recent data.
    """
    confirms = 0
    rejects = 0
    tags = []
    for venue, label in (("coinbase", "CB"), ("bybit", "BY")):
        px, age = _latest_venue_price(venue, max_age_sec)
        if px is None or strike <= 0:
            tags.append("{}?".format(label))
            continue
        venue_delta_bps = (px - strike) / strike * 10000
        if abs(venue_delta_bps) < tolerance_bps:
            tags.append("{}~".format(label))
            continue
        venue_side = "YES" if venue_delta_bps > 0 else "NO"
        if venue_side == direction:
            confirms += 1
            tags.append("{}{:+.1f}✓".format(label, venue_delta_bps))
        else:
            rejects += 1
            tags.append("{}{:+.1f}✗".format(label, venue_delta_bps))
    return confirms, rejects, tags


def _vol_regime_ok(engine):
    """True if current 1h annualized vol is in the acceptable trading range."""
    from bot.shared.volatility import vol_tracker as _vt
    vol = _vt.get_realized_vol_pct()
    if vol is None:
        return True  # no data, allow trading
    min_vol = getattr(engine, 'TITAN_MIN_VOL_PCT', TITAN_MIN_VOL_PCT)
    max_vol = getattr(engine, 'TITAN_MAX_VOL_PCT', TITAN_MAX_VOL_PCT)
    return min_vol <= vol <= max_vol


def _ingest_settled_pnl(state):
    """Pick up newly settled trades into the rolling halt window AND the daily budget log."""
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


def on_tick(state, price, ts):
    _load_table()
    _start_exchange_feeds()  # idempotent — only starts once


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine

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
    if time_remaining < 5 or time_remaining > 295:
        return

    if state.binance_price is None or not state.window_open or state.window_open <= 0:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'OR_COOLDOWN_SEC', OR_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    # ═══ 1. Vol regime gate ═══
    if not _vol_regime_ok(engine):
        _stats["skipped_vol"] += 1
        return

    # ═══ 2. Halt cooldown ═══
    if now < _halt_until[0]:
        return

    # ═══ 2b. Daily loss budget (resets at UTC midnight) ═══
    daily_budget = getattr(engine, 'TITAN_DAILY_LOSS_BUDGET', TITAN_DAILY_LOSS_BUDGET)
    today = _today_pnl()
    if today < daily_budget:
        next_midnight = (int(now // 86400) + 1) * 86400
        _halt_until[0] = next_midnight
        _stats["skipped_halt"] += 1
        print("  {}[TITAN] DAILY BUDGET HIT  today=${:+.0f} < ${} → halt until UTC midnight{}".format(
            engine.R, today, daily_budget, engine.RST))
        return

    # ═══ 3. Halt trigger ═══
    halt_lookback = getattr(engine, 'TITAN_HALT_LOOKBACK', TITAN_HALT_LOOKBACK)
    halt_thresh = getattr(engine, 'TITAN_HALT_LOSS_THRESHOLD', TITAN_HALT_LOSS_THRESHOLD)
    halt_dur_min = getattr(engine, 'TITAN_HALT_DURATION_MIN', TITAN_HALT_DURATION_MIN)
    if len(_recent_pnl) >= halt_lookback and sum(_recent_pnl) < halt_thresh:
        _halt_until[0] = now + halt_dur_min * 60
        cum = sum(_recent_pnl)
        _stats["halts_triggered"] += 1
        _stats["skipped_halt"] += 1
        print("  {}[TITAN] HALT  cum({})=${:+.0f} < ${} → cooldown {}min{}".format(
            engine.R, halt_lookback, cum, halt_thresh, halt_dur_min, engine.RST))
        _recent_pnl.clear()
        return

    # ═══ 4. Overnight skip (ET — fixed 2026-04-10) ═══
    hour_et = datetime.now(_ET).hour if _ET else datetime.utcnow().hour
    skip_s = getattr(engine, 'TITAN_SKIP_HOURS_START', TITAN_SKIP_HOURS_START)
    skip_e = getattr(engine, 'TITAN_SKIP_HOURS_END', TITAN_SKIP_HOURS_END)
    if skip_s <= hour_et < skip_e:
        _stats["skipped_overnight"] += 1
        return

    # ═══ 5. Compute current state + chrono filter ═══
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
    blocked = getattr(engine, 'TITAN_BLOCKED', TITAN_BLOCKED)
    boosted = getattr(engine, 'TITAN_BOOSTED', TITAN_BOOSTED)
    is_blocked = bd_key in blocked
    is_boosted = bd_key in boosted

    if is_blocked:
        _stats["skipped_chrono"] += 1
        return

    # ═══ 6. Edge table ═══
    edge, wr, level = _compute_edge(state, direction, entry, time_remaining)
    phase1_end = getattr(engine, 'TITAN_PHASE1_END', TITAN_PHASE1_END)
    if time_remaining > phase1_end:
        min_edge = getattr(engine, 'TITAN_PHASE1_MIN_EDGE', TITAN_PHASE1_MIN_EDGE)
    else:
        min_edge = getattr(engine, 'TITAN_PHASE2_MIN_EDGE', TITAN_PHASE2_MIN_EDGE)

    if edge is None or edge < min_edge:
        _stats["skipped_edge"] += 1
        return

    # ═══ 7. Cross-venue level consensus ═══
    require_cons = getattr(engine, 'TITAN_REQUIRE_CONSENSUS', TITAN_REQUIRE_CONSENSUS)
    confirm_count = 0
    reject_count = 0
    venue_tags = []
    if require_cons:
        max_age = getattr(engine, 'TITAN_MAX_VENUE_AGE_SEC', TITAN_MAX_VENUE_AGE_SEC)
        tol = getattr(engine, 'TITAN_LEVEL_TOLERANCE_BPS', TITAN_LEVEL_TOLERANCE_BPS)
        confirm_count, reject_count, venue_tags = _check_level_consensus(
            state.window_open, direction, max_age, tol)
        veto = getattr(engine, 'TITAN_VETO_ON_DISAGREEMENT', TITAN_VETO_ON_DISAGREEMENT)
        min_confirms = getattr(engine, 'TITAN_MIN_CONFIRMS', TITAN_MIN_CONFIRMS)
        if veto and reject_count > 0:
            _stats["skipped_venue_veto"] += 1
            return
        if confirm_count < min_confirms:
            _stats["skipped_no_consensus"] += 1
            return

    combo = state.combos[0]
    if combo.has_position_in_window(state.window_start):
        return

    # ═══ Sizing (compounded multipliers) ═══
    base_dollars = getattr(engine, 'OR_BASE_DOLLARS', OR_BASE_DOLLARS)
    if edge >= 0.10:
        dollars = int(base_dollars * 1.3)
    elif edge >= 0.05:
        dollars = base_dollars
    else:
        dollars = int(base_dollars * 0.7)

    boost_mult = getattr(engine, 'TITAN_BOOST_MULTIPLIER', TITAN_BOOST_MULTIPLIER)
    if is_boosted:
        dollars = int(dollars * boost_mult)
        _stats["boosted"] += 1

    both_bonus = getattr(engine, 'TITAN_BOTH_VENUE_BONUS', TITAN_BOTH_VENUE_BONUS)
    if confirm_count >= 2:
        dollars = int(dollars * both_bonus)
        _stats["both_venues"] += 1

    # Vol-adjusted sizing
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    # Status (every 12s)
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        edge_str = "{:+.0%}".format(edge) if edge is not None else "n/a"
        ven = ",".join(venue_tags) if venue_tags else "n/a"
        cum_recent = sum(_recent_pnl) if _recent_pnl else 0.0
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | edge={} | venues={} | cum{}=${:+.0f} | (f{} v{} h{})".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, direction, entry * 100, edge_str, ven,
            len(_recent_pnl), cum_recent,
            _stats["fires"], _stats["skipped_venue_veto"], _stats["halts_triggered"]))
        _last_status[0] = now

    venue_str = ",".join(venue_tags) if venue_tags else "n/a"
    tag = "BOOST" if is_boosted else "norm"
    print("  {}[TITAN] FIRE {} edge={:.0%} {}{} [{}] @{:.0f}c ${} venues={} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, edge, chrono_bucket, direction, tag,
        entry * 100, dollars, venue_str, level, time_remaining, engine.RST))

    _stats["fires"] += 1
    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_titan",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "TITAN_PHASE1_END": TITAN_PHASE1_END,
        "TITAN_PHASE1_MIN_EDGE": TITAN_PHASE1_MIN_EDGE,
        "TITAN_PHASE2_MIN_EDGE": TITAN_PHASE2_MIN_EDGE,
        "TITAN_BLOCKED": TITAN_BLOCKED,
        "TITAN_BOOSTED": TITAN_BOOSTED,
        "TITAN_BOOST_MULTIPLIER": TITAN_BOOST_MULTIPLIER,
        "TITAN_SKIP_HOURS_START": TITAN_SKIP_HOURS_START,
        "TITAN_SKIP_HOURS_END": TITAN_SKIP_HOURS_END,
        "TITAN_MIN_VOL_PCT": TITAN_MIN_VOL_PCT,
        "TITAN_MAX_VOL_PCT": TITAN_MAX_VOL_PCT,
        "TITAN_REQUIRE_CONSENSUS": TITAN_REQUIRE_CONSENSUS,
        "TITAN_MIN_CONFIRMS": TITAN_MIN_CONFIRMS,
        "TITAN_MAX_VENUE_AGE_SEC": TITAN_MAX_VENUE_AGE_SEC,
        "TITAN_LEVEL_TOLERANCE_BPS": TITAN_LEVEL_TOLERANCE_BPS,
        "TITAN_VETO_ON_DISAGREEMENT": TITAN_VETO_ON_DISAGREEMENT,
        "TITAN_BOTH_VENUE_BONUS": TITAN_BOTH_VENUE_BONUS,
        "TITAN_HALT_LOOKBACK": TITAN_HALT_LOOKBACK,
        "TITAN_HALT_LOSS_THRESHOLD": TITAN_HALT_LOSS_THRESHOLD,
        "TITAN_HALT_DURATION_MIN": TITAN_HALT_DURATION_MIN,
        "TITAN_DAILY_LOSS_BUDGET": TITAN_DAILY_LOSS_BUDGET,
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
