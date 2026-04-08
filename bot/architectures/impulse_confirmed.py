"""
Architecture: impulse_confirmed (Multi-Exchange Confirmed Impulse)
Thesis: Same as impulse_lag (Binance leads PM by 2-10s), but with a confirmation
gate: at least 1-2 other exchanges (Coinbase, Bybit) must show the same direction
momentum. This filters out Binance-only noise and only trades when the move is
real across venues.

Single-exchange impulse = could be a single large order, might reverse.
Multi-exchange impulse = institutional flow, more likely to sustain.

Uses the same combo parameters as impulse_lag (threshold + lookback) plus
a confirmation requirement from additional exchanges.
"""

import time
import threading
import json as _json
from collections import deque

# Reuse impulse_lag combo params
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

# Confirmation parameters
IC_MIN_CONFIRM_EXCHANGES = 1   # need at least N other exchanges to confirm
IC_CONFIRM_WINDOW_SEC = 5      # look at momentum over last N seconds
IC_CONFIRM_MIN_BPS = 1.0       # other exchange must show >= Nbp same direction

_DEFAULTS = {
    "MAX_IMPULSE_BP": 25,
    "DEAD_ZONE_START": 90,
    "DEAD_ZONE_END": 210,
    "MAX_SPREAD": 0.03,
    "MIN_BOOK_LEVELS": 3,
    "MIN_ENTRY_PRICE": 0.20,
    "MAX_ENTRY_PRICE": 0.80,
    "PRINT_STATUS_INTERVAL": 15,
    "IC_MIN_CONFIRM_EXCHANGES": IC_MIN_CONFIRM_EXCHANGES,
    "IC_CONFIRM_MIN_BPS": IC_CONFIRM_MIN_BPS,
}

# ═══ Exchange feeds (shared with cross_pressure if both loaded) ═══
_exchange_prices = {
    "coinbase": deque(maxlen=120),
    "bybit": deque(maxlen=120),
}
_lock = threading.Lock()
_threads_started = False
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
                            _exchange_prices["coinbase"].append((time.time(), float(data["price"])))
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
        t = threading.Thread(target=target, name="ic-" + name, daemon=True)
        t.start()
    print("  [IC] Started Coinbase + Bybit confirmation feeds")


def _get_exchange_momentum(exchange, window_sec=5):
    """Get momentum in bps over last N seconds for a given exchange."""
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
    current = prices[-1][1]
    return (current - old_price) / old_price * 10000


def on_tick(state, price, ts):
    _start_feeds()


def check_signals(state, now_s):
    """Impulse detection (same as impulse_lag) + multi-exchange confirmation gate."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    _min_levels = getattr(engine, 'MIN_BOOK_LEVELS', 3)
    if len(state.book.bids) < _min_levels or len(state.book.asks) < _min_levels:
        return
    if state.book.spread > getattr(engine, 'MAX_SPREAD', 0.03):
        return
    if len(state.price_buffer) < 5:
        return

    time_remaining = state.window_end - time.time()
    _buf_end = getattr(engine, 'WINDOW_BUFFER_END', 5)
    _buf_start = getattr(engine, 'WINDOW_BUFFER_START', 3)
    if time_remaining < _buf_end or time_remaining > (300 - _buf_start):
        return

    # Book freshness
    book_age_ms = (time.time() - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    price_now = state.binance_price
    if price_now is None:
        return

    # Get confirmation status from other exchanges
    mom_cb = _get_exchange_momentum("coinbase", IC_CONFIRM_WINDOW_SEC)
    mom_by = _get_exchange_momentum("bybit", IC_CONFIRM_WINDOW_SEC)

    # Status
    now_t = time.time()
    if now_t - _last_status[0] >= getattr(engine, 'PRINT_STATUS_INTERVAL', 15):
        btc = price_now
        corrected = btc - state.offset
        delta = 0
        if state.window_open and state.window_open > 0:
            delta = (corrected - state.window_open) / state.window_open * 10000
        dc = engine.G if delta > 0 else engine.R if delta < 0 else engine.RST
        cb_str = "{:+.1f}bp".format(mom_cb) if mom_cb is not None else "n/a"
        by_str = "{:+.1f}bp".format(mom_by) if mom_by is not None else "n/a"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC ${:,.2f} {}{:+.1f}bp{} | CB:{} BY:{} | Up:{:.0f}c Dn:{:.0f}c".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            corrected, dc, delta, engine.RST, cb_str, by_str,
            state.book.best_ask * 100, (1 - state.book.best_bid) * 100))
        _last_status[0] = now_t

    _min_entry = getattr(engine, 'MIN_ENTRY_PRICE', 0.20)
    _max_entry = getattr(engine, 'MAX_ENTRY_PRICE', 0.80)
    _max_impulse = getattr(engine, 'MAX_IMPULSE_BP', 25)
    _dz_start = getattr(engine, 'DEAD_ZONE_START', 90)
    _dz_end = getattr(engine, 'DEAD_ZONE_END', 210)
    _min_confirm = getattr(engine, 'IC_MIN_CONFIRM_EXCHANGES', IC_MIN_CONFIRM_EXCHANGES)
    _confirm_bps = getattr(engine, 'IC_CONFIRM_MIN_BPS', IC_CONFIRM_MIN_BPS)

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
        if abs(impulse_bps) > _max_impulse:
            continue

        # F2: Entry price range
        if entry_price < _min_entry or entry_price > _max_entry:
            continue

        # F3: Dead zone
        if _dz_start <= time_remaining <= _dz_end:
            continue

        # F4: MULTI-EXCHANGE CONFIRMATION — the key difference from impulse_lag
        confirm_count = 0
        confirms = []
        if mom_cb is not None:
            if direction == "YES" and mom_cb >= _confirm_bps:
                confirm_count += 1
                confirms.append("CB{:+.1f}".format(mom_cb))
            elif direction == "NO" and mom_cb <= -_confirm_bps:
                confirm_count += 1
                confirms.append("CB{:+.1f}".format(mom_cb))
        if mom_by is not None:
            if direction == "YES" and mom_by >= _confirm_bps:
                confirm_count += 1
                confirms.append("BY{:+.1f}".format(mom_by))
            elif direction == "NO" and mom_by <= -_confirm_bps:
                confirm_count += 1
                confirms.append("BY{:+.1f}".format(mom_by))

        if confirm_count < _min_confirm:
            continue  # not enough exchanges confirm — skip

        # Dynamic sizing: 1.5x when 2 exchanges confirm
        _dollars = None
        if confirm_count >= 2:
            _dollars = int(getattr(engine, 'BASE_TRADE_DOLLARS', 100) * 1.5)

        print("  [IC] FIRE {} {} {:.1f}bp confirmed by {} [{}] ${}T-{:.0f}s".format(
            combo.name, direction, impulse_bps, confirm_count, "+".join(confirms),
            str(_dollars) + " " if _dollars else "", time_remaining))
        engine.execute_paper_trade(combo, direction, impulse_bps, time_remaining, entry_price,
                                   override_dollars=_dollars)


ARCH_SPEC = {
    "name": "impulse_confirmed",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": _DEFAULTS,
    "on_window_start": None,
    "on_window_end": None,
    "on_tick": on_tick,
}
