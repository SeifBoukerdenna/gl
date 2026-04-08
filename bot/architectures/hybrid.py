"""
Architecture: hybrid (Impulse Confirmed + Certainty Premium Merged)

The two proven profitable architectures combined into one session that
trades the ENTIRE window with BIG sizing:

  T-295 to T-60: Impulse Confirmed logic (proven +$5.7/trade, 66% WR)
    - Binance impulse with multi-exchange confirmation
    - Multiple lookback windows: 3s, 5s, 10s, 15s, 30s (fires on whichever hits first)
    - Both directions (IC is positive on YES AND NO)
    - NO dead zone (IC data shows dead zone hurts more than helps)

  T-60 to T-5: Certainty Premium logic (proven +$3.9/trade, 93% WR)
    - z-score based, buy near-certain winner at discount
    - Both directions

Max 2 trades per window (one per phase). Big sizing on both.
NO filters. NO gates. NO YES-only. BOTH directions.
Just the raw proven signals, scaled up.
"""

import time
import math
import threading
import json as _json
from collections import deque

from bot.shared.volatility import vol_tracker

# ═══ Impulse Confirmed params ═══
HY_IC_CONFIRM_BPS = 1.0
HY_IC_MAX_IMPULSE = 25
HY_IC_MIN_THRESHOLD = 3    # minimum impulse in bps
HY_IC_LOOKBACKS = [3, 5, 10, 15, 30]  # try multiple lookbacks, fire on first hit
HY_IC_COOLDOWN = 8

# ═══ Certainty Premium params ═══
HY_CP_MIN_Z = 1.5
HY_CP_MAX_WINNER = 0.95
HY_CP_MIN_WINNER = 0.80

# ═══ General ═══
HY_MAX_SPREAD = 0.03
HY_MIN_BOOK_LEVELS = 2
HY_MIN_ENTRY = 0.15
HY_MAX_ENTRY = 0.85
HY_PHASE_SPLIT = 60        # last 60s = certainty phase
HY_BASE_DOLLARS = 250
HY_CP_DOLLARS = 200        # certainty phase sizing

