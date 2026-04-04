"""
Architecture: impulse_lag
Thesis: Binance BTC/USDT leads Polymarket's 5-min BTC Up/Down book by 2-10 seconds.
When BTC moves sharply (impulse > N bps in M seconds), buy the direction on PM before
PM market makers reprice. The edge is the structural lag between Binance price discovery
and PM orderbook updates. Profitable at high-conviction entries (actual cost 65-80c) with
fast lookback windows (10-30s) and plenty of time remaining (>210s).
"""

import time

# Default combo definitions for this architecture
COMBO_PARAMS = [
    {"name": "A_5bp_30s", "btc_threshold_bp": 5, "lookback_s": 30},
    {"name": "B_7bp_30s", "btc_threshold_bp": 7, "lookback_s": 30},
    {"name": "C_10bp_30s", "btc_threshold_bp": 10, "lookback_s": 30},
    {"name": "D_15bp_15s", "btc_threshold_bp": 15, "lookback_s": 15},
    {"name": "E_5bp_15s", "btc_threshold_bp": 5, "lookback_s": 15},
    {"name": "F_7bp_15s", "btc_threshold_bp": 7, "lookback_s": 15},
    {"name": "G_3bp_10s", "btc_threshold_bp": 3, "lookback_s": 10},
    {"name": "H_5bp_10s", "btc_threshold_bp": 5, "lookback_s": 10},
    {"name": "I_10bp_15s", "btc_threshold_bp": 10, "lookback_s": 15},
    {"name": "J_7bp_45s", "btc_threshold_bp": 7, "lookback_s": 45},
    {"name": "K_5bp_60s", "btc_threshold_bp": 5, "lookback_s": 60},
    {"name": "L_10bp_60s", "btc_threshold_bp": 10, "lookback_s": 60},
]


# These will be injected by the engine when the module is loaded
# They can be overridden by config JSON
_DEFAULTS = {
    "MAX_IMPULSE_BP": 25,
    "DEAD_ZONE_START": 90,
    "DEAD_ZONE_END": 210,
    "MAX_SPREAD": 0.03,
    "MIN_BOOK_LEVELS": 3,
    "MIN_ENTRY_PRICE": 0.20,
    "MAX_ENTRY_PRICE": 0.80,
    "PRINT_STATUS_INTERVAL": 15,
    "MAX_SKIP_PRINTS": 3,
}

_last_status = [0.0]


def check_signals(state, now_s):
    """Impulse lag signal detection — called once per Binance tick (1/sec)."""
    # Import engine functions and globals dynamically
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return

    if time.time() < state.cooldown_until:
        return

    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < engine.MIN_BOOK_LEVELS or len(state.book.asks) < engine.MIN_BOOK_LEVELS:
        return

    if state.book.spread > engine.MAX_SPREAD:
        return

    if len(state.price_buffer) < 5:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < engine.WINDOW_BUFFER_END or time_remaining > (300 - engine.WINDOW_BUFFER_START):
        return

    # Status line
    now_t = time.time()
    if now_t - _last_status[0] >= engine.PRINT_STATUS_INTERVAL:
        btc = state.binance_price or 0
        corrected = btc - state.offset
        delta = 0
        if state.window_open and state.window_open > 0:
            delta = (corrected - state.window_open) / state.window_open * 10000
        pm_up = state.book.best_ask * 100
        pm_down = (1 - state.book.best_bid) * 100
        dc = engine.G if delta > 0 else engine.R if delta < 0 else engine.RST
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC ${:,.2f} {}{:+.1f}bp{} | {}Up:{:.0f}c{} {}Down:{:.0f}c{} Spr:{:.0f}c".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            corrected, dc, delta, engine.RST,
            engine.G, pm_up, engine.RST, engine.R, pm_down, engine.RST, state.book.spread * 100))
        _last_status[0] = now_t

    price_now = state.binance_price
    if price_now is None:
        return

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        lookback_ts = now_s - combo.lookback_s
        price_ago = engine.find_price_at(state.price_buffer, lookback_ts)
        if price_ago is None or price_ago <= 0:
            continue

        impulse_bps = (price_now - price_ago) / price_ago * 10000
        if abs(impulse_bps) < combo.btc_threshold_bp:
            continue

        direction = "YES" if impulse_bps > 0 else "NO"
        entry_price = state.book.best_ask if direction == "YES" else state.book.best_bid

        # F1: Impulse cap
        if abs(impulse_bps) > engine.MAX_IMPULSE_BP:
            engine.log_skip(combo.name, direction, entry_price, impulse_bps, time_remaining,
                            "impulse {:.0f}bp > {}bp".format(abs(impulse_bps), engine.MAX_IMPULSE_BP))
            continue

        # F2: Actual cost filter
        actual_cost = entry_price if direction == "YES" else (1.0 - entry_price)
        if actual_cost < engine.MIN_ENTRY_PRICE or actual_cost > engine.MAX_ENTRY_PRICE:
            engine.log_skip(combo.name, direction, entry_price, impulse_bps, time_remaining,
                            "actual cost {:.0f}c outside {:.0f}-{:.0f}c (YES={:.0f}c, dir={})".format(
                                actual_cost * 100, engine.MIN_ENTRY_PRICE * 100, engine.MAX_ENTRY_PRICE * 100,
                                entry_price * 100, direction))
            continue

        # F3: Dead zone
        if engine.DEAD_ZONE_START <= time_remaining <= engine.DEAD_ZONE_END:
            engine.log_skip(combo.name, direction, entry_price, impulse_bps, time_remaining,
                            "dead zone T-{:.0f}s".format(time_remaining))
            continue

        # ALL FILTERS PASSED
        engine.execute_paper_trade(combo, direction, impulse_bps, time_remaining, entry_price)


ARCH_SPEC = {
    "name": "impulse_lag",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": _DEFAULTS,
    "on_window_start": None,
    "on_window_end": None,
    "on_tick": None,
}
