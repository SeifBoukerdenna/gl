"""
Architecture: oracle_alpha (Kitchen Sink Printer)

THE FULL THESIS
===============
Combine every single learning from 36+ hours of paper trading into one architecture:

  1. Edge table (V2)                            — historical setup probability
  2. Dynamic vol-scaled IC confirmation         — Coinbase + Bybit must move N×sigma
  3. Vol regime gate                            — block trading in dead/panic regimes
  4. Direction-aware rolling WR                 — skip the losing direction
  5. 6h drawdown self-halt                      — circuit breaker
  6. 45c entry cap                              — favorable R:R only (oracle_safe insight)
  7. Direction × time bucket blocks             — chrono filter
  8. Edge ≥ 10pp                                — stricter than oracle_1's 8pp

The hypothesis: with all these layers, the bot fires LESS but every fire has
multiple confirmations. Lower volume, much higher per-trade quality.

This is the "if any of our experiments matter, this should print" architecture.
If it doesn't outperform oracle_1, we know the layered defense theory is wrong.

LIVE-TRADING READINESS
======================
This is the architecture I'd actually deploy live with real money. Every defense
is in place. The only thing it's missing is the live execution layer (real PM
order placement) which is independent of the strategy logic.
"""

import time
import json as _json
from collections import deque
from pathlib import Path

# Reuse exchange feeds from impulse_confirmed
from bot.architectures.impulse_confirmed import (
    _start_feeds as _start_exchange_feeds,
    _get_exchange_momentum,
)

# ═══ Edge table parameters ═══
OR_PHASE1_END = 90
OR_PHASE1_MIN_EDGE = 0.10        # tighter: 10pp vs oracle_1's 8pp
OR_PHASE2_MIN_EDGE = 0.07        # tighter: 7pp vs oracle_1's 5pp
OR_MIN_ENTRY = 0.10
OR_MAX_ENTRY = 0.45              # oracle_safe-style cap — only cheap entries
OR_MAX_SPREAD = 0.04
OR_MIN_BOOK_LEVELS = 2
OR_COOLDOWN_SEC = 15
OR_BASE_DOLLARS = 200

# ═══ Multi-exchange confirmation (DYNAMIC) ═══
ALPHA_MIN_CONFIRMS = 1            # at least 1 of CB/BY agrees
ALPHA_CONFIRM_WINDOW_SEC = 10
ALPHA_USE_DYNAMIC = True
ALPHA_SIGMA_MULT = 1.5
ALPHA_MIN_BPS_FLOOR = 1.5         # static safety floor

# ═══ Vol regime gate ═══
ALPHA_MIN_VOL_PCT = 25.0
ALPHA_MAX_VOL_PCT = 80.0

# ═══ Direction-aware rolling WR ═══
ALPHA_DIR_WINDOW = 15             # last N trades per direction
ALPHA_DIR_MIN_WR = 0.40           # if rolling WR for that direction < this, skip

# ═══ 6h drawdown halt ═══
ALPHA_DD_HALT_THRESHOLD = -800.0
ALPHA_DD_HALT_DURATION_MIN = 90
ALPHA_DD_LOOKBACK_SEC = 21600

# ═══ Time × direction filter (from chrono) ═══
ALPHA_BLOCKED_BUCKETS = [
    "60-90|NO",
    "5-30|NO",
    "270-300|NO",
]

COMBO_PARAMS = [
    {"name": "ALPHA", "btc_threshold_bp": 0, "lookback_s": 0},
]

# ═══ State ═══
_last_signal_time = [0.0]
_last_status = [0.0]
_last_hb = [0.0]
_edge_table_local = None
_yes_results = deque(maxlen=ALPHA_DIR_WINDOW)
_no_results = deque(maxlen=ALPHA_DIR_WINDOW)
_recent_pnl = deque()  # (ts, pnl) for 6h rolling DD
_halt_until = [0.0]
_last_seen_trade_count = [0]

