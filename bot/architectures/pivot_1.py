"""
Architecture: pivot_1 (Half-Window BTC Autocorrelation + Cross-Venue Gate)

HYPOTHESIS
==========
NOT an oracle variant. No edge table. No (delta × time × momentum × side)
bucketing. The signal is one observation: at the half-window mark (T 60-180s),
which side of the strike is BTC currently on?

If BTC is meaningfully past the strike (|delta| ≥ 5bp) at the half-window
mark, BTC autocorrelation is strong enough that it stays on that side
through window close ~78% of the time.

The market prices this at ~60-67c (it treats the half-mark as ~60% confident).
The realized WR is ~78%. The 11-18pp gap is the alpha.

V2 ADDITION — CROSS-VENUE LEVEL GATE
====================================
The original signal is Binance-only. To defend against single-venue artifacts
(feed glitches, cascading liquidations on one exchange), we now require ≥1
other venue (Coinbase or Bybit) to also show BTC on the same side of the
strike. Any venue showing the OPPOSITE side is a hard veto.

This is the same trick that makes blitz / cross_pressure / impulse_confirmed
robust: present-tense cross-venue agreement instead of single-source statistics.
The architectures that survived the April 9 bloodbath all had this property.

VALIDATED ON
============
69 days of 1s BTC klines (19,968 windows): WR = 78.5% in the T 60-180,
|delta|≥5 cell. Empirical PM oracle data confirms: at T 90-180s, |delta|
6-9bp, n=47 trades, WR 74.5%, median entry 67c, +$39/trade.

WHY NON-ORACLE
==============
- No edge table lookup
- No table-vs-breakeven math
- No bucket boosting
- Strategy is a direct microstructure read on BTC autocorrelation
- Latency-tolerant: fires at the half-window mark, not in any race zone
"""

import time
from collections import deque
from datetime import datetime
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

# Reuse the exchange feed infrastructure from impulse_confirmed.
# Each session runs in its own process, so the feeds start fresh per session.
from bot.architectures.impulse_confirmed import (
    _start_feeds as _start_exchange_feeds,
    _exchange_prices,
    _lock as _exchange_lock,
)

# ═══ Triggers ═══
PIV_T_MIN = 60     # earliest fire (T_remaining = 60s)
PIV_T_MAX = 180    # latest fire (T_remaining = 180s)
PIV_DELTA_MIN = 5  # bps minimum from strike
PIV_DELTA_MAX = 25 # bps maximum (above this market is correctly priced)
PIV_MAX_ENTRY = 0.75  # don't pay above 75c

# ═══ Sizing ═══
PIV_BASE_DOLLARS = 200    # base trade size (everything scales from this)
PIV_DELTA_BOOST_BP = 9    # |delta| ≥ this gets size boost
PIV_DELTA_BOOST_MULT = 1.25

# ═══ Standard guards ═══
PIV_MAX_SPREAD = 0.04
PIV_MIN_BOOK_LEVELS = 2
PIV_COOLDOWN_SEC = 10
PIV_MAX_BOOK_AGE_MS = 500

# ═══ Cross-venue level gate (V2) ═══
PIV_REQUIRE_CONSENSUS = True       # require ≥N other venues to agree on side
PIV_MIN_CONFIRMS = 1               # how many independent venues must agree
PIV_MAX_VENUE_AGE_SEC = 3          # reject venue prices older than this
PIV_LEVEL_TOLERANCE_BPS = 0.5      # venue within this of strike → no opinion (abstain)
PIV_VETO_ON_DISAGREEMENT = True    # any venue showing opposite side blocks the trade

# ═══ Halt parameters (TIGHTENED 2026-04-10 — post-fix) ═══
# Pre-fix values were lookback=16, mult=-2.5 (-$500), cooldown=60.
# Post-fix sizing is correct, so we trip earlier and stay out longer.
PIV_HALT_LOOKBACK = 5
PIV_HALT_MULT_OF_BASE = -1.0  # threshold = base × this  → -$200 for $200 base
PIV_HALT_DURATION_MIN = 90
PIV_OVERNIGHT_SKIP_START = 0   # skip 12-5 ET (volatility regime drop)
PIV_OVERNIGHT_SKIP_END = 5

# ═══ Daily Loss Budget — hard ceiling, resets at UTC midnight ═══
PIV_DAILY_LOSS_BUDGET = -1500

COMBO_PARAMS = [
    {"name": "PIV_half", "btc_threshold_bp": 0, "lookback_s": 0},
]

_last_signal_time = [0.0]
_last_status = [0.0]
_recent_pnl = deque(maxlen=PIV_HALT_LOOKBACK)
_halt_until = [0.0]
_last_seen_total = [0]  # combo.total_trades counter (monotonic, survives the 50-trade cap)
_daily_pnl_log = deque(maxlen=500)

