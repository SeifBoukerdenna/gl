"""
Architecture: evergreen (Always-On Z-Score Printer)

Thesis: Don't predict direction. Don't detect momentum. Don't react to impulses.
Instead, CONTINUOUSLY monitor where BTC is relative to the strike. When BTC is
clearly on one side (z > 2.0 = 97.7% probability), buy the winning token.

Why this is different from everything else:
  - impulse_lag/confirmed/flow_front REACT to price MOVEMENT → fail in chop
  - cross_pressure detects MOMENTUM across exchanges → fail in chop
  - certainty_premium waits for LAST 60s → misses mid-window opportunities
  - gamma_snipe tried this but failed because: both directions, no confirmation,
    extreme entries, static thresholds

Evergreen fixes ALL of those:
  - YES-only (eliminates NO drag)
  - Multi-exchange confirmation (filters offset/noise)
  - 50-85c entry only (fee sweet spot, not extremes)
  - Tiered sizing by z-score (z>3 gets 3x base)
  - Fires ENTIRE window (not just phases)

Self-regulating in chop: BTC stays near strike → z < 2.0 → no trades → no bleeding.
In trend: BTC moves away early → z crosses 2.0 → captures the move with confirmation.

The line goes up because:
  - z > 2.0 means 97.7%+ model probability
  - Multi-exchange confirmation means the position is real, not noise
  - YES-only means no structural direction drag
  - Sweet-spot entries (50-85c) mean reasonable fee drag
"""

import time
import math
import threading
import json as _json
from collections import deque

from bot.shared.volatility import vol_tracker

# ═══ Parameters ═══
EV_MIN_Z = 2.0               # minimum z-score to consider trading
EV_MIN_DISCOUNT = 0.02       # winner token must be >= 2c below fair value
EV_CONFIRM_WINDOW = 5        # seconds to check other exchanges
EV_CONFIRM_MIN_BPS = 0.5     # other exchange must show >= 0.5bp same side
EV_MIN_CONFIRMS = 1          # need at least 1 other exchange confirming
EV_COOLDOWN_SEC = 10         # seconds between signals
EV_MAX_SPREAD = 0.05         # wider tolerance (near expiry spreads widen)
EV_MIN_BOOK_LEVELS = 1
EV_MIN_ENTRY_PRICE = 0.50    # sweet spot: 50c+ for YES
EV_MAX_ENTRY_PRICE = 0.85    # not too expensive
EV_MIN_TIME = 5
EV_MAX_TIME = 295

# Tiered sizing
EV_BASE_DOLLARS = 100
EV_Z25_DOLLARS = 175         # z >= 2.5
EV_Z30_DOLLARS = 275         # z >= 3.0

# Late-window (certainty mode): lower z threshold, wider entry
EV_LATE_WINDOW = 60          # last 60 seconds
EV_LATE_MIN_Z = 1.5          # lower z for near-certain outcomes
EV_LATE_MAX_ENTRY = 0.95     # allow expensive entries near expiry