COMBO_PARAMS = [
    {"name": "HY_impulse",   "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "HY_certainty", "btc_threshold_bp": 0, "lookback_s": 0},
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
        t = threading.Thread(target=target, name="hy-" + name, daemon=True)
        t.start()
    print("  [HY] Started Coinbase + Bybit feeds")


def _get_momentum(exchange, window_sec):
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


def on_tick(state, price, ts):
    _start_feeds()
    with _lock:
        _exchange_prices["binance"].append((time.time(), price))
    vol_tracker.record_tick(price, ts)


def check_signals(state, now_s):
    """Impulse confirmed (full window) + certainty premium (last 60s)."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    min_levels = getattr(engine, 'HY_MIN_BOOK_LEVELS', HY_MIN_BOOK_LEVELS)
    if len(state.book.bids) < min_levels or len(state.book.asks) < min_levels:
        return
    if state.book.spread > getattr(engine, 'HY_MAX_SPREAD', HY_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < 5 or time_remaining > 295:
        return

    if state.binance_price is None:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'HY_IC_COOLDOWN', HY_IC_COOLDOWN):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    phase_split = getattr(engine, 'HY_PHASE_SPLIT', HY_PHASE_SPLIT)

    # Status
    if now - _last_status[0] >= 15:
        btc = state.binance_price - state.offset
        delta = (btc - state.window_open) / state.window_open * 10000 if state.window_open else 0
        dc = engine.G if delta > 0 else engine.R if delta < 0 else engine.RST
        mom_cb = _get_momentum("coinbase", 5)
        mom_by = _get_momentum("bybit", 5)
        phase = "IC" if time_remaining > phase_split else "CP"
        cb_str = "{:+.1f}".format(mom_cb) if mom_cb is not None else "n/a"
        by_str = "{:+.1f}".format(mom_by) if mom_by is not None else "n/a"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC ${:,.2f} {}{:+.1f}bp{} | CB:{} BY:{} | {}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            btc, dc, delta, engine.RST, cb_str, by_str, phase))
        _last_status[0] = now

    # ═══ IMPULSE CONFIRMED PHASE (T-295 to T-60) ═══
    if time_remaining > phase_split:
        combo = next((c for c in state.combos if c.name == "HY_impulse"), None)
        if combo and not combo.has_position_in_window(state.window_start):
            _check_impulse_phase(state, now_s, time_remaining, now, combo, engine)
        return

    # ═══ CERTAINTY PREMIUM PHASE (T-60 to T-5) ═══
    combo = next((c for c in state.combos if c.name == "HY_certainty"), None)
    if combo and not combo.has_position_in_window(state.window_start):
        _check_certainty_phase(state, time_remaining, now, combo, engine)


def _check_impulse_phase(state, now_s, time_remaining, now, combo, engine):
    """Multi-lookback impulse with exchange confirmation."""
    price_now = state.binance_price
    if price_now is None or len(state.price_buffer) < 5:
        return

    threshold = getattr(engine, 'HY_IC_MIN_THRESHOLD', HY_IC_MIN_THRESHOLD)
    max_imp = getattr(engine, 'HY_IC_MAX_IMPULSE', HY_IC_MAX_IMPULSE)
    confirm_bps = getattr(engine, 'HY_IC_CONFIRM_BPS', HY_IC_CONFIRM_BPS)
    min_entry = getattr(engine, 'HY_MIN_ENTRY', HY_MIN_ENTRY)
    max_entry = getattr(engine, 'HY_MAX_ENTRY', HY_MAX_ENTRY)

    # Try multiple lookbacks — fire on the first that passes
    for lookback in HY_IC_LOOKBACKS:
        price_ago = engine.find_price_at(state.price_buffer, now_s - lookback)
        if price_ago is None or price_ago <= 0:
            continue

        impulse = (price_now - price_ago) / price_ago * 10000
        if abs(impulse) < threshold or abs(impulse) > max_imp:
            continue

        direction = "YES" if impulse > 0 else "NO"

        # Multi-exchange confirmation
        mom_cb = _get_momentum("coinbase", min(lookback, 5))
        mom_by = _get_momentum("bybit", min(lookback, 5))

        confirms = 0
        conf_str = []
        if mom_cb is not None:
            if (direction == "YES" and mom_cb >= confirm_bps) or \
               (direction == "NO" and mom_cb <= -confirm_bps):
                confirms += 1
                conf_str.append("CB{:+.1f}".format(mom_cb))
        if mom_by is not None:
            if (direction == "YES" and mom_by >= confirm_bps) or \
               (direction == "NO" and mom_by <= -confirm_bps):
                confirms += 1
                conf_str.append("BY{:+.1f}".format(mom_by))

        if confirms < 1:
            continue

        entry = state.book.best_ask if direction == "YES" else state.book.best_bid
        if entry < min_entry or entry > max_entry:
            continue

        dollars = getattr(engine, 'HY_BASE_DOLLARS', HY_BASE_DOLLARS)
        if confirms >= 2:
            dollars = int(dollars * 1.5)

        print("  {}[HY] IC {} {:.1f}bp/{}s {}ex [{}] @{:.0f}c ${} T-{:.0f}s{}".format(
            engine.G if direction == "YES" else engine.R,
            direction, impulse, lookback, confirms,
            "+".join(conf_str), entry*100, dollars, time_remaining, engine.RST))

        _last_signal_time[0] = now
        engine.execute_paper_trade(combo, direction, abs(impulse), time_remaining,
                                   entry, override_dollars=dollars)
        return  # fired, done


def _check_certainty_phase(state, time_remaining, now, combo, engine):
    """Near-certain outcome at discount."""
    if not state.window_open or state.window_open <= 0:
        return

    btc_corrected = (state.binance_price or 0) - state.offset
    strike = state.window_open

    z = vol_tracker.get_z_score(btc_corrected, strike, time_remaining)
    abs_z = abs(z)
    min_z = getattr(engine, 'HY_CP_MIN_Z', HY_CP_MIN_Z)

    if abs_z < min_z:
        return

    if z > 0:
        direction = "YES"
        winner_cost = state.book.best_ask
    else:
        direction = "NO"
        winner_cost = 1.0 - state.book.best_bid

    max_wp = getattr(engine, 'HY_CP_MAX_WINNER', HY_CP_MAX_WINNER)
    min_wp = getattr(engine, 'HY_CP_MIN_WINNER', HY_CP_MIN_WINNER)
    if winner_cost > max_wp or winner_cost < min_wp:
        return

    entry = state.book.best_ask if direction == "YES" else state.book.best_bid
    dollars = getattr(engine, 'HY_CP_DOLLARS', HY_CP_DOLLARS)
    if abs_z >= 2.0:
        dollars = int(dollars * 1.5)

    print("  {}[HY] CP {} z={:.1f} @{:.0f}c ${} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        direction, z, entry*100, dollars, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, abs_z, time_remaining,
                               entry, override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "hybrid",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "HY_IC_CONFIRM_BPS": HY_IC_CONFIRM_BPS,
        "HY_IC_MAX_IMPULSE": HY_IC_MAX_IMPULSE,
        "HY_IC_MIN_THRESHOLD": HY_IC_MIN_THRESHOLD,
        "HY_IC_COOLDOWN": HY_IC_COOLDOWN,
        "HY_CP_MIN_Z": HY_CP_MIN_Z,
        "HY_CP_MAX_WINNER": HY_CP_MAX_WINNER,
        "HY_CP_MIN_WINNER": HY_CP_MIN_WINNER,
        "HY_MAX_SPREAD": HY_MAX_SPREAD,
        "HY_MIN_BOOK_LEVELS": HY_MIN_BOOK_LEVELS,
        "HY_MIN_ENTRY": HY_MIN_ENTRY,
        "HY_MAX_ENTRY": HY_MAX_ENTRY,
        "HY_PHASE_SPLIT": HY_PHASE_SPLIT,
        "HY_BASE_DOLLARS": HY_BASE_DOLLARS,
        "HY_CP_DOLLARS": HY_CP_DOLLARS,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
