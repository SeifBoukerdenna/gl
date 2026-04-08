"""
Architecture: accel (Momentum Acceleration Detector)

Thesis: A move that is ACCELERATING is more likely to sustain through a 5-minute
window than a move that already happened. Most architectures detect "BTC moved X bp"
(reactive). This one detects "BTC is moving FASTER NOW than it was 5 seconds ago"
(predictive of continuation).

Checks momentum across 3 timeframes simultaneously:
  - Fast:  2-second momentum  (instant impulse)
  - Mid:   5-second momentum  (short trend)
  - Slow:  15-second momentum (confirmed direction)

Signal fires when:
  1. All 3 timeframes agree on direction
  2. Fast > Mid > 0 (accelerating — move is getting stronger)
  3. At least 1 other exchange (Coinbase/Bybit) confirms direction
  4. Each timeframe's momentum exceeds its vol-normalized threshold
  5. Book is stale enough (PM hasn't repriced yet)

Why this works: PM MMs reprice to CURRENT price, not to price ACCELERATION.
When BTC is accelerating upward, the PM book reflects where BTC IS, not where
it's GOING. The acceleration tells us the move isn't done yet.
"""

import time
import math
import threading
import json as _json
from collections import deque

from bot.shared.volatility import vol_tracker

# ═══ Parameters ═══
AC_FAST_WINDOW = 2          # seconds
AC_MID_WINDOW = 5
AC_SLOW_WINDOW = 15
AC_MIN_SIGMA = 1.0          # minimum sigma threshold per timeframe
AC_ACCEL_RATIO = 1.2        # fast momentum must be >= 1.2x mid momentum
AC_CONFIRM_MIN_BPS = 0.5    # other exchange must show >= 0.5bp same direction
AC_COOLDOWN_SEC = 10
AC_MAX_SPREAD = 0.04
AC_MIN_BOOK_LEVELS = 2
AC_MIN_ENTRY_PRICE = 0.30
AC_MAX_ENTRY_PRICE = 0.80
AC_MIN_TIME_REMAINING = 10
AC_MAX_TIME_REMAINING = 290
AC_PHASE1_END = 210         # only trade T-295 to T-210 (momentum phase)

COMBO_PARAMS = [
    {"name": "AC_0.7sig",  "btc_threshold_bp": 0, "lookback_s": 0,
     "min_sigma": 0.7, "accel_ratio": 1.0},
    {"name": "AC_1.0sig",  "btc_threshold_bp": 0, "lookback_s": 0,
     "min_sigma": 1.0, "accel_ratio": 1.2},
]
_combo_config = {p["name"]: p for p in COMBO_PARAMS}

