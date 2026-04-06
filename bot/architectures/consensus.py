"""
Architecture: consensus (Multi-Architecture Consensus)
Thesis: Individual architectures are regime-sensitive. But when MULTIPLE independent
architectures agree on the same direction in the same window, conviction is much higher.
Each architecture uses different data sources and logic, so agreement = genuine signal.

Runs signal logic from flow_front, cross_pressure, impulse_lag, and certainty_premium
internally. Only executes when N or more agree on direction within the same window.

This is regime-adaptive: in choppy markets, architectures disagree → fewer trades.
In trending markets, they agree → more trades. Natural regime filter.
"""

import time
import math
import threading
import json as _json
from collections import deque

# ═══ Exchange price feeds (shared across sub-signals) ═══
_exchange_prices = {
    "binance": deque(maxlen=60),
    "coinbase": deque(maxlen=60),
    "bybit": deque(maxlen=60),
}
_lock = threading.Lock()
_threads_started = False

COMBO_PARAMS = [
    {"name": "CON_3of4", "btc_threshold_bp": 0, "lookback_s": 0, "min_agree": 3},
    {"name": "CON_2of4", "btc_threshold_bp": 0, "lookback_s": 0, "min_agree": 2},
    {"name": "CON_4of4", "btc_threshold_bp": 0, "lookback_s": 0, "min_agree": 4},
]
_combo_config = {p["name"]: p for p in COMBO_PARAMS}

_last_status = [0.0]
_last_signal_time = [0.0]

# ═══ Normal CDF for certainty signal ═══
def _normal_cdf(x):
    if x < -8: return 0.0
    if x > 8: return 1.0
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return 0.5 + sign * (0.5 - d * math.exp(-0.5 * x * x) * poly)


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
    if _threads_started: return
    _threads_started = True
    for target, name in [(_coinbase_feed, "coinbase"), (_bybit_feed, "bybit")]:
        t = threading.Thread(target=target, name="con-" + name, daemon=True)
        t.start()
    print("  [CON] Started Coinbase + Bybit feeds")


def _get_momentum(exchange, window_sec=5):
    with _lock:
        prices = list(_exchange_prices[exchange])
    if len(prices) < 2: return None
    now = time.time()
    old = None
    for ts, px in prices:
        if ts >= now - window_sec - 1:
            old = px; break
    if old is None or old <= 0: return None
    return (prices[-1][1] - old) / old * 10000


def on_tick(state, price, ts):
    _start_feeds()
    with _lock:
        _exchange_prices["binance"].append((time.time(), price))


