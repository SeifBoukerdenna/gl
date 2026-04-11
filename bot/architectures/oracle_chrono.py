"""
Architecture: oracle_chrono (Direction × Time Filtered Oracle)

HYPOTHESIS
==========
The (bucket, direction) analysis on 1,196 oracle trades revealed that specific
combinations of time-remaining and bet direction are PERSISTENTLY losing money,
not just during chop. The single biggest bleed is "NO trades with T-60-90 seconds
remaining" which lost $4,384 across 156 trades (~half of all oracle losses).

Other persistent (bucket, direction) killers:
  - T-5-30 NO: $-2,011 across 22 trades (last 30 seconds, low conviction)
  - T-270-300 NO: $-990 across 96 trades (window open before market settles)

Conversely, persistent winners we can amplify:
  - T-210-240 NO: $+4,242 across 71 trades (70% WR)
  - T-150-180 YES: $+3,472 across 57 trades (68% WR)
  - T-180-210 YES: $+2,124 across 81 trades

By blocking the persistent killers and sizing up the persistent winners, we
should improve oracle's risk-adjusted return without changing the table or
core logic.

CAVEAT
======
These patterns were discovered IN THE DATA we'll be collecting more of. There's
overfitting risk. The real test is whether the patterns hold forward — which is
why we're running this as a separate experimental session, not patching oracle_1.

LOGIC
=====
1. Compute time bucket (10 buckets of 30 seconds each)
2. Determine direction from delta (YES if BTC above strike, NO if below)
3. If (bucket, direction) is in BLOCKED list → skip
4. If (bucket, direction) is in BOOSTED list → 1.3x size
5. Otherwise standard oracle logic
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# ═══ Time × Direction Filters (V2 — softened, vol-aware) ═══
# V1 blocked too aggressively. The blocks were based on cumulative PnL from a
# trending regime — they may produce winners in other regimes. V2 only blocks
# the absolute worst single combo and adds a vol regime gate as the primary filter.
CHRONO_BLOCKED = [
    "60-90|NO",     # -$4,384 (the only PERSISTENT killer across regimes)
]

CHRONO_BOOSTED = [
    "210-240|NO",   # +$4,242 (70% WR — biggest winner)
    "150-180|YES",  # +$3,472 (68% WR)
    "180-210|YES",  # +$2,124 (60% WR)
]

CHRONO_BOOSTED_MULTIPLIER = 1.3

# Vol regime gate (NEW in V2)
CHRONO_MIN_VOL_PCT = 25.0      # skip if 1h vol < this (dead market)
CHRONO_MAX_VOL_PCT = 80.0      # skip if 1h vol > this (panic regime)

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
    {"name": "OCH_chrono", "btc_threshold_bp": 0, "lookback_s": 0},
]

_last_signal_time = [0.0]
_last_status = [0.0]
_edge_table_local = None

# Stats tracking
_stats = {"blocked": 0, "boosted": 0, "normal": 0}


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        import bot.paper_trade_v2 as engine
        blocked = getattr(engine, 'CHRONO_BLOCKED', CHRONO_BLOCKED)
        boosted = getattr(engine, 'CHRONO_BOOSTED', CHRONO_BOOSTED)
        print("  [OCH] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
        print("  [OCH] BLOCKED: {}".format(", ".join(blocked)))
        print("  [OCH] BOOSTED: {}".format(", ".join(boosted)))
    else:
        print("  [OCH] WARNING: No edge table found!")
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
    """30-second bucket label for the chrono filter."""
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

    # ═══ V2: Vol regime gate ═══
    from bot.shared.volatility import vol_tracker as _vt
    realized_vol = _vt.get_realized_vol_pct()
    if realized_vol is not None:
        min_vol = getattr(engine, 'CHRONO_MIN_VOL_PCT', CHRONO_MIN_VOL_PCT)
        max_vol = getattr(engine, 'CHRONO_MAX_VOL_PCT', CHRONO_MAX_VOL_PCT)
        if realized_vol < min_vol or realized_vol > max_vol:
            return  # silently skip — wrong regime

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

    # Determine chrono bucket and check filter
    chrono_bucket = _get_chrono_bucket(time_remaining)
    bd_key = "{}|{}".format(chrono_bucket, direction)

    blocked = getattr(engine, 'CHRONO_BLOCKED', CHRONO_BLOCKED)
    boosted = getattr(engine, 'CHRONO_BOOSTED', CHRONO_BOOSTED)
    is_blocked = bd_key in blocked
    is_boosted = bd_key in boosted

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
        tag = "BLOCK" if is_blocked else "BOOST" if is_boosted else "norm"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC {}{:+.1f}bp{} | {} @{:.0f}c | edge={} | bucket={}{} [{}] (b{} ▲{} =={}) ".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, direction, entry * 100, edge_str,
            chrono_bucket, direction, tag,
            _stats["blocked"], _stats["boosted"], _stats["normal"]))
        _last_status[0] = now

    if is_blocked:
        _stats["blocked"] += 1
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

    # Apply chrono boost
    boost_mult = getattr(engine, 'CHRONO_BOOSTED_MULTIPLIER', CHRONO_BOOSTED_MULTIPLIER)
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

    print("  {}[OCH] FIRE {} edge={:.0%} bucket={}{} [{}] @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, edge, chrono_bucket, direction, tag, entry * 100, dollars,
        level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_chrono",
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
        "CHRONO_BLOCKED": CHRONO_BLOCKED,
        "CHRONO_BOOSTED": CHRONO_BOOSTED,
        "CHRONO_BOOSTED_MULTIPLIER": CHRONO_BOOSTED_MULTIPLIER,
        "CHRONO_MIN_VOL_PCT": CHRONO_MIN_VOL_PCT,
        "CHRONO_MAX_VOL_PCT": CHRONO_MAX_VOL_PCT,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