# ═══ Exchange feeds ═══
_exchange_prices = {
    "binance":  deque(maxlen=300),  # 5 ticks/sec * 60s
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
        t = threading.Thread(target=target, name="ac-" + name, daemon=True)
        t.start()
    print("  [AC] Started Coinbase + Bybit feeds")


def _get_momentum(exchange, window_sec):
    """Price momentum in bps over last N seconds."""
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


def _get_sigma_bps(state, seconds):
    """Vol-normalized expected move in bps for a time window."""
    sigma = vol_tracker.get_sigma()
    if sigma and state.binance_price and state.binance_price > 0:
        return max((sigma * math.sqrt(max(seconds, 0.1)) / state.binance_price) * 10000, 0.3)
    return 3.0


def _get_regime_multiplier(state):
    """Chop detection from recent window outcomes."""
    outcomes = list(getattr(state, 'recent_outcomes', []))
    if len(outcomes) < 4:
        return 1.0
    alternations = sum(1 for i in range(1, len(outcomes)) if outcomes[i] != outcomes[i - 1])
    alt_rate = alternations / (len(outcomes) - 1)
    if alt_rate <= 0.35:
        return 1.0
    if alt_rate >= 0.80:
        return 2.0
    return 1.0 + (alt_rate - 0.35) / 0.45


# ═══ Hooks ═══

def on_tick(state, price, ts):
    _start_feeds()
    with _lock:
        _exchange_prices["binance"].append((time.time(), price))
    vol_tracker.record_tick(price, ts)


def check_signals(state, now_s):
    """Detect momentum acceleration across 3 timeframes."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    min_levels = getattr(engine, 'AC_MIN_BOOK_LEVELS', AC_MIN_BOOK_LEVELS)
    if len(state.book.bids) < min_levels or len(state.book.asks) < min_levels:
        return
    if state.book.spread > getattr(engine, 'AC_MAX_SPREAD', AC_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    min_t = getattr(engine, 'AC_MIN_TIME_REMAINING', AC_MIN_TIME_REMAINING)
    phase1_end = getattr(engine, 'AC_PHASE1_END', AC_PHASE1_END)
    if time_remaining < min_t or time_remaining < phase1_end:
        return  # only trade in Phase 1 (momentum phase)
    max_t = getattr(engine, 'AC_MAX_TIME_REMAINING', AC_MAX_TIME_REMAINING)
    if time_remaining > max_t:
        return

    if not state.window_open or state.window_open <= 0:
        return
    if state.binance_price is None:
        return

    now = time.time()

    if now - _last_signal_time[0] < getattr(engine, 'AC_COOLDOWN_SEC', AC_COOLDOWN_SEC):
        return

    # Book freshness
    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    # Intra-window chop: if BTC crossed strike both ways, skip
    if state.window_crossed_above and state.window_crossed_below:
        elapsed = now - state.window_start if state.window_start else 0
        if elapsed > 60:
            return

    regime_mult = _get_regime_multiplier(state)

    # ── Compute momentum at 3 timeframes on Binance ──
    fast_window = getattr(engine, 'AC_FAST_WINDOW', AC_FAST_WINDOW)
    mid_window = getattr(engine, 'AC_MID_WINDOW', AC_MID_WINDOW)
    slow_window = getattr(engine, 'AC_SLOW_WINDOW', AC_SLOW_WINDOW)

    mom_fast = _get_momentum("binance", fast_window)
    mom_mid = _get_momentum("binance", mid_window)
    mom_slow = _get_momentum("binance", slow_window)

    if mom_fast is None or mom_mid is None or mom_slow is None:
        return

    # Status
    if now - _last_status[0] >= 15:
        sigma = vol_tracker.get_sigma()
        regime = "CHOP" if regime_mult >= 1.5 else "TREND" if regime_mult <= 1.1 else "MIX"
        print("  {}{}{} {}T-{:>3.0f}s{} | mom: {:.1f}/{:.1f}/{:.1f}bp (2/5/15s) | sig=${:.1f} {} x{:.1f}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            mom_fast, mom_mid, mom_slow, sigma, regime, regime_mult))
        _last_status[0] = now

    # ── Check each combo ──
    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_sigma = cfg.get("min_sigma", 1.0)
        accel_ratio = cfg.get("accel_ratio", 1.2)

        # 1. All 3 must agree on direction
        if not (mom_fast > 0 and mom_mid > 0 and mom_slow > 0) and \
           not (mom_fast < 0 and mom_mid < 0 and mom_slow < 0):
            continue

        direction = "YES" if mom_fast > 0 else "NO"

        # 2. Vol-normalized thresholds for each timeframe
        # Cap regime impact at 1.3x — don't choke in mixed markets
        capped_regime = min(regime_mult, 1.3)
        sigma_fast = _get_sigma_bps(state, fast_window) * min_sigma * capped_regime
        sigma_mid = _get_sigma_bps(state, mid_window) * min_sigma * capped_regime
        sigma_slow = _get_sigma_bps(state, slow_window) * min_sigma * capped_regime

        if abs(mom_fast) < sigma_fast or abs(mom_mid) < sigma_mid or abs(mom_slow) < sigma_slow:
            continue

        # 3. Acceleration: fast momentum must be stronger than mid
        if abs(mom_fast) < abs(mom_mid) * accel_ratio:
            continue

        # 4. Multi-exchange confirmation (at least 1)
        confirm_bps = getattr(engine, 'AC_CONFIRM_MIN_BPS', AC_CONFIRM_MIN_BPS)
        mom_cb = _get_momentum("coinbase", mid_window)
        mom_by = _get_momentum("bybit", mid_window)

        confirms = 0
        confirms_str = []
        if mom_cb is not None:
            if (direction == "YES" and mom_cb >= confirm_bps) or \
               (direction == "NO" and mom_cb <= -confirm_bps):
                confirms += 1
                confirms_str.append("CB{:+.1f}".format(mom_cb))
        if mom_by is not None:
            if (direction == "YES" and mom_by >= confirm_bps) or \
               (direction == "NO" and mom_by <= -confirm_bps):
                confirms += 1
                confirms_str.append("BY{:+.1f}".format(mom_by))

        if confirms < 1:
            continue

        # 5. Entry price
        entry = state.book.best_ask if direction == "YES" else state.book.best_bid
        min_entry = getattr(engine, 'AC_MIN_ENTRY_PRICE', AC_MIN_ENTRY_PRICE)
        max_entry = getattr(engine, 'AC_MAX_ENTRY_PRICE', AC_MAX_ENTRY_PRICE)
        if entry < min_entry or entry > max_entry:
            continue

        # Fire
        accel_str = "{:.1f}>{:.1f}>{:.1f}bp".format(abs(mom_fast), abs(mom_mid), abs(mom_slow))
        print("  {}[AC] FIRE {} {} accel=[{}] confirmed=[{}] T-{:.0f}s rx{:.1f}{}".format(
            engine.G if direction == "YES" else engine.R,
            combo.name, direction, accel_str,
            "+".join(confirms_str), time_remaining, regime_mult, engine.RST))

        _last_signal_time[0] = now
        engine.execute_paper_trade(combo, direction, abs(mom_fast), time_remaining, entry)
        break


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "accel",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "AC_FAST_WINDOW": AC_FAST_WINDOW,
        "AC_MID_WINDOW": AC_MID_WINDOW,
        "AC_SLOW_WINDOW": AC_SLOW_WINDOW,
        "AC_MIN_SIGMA": AC_MIN_SIGMA,
        "AC_ACCEL_RATIO": AC_ACCEL_RATIO,
        "AC_CONFIRM_MIN_BPS": AC_CONFIRM_MIN_BPS,
        "AC_COOLDOWN_SEC": AC_COOLDOWN_SEC,
        "AC_MAX_SPREAD": AC_MAX_SPREAD,
        "AC_MIN_BOOK_LEVELS": AC_MIN_BOOK_LEVELS,
        "AC_MIN_ENTRY_PRICE": AC_MIN_ENTRY_PRICE,
        "AC_MAX_ENTRY_PRICE": AC_MAX_ENTRY_PRICE,
        "AC_MIN_TIME_REMAINING": AC_MIN_TIME_REMAINING,
        "AC_MAX_TIME_REMAINING": AC_MAX_TIME_REMAINING,
        "AC_PHASE1_END": AC_PHASE1_END,
        "ALLOWED_DIRECTIONS": None,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
