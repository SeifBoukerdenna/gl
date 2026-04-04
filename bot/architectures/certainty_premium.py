"""
Architecture: certainty_premium
Thesis: In the final 30-60 seconds, when the outcome is near-certain (z > 1.5), the winning
token trades at 90-94c instead of 97-99c. Market makers pull quotes near expiry creating a
liquidity vacuum. Buy the near-certainty at a discount and collect a small but extremely
high-probability payout. 6% return in 60 seconds at 98% probability = absurd Sharpe.
"""

import time

COMBO_PARAMS = [
    {"name": "CP_z1.5_60s_95c", "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 1.5, "max_time": 60, "max_winner_price": 0.95},
    {"name": "CP_z2_60s_95c",   "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 2.0, "max_time": 60, "max_winner_price": 0.95},
    {"name": "CP_z1.5_30s_95c", "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 1.5, "max_time": 30, "max_winner_price": 0.95},
    {"name": "CP_z1.5_60s_92c", "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 1.5, "max_time": 60, "max_winner_price": 0.92},
    {"name": "CP_z2_30s_93c",   "btc_threshold_bp": 0, "lookback_s": 0, "min_z": 2.0, "max_time": 30, "max_winner_price": 0.93},
]

_combo_config = {p["name"]: p for p in COMBO_PARAMS}
_last_status = [0.0]

CP_MIN_TIME = 3
CP_MAX_SPREAD = 0.06  # wider tolerance — books thin near expiry
CP_MIN_BOOK_LEVELS = 1


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
    if len(state.book.bids) < CP_MIN_BOOK_LEVELS or len(state.book.asks) < CP_MIN_BOOK_LEVELS:
        return
    if state.book.spread > CP_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < CP_MIN_TIME or time_remaining > 60:
        return

    if not state.window_open or state.window_open <= 0:
        return
    btc_corrected = (state.binance_price or 0) - state.offset
    strike = state.window_open

    z = vol_tracker.get_z_score(btc_corrected, strike, time_remaining)
    abs_z = abs(z)
    sigma = vol_tracker.get_sigma()
    dist = btc_corrected - strike

    # Determine winning side and its price
    if z > 0:
        # BTC above strike → YES wins
        winning_dir = "YES"
        winner_cost = state.book.best_ask  # YES ask = what you pay
    else:
        # BTC below strike → NO wins
        winning_dir = "NO"
        winner_cost = 1.0 - state.book.best_bid  # NO cost

    fair_winner = vol_tracker.get_fair_probability(btc_corrected, strike, time_remaining)
    if z < 0:
        fair_winner = 1.0 - fair_winner

    expected_profit = (1.0 - winner_cost)  # if you buy at 92c and win, profit = 8c
    discount = fair_winner - winner_cost  # fair=98c, market=92c → 6c discount

    # Status
    now_t = time.time()
    if now_t - _last_status[0] >= 5:
        print("  {}{}{} {}T-{:>2.0f}s{} | z={:+.1f} dist=${:.0f} sig=${:.1f} | {} winner@{:.0f}c (fair {:.0f}c) profit={:.0f}c".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            z, dist, sigma, winning_dir, winner_cost * 100, fair_winner * 100,
            expected_profit * 100))
        _last_status[0] = now_t

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_z = cfg.get("min_z", 1.5)
        max_time = cfg.get("max_time", 60)
        max_wp = cfg.get("max_winner_price", 0.95)

        if time_remaining > max_time:
            continue
        if abs_z < min_z:
            continue
        if winner_cost > max_wp:
            # Winner already priced too high — no discount to capture
            continue
        if winner_cost < 0.80:
            # Winner priced too low — model might be wrong
            continue

        # Buy the winning side
        if winning_dir == "YES":
            entry = state.book.best_ask
            if 0.01 <= entry <= 0.99:
                bk = len(state.book.asks)
                print("  [CP] FIRE {} YES z={:.1f} entry={:.0f}c asks={} T-{:.0f}s".format(
                    combo.name, abs_z, entry*100, bk, time_remaining))
                engine.execute_paper_trade(combo, "YES", abs_z, time_remaining, entry)
        else:
            entry = state.book.best_bid
            if 0.01 <= entry <= 0.99:
                bk = len(state.book.bids)
                print("  [CP] FIRE {} NO z={:.1f} entry={:.0f}c bids={} T-{:.0f}s".format(
                    combo.name, abs_z, entry*100, bk, time_remaining))
                engine.execute_paper_trade(combo, "NO", abs_z, time_remaining, entry)


def on_window_start(state):
    _last_status[0] = 0.0


ARCH_SPEC = {
    "name": "certainty_premium",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "CP_MAX_SPREAD": CP_MAX_SPREAD,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0, "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