_stats = {
    "fires": 0, "skipped_t": 0, "skipped_delta": 0, "skipped_entry": 0,
    "skipped_overnight": 0, "skipped_halt": 0, "halts_triggered": 0,
    "skipped_no_consensus": 0, "skipped_venue_veto": 0,
}


def _latest_venue_price(name, max_age_sec):
    """Return (price, age_sec) for the latest tick from a venue, or (None, None)
    if no data or stale beyond max_age_sec."""
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

    For each independent venue (Coinbase, Bybit), check whether its latest
    price is on the same side of the strike as our bet direction. Venues
    within `tolerance_bps` of strike abstain (no opinion).

    Returns (confirms, rejects, tags).
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


def _ingest_settled(state):
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
    _start_exchange_feeds()  # idempotent — only starts once


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine

    _ingest_settled(state)

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    min_levels = getattr(engine, 'PIV_MIN_BOOK_LEVELS', PIV_MIN_BOOK_LEVELS)
    if len(state.book.bids) < min_levels or len(state.book.asks) < min_levels:
        return
    if state.book.spread > getattr(engine, 'PIV_MAX_SPREAD', PIV_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    t_min = getattr(engine, 'PIV_T_MIN', PIV_T_MIN)
    t_max = getattr(engine, 'PIV_T_MAX', PIV_T_MAX)
    if time_remaining < t_min or time_remaining > t_max:
        _stats["skipped_t"] += 1
        return

    if state.binance_price is None or not state.window_open or state.window_open <= 0:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'PIV_COOLDOWN_SEC', PIV_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'PIV_MAX_BOOK_AGE_MS', PIV_MAX_BOOK_AGE_MS):
        return

    # Halt cooldown
    if now < _halt_until[0]:
        return

    # Daily loss budget — hard ceiling, resets at UTC midnight
    daily_budget = getattr(engine, 'PIV_DAILY_LOSS_BUDGET', PIV_DAILY_LOSS_BUDGET)
    today = _today_pnl()
    if today < daily_budget:
        next_midnight = (int(now // 86400) + 1) * 86400
        _halt_until[0] = next_midnight
        _stats["skipped_halt"] += 1
        print("  {}[PIV] DAILY BUDGET HIT  today=${:+.0f} < ${} → halt until UTC midnight{}".format(
            engine.R, today, daily_budget, engine.RST))
        return

    # Halt trigger — check rolling cum loss
    base_dollars = getattr(engine, 'PIV_BASE_DOLLARS', PIV_BASE_DOLLARS)
    halt_lookback = getattr(engine, 'PIV_HALT_LOOKBACK', PIV_HALT_LOOKBACK)
    halt_mult = getattr(engine, 'PIV_HALT_MULT_OF_BASE', PIV_HALT_MULT_OF_BASE)
    halt_threshold = base_dollars * halt_mult  # negative number
    halt_dur_min = getattr(engine, 'PIV_HALT_DURATION_MIN', PIV_HALT_DURATION_MIN)

    if len(_recent_pnl) >= halt_lookback and sum(_recent_pnl) < halt_threshold:
        _halt_until[0] = now + halt_dur_min * 60
        cum = sum(_recent_pnl)
        _stats["halts_triggered"] += 1
        _stats["skipped_halt"] += 1
        print("  {}[PIV] HALT  cum({})=${:+.0f} < ${:.0f} → cooldown {}min{}".format(
            engine.R, halt_lookback, cum, halt_threshold, halt_dur_min, engine.RST))
        _recent_pnl.clear()
        return

    # Overnight skip (ET — fixed 2026-04-10)
    hour_et = datetime.now(_ET).hour if _ET else datetime.utcnow().hour
    skip_s = getattr(engine, 'PIV_OVERNIGHT_SKIP_START', PIV_OVERNIGHT_SKIP_START)
    skip_e = getattr(engine, 'PIV_OVERNIGHT_SKIP_END', PIV_OVERNIGHT_SKIP_END)
    if skip_s <= hour_et < skip_e:
        _stats["skipped_overnight"] += 1
        return

    # Compute current state
    btc_corrected = state.binance_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000
    abs_delta = abs(delta_bps)

    delta_min = getattr(engine, 'PIV_DELTA_MIN', PIV_DELTA_MIN)
    delta_max = getattr(engine, 'PIV_DELTA_MAX', PIV_DELTA_MAX)
    if abs_delta < delta_min or abs_delta > delta_max:
        _stats["skipped_delta"] += 1
        return

    if delta_bps > 0:
        direction = "YES"
        entry = state.book.best_ask
    else:
        direction = "NO"
        entry = state.book.best_bid

    max_entry = getattr(engine, 'PIV_MAX_ENTRY', PIV_MAX_ENTRY)
    if entry < 0.10 or entry > max_entry:
        _stats["skipped_entry"] += 1
        return

    # ═══ V2: Cross-venue level gate ═══
    require_cons = getattr(engine, 'PIV_REQUIRE_CONSENSUS', PIV_REQUIRE_CONSENSUS)
    confirm_count = 0
    reject_count = 0
    venue_tags = []
    if require_cons:
        max_age = getattr(engine, 'PIV_MAX_VENUE_AGE_SEC', PIV_MAX_VENUE_AGE_SEC)
        tol = getattr(engine, 'PIV_LEVEL_TOLERANCE_BPS', PIV_LEVEL_TOLERANCE_BPS)
        confirm_count, reject_count, venue_tags = _check_level_consensus(
            state.window_open, direction, max_age, tol)
        veto = getattr(engine, 'PIV_VETO_ON_DISAGREEMENT', PIV_VETO_ON_DISAGREEMENT)
        min_confirms = getattr(engine, 'PIV_MIN_CONFIRMS', PIV_MIN_CONFIRMS)
        if veto and reject_count > 0:
            _stats["skipped_venue_veto"] += 1
            return
        if confirm_count < min_confirms:
            _stats["skipped_no_consensus"] += 1
            return

    combo = state.combos[0]
    if combo.has_position_in_window(state.window_start):
        return

    # Sizing
    boost_bp = getattr(engine, 'PIV_DELTA_BOOST_BP', PIV_DELTA_BOOST_BP)
    boost_mult = getattr(engine, 'PIV_DELTA_BOOST_MULT', PIV_DELTA_BOOST_MULT)
    if abs_delta >= boost_bp:
        dollars = int(base_dollars * boost_mult)
    else:
        dollars = base_dollars

    # Vol-adjusted sizing (steal from oracle pattern)
    from bot.shared.volatility import vol_tracker as _vt
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    # Status (every 12s)
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        cum_recent = sum(_recent_pnl) if _recent_pnl else 0.0
        ven = ",".join(venue_tags) if venue_tags else "n/a"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | venues={} | cum{}=${:+.0f} | (f{} v{} h{})".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, direction, entry * 100, ven,
            len(_recent_pnl), cum_recent,
            _stats["fires"], _stats["skipped_venue_veto"], _stats["halts_triggered"]))
        _last_status[0] = now

    venue_str = ",".join(venue_tags) if venue_tags else "n/a"
    print("  {}[PIV] FIRE {} delta={:+.1f}bp @{:.0f}c ${} venues={} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, delta_bps, entry * 100, dollars, venue_str, time_remaining, engine.RST))

    _stats["fires"] += 1
    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs_delta, time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "pivot_1",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "PIV_T_MIN": PIV_T_MIN,
        "PIV_T_MAX": PIV_T_MAX,
        "PIV_DELTA_MIN": PIV_DELTA_MIN,
        "PIV_DELTA_MAX": PIV_DELTA_MAX,
        "PIV_MAX_ENTRY": PIV_MAX_ENTRY,
        "PIV_BASE_DOLLARS": PIV_BASE_DOLLARS,
        "PIV_DELTA_BOOST_BP": PIV_DELTA_BOOST_BP,
        "PIV_DELTA_BOOST_MULT": PIV_DELTA_BOOST_MULT,
        "PIV_MAX_SPREAD": PIV_MAX_SPREAD,
        "PIV_MIN_BOOK_LEVELS": PIV_MIN_BOOK_LEVELS,
        "PIV_COOLDOWN_SEC": PIV_COOLDOWN_SEC,
        "PIV_MAX_BOOK_AGE_MS": PIV_MAX_BOOK_AGE_MS,
        "PIV_HALT_LOOKBACK": PIV_HALT_LOOKBACK,
        "PIV_HALT_MULT_OF_BASE": PIV_HALT_MULT_OF_BASE,
        "PIV_HALT_DURATION_MIN": PIV_HALT_DURATION_MIN,
        "PIV_DAILY_LOSS_BUDGET": PIV_DAILY_LOSS_BUDGET,
        "PIV_OVERNIGHT_SKIP_START": PIV_OVERNIGHT_SKIP_START,
        "PIV_OVERNIGHT_SKIP_END": PIV_OVERNIGHT_SKIP_END,
        "PIV_REQUIRE_CONSENSUS": PIV_REQUIRE_CONSENSUS,
        "PIV_MIN_CONFIRMS": PIV_MIN_CONFIRMS,
        "PIV_MAX_VENUE_AGE_SEC": PIV_MAX_VENUE_AGE_SEC,
        "PIV_LEVEL_TOLERANCE_BPS": PIV_LEVEL_TOLERANCE_BPS,
        "PIV_VETO_ON_DISAGREEMENT": PIV_VETO_ON_DISAGREEMENT,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