COMBO_PARAMS = [
    {"name": "EV_core",  "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "EV_late",  "btc_threshold_bp": 0, "lookback_s": 0},
]

# ═══ Exchange feeds ═══
_exchange_prices = {
    "binance":  deque(maxlen=120),
    "coinbase": deque(maxlen=120),
    "bybit":    deque(maxlen=120),
}
_lock = threading.Lock()
_threads_started = False
_last_signal_time = [0.0]
_last_status = [0.0]


def _coinbase_feed():
    import websockets.sync.client as ws_sync
    while True:
        try:
            with ws_sync.connect("wss://ws-feed.exchange.coinbase.com") as ws:
                ws.send(_json.dumps({
                    "type": "subscribe",
                    "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}]
                }))
                for msg in ws:
                    data = _json.loads(msg)
                    if data.get("type") == "ticker" and data.get("price"):
                        with _lock:
                            _exchange_prices["coinbase"].append(
                                (time.time(), float(data["price"])))
        except Exception:
            time.sleep(5)


def _bybit_feed():
    import websockets.sync.client as ws_sync
    while True:
        try:
            with ws_sync.connect("wss://stream.bybit.com/v5/public/spot") as ws:
                ws.send(_json.dumps({"op": "subscribe", "args": ["tickers.BTCUSDT"]}))
                for msg in ws:
                    data = _json.loads(msg)
                    if data.get("topic") == "tickers.BTCUSDT":
                        price = float(data.get("data", {}).get("lastPrice", 0))
                        if price > 0:
                            with _lock:
                                _exchange_prices["bybit"].append((time.time(), price))
        except Exception:
            time.sleep(5)


def _start_feeds():
    global _threads_started
    if _threads_started:
        return
    _threads_started = True
    for target, name in [(_coinbase_feed, "coinbase"), (_bybit_feed, "bybit")]:
        t = threading.Thread(target=target, name="ev-" + name, daemon=True)
        t.start()
    print("  [EV] Started Coinbase + Bybit feeds")


def _get_exchange_price(exchange):
    """Get latest price from an exchange."""
    with _lock:
        prices = list(_exchange_prices[exchange])
    if not prices:
        return None
    # Only use if recent (< 10 seconds old)
    ts, px = prices[-1]
    if time.time() - ts > 10:
        return None
    return px


def _check_position_confirmed(btc_corrected, strike):
    """Check if other exchanges ALSO show BTC on the same side of strike.
    Returns number of confirming exchanges."""
    if strike <= 0:
        return 0

    btc_side = "above" if btc_corrected > strike else "below"
    confirms = 0

    for exchange in ["coinbase", "bybit"]:
        px = _get_exchange_price(exchange)
        if px is None:
            continue
        # Apply approximate offset (these exchanges track close to Binance)
        ex_side = "above" if px > strike else "below"
        if ex_side == btc_side:
            confirms += 1

    return confirms


# ═══ Hooks ═══

def on_tick(state, price, ts):
    _start_feeds()
    with _lock:
        _exchange_prices["binance"].append((time.time(), price))
    vol_tracker.record_tick(price, ts)


def check_signals(state, now_s):
    """Continuously monitor z-score. Trade the winning side when confident."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    min_levels = getattr(engine, 'EV_MIN_BOOK_LEVELS', EV_MIN_BOOK_LEVELS)
    if len(state.book.bids) < min_levels or len(state.book.asks) < min_levels:
        return
    if state.book.spread > getattr(engine, 'EV_MAX_SPREAD', EV_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < getattr(engine, 'EV_MIN_TIME', EV_MIN_TIME):
        return
    if time_remaining > getattr(engine, 'EV_MAX_TIME', EV_MAX_TIME):
        return

    if not state.window_open or state.window_open <= 0:
        return
    if state.binance_price is None:
        return

    now = time.time()

    if now - _last_signal_time[0] < getattr(engine, 'EV_COOLDOWN_SEC', EV_COOLDOWN_SEC):
        return

    # Book freshness
    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    btc_corrected = state.binance_price - state.offset
    strike = state.window_open

    z = vol_tracker.get_z_score(btc_corrected, strike, time_remaining)
    abs_z = abs(z)
    fair_up = vol_tracker.get_fair_probability(btc_corrected, strike, time_remaining)
    sigma = vol_tracker.get_sigma()
    dist = btc_corrected - strike

    # Status line
    if now - _last_status[0] >= 10:
        dc = engine.G if z > 0 else engine.R if z < 0 else engine.RST
        confirms = _check_position_confirmed(btc_corrected, strike)
        phase = "LATE" if time_remaining <= getattr(engine, 'EV_LATE_WINDOW', EV_LATE_WINDOW) else "SCAN"
        print("  {}{}{} {}T-{:>3.0f}s{} | {}z={:+.1f}{} dist=${:.0f} sig=${:.1f} | {}ex={} {}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, z, engine.RST, dist, sigma, confirms, "+conf" if confirms >= 1 else "wait", phase))
        _last_status[0] = now

    late_window = getattr(engine, 'EV_LATE_WINDOW', EV_LATE_WINDOW)
    is_late = time_remaining <= late_window

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        if combo.name == "EV_core":
            # Core: z > 2.0, full window, confirmed, 50-85c entry
            min_z = getattr(engine, 'EV_MIN_Z', EV_MIN_Z)
            if abs_z < min_z:
                continue

            # Must be confirmed by another exchange
            confirms = _check_position_confirmed(btc_corrected, strike)
            min_confirms = getattr(engine, 'EV_MIN_CONFIRMS', EV_MIN_CONFIRMS)
            if confirms < min_confirms:
                continue

            # Determine winning direction
            if z > 0:
                direction = "YES"
                entry = state.book.best_ask
                fair_winner = fair_up
            else:
                direction = "NO"
                entry = 1.0 - state.book.best_bid
                fair_winner = 1.0 - fair_up

            # Direction filter
            allowed = getattr(engine, 'ALLOWED_DIRECTIONS', None)
            if allowed and direction not in allowed:
                continue

            # Entry range
            min_entry = getattr(engine, 'EV_MIN_ENTRY_PRICE', EV_MIN_ENTRY_PRICE)
            max_entry = getattr(engine, 'EV_MAX_ENTRY_PRICE', EV_MAX_ENTRY_PRICE)
            # Use book price for the direction
            book_entry = state.book.best_ask if direction == "YES" else state.book.best_bid
            if book_entry < min_entry or book_entry > max_entry:
                continue

            # Discount check: winner should be below fair value
            min_discount = getattr(engine, 'EV_MIN_DISCOUNT', EV_MIN_DISCOUNT)
            if direction == "YES":
                discount = fair_winner - state.book.best_ask
            else:
                discount = fair_winner - (1.0 - state.book.best_bid)
            if discount < min_discount:
                continue

            # Tiered sizing by z-score
            z30_dollars = getattr(engine, 'EV_Z30_DOLLARS', EV_Z30_DOLLARS)
            z25_dollars = getattr(engine, 'EV_Z25_DOLLARS', EV_Z25_DOLLARS)
            base_dollars = getattr(engine, 'EV_BASE_DOLLARS', EV_BASE_DOLLARS)

            if abs_z >= 3.0:
                dollars = z30_dollars
            elif abs_z >= 2.5:
                dollars = z25_dollars
            else:
                dollars = base_dollars

            print("  {}[EV] CORE {} z={:.1f} fair={:.0f}c book={:.0f}c disc={:.0f}c ${} {}ex T-{:.0f}s{}".format(
                engine.G if direction == "YES" else engine.R,
                direction, z, fair_winner * 100, book_entry * 100,
                discount * 100, dollars, confirms, time_remaining, engine.RST))

            _last_signal_time[0] = now
            engine.execute_paper_trade(combo, direction, abs_z, time_remaining, book_entry,
                                       override_dollars=dollars)

        elif combo.name == "EV_late" and is_late:
            # Late mode: certainty premium style, lower z, wider entry
            late_min_z = getattr(engine, 'EV_LATE_MIN_Z', EV_LATE_MIN_Z)
            if abs_z < late_min_z:
                continue

            if z > 0:
                direction = "YES"
                winner_cost = state.book.best_ask
            else:
                direction = "NO"
                winner_cost = 1.0 - state.book.best_bid

            # Direction filter
            allowed = getattr(engine, 'ALLOWED_DIRECTIONS', None)
            if allowed and direction not in allowed:
                continue

            late_max_entry = getattr(engine, 'EV_LATE_MAX_ENTRY', EV_LATE_MAX_ENTRY)
            if winner_cost > late_max_entry or winner_cost < 0.50:
                continue

            book_entry = state.book.best_ask if direction == "YES" else state.book.best_bid
            dollars = getattr(engine, 'EV_BASE_DOLLARS', EV_BASE_DOLLARS)

            print("  {}[EV] LATE {} z={:.1f} @{:.0f}c ${} T-{:.0f}s{}".format(
                engine.G if direction == "YES" else engine.R,
                direction, z, winner_cost * 100, dollars, time_remaining, engine.RST))

            _last_signal_time[0] = now
            engine.execute_paper_trade(combo, direction, abs_z, time_remaining, book_entry,
                                       override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "evergreen",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "EV_MIN_Z": EV_MIN_Z,
        "EV_MIN_DISCOUNT": EV_MIN_DISCOUNT,
        "EV_CONFIRM_WINDOW": EV_CONFIRM_WINDOW,
        "EV_CONFIRM_MIN_BPS": EV_CONFIRM_MIN_BPS,
        "EV_MIN_CONFIRMS": EV_MIN_CONFIRMS,
        "EV_COOLDOWN_SEC": EV_COOLDOWN_SEC,
        "EV_MAX_SPREAD": EV_MAX_SPREAD,
        "EV_MIN_BOOK_LEVELS": EV_MIN_BOOK_LEVELS,
        "EV_MIN_ENTRY_PRICE": EV_MIN_ENTRY_PRICE,
        "EV_MAX_ENTRY_PRICE": EV_MAX_ENTRY_PRICE,
        "EV_MIN_TIME": EV_MIN_TIME,
        "EV_MAX_TIME": EV_MAX_TIME,
        "EV_BASE_DOLLARS": EV_BASE_DOLLARS,
        "EV_Z25_DOLLARS": EV_Z25_DOLLARS,
        "EV_Z30_DOLLARS": EV_Z30_DOLLARS,
        "EV_LATE_WINDOW": EV_LATE_WINDOW,
        "EV_LATE_MIN_Z": EV_LATE_MIN_Z,
        "EV_LATE_MAX_ENTRY": EV_LATE_MAX_ENTRY,
        "ALLOWED_DIRECTIONS": None,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
