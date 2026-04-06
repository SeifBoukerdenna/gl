"""
Architecture: cross_pressure (Cross-Exchange Pressure)
Thesis: When Binance, Coinbase, AND Bybit all show simultaneous buy/sell pressure,
the move is institutional and more likely to sustain through the 5-minute window.
Single-exchange impulses are noise; triple-exchange agreement is signal.

PM market makers watch Binance spot. They do NOT watch composite pressure across
3+ exchanges. When all three agree, we have information asymmetry — our signal
is higher conviction than what PM's book reflects.

Implementation:
- Background threads connect to Coinbase and Bybit WS feeds
- on_tick records Binance prices (already provided by engine)
- check_signals computes 5-second momentum on each exchange
- Fires when all 3 show same-direction momentum exceeding threshold
"""

import time
import math
import threading
import json as _json
from collections import deque

# ═══ Parameters ═══
CP_MOMENTUM_WINDOW = 5       # seconds to measure momentum
CP_MIN_EXCHANGES = 3         # require N exchanges to agree (2 or 3)
CP_MIN_MOMENTUM_BPS = 2.0    # minimum per-exchange momentum in basis points
CP_COMPOSITE_MIN_BPS = 5.0   # minimum combined momentum across all exchanges
CP_COOLDOWN_SEC = 10         # seconds between signals
CP_MIN_TIME_REMAINING = 10
CP_MAX_TIME_REMAINING = 290
CP_MAX_SPREAD = 0.04
CP_MIN_BOOK_LEVELS = 2
CP_MIN_ENTRY_PRICE = 0.15
CP_MAX_ENTRY_PRICE = 0.85

COMBO_PARAMS = [
    {"name": "XP_3ex_5bp", "btc_threshold_bp": 0, "lookback_s": 0,
     "min_exchanges": 3, "composite_min": 5.0, "per_exchange_min": 2.0},
    {"name": "XP_3ex_3bp", "btc_threshold_bp": 0, "lookback_s": 0,
     "min_exchanges": 3, "composite_min": 3.0, "per_exchange_min": 1.0},
    {"name": "XP_2ex_5bp", "btc_threshold_bp": 0, "lookback_s": 0,
     "min_exchanges": 2, "composite_min": 5.0, "per_exchange_min": 2.0},
    {"name": "XP_2ex_8bp", "btc_threshold_bp": 0, "lookback_s": 0,
     "min_exchanges": 2, "composite_min": 8.0, "per_exchange_min": 3.0},
]
_combo_config = {p["name"]: p for p in COMBO_PARAMS}

# ═══ Exchange price tracking ═══
_exchange_prices = {
    "binance": deque(maxlen=120),   # (timestamp, price)
    "coinbase": deque(maxlen=120),
    "bybit": deque(maxlen=120),
}
_lock = threading.Lock()
_threads_started = False
_last_signal_time = 0
_last_status = [0.0]


def _coinbase_feed():
    """Background thread: Coinbase BTC-USD ticker via WebSocket."""
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
                        price = float(data["price"])
                        with _lock:
                            _exchange_prices["coinbase"].append((time.time(), price))
        except Exception:
            time.sleep(5)


def _bybit_feed():
    """Background thread: Bybit BTCUSDT ticker via WebSocket."""
    import websockets.sync.client as ws_sync
    while True:
        try:
            with ws_sync.connect("wss://stream.bybit.com/v5/public/spot") as ws:
                ws.send(_json.dumps({
                    "op": "subscribe",
                    "args": ["tickers.BTCUSDT"]
                }))
                for msg in ws:
                    data = _json.loads(msg)
                    if data.get("topic") == "tickers.BTCUSDT":
                        info = data.get("data", {})
                        price = float(info.get("lastPrice", 0))
                        if price > 0:
                            with _lock:
                                _exchange_prices["bybit"].append((time.time(), price))
        except Exception:
            time.sleep(5)


def _start_feeds():
    """Start background threads for Coinbase and Bybit feeds."""
    global _threads_started
    if _threads_started:
        return
    _threads_started = True
    for target, name in [(_coinbase_feed, "coinbase"), (_bybit_feed, "bybit")]:
        t = threading.Thread(target=target, name="xp-" + name, daemon=True)
        t.start()
    print("  [XP] Started Coinbase + Bybit feeds")