def check_signals(state, now_s):
    """Consensus: run 4 independent signal checks, trade when N agree."""
    import bot.paper_trade_v2 as engine

    if not state.window_active: return
    if time.time() < state.cooldown_until: return
    if not state.book.bids or not state.book.asks: return
    if len(state.book.bids) < 2 or len(state.book.asks) < 2: return
    if state.book.spread > 0.04: return

    time_remaining = state.window_end - time.time()
    if time_remaining < 5 or time_remaining > 295: return
    if not state.window_open or state.window_open <= 0: return

    price_now = state.binance_price
    if price_now is None: return

    # Book freshness
    book_age_ms = (time.time() - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500): return

    # Cooldown between signals (one per window handled by combo check, but also per-arch cooldown)
    now = time.time()
    if now - _last_signal_time[0] < 5: return

    btc_corrected = price_now - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000 if state.window_open > 0 else 0

    # ═══ Signal 1: Impulse (from price_buffer) ═══
    impulse_vote = None
    if len(state.price_buffer) >= 5:
        # 10-second lookback, 3bp threshold (G_3bp_10s logic)
        lookback_ts = now_s - 10
        price_ago = engine.find_price_at(state.price_buffer, lookback_ts)
        if price_ago and price_ago > 0:
            imp_bps = (price_now - price_ago) / price_ago * 10000
            if imp_bps >= 3: impulse_vote = "YES"
            elif imp_bps <= -3: impulse_vote = "NO"

    # ═══ Signal 2: Cross-exchange momentum ═══
    cross_vote = None
    mom_bn = _get_momentum("binance", 5)
    mom_cb = _get_momentum("coinbase", 5)
    mom_by = _get_momentum("bybit", 5)
    up_count = sum(1 for m in [mom_bn, mom_cb, mom_by] if m is not None and m >= 2.0)
    dn_count = sum(1 for m in [mom_bn, mom_cb, mom_by] if m is not None and m <= -2.0)
    if up_count >= 2: cross_vote = "YES"
    elif dn_count >= 2: cross_vote = "NO"

    # ═══ Signal 3: Flow cluster (rapid 2s price change) ═══
    flow_vote = None
    if len(state.price_buffer) >= 5:
        recent_2s = [(t, p) for t, p in state.price_buffer if now_s - t <= 2]
        recent_30s = [(t, p) for t, p in state.price_buffer if now_s - t <= 30]
        if len(recent_2s) >= 2 and len(recent_30s) >= 5:
            move_2s = abs(price_now - recent_2s[0][1])
            avg_moves = []
            for i in range(1, len(recent_30s)):
                dt = recent_30s[i][0] - recent_30s[i-1][0]
                if dt > 0:
                    avg_moves.append(abs(recent_30s[i][1] - recent_30s[i-1][1]) * 2 / dt)
            if avg_moves:
                avg_move = sum(avg_moves) / len(avg_moves)
                if avg_move > 0 and move_2s / avg_move >= 3.0:
                    if price_now > recent_2s[0][1]: flow_vote = "YES"
                    else: flow_vote = "NO"

    # ═══ Signal 4: Certainty/z-score (if near settlement) ═══
    certainty_vote = None
    if time_remaining <= 120:
        # Simple vol estimate from price_buffer
        prices_60s = [(t, p) for t, p in state.price_buffer if now_s - t <= 60]
        if len(prices_60s) >= 10:
            rets = []
            for i in range(1, len(prices_60s)):
                dt = prices_60s[i][0] - prices_60s[i-1][0]
                if dt > 0:
                    r = math.log(prices_60s[i][1] / prices_60s[i-1][1])
                    rets.append(r / math.sqrt(dt))
            if len(rets) >= 5:
                mean_r = sum(rets) / len(rets)
                var_r = sum((r - mean_r)**2 for r in rets) / (len(rets) - 1)
                vol = max(0.5, math.sqrt(var_r) * price_now) if var_r > 0 else 0.5
                uncertainty = vol * math.sqrt(time_remaining)
                if uncertainty > 0:
                    z = (btc_corrected - state.window_open) / uncertainty
                    if z > 1.5: certainty_vote = "YES"
                    elif z < -1.5: certainty_vote = "NO"

    # ═══ Count votes ═══
    votes = [impulse_vote, cross_vote, flow_vote, certainty_vote]
    yes_votes = sum(1 for v in votes if v == "YES")
    no_votes = sum(1 for v in votes if v == "NO")
    vote_names = []
    for name, v in [("IMP", impulse_vote), ("XEX", cross_vote), ("FLO", flow_vote), ("CER", certainty_vote)]:
        if v: vote_names.append("{}:{}".format(name, v[0]))

    # Status
    if now - _last_status[0] >= 15:
        print("  {}{}{} {}T-{:>3.0f}s{} | votes: {} YES={} NO={} [{}]".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            sum(1 for v in votes if v), yes_votes, no_votes, " ".join(vote_names) or "none"))
        _last_status[0] = now

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_agree = cfg.get("min_agree", 3)

        direction = None
        agree_count = 0
        if yes_votes >= min_agree:
            direction = "YES"
            agree_count = yes_votes
        elif no_votes >= min_agree:
            direction = "NO"
            agree_count = no_votes

        if direction is None:
            continue

        entry = state.book.best_ask if direction == "YES" else state.book.best_bid
        if entry < 0.15 or entry > 0.85:
            continue

        print("  [CON] FIRE {} {} agree={}/4 [{}] T-{:.0f}s".format(
            combo.name, direction, agree_count, " ".join(vote_names), time_remaining))
        _last_signal_time[0] = now
        engine.execute_paper_trade(combo, direction, agree_count, time_remaining, entry)
        break


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "consensus",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
