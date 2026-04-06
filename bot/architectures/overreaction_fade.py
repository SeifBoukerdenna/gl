"""
Architecture: overreaction_fade
Thesis: In the first 60 seconds of a 5-min window, BTC moves cause PM's book to overreact.
A $30-40 BTC move shifts the true terminal probability by only a few percent (4+ minutes of
random walk remain), but PM's book moves 15-20 cents. Short-timeframe BTC returns have near-
zero autocorrelation — minute 1 doesn't predict minutes 2-5. Buy the hammered token.
This is ANTI-CORRELATED with impulse_lag: they buy the dominant side, we buy the hammered side.
"""

import time

COMBO_PARAMS = [
    {"name": "OF_30usd_60s",     "btc_threshold_bp": 0, "lookback_s": 0, "min_move_usd": 30, "max_time_since_open": 60, "min_overreaction": 2.0, "vol_filter": False},
    {"name": "OF_50usd_60s",     "btc_threshold_bp": 0, "lookback_s": 0, "min_move_usd": 50, "max_time_since_open": 60, "min_overreaction": 2.0, "vol_filter": False},
    {"name": "OF_30usd_45s",     "btc_threshold_bp": 0, "lookback_s": 0, "min_move_usd": 30, "max_time_since_open": 45, "min_overreaction": 2.0, "vol_filter": False},
    {"name": "OF_30usd_60s_3x",  "btc_threshold_bp": 0, "lookback_s": 0, "min_move_usd": 30, "max_time_since_open": 60, "min_overreaction": 3.0, "vol_filter": False},
    {"name": "OF_30usd_60s_vol", "btc_threshold_bp": 0, "lookback_s": 0, "min_move_usd": 30, "max_time_since_open": 60, "min_overreaction": 2.0, "vol_filter": True},
    # Aggressive combos — lower thresholds to actually trade
    {"name": "OF_15usd_60s",     "btc_threshold_bp": 0, "lookback_s": 0, "min_move_usd": 15, "max_time_since_open": 60, "min_overreaction": 1.3, "vol_filter": False},
    {"name": "OF_10usd_60s",     "btc_threshold_bp": 0, "lookback_s": 0, "min_move_usd": 10, "max_time_since_open": 60, "min_overreaction": 1.5, "vol_filter": False},
    {"name": "OF_15usd_45s_1.2x","btc_threshold_bp": 0, "lookback_s": 0, "min_move_usd": 15, "max_time_since_open": 45, "min_overreaction": 1.2, "vol_filter": False},
]

_combo_config = {p["name"]: p for p in COMBO_PARAMS}
_last_status = [0.0]

OF_MAX_SPREAD = 0.04
OF_MIN_BOOK_LEVELS = 2

# Per-window state
_window_open_btc = [None]    # BTC price at window open (Binance)
_window_open_yes = [None]    # PM YES ask at window open
_window_open_no = [None]     # PM NO cost at window open
_window_start_ts = [0]


def on_tick(state, price, ts):
    from bot.shared.volatility import vol_tracker
    vol_tracker.record_tick(price, ts)


def on_window_start(state):
    """Snapshot the opening state."""
    _window_open_btc[0] = state.binance_price
    _window_open_yes[0] = state.book.best_ask if state.book.best_ask > 0 else 0.50
    _window_open_no[0] = (1.0 - state.book.best_bid) if state.book.best_bid > 0 else 0.50
    _window_start_ts[0] = time.time()
    _last_status[0] = 0.0


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine
    from bot.shared.volatility import vol_tracker

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < OF_MIN_BOOK_LEVELS or len(state.book.asks) < OF_MIN_BOOK_LEVELS:
        return
    if state.book.spread > OF_MAX_SPREAD:
        return

    # Only active in the first 60 seconds of the window
    time_remaining = state.window_end - time.time()
    time_since_open = 300 - time_remaining
    if time_since_open > 65 or time_since_open < 5:
        return

    if _window_open_btc[0] is None or not state.window_open or state.window_open <= 0:
        return

    btc_now = state.binance_price or 0
    btc_open = _window_open_btc[0]
    btc_move_usd = btc_now - btc_open
    abs_move = abs(btc_move_usd)
    strike = state.window_open

    # How much did PM book shift?
    yes_now = state.book.best_ask
    no_now = 1.0 - state.book.best_bid
    yes_open = _window_open_yes[0] or 0.50
    no_open = _window_open_no[0] or 0.50

    if btc_move_usd > 0:
        # BTC went UP → YES got more expensive, NO got hammered
        market_shift = (yes_now - yes_open)  # how much YES moved up
        hammered_dir = "NO"
        hammered_now = no_now
    else:
        # BTC went DOWN → NO got more expensive, YES got hammered
        market_shift = (no_now - no_open)  # how much NO moved up
        hammered_dir = "YES"
        hammered_now = yes_now

    market_shift = abs(market_shift)

    # Model's fair shift: how much SHOULD probability change given the BTC move?
    btc_corrected = btc_now - state.offset
    fair_now = vol_tracker.get_fair_probability(btc_corrected, strike, time_remaining)
    fair_open = 0.50  # at window open, fair is ~50% by definition
    model_shift = abs(fair_now - fair_open)

    # Overreaction ratio
    overreaction = market_shift / model_shift if model_shift > 0.01 else 0

    # Volume ratio for volume filter
    vol_ratio = vol_tracker.get_binance_volume_ratio(window_seconds=60)

    sigma = vol_tracker.get_sigma()

    # Status
    now_t = time.time()
    if now_t - _last_status[0] >= 5:
        print("  {}{}{} {}T+{:.0f}s{} | BTC {:+.0f}$ | mkt_shift={:.0f}c model={:.0f}c ratio={:.1f}x | hammer={} vol_r={:.1f}x".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_since_open, engine.RST,
            btc_move_usd, market_shift * 100, model_shift * 100, overreaction,
            hammered_dir, vol_ratio))
        _last_status[0] = now_t

    if abs_move < 10:  # BTC barely moved, no overreaction possible
        return

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_move = cfg.get("min_move_usd", 30)
        max_tso = cfg.get("max_time_since_open", 60)
        min_or = cfg.get("min_overreaction", 2.0)
        use_vol_filter = cfg.get("vol_filter", False)

        if time_since_open > max_tso:
            continue
        if abs_move < min_move:
            continue
        if overreaction < min_or:
            continue
        if use_vol_filter and vol_ratio > 3.0:
            # Unusual volume — might be real news, skip
            engine.log_skip(combo.name, hammered_dir, hammered_now, abs_move, time_remaining,
                            "vol_filter: ratio {:.1f}x > 3x".format(vol_ratio))
            continue

        # Buy the hammered side
        if hammered_dir == "YES":
            entry = state.book.best_ask
            if 0.05 <= entry <= 0.95:
                engine.execute_paper_trade(combo, "YES", overreaction, time_remaining, entry)
        else:
            entry = state.book.best_bid
            if 0.05 <= (1.0 - entry) <= 0.95:
                engine.execute_paper_trade(combo, "NO", overreaction, time_remaining, entry)


ARCH_SPEC = {
    "name": "overreaction_fade",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "OF_MAX_SPREAD": OF_MAX_SPREAD,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0, "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