# Stats tracking
_stats = {
    "skip_vol_low": 0,
    "skip_vol_high": 0,
    "skip_dir_wr": 0,
    "skip_bucket_block": 0,
    "skip_no_consensus": 0,
    "skip_no_edge": 0,
    "skip_drawdown_halt": 0,
    "fires": 0,
}


def _load_table():
    global _edge_table_local
    if _edge_table_local is not None:
        return
    table_path = Path("data/edge_table.json")
    if table_path.exists():
        with open(table_path) as f:
            _edge_table_local = _json.load(f)
        print("  [ALPHA] Edge table loaded: V{}".format(_edge_table_local.get("version", 1)))
        print("  [ALPHA] Defenses: vol gate {:.0f}-{:.0f}%, dir WR floor {:.0%}, DD halt -${:.0f}, max entry {:.0%}".format(
            ALPHA_MIN_VOL_PCT, ALPHA_MAX_VOL_PCT, ALPHA_DIR_MIN_WR,
            abs(ALPHA_DD_HALT_THRESHOLD), OR_MAX_ENTRY))
    else:
        print("  [ALPHA] WARNING: No edge table found!")
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


def _check_dynamic_consensus(direction, window_sec):
    """Check exchange consensus with sigma-based dynamic threshold."""
    from bot.shared.volatility import vol_tracker as _vt

    sigma_bps = _vt.get_sigma_bps_over(window_sec)
    if sigma_bps is None:
        min_bps = ALPHA_MIN_BPS_FLOOR
    else:
        min_bps = max(ALPHA_MIN_BPS_FLOOR, sigma_bps * ALPHA_SIGMA_MULT)

    confirms = 0
    sessions = []
    mom_cb = _get_exchange_momentum("coinbase", window_sec)
    if mom_cb is not None:
        if direction == "YES" and mom_cb >= min_bps:
            confirms += 1; sessions.append("CB{:+.1f}".format(mom_cb))
        elif direction == "NO" and mom_cb <= -min_bps:
            confirms += 1; sessions.append("CB{:+.1f}".format(mom_cb))

    mom_by = _get_exchange_momentum("bybit", window_sec)
    if mom_by is not None:
        if direction == "YES" and mom_by >= min_bps:
            confirms += 1; sessions.append("BY{:+.1f}".format(mom_by))
        elif direction == "NO" and mom_by <= -min_bps:
            confirms += 1; sessions.append("BY{:+.1f}".format(mom_by))

    return confirms, sessions, min_bps


def _update_recent_pnl(state):
    """Track our own recent trade outcomes for 6h drawdown check."""
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
        result = t.get("result", "")
        direction = t.get("direction", "")
        _recent_pnl.append((ts, pnl))
        # Update direction-specific WR tracking
        if result == "WIN":
            if direction == "YES": _yes_results.append(1)
            elif direction == "NO": _no_results.append(1)
        elif result == "LOSS":
            if direction == "YES": _yes_results.append(0)
            elif direction == "NO": _no_results.append(0)
    _last_seen_trade_count[0] = len(all_trades)
    cutoff = time.time() - ALPHA_DD_LOOKBACK_SEC
    while _recent_pnl and _recent_pnl[0][0] < cutoff:
        _recent_pnl.popleft()


def _get_dir_wr(direction):
    """Rolling WR for a specific direction. None if not enough data."""
    deck = _yes_results if direction == "YES" else _no_results
    if len(deck) < 5:
        return None
    return sum(deck) / len(deck)


def _get_rolling_6h_pnl():
    return sum(p for _, p in _recent_pnl)


