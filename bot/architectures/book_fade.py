"""
Architecture: book_fade
Thesis: PM's thin 5-min BTC books get temporarily dislocated when aggressive takers strip
one side. When bid size is 3x+ ask size (or vice versa), the book is overreacting to a move
that BTC's actual delta doesn't justify. The book_fade architecture trades against the
imbalance — buying the underweight side — because settlement only cares about mid vs 0.50,
and the fundamental (BTC price) hasn't moved enough to justify the dislocation. Market makers
will refill the stripped side, pushing the book back toward equilibrium.
"""

import time
from collections import deque

BF_MIN_BOOK_SIZE = 30        # minimum total shares on each side
BF_MAX_DELTA_BPS = 8         # max absolute BTC delta to qualify as "overreaction"
BF_MIN_TIME_REMAINING = 20
BF_MAX_TIME_REMAINING = 270
BF_MAX_SPREAD = 0.05
BF_MIN_BOOK_LEVELS = 2
BF_CONFIRMATION_TICKS = 3    # imbalance must persist for N seconds
BF_MIN_ENTRY_PRICE = 0.30
BF_MAX_ENTRY_PRICE = 0.90

# Track imbalance history per window
_imbalance_history = deque(maxlen=30)  # (ts, ratio) where ratio = bid_size/ask_size


COMBO_PARAMS = [
    {"name": "BF_1.5x_fast", "btc_threshold_bp": 0, "lookback_s": 0, "imbalance_threshold": 1.5, "confirm_ticks": 1},
    {"name": "BF_2x_fast",   "btc_threshold_bp": 0, "lookback_s": 0, "imbalance_threshold": 2.0, "confirm_ticks": 1},
    {"name": "BF_2x_fade",   "btc_threshold_bp": 0, "lookback_s": 0, "imbalance_threshold": 2.0, "confirm_ticks": 3},
    {"name": "BF_3x_fade",   "btc_threshold_bp": 0, "lookback_s": 0, "imbalance_threshold": 3.0, "confirm_ticks": 2},
    {"name": "BF_3x_snap",   "btc_threshold_bp": 0, "lookback_s": 0, "imbalance_threshold": 3.0, "confirm_ticks": 1},
    {"name": "BF_5x_snap",   "btc_threshold_bp": 0, "lookback_s": 0, "imbalance_threshold": 5.0, "confirm_ticks": 1},
]

_combo_config = {p["name"]: p for p in COMBO_PARAMS}
_last_status = [0.0]


def on_tick(state, price, ts):
    """Track book imbalance on every tick."""
    if not state.book.bids or not state.book.asks:
        return
    bid_size = sum(s for _, s in state.book.bids[:5])
    ask_size = sum(s for _, s in state.book.asks[:5])
    if ask_size > 0:
        ratio = bid_size / ask_size
    else:
        ratio = 99.0
    _imbalance_history.append((ts, ratio))


def check_signals(state, now_s):
    """Book fade signal detection — trade against PM orderbook imbalance."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < BF_MIN_BOOK_LEVELS or len(state.book.asks) < BF_MIN_BOOK_LEVELS:
        return
    if state.book.spread > BF_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < BF_MIN_TIME_REMAINING or time_remaining > BF_MAX_TIME_REMAINING:
        return

    # Compute current imbalance
    bid_size = sum(s for _, s in state.book.bids[:5])
    ask_size = sum(s for _, s in state.book.asks[:5])
    if bid_size < BF_MIN_BOOK_SIZE or ask_size < BF_MIN_BOOK_SIZE:
        return

    ratio = bid_size / ask_size  # >1 = bid heavy (market pricing UP), <1 = ask heavy (DOWN)

    # Check BTC delta — only fade if delta is small (market overreacting)
    if not state.window_open or state.window_open <= 0:
        return
    btc_corrected = (state.binance_price or 0) - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # Status line
    now_t = time.time()
    if now_t - _last_status[0] >= 15:
        print("  {}{}{} {}T-{:>3.0f}s{} | imbalance {:.1f}x (bid/ask) | delta {:+.1f}bp".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            ratio, delta_bps))
        _last_status[0] = now_t

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        threshold = cfg.get("imbalance_threshold", 2.0)
        confirm = cfg.get("confirm_ticks", 3)

        # BID HEAVY (ratio >> 1): market pricing UP aggressively
        # Fade by buying NO if BTC delta is small
        if ratio >= threshold and abs(delta_bps) <= BF_MAX_DELTA_BPS:
            # Check persistence
            if _check_persistence(now_s, lambda r: r >= threshold, confirm):
                no_cost = 1.0 - state.book.best_bid
                if BF_MIN_ENTRY_PRICE <= no_cost <= BF_MAX_ENTRY_PRICE:
                    engine.execute_paper_trade(combo, "NO", ratio, time_remaining, state.book.best_bid)
                    continue

        # ASK HEAVY (1/ratio >> 1 i.e. ratio << 1): market pricing DOWN aggressively
        # Fade by buying YES if BTC delta is small
        inv_ratio = 1.0 / ratio if ratio > 0 else 99.0
        if inv_ratio >= threshold and abs(delta_bps) <= BF_MAX_DELTA_BPS:
            if _check_persistence(now_s, lambda r: (1.0/r if r > 0 else 99) >= threshold, confirm):
                yes_cost = state.book.best_ask
                if BF_MIN_ENTRY_PRICE <= yes_cost <= BF_MAX_ENTRY_PRICE:
                    engine.execute_paper_trade(combo, "YES", inv_ratio, time_remaining, yes_cost)


def _check_persistence(now_s, condition_fn, min_ticks):
    """Check if the imbalance condition held for min_ticks consecutive seconds."""
    if len(_imbalance_history) < min_ticks:
        return False
    recent = list(_imbalance_history)[-min_ticks:]
    return all(condition_fn(r) for _, r in recent)


def on_window_start(state):
    """Clear imbalance history for new window."""
    _imbalance_history.clear()
    _last_status[0] = 0.0


ARCH_SPEC = {
    "name": "book_fade",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "BF_MIN_BOOK_SIZE": BF_MIN_BOOK_SIZE,
        "BF_MAX_DELTA_BPS": BF_MAX_DELTA_BPS,
        "BF_MIN_TIME_REMAINING": BF_MIN_TIME_REMAINING,
        "BF_MAX_TIME_REMAINING": BF_MAX_TIME_REMAINING,
        "BF_MAX_SPREAD": BF_MAX_SPREAD,
        "BF_MIN_BOOK_LEVELS": BF_MIN_BOOK_LEVELS,
        "BF_CONFIRMATION_TICKS": BF_CONFIRMATION_TICKS,
        "BF_MIN_ENTRY_PRICE": BF_MIN_ENTRY_PRICE,
        "BF_MAX_ENTRY_PRICE": BF_MAX_ENTRY_PRICE,
        "MIN_ENTRY_PRICE": 0.01,
        "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0,
        "DEAD_ZONE_END": 0,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
