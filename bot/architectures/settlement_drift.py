"""
Architecture: settlement_drift
Thesis: In the last 60 seconds of each 5-min window, PM's book mid converges toward its
settlement value in discrete jumps. When the mid crosses the 0.50 line (the ONLY thing that
determines settlement), the crossing tends to be sticky — it rarely reverses in the final
seconds. Additionally, when the previous window settled in one direction and BTC maintains
that delta into the new window, market makers are slow to reprice the new window's book.
This creates an early-window momentum carry signal. Two modes: (1) late-window crossover
(trade when mid crosses 0.50 with margin), (2) early-window carry (trade when prior outcome
aligns with current delta).
"""

import time
from collections import deque

SD_MAX_SPREAD = 0.05          # wider tolerance for late-window
SD_MIN_BOOK_LEVELS = 2
SD_CROSS_MIN_MARGIN = 0.02   # mid must be at least 2c past 0.50
SD_CROSS_CONFIRM_TICKS = 2   # must hold for 2s
SD_MIN_ENTRY_PRICE = 0.50
SD_MAX_ENTRY_PRICE = 0.90

# Mid history for crossover detection
_mid_history = deque(maxlen=60)


COMBO_PARAMS = [
    # Late-window crossover: trade when mid crosses 0.50 in final seconds
    {"name": "SD_cross_20s",     "btc_threshold_bp": 0, "lookback_s": 0, "mode": "cross", "max_time": 20, "margin": 0.03, "confirm": 2},
    {"name": "SD_cross_45s",     "btc_threshold_bp": 0, "lookback_s": 0, "mode": "cross", "max_time": 45, "margin": 0.02, "confirm": 2},
    {"name": "SD_cross_60s",     "btc_threshold_bp": 0, "lookback_s": 0, "mode": "cross", "max_time": 60, "margin": 0.02, "confirm": 3},
    {"name": "SD_cross_60s_lax", "btc_threshold_bp": 0, "lookback_s": 0, "mode": "cross", "max_time": 60, "margin": 0.01, "confirm": 2},
    # Early-window carry: trade when prior window aligns with current delta
    {"name": "SD_carry_3bp",     "btc_threshold_bp": 0, "lookback_s": 0, "mode": "carry", "min_delta": 3, "max_time": 270, "min_time": 200},
    {"name": "SD_carry_5bp",     "btc_threshold_bp": 0, "lookback_s": 0, "mode": "carry", "min_delta": 5, "max_time": 270, "min_time": 200},
]

_combo_config = {p["name"]: p for p in COMBO_PARAMS}
_last_status = [0.0]
_prior_outcome = [None]  # outcome of last settled window


def on_tick(state, price, ts):
    """Track mid price history every tick."""
    if state.book.mid > 0:
        _mid_history.append((ts, state.book.mid))