def on_tick(state, price, ts):
    _load_table()
    _start_exchange_feeds()


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

    if now - _last_signal_time[0] < getattr(engine, 'OR_COOLDOWN_SEC', OR_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    # Unconditional 12s heartbeat — shows which gate is accumulating skips,
    # independent of which branch rejected this tick
    if now - _last_hb[0] >= 12:
        from bot.shared.volatility import vol_tracker as _vt_hb
        rv = _vt_hb.get_realized_vol_pct()
        halt_left = max(0, _halt_until[0] - now)
        print("  {}[ALPHA hb] T-{:.0f}s vol={} halt_left={:.0f}s | (vL{} vH{} bk{} dr{} ne{} cf{} dd{} fr{}){}".format(
            engine.DIM,
            time_remaining,
            "{:.0f}%".format(rv) if rv is not None else "n/a",
            halt_left,
            _stats["skip_vol_low"], _stats["skip_vol_high"],
            _stats["skip_bucket_block"], _stats["skip_dir_wr"],
            _stats["skip_no_edge"], _stats["skip_no_consensus"],
            _stats["skip_drawdown_halt"], _stats["fires"],
            engine.RST))
        _last_hb[0] = now

    # Update PnL tracking and check 6h DD
    _update_recent_pnl(state)
    rolling_pnl = _get_rolling_6h_pnl()
    halt_threshold = getattr(engine, 'ALPHA_DD_HALT_THRESHOLD', ALPHA_DD_HALT_THRESHOLD)
    halt_duration = getattr(engine, 'ALPHA_DD_HALT_DURATION_MIN', ALPHA_DD_HALT_DURATION_MIN)
    if rolling_pnl < halt_threshold and len(_recent_pnl) >= 5:
        _halt_until[0] = now + halt_duration * 60
        _stats["skip_drawdown_halt"] += 1
        print("  {}[ALPHA] DD HALT — 6h PnL ${:+.0f} < ${:.0f}, pausing {} min{}".format(
            engine.R, rolling_pnl, halt_threshold, halt_duration, engine.RST))
        return

    # ═══ Vol regime gate ═══
    from bot.shared.volatility import vol_tracker as _vt
    realized_vol = _vt.get_realized_vol_pct()
    if realized_vol is not None:
        min_vol = getattr(engine, 'ALPHA_MIN_VOL_PCT', ALPHA_MIN_VOL_PCT)
        max_vol = getattr(engine, 'ALPHA_MAX_VOL_PCT', ALPHA_MAX_VOL_PCT)
        if realized_vol < min_vol:
            _stats["skip_vol_low"] += 1
            return
        if realized_vol > max_vol:
            _stats["skip_vol_high"] += 1
            return

    btc_corrected = state.binance_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    if delta_bps > 0:
        direction = "YES"
        entry = state.book.best_ask
    else:
        direction = "NO"
        entry = state.book.best_bid

    # ═══ Entry cap (oracle_safe insight) ═══
    min_entry = getattr(engine, 'OR_MIN_ENTRY', OR_MIN_ENTRY)
    max_entry = getattr(engine, 'OR_MAX_ENTRY', OR_MAX_ENTRY)
    if entry < min_entry or entry > max_entry:
        return

    # ═══ Direction-aware rolling WR ═══
    dir_wr = _get_dir_wr(direction)
    dir_min = getattr(engine, 'ALPHA_DIR_MIN_WR', ALPHA_DIR_MIN_WR)
    if dir_wr is not None and dir_wr < dir_min:
        _stats["skip_dir_wr"] += 1
        return

    # ═══ Edge table ═══
    edge, wr, level = _compute_edge(state, direction, entry, time_remaining)
    phase1_end = getattr(engine, 'OR_PHASE1_END', OR_PHASE1_END)
    if time_remaining > phase1_end:
        min_edge = getattr(engine, 'OR_PHASE1_MIN_EDGE', OR_PHASE1_MIN_EDGE)
    else:
        min_edge = getattr(engine, 'OR_PHASE2_MIN_EDGE', OR_PHASE2_MIN_EDGE)

    if edge is None or edge < min_edge:
        if edge is not None:
            _stats["skip_no_edge"] += 1
        return

    # ═══ Time × direction bucket filter ═══
    chrono_bucket = _get_chrono_bucket(time_remaining)
    bd_key = "{}|{}".format(chrono_bucket, direction)
    blocked = getattr(engine, 'ALPHA_BLOCKED_BUCKETS', ALPHA_BLOCKED_BUCKETS)
    if bd_key in blocked:
        _stats["skip_bucket_block"] += 1
        return

    # ═══ Multi-exchange consensus with dynamic threshold ═══
    confirm_window = getattr(engine, 'ALPHA_CONFIRM_WINDOW_SEC', ALPHA_CONFIRM_WINDOW_SEC)
    confirm_count, confirm_sessions, dynamic_min = _check_dynamic_consensus(direction, confirm_window)
    min_confirms = getattr(engine, 'ALPHA_MIN_CONFIRMS', ALPHA_MIN_CONFIRMS)
    if confirm_count < min_confirms:
        _stats["skip_no_consensus"] += 1
        # Status print every 12s
        if now - _last_status[0] >= 12:
            yes_wr_str = "{:.0%}".format(_get_dir_wr("YES")) if _get_dir_wr("YES") is not None else "—"
            no_wr_str = "{:.0%}".format(_get_dir_wr("NO")) if _get_dir_wr("NO") is not None else "—"
            print("  {}{}{} {}T-{:>3.0f}s{} | {} {:+.1f}bp @{:.0f}c | edge={:+.0%} | vol={:.0f}% | dyn_min={:.1f}bp | dirWR Y{}/N{} | 6h:${:+.0f} | (vL{} vH{} bk{} dr{} ne{} cf{} fr{})".format(
                engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
                direction, delta_bps, entry*100, edge,
                realized_vol if realized_vol else 0, dynamic_min,
                yes_wr_str, no_wr_str, rolling_pnl,
                _stats["skip_vol_low"], _stats["skip_vol_high"],
                _stats["skip_bucket_block"], _stats["skip_dir_wr"],
                _stats["skip_no_edge"], _stats["skip_no_consensus"], _stats["fires"]))
            _last_status[0] = now
        return

    # ═══ All gates passed — FIRE ═══
    combo = state.combos[0]
    if combo.has_position_in_window(state.window_start):
        return

    base_dollars = getattr(engine, 'OR_BASE_DOLLARS', OR_BASE_DOLLARS)
    # Edge-scaled
    if edge >= 0.13:
        dollars = int(base_dollars * 1.3)
    elif edge >= 0.10:
        dollars = base_dollars
    else:
        dollars = int(base_dollars * 0.8)
    # Bonus if both exchanges confirm
    if confirm_count >= 2:
        dollars = int(dollars * 1.2)
    # Vol-adjusted (existing oracle behavior)
    _sigma = _vt.get_sigma()
    if _sigma and _sigma > 0:
        vol_mult = min(1.0, 3.0 / _sigma)
        dollars = max(25, int(dollars * vol_mult))

    _stats["fires"] += 1

    print("  {}[ALPHA] FIRE {} edge={:+.0%} vol={:.0f}% confirms={}({}) @{:.0f}c ${} {} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, edge, realized_vol if realized_vol else 0,
        confirm_count, "+".join(confirm_sessions),
        entry * 100, dollars, level, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs(delta_bps), time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "oracle_alpha",
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
        "ALPHA_MIN_CONFIRMS": ALPHA_MIN_CONFIRMS,
        "ALPHA_CONFIRM_WINDOW_SEC": ALPHA_CONFIRM_WINDOW_SEC,
        "ALPHA_USE_DYNAMIC": ALPHA_USE_DYNAMIC,
        "ALPHA_SIGMA_MULT": ALPHA_SIGMA_MULT,
        "ALPHA_MIN_BPS_FLOOR": ALPHA_MIN_BPS_FLOOR,
        "ALPHA_MIN_VOL_PCT": ALPHA_MIN_VOL_PCT,
        "ALPHA_MAX_VOL_PCT": ALPHA_MAX_VOL_PCT,
        "ALPHA_DIR_MIN_WR": ALPHA_DIR_MIN_WR,
        "ALPHA_DD_HALT_THRESHOLD": ALPHA_DD_HALT_THRESHOLD,
        "ALPHA_DD_HALT_DURATION_MIN": ALPHA_DD_HALT_DURATION_MIN,
        "ALPHA_BLOCKED_BUCKETS": ALPHA_BLOCKED_BUCKETS,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
