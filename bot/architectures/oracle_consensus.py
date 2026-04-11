"""
Architecture: oracle_consensus (Edge Table + Multi-Exchange LEVEL Confirmation)

HYPOTHESIS
==========
Single-exchange Binance prices can be artifacts (one large order, an internal
liquidation, a feed glitch). If we require independent venues to AGREE ON THE
LEVEL — Coinbase and Bybit also show BTC on the same side of the strike — we
filter out single-venue noise without filtering out real edge-table value.

NOTE: V1 of this file confirmed 5-second MOMENTUM direction, which was the
wrong filter (it forced the bot into momentum-continuation regimes and
anti-selected good NO trades). V2 confirms LEVEL — does Coinbase / Bybit also
show BTC > strike (for YES bets) or BTC < strike (for NO bets)?

LOGIC
=====
Fire only when ALL of:
  1. Edge table edge >= OR_PHASE1_MIN_EDGE
  2. At least IC_MIN_CONFIRM_EXCHANGES other exchanges show BTC LEVEL on the
     same side of the strike as the bet direction
  3. Standard book/spread/timing gates from oracle

With 1 confirmation: normal size
With 2 confirmations (both Coinbase AND Bybit): 1.3x size — high conviction

Imports the exchange feed module from impulse_confirmed to reuse the
websocket infrastructure (but ignores its momentum function).
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# Reuse the existing exchange feed infrastructure from impulse_confirmed.
# We import the underlying price deque and the feed starter, but NOT the
# momentum helper — V2 uses level-based confirmation instead.
from bot.architectures.impulse_confirmed import (
    _start_feeds as _start_exchange_feeds,
    _exchange_prices,
    _lock as _exchange_lock,
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

# Multi-exchange LEVEL confirmation parameters (V2)
IC_MIN_CONFIRM_EXCHANGES = 1   # at least N other venues must agree on side
IC_MAX_PRICE_AGE_SEC = 3       # reject venue prices older than this
IC_LEVEL_TOLERANCE_BPS = 0.5   # ignore venue if it's within this of strike (no opinion)
# Vol regime gate
IC_MIN_VOL_PCT = 25.0          # skip if 1h annualized vol < this
IC_MAX_VOL_PCT = 80.0          # skip if vol > this (panic regime)

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
    """Return (price, age_sec) for the latest tick from a venue, or (None, None)
    if no data or stale."""
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
    """LEVEL-BASED CONSENSUS (V2).

    For each independent venue (Coinbase, Bybit), check whether its latest
    price is on the same side of the strike as our bet direction. Venues
    within `tolerance_bps` of the strike are treated as "no opinion" and
    don't count as either confirmation or rejection.

    Returns (confirms, rejects, tags) — a venue can confirm, reject, or
    abstain. We also report rejections so the caller can choose to skip
    on disagreement (not just absence of confirmation).
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
            tags.append("{}~".format(label))  # too close to strike to call
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
    """Returns True if current 1h vol is within the acceptable trading range."""
    from bot.shared.volatility import vol_tracker as _vt
    vol = _vt.get_realized_vol_pct()
    if vol is None:
        return True  # no data, allow trading
    min_vol = getattr(engine, 'IC_MIN_VOL_PCT', IC_MIN_VOL_PCT)
    max_vol = getattr(engine, 'IC_MAX_VOL_PCT', IC_MAX_VOL_PCT)
    return min_vol <= vol <= max_vol


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

    # ═══ Vol regime gate ═══
    if not _vol_regime_ok(engine):
        return  # silently skip in dead/panic markets

    # ═══ V2: Multi-exchange LEVEL consensus ═══
    max_age = getattr(engine, 'IC_MAX_PRICE_AGE_SEC', IC_MAX_PRICE_AGE_SEC)
    tol = getattr(engine, 'IC_LEVEL_TOLERANCE_BPS', IC_LEVEL_TOLERANCE_BPS)
    confirm_count, reject_count, confirm_sessions = _check_level_consensus(
        state.window_open, direction, max_age, tol)

    # Status
    if now - _last_status[0] >= 12:
        dc = engine.G if delta_bps > 0 else engine.R
        edge_str = "{:+.0%}".format(edge) if edge is not None else "n/a"
        confirm_str = "{}({}/{})".format(",".join(confirm_sessions), confirm_count, reject_count) if confirm_sessions else "no_data"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | edge={} | venues={}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, direction, entry * 100, edge_str, confirm_str))
        _last_status[0] = now

    if edge is None or edge < min_edge:
        return

    # ═══ V2 GATE: at least N venues must agree on LEVEL, AND no venue rejects ═══
    min_confirms = getattr(engine, 'IC_MIN_CONFIRM_EXCHANGES', IC_MIN_CONFIRM_EXCHANGES)
    if confirm_count < min_confirms:
        return
    if reject_count > 0:
        # Any venue showing BTC on the OPPOSITE side is a hard veto.
        # If venues disagree about which side BTC is on, the move is suspect.
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
        "IC_MAX_PRICE_AGE_SEC": IC_MAX_PRICE_AGE_SEC,
        "IC_LEVEL_TOLERANCE_BPS": IC_LEVEL_TOLERANCE_BPS,
        "IC_MIN_VOL_PCT": IC_MIN_VOL_PCT,
        "IC_MAX_VOL_PCT": IC_MAX_VOL_PCT,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
