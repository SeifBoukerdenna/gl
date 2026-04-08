"""
Architecture: phoenix (Smart Cheap Loser Flip)

Thesis: Buy the LOSER token at dirt-cheap prices (5-20c) for 5x-10x payout.
But NOT blindly — only when there's a REASON to believe the flip will happen:

  1. Multi-exchange momentum TOWARD the strike — Coinbase/Bybit show BTC
     moving in the direction that would flip the outcome
  2. Volume surge — Binance msg rate spiking, something big is happening
     and BTC is close enough to strike to go either way

Either signal alone is enough to fire. Both together = 1.5x sizing.

Loss is CAPPED at the cheap entry price (5-20c/share = $100-200 max).
Win is 80-95c/share = $500-800+.
Only needs 15% WR to be profitable. With confirmation signals: 20-30% WR expected.
"""

import time
import math
import threading
import json as _json
from collections import deque

from bot.shared.volatility import vol_tracker

# ═══ Parameters ═══
PX_MAX_TIME = 120          # last 120 seconds
PX_MIN_TIME = 5
PX_MIN_DISTANCE_BPS = 1.5
PX_MAX_DISTANCE_BPS = 8.0
PX_MIN_LOSER_PRICE = 0.04
PX_MAX_LOSER_PRICE = 0.22
PX_MAX_SPREAD = 0.06
PX_MIN_BOOK_LEVELS = 1
PX_COOLDOWN_SEC = 5
PX_BASE_DOLLARS = 100
PX_STRONG_DOLLARS = 150     # when both momentum + volume confirm

# Momentum confirmation
PX_MOMENTUM_WINDOW = 5      # seconds to check exchange momentum
PX_MOMENTUM_MIN_BPS = 0.5   # exchange must show >= 0.5bp toward strike

# Volume surge confirmation
PX_VS_SHORT_WINDOW = 2
PX_VS_LONG_WINDOW = 30
PX_VS_MIN_RATIO = 2.5       # short rate must be 2.5x long rate

COMBO_PARAMS = [
    {"name": "PX_near",  "btc_threshold_bp": 0, "lookback_s": 0,
     "max_distance": 5.0, "max_price": 0.18},
    {"name": "PX_far",   "btc_threshold_bp": 0, "lookback_s": 0,
     "max_distance": 8.0, "max_price": 0.12},
]
_combo_config = {p["name"]: p for p in COMBO_PARAMS}

# ═══ Exchange feeds ═══
_exchange_prices = {
    "coinbase": deque(maxlen=120),
    "bybit":    deque(maxlen=120),
}
_lock = threading.Lock()
_threads_started = False

# Volume surge tick log
_tick_log = deque(maxlen=3000)
_last_tick_price = [0.0]

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
        t = threading.Thread(target=target, name="px-" + name, daemon=True)
        t.start()
    print("  [PX] Started Coinbase + Bybit feeds")


def _get_exchange_momentum(exchange, window_sec):
    """Get price momentum in bps over last N seconds."""
    with _lock:
        prices = list(_exchange_prices[exchange])
    if len(prices) < 2:
        return None
    now = time.time()
    old_price = None
    for ts, px in prices:
        if ts >= now - window_sec - 1:
            old_price = px
            break
    if old_price is None or old_price <= 0:
        return None
    return (prices[-1][1] - old_price) / old_price * 10000


def _check_momentum_toward_strike(btc_corrected, strike):
    """Check if other exchanges show BTC moving TOWARD the strike.
    Returns (confirmed: bool, n_confirms: int, details: str)."""
    # If BTC is below strike, we need exchanges showing UPWARD momentum (toward strike)
    # If BTC is above strike, we need exchanges showing DOWNWARD momentum (toward strike)
    need_up = btc_corrected < strike  # below strike → need up movement to flip

    min_bps = PX_MOMENTUM_MIN_BPS
    confirms = 0
    details = []

    for exchange in ["coinbase", "bybit"]:
        mom = _get_exchange_momentum(exchange, PX_MOMENTUM_WINDOW)
        if mom is None:
            continue
        if need_up and mom >= min_bps:
            confirms += 1
            details.append("{}{:+.1f}".format(exchange[:2].upper(), mom))
        elif not need_up and mom <= -min_bps:
            confirms += 1
            details.append("{}{:+.1f}".format(exchange[:2].upper(), mom))

    return confirms >= 1, confirms, " ".join(details)


def _check_volume_surge():
    """Check if Binance trading rate is elevated (something big happening).
    Returns (surging: bool, ratio: float)."""
    now = time.time()
    short_count = sum(1 for ts, _, _ in _tick_log if now - ts <= PX_VS_SHORT_WINDOW)
    long_count = sum(1 for ts, _, _ in _tick_log if now - ts <= PX_VS_LONG_WINDOW)

    if long_count < 10:
        return False, 0

    short_rate = short_count / PX_VS_SHORT_WINDOW
    long_rate = long_count / PX_VS_LONG_WINDOW

    if long_rate <= 0:
        return False, 0

    ratio = short_rate / long_rate
    return ratio >= PX_VS_MIN_RATIO, ratio


def on_raw_tick(state, price, ts_ms):
    """Every Binance WS message — feed tick log for volume surge detection."""
    now = time.time()
    direction = 1 if price >= _last_tick_price[0] else -1
    _tick_log.append((now, price, direction))
    _last_tick_price[0] = price


def on_tick(state, price, ts):
    _start_feeds()
    vol_tracker.record_tick(price, ts)