def check_signals(state, now_s):
    """Settlement drift signal detection."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < SD_MIN_BOOK_LEVELS or len(state.book.asks) < SD_MIN_BOOK_LEVELS:
        return
    if state.book.spread > SD_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    mid = state.book.mid

    # Status line
    now_t = time.time()
    if now_t - _last_status[0] >= 15:
        side = "UP" if mid > 0.50 else "DN"
        margin = abs(mid - 0.50) * 100
        prior = _prior_outcome[0] or "?"
        btc_corrected = (state.binance_price or 0) - state.offset
        delta = 0
        if state.window_open and state.window_open > 0:
            delta = (btc_corrected - state.window_open) / state.window_open * 10000
        print("  {}{}{} {}T-{:>3.0f}s{} | mid={:.0f}c ({} +{:.1f}c) | delta {:+.1f}bp | prior={}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            mid * 100, side, margin, delta, prior))
        _last_status[0] = now_t

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})

        if cfg.get("mode") == "cross":
            _check_cross(state, combo, cfg, time_remaining, mid, engine)
        elif cfg.get("mode") == "carry":
            _check_carry(state, combo, cfg, time_remaining, engine)


def _check_cross(state, combo, cfg, time_remaining, mid, engine):
    """Late-window crossover: trade when mid crosses 0.50 with margin."""
    max_time = cfg.get("max_time", 60)
    margin = cfg.get("margin", 0.02)
    confirm = cfg.get("confirm", 2)

    if time_remaining > max_time or time_remaining < 5:
        return

    # Check if mid is on one side of 0.50 with sufficient margin
    if mid > 0.50 + margin:
        # Mid favors UP — check that it recently crossed (wasn't always there)
        if _was_below_recently(0.50, confirm + 2):
            # Confirm: mid stayed above 0.50 for `confirm` ticks
            if _held_above(0.50, confirm):
                yes_cost = state.book.best_ask
                if SD_MIN_ENTRY_PRICE <= yes_cost <= SD_MAX_ENTRY_PRICE:
                    signal_strength = (mid - 0.50) * 100
                    engine.execute_paper_trade(combo, "YES", signal_strength, time_remaining, yes_cost)
                    return

    elif mid < 0.50 - margin:
        # Mid favors DOWN
        if _was_above_recently(0.50, confirm + 2):
            if _held_below(0.50, confirm):
                no_cost = 1.0 - state.book.best_bid
                if SD_MIN_ENTRY_PRICE <= no_cost <= SD_MAX_ENTRY_PRICE:
                    signal_strength = (0.50 - mid) * 100
                    engine.execute_paper_trade(combo, "NO", signal_strength, time_remaining, state.book.best_bid)


def _check_carry(state, combo, cfg, time_remaining, engine):
    """Early-window momentum carry: trade when prior outcome aligns with current delta."""
    min_time = cfg.get("min_time", 200)
    max_time = cfg.get("max_time", 270)
    min_delta = cfg.get("min_delta", 3)

    if time_remaining < min_time or time_remaining > max_time:
        return

    prior = _prior_outcome[0]
    if prior is None:
        return

    if not state.window_open or state.window_open <= 0:
        return
    btc_corrected = (state.binance_price or 0) - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # Prior was UP and current delta is positive (BTC still above PTB)
    if prior == "Up" and delta_bps >= min_delta:
        yes_cost = state.book.best_ask
        if SD_MIN_ENTRY_PRICE <= yes_cost <= SD_MAX_ENTRY_PRICE:
            engine.execute_paper_trade(combo, "YES", delta_bps, time_remaining, yes_cost)

    # Prior was DOWN and current delta is negative
    elif prior == "Down" and delta_bps <= -min_delta:
        no_cost = 1.0 - state.book.best_bid
        if SD_MIN_ENTRY_PRICE <= no_cost <= SD_MAX_ENTRY_PRICE:
            engine.execute_paper_trade(combo, "NO", abs(delta_bps), time_remaining, state.book.best_bid)


def _was_below_recently(level, lookback):
    """Check if mid was below `level` at any point in the last `lookback` entries."""
    recent = list(_mid_history)[-lookback:] if len(_mid_history) >= lookback else list(_mid_history)
    return any(m < level for _, m in recent)


def _was_above_recently(level, lookback):
    recent = list(_mid_history)[-lookback:] if len(_mid_history) >= lookback else list(_mid_history)
    return any(m > level for _, m in recent)


def _held_above(level, ticks):
    """Check if mid has been above `level` for the last `ticks` entries."""
    if len(_mid_history) < ticks:
        return False
    recent = list(_mid_history)[-ticks:]
    return all(m > level for _, m in recent)


def _held_below(level, ticks):
    if len(_mid_history) < ticks:
        return False
    recent = list(_mid_history)[-ticks:]
    return all(m < level for _, m in recent)


def on_window_start(state):
    """Clear mid history, record prior outcome."""
    _mid_history.clear()
    _last_status[0] = 0.0
    if state.completed_windows:
        _prior_outcome[0] = state.completed_windows[-1].get("outcome")


ARCH_SPEC = {
    "name": "settlement_drift",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "SD_MAX_SPREAD": SD_MAX_SPREAD,
        "SD_MIN_BOOK_LEVELS": SD_MIN_BOOK_LEVELS,
        "SD_CROSS_MIN_MARGIN": SD_CROSS_MIN_MARGIN,
        "SD_CROSS_CONFIRM_TICKS": SD_CROSS_CONFIRM_TICKS,
        "SD_MIN_ENTRY_PRICE": SD_MIN_ENTRY_PRICE,
        "SD_MAX_ENTRY_PRICE": SD_MAX_ENTRY_PRICE,
        "MIN_ENTRY_PRICE": 0.01,
        "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0,
        "DEAD_ZONE_END": 0,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