def _get_momentum(exchange, window_sec=5):
    """Get price momentum in basis points over the last N seconds."""
    with _lock:
        prices = list(_exchange_prices[exchange])
    if len(prices) < 2:
        return None
    now = time.time()
    # Find price at (now - window_sec)
    old_price = None
    for ts, px in prices:
        if ts >= now - window_sec - 1:
            old_price = px
            break
    if old_price is None or old_price <= 0:
        return None
    current_price = prices[-1][1]
    return (current_price - old_price) / old_price * 10000  # basis points


def on_tick(state, price, ts):
    """Record Binance price and ensure extra feeds are running."""
    _start_feeds()
    with _lock:
        _exchange_prices["binance"].append((time.time(), price))


def check_signals(state, now_s):
    """Cross-exchange pressure detection."""
    import bot.paper_trade_v2 as engine
    global _last_signal_time

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
    if time_remaining < CP_MIN_TIME_REMAINING or time_remaining > CP_MAX_TIME_REMAINING:
        return

    if not state.window_open or state.window_open <= 0:
        return

    # Cooldown
    now = time.time()
    if now - _last_signal_time < CP_COOLDOWN_SEC:
        return

    # Book freshness
    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > engine.MAX_BOOK_AGE_MS:
        return

    # Get momentum from each exchange
    mom_binance = _get_momentum("binance", CP_MOMENTUM_WINDOW)
    mom_coinbase = _get_momentum("coinbase", CP_MOMENTUM_WINDOW)
    mom_bybit = _get_momentum("bybit", CP_MOMENTUM_WINDOW)

    momentums = {}
    if mom_binance is not None:
        momentums["BN"] = mom_binance
    if mom_coinbase is not None:
        momentums["CB"] = mom_coinbase
    if mom_bybit is not None:
        momentums["BY"] = mom_bybit

    # Status line
    if now - _last_status[0] >= 15:
        mom_str = " | ".join("{} {:+.1f}bp".format(k, v) for k, v in momentums.items())
        print("  {}{}{} {}T-{:>3.0f}s{} | exchanges: {} | {}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            len(momentums), mom_str or "waiting for feeds"))
        _last_status[0] = now

    if len(momentums) < 2:
        return  # need at least 2 exchanges

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_ex = cfg.get("min_exchanges", 3)
        composite_min = cfg.get("composite_min", 5.0)
        per_ex_min = cfg.get("per_exchange_min", 2.0)

        if len(momentums) < min_ex:
            continue

        # Count exchanges agreeing on direction
        up_count = sum(1 for v in momentums.values() if v >= per_ex_min)
        down_count = sum(1 for v in momentums.values() if v <= -per_ex_min)

        composite_up = sum(v for v in momentums.values() if v > 0)
        composite_down = sum(v for v in momentums.values() if v < 0)

        direction = None
        composite = 0

        if up_count >= min_ex and composite_up >= composite_min:
            direction = "YES"
            composite = composite_up
        elif down_count >= min_ex and abs(composite_down) >= composite_min:
            direction = "NO"
            composite = abs(composite_down)

        if direction is None:
            continue

        entry = state.book.best_ask if direction == "YES" else state.book.best_bid
        _min_entry = getattr(engine, 'CP_MIN_ENTRY_PRICE', CP_MIN_ENTRY_PRICE)
        _max_entry = getattr(engine, 'CP_MAX_ENTRY_PRICE', CP_MAX_ENTRY_PRICE)
        if entry < _min_entry or entry > _max_entry:
            continue

        mom_str = "+".join("{}{:+.1f}".format(k, v) for k, v in sorted(momentums.items()))
        print("  [XP] FIRE {} {} composite={:.1f}bp exchanges={}/{} [{}] T-{:.0f}s".format(
            combo.name, direction, composite, up_count if direction == "YES" else down_count,
            len(momentums), mom_str, time_remaining))

        _last_signal_time = now
        engine.execute_paper_trade(combo, direction, composite, time_remaining, entry)
        break


def on_window_start(state):
    _last_status[0] = 0.0
    global _last_signal_time
    _last_signal_time = 0


ARCH_SPEC = {
    "name": "cross_pressure",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "CP_MIN_ENTRY_PRICE": CP_MIN_ENTRY_PRICE,
        "CP_MAX_ENTRY_PRICE": CP_MAX_ENTRY_PRICE,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
