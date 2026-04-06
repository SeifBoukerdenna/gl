"""
Architecture: lottery_fade
Thesis: Favorite-longshot bias. In the final 120 seconds when BTC is comfortably on one side
of the strike (z-score > 2), the losing token should be worth 2-4c based on actual reversal
probability. It regularly trades at 8-15c because retail buys lottery tickets. We buy the
winning side (equivalent to selling the overpriced loser) and hold to settlement. Wins ~95%
of the time, loses big when it loses — but the edge is the spread between fair and market
price on the losing token.
"""

import time

COMBO_PARAMS = [
    {"name": "LF_z2_120s",   "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 2.0, "max_time": 120},
    {"name": "LF_z2_90s",    "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 2.0, "max_time": 90},
    {"name": "LF_z2.5_120s", "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 2.5, "max_time": 120},
    {"name": "LF_z3_120s",   "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 3.0, "max_time": 120},
    {"name": "LF_z2_60s",    "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 2.0, "max_time": 60},
]

_combo_config = {p["name"]: p for p in COMBO_PARAMS}
_last_status = [0.0]

LF_MIN_TIME = 3
LF_MAX_SPREAD = 0.05
LF_MIN_BOOK_LEVELS = 1
LF_MIN_LOSING_OVERPRICE = 0.03  # losing token must be 3c+ above fair value


def on_tick(state, price, ts):
    from bot.shared.volatility import vol_tracker
    vol_tracker.record_tick(price, ts)


def check_signals(state, now_s):
    import bot.paper_trade_v2 as engine
    from bot.shared.volatility import vol_tracker

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < LF_MIN_BOOK_LEVELS or len(state.book.asks) < LF_MIN_BOOK_LEVELS:
        return
    if state.book.spread > LF_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < LF_MIN_TIME or time_remaining > 120:
        return

    if not state.window_open or state.window_open <= 0:
        return
    btc_corrected = (state.binance_price or 0) - state.offset
    strike = state.window_open

    z = vol_tracker.get_z_score(btc_corrected, strike, time_remaining)
    abs_z = abs(z)
    fair_up = vol_tracker.get_fair_probability(btc_corrected, strike, time_remaining)
    sigma = vol_tracker.get_sigma()
    dist = btc_corrected - strike

    # Determine winning/losing side
    if z > 0:
        winning_dir = "YES"
        fair_winner = fair_up
        fair_loser = 1.0 - fair_up
        loser_market_price = 1.0 - state.book.best_bid  # NO cost
        winner_cost = state.book.best_ask  # YES cost
    else:
        winning_dir = "NO"
        fair_winner = 1.0 - fair_up
        fair_loser = fair_up
        loser_market_price = state.book.best_ask  # YES price = loser cost
        winner_cost = 1.0 - state.book.best_bid  # NO cost

    losing_overprice = loser_market_price - fair_loser

    # Status
    now_t = time.time()
    if now_t - _last_status[0] >= 5:
        print("  {}{}{} {}T-{:>3.0f}s{} | z={:+.1f} dist=${:.0f} sig=${:.1f} | {}:{:.0f}%fair loser@{:.0f}c(fair {:.0f}c) +{:.0f}c edge".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            z, dist, sigma, winning_dir, fair_winner * 100,
            loser_market_price * 100, fair_loser * 100, losing_overprice * 100))
        _last_status[0] = now_t

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_z = cfg.get("min_z", 2.0)
        max_time = cfg.get("max_time", 120)

        if time_remaining > max_time:
            continue
        if abs_z < min_z:
            continue
        if losing_overprice < getattr(engine, 'LF_MIN_LOSING_OVERPRICE', LF_MIN_LOSING_OVERPRICE):
            continue

        # Buy the winning side
        if winning_dir == "YES":
            entry = state.book.best_ask
            if 0.01 <= entry <= 0.99:
                engine.execute_paper_trade(combo, "YES", abs_z, time_remaining, entry)
        else:
            entry = state.book.best_bid
            if 0.01 <= entry <= 0.99:
                engine.execute_paper_trade(combo, "NO", abs_z, time_remaining, entry)


def on_window_start(state):
    _last_status[0] = 0.0


ARCH_SPEC = {
    "name": "lottery_fade",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "LF_MIN_LOSING_OVERPRICE": LF_MIN_LOSING_OVERPRICE,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0, "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