def check_signals(state, now_s):
    """Buy cheap loser tokens ONLY when momentum or volume confirms a potential flip."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    if len(state.book.bids) < PX_MIN_BOOK_LEVELS or len(state.book.asks) < PX_MIN_BOOK_LEVELS:
        return
    if state.book.spread > getattr(engine, 'PX_MAX_SPREAD', PX_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    max_time = getattr(engine, 'PX_MAX_TIME', PX_MAX_TIME)
    min_time = getattr(engine, 'PX_MIN_TIME', PX_MIN_TIME)
    if time_remaining > max_time or time_remaining < min_time:
        return

    if not state.window_open or state.window_open <= 0:
        return
    if state.binance_price is None:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'PX_COOLDOWN_SEC', PX_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    btc_corrected = state.binance_price - state.offset
    strike = state.window_open

    delta_bps = abs(btc_corrected - strike) / strike * 10000
    min_dist = getattr(engine, 'PX_MIN_DISTANCE_BPS', PX_MIN_DISTANCE_BPS)

    if delta_bps < min_dist:
        return

    # Determine the LOSING side
    if btc_corrected > strike:
        loser_direction = "NO"
        loser_price = 1.0 - state.book.best_bid
        entry_book = state.book.best_bid
    else:
        loser_direction = "YES"
        loser_price = state.book.best_ask
        entry_book = state.book.best_ask

    # ── CONFIRMATION CHECKS ──
    mom_ok, mom_confirms, mom_detail = _check_momentum_toward_strike(btc_corrected, strike)
    vol_ok, vol_ratio = _check_volume_surge()

    # Need at least ONE confirmation (momentum OR volume)
    if not mom_ok and not vol_ok:
        # Status line (show why we're waiting)
        if now - _last_status[0] >= 8:
            side = "UP" if btc_corrected > strike else "DOWN"
            dc = engine.G if btc_corrected > strike else engine.R
            print("  {}{}{} {}T-{:>2.0f}s{} | BTC {}{:.1f}bp{} {} | loser={} @{:.0f}c | mom:{} vol:{:.1f}x WAIT".format(
                engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
                dc, delta_bps, engine.RST, side, loser_direction, loser_price * 100,
                mom_detail if mom_detail else "none", vol_ratio))
            _last_status[0] = now
        return

    # Status when confirmed
    if now - _last_status[0] >= 8:
        side = "UP" if btc_corrected > strike else "DOWN"
        dc = engine.G if btc_corrected > strike else engine.R
        reasons = []
        if mom_ok: reasons.append("mom[{}]".format(mom_detail))
        if vol_ok: reasons.append("vol[{:.1f}x]".format(vol_ratio))
        print("  {}{}{} {}T-{:>2.0f}s{} | BTC {}{:.1f}bp{} {} | loser={} @{:.0f}c | {} READY".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            dc, delta_bps, engine.RST, side, loser_direction, loser_price * 100,
            "+".join(reasons)))
        _last_status[0] = now

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        max_dist = cfg.get("max_distance", 5.0)
        max_price = cfg.get("max_price", 0.18)

        if delta_bps > max_dist:
            continue

        min_price = getattr(engine, 'PX_MIN_LOSER_PRICE', PX_MIN_LOSER_PRICE)
        if loser_price < min_price or loser_price > max_price:
            continue

        # Dynamic sizing: both confirmations = 1.5x
        if mom_ok and vol_ok:
            dollars = getattr(engine, 'PX_STRONG_DOLLARS', PX_STRONG_DOLLARS)
        else:
            dollars = getattr(engine, 'PX_BASE_DOLLARS', PX_BASE_DOLLARS)

        payout_if_win = (1.0 - loser_price)
        max_loss = loser_price
        rr = payout_if_win / max_loss if max_loss > 0 else 0

        reasons = []
        if mom_ok: reasons.append("mom{}ex".format(mom_confirms))
        if vol_ok: reasons.append("vol{:.1f}x".format(vol_ratio))

        print("  {}[PX] FIRE {} {} @{:.0f}c d={:.1f}bp R:R={:.1f}:1 ${} [{}] T-{:.0f}s{}".format(
            engine.G if loser_direction == "YES" else engine.R,
            combo.name, loser_direction, loser_price * 100, delta_bps,
            rr, dollars, "+".join(reasons), time_remaining, engine.RST))

        _last_signal_time[0] = now
        engine.execute_paper_trade(combo, loser_direction, delta_bps, time_remaining,
                                   entry_book, override_dollars=dollars)
        break


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "phoenix",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "PX_MAX_TIME": PX_MAX_TIME,
        "PX_MIN_TIME": PX_MIN_TIME,
        "PX_MIN_DISTANCE_BPS": PX_MIN_DISTANCE_BPS,
        "PX_MAX_DISTANCE_BPS": PX_MAX_DISTANCE_BPS,
        "PX_MIN_LOSER_PRICE": PX_MIN_LOSER_PRICE,
        "PX_MAX_LOSER_PRICE": PX_MAX_LOSER_PRICE,
        "PX_MAX_SPREAD": PX_MAX_SPREAD,
        "PX_MIN_BOOK_LEVELS": PX_MIN_BOOK_LEVELS,
        "PX_COOLDOWN_SEC": PX_COOLDOWN_SEC,
        "PX_BASE_DOLLARS": PX_BASE_DOLLARS,
        "PX_STRONG_DOLLARS": PX_STRONG_DOLLARS,
        "PX_MOMENTUM_WINDOW": PX_MOMENTUM_WINDOW,
        "PX_MOMENTUM_MIN_BPS": PX_MOMENTUM_MIN_BPS,
        "PX_VS_SHORT_WINDOW": PX_VS_SHORT_WINDOW,
        "PX_VS_LONG_WINDOW": PX_VS_LONG_WINDOW,
        "PX_VS_MIN_RATIO": PX_VS_MIN_RATIO,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
    "on_raw_tick": on_raw_tick,
}
