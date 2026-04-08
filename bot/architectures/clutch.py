"""
Architecture: clutch (Blitz without the broken impulse phase)

Data showed blitz's 3 phases performed wildly differently:
  BZ_impulse (T-295 to T-120):  -$76.4/trade  ← BROKEN, removed
  BZ_pressure (T-120 to T-60):  +$167.6/trade ← BEST SIGNAL IN THE ENTIRE BOT
  BZ_certainty (T-60 to T-5):   +$24.8/trade  ← 100% WR

Clutch = blitz minus the garbage. Only the two phases that print:
  Phase 1 (T-120 to T-60): Cross-pressure — 3 exchanges agreeing, 80% WR
  Phase 2 (T-60 to T-5): Certainty premium — near-certain outcomes, 100% WR

Both directions. Big sizing. No impulse noise.
"""

import time
import math
import threading
import json as _json
from collections import deque

from bot.shared.volatility import vol_tracker

# ═══ Cross-pressure params (Phase 1: T-120 to T-60) ═══
CL_XP_MOMENTUM_WINDOW = 5
CL_XP_MIN_EXCHANGES = 3     # strict: all 3 must agree (proven best)
CL_XP_PER_EX_MIN = 2.0
CL_XP_COMPOSITE_MIN = 5.0

# ═══ Certainty premium params (Phase 2: T-60 to T-5) ═══
CL_CP_MIN_Z = 1.5
CL_CP_MAX_WINNER = 0.95
CL_CP_MIN_WINNER = 0.80

# ═══ General ═══
CL_MAX_SPREAD = 0.03
CL_MIN_BOOK_LEVELS = 2
CL_MIN_ENTRY = 0.15
CL_MAX_ENTRY = 0.85
CL_COOLDOWN_SEC = 8
CL_PRESSURE_DOLLARS = 300    # big sizing on the best signal
CL_CERTAINTY_DOLLARS = 200

COMBO_PARAMS = [
    {"name": "CL_pressure",  "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "CL_certainty", "btc_threshold_bp": 0, "lookback_s": 0},
]

_exchange_prices = {
    "binance":  deque(maxlen=120),
    "coinbase": deque(maxlen=120),
    "bybit":    deque(maxlen=120),
}
_lock = threading.Lock()
_threads_started = False
_last_signal_time = [0.0]
_last_status = [0.0]
_phase_traded = {}


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
        t = threading.Thread(target=target, name="cl-" + name, daemon=True)
        t.start()
    print("  [CL] Started Coinbase + Bybit feeds")


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
    """Two proven phases: cross-pressure (T-120 to T-60) + certainty (T-60 to T-5)."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    min_levels = getattr(engine, 'CL_MIN_BOOK_LEVELS', CL_MIN_BOOK_LEVELS)
    if len(state.book.bids) < min_levels or len(state.book.asks) < min_levels:
        return
    if state.book.spread > getattr(engine, 'CL_MAX_SPREAD', CL_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < 5 or time_remaining > 120:
        return  # ONLY active T-120 to T-5

    if state.binance_price is None:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'CL_COOLDOWN_SEC', CL_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    ws = state.window_start
    if ws not in _phase_traded:
        _phase_traded[ws] = set()
    traded = _phase_traded[ws]

    # Status
    if now - _last_status[0] >= 12:
        btc = state.binance_price - state.offset
        delta = (btc - state.window_open) / state.window_open * 10000 if state.window_open else 0
        dc = engine.G if delta > 0 else engine.R if delta < 0 else engine.RST
        phase = "XP" if time_remaining > 60 else "CP"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC ${:,.2f} {}{:+.1f}bp{} | {} [{}]".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            btc, dc, delta, engine.RST, phase, ",".join(traded) if traded else "none"))
        _last_status[0] = now

    min_entry = getattr(engine, 'CL_MIN_ENTRY', CL_MIN_ENTRY)
    max_entry = getattr(engine, 'CL_MAX_ENTRY', CL_MAX_ENTRY)

    # ═══ PHASE 1: Cross Pressure (T-120 to T-60) ═══
    if time_remaining > 60 and "XP" not in traded:
        window = getattr(engine, 'CL_XP_MOMENTUM_WINDOW', CL_XP_MOMENTUM_WINDOW)
        min_ex = getattr(engine, 'CL_XP_MIN_EXCHANGES', CL_XP_MIN_EXCHANGES)
        per_ex = getattr(engine, 'CL_XP_PER_EX_MIN', CL_XP_PER_EX_MIN)
        comp_min = getattr(engine, 'CL_XP_COMPOSITE_MIN', CL_XP_COMPOSITE_MIN)

        mom_bn = _get_momentum("binance", window)
        mom_cb = _get_momentum("coinbase", window)
        mom_by = _get_momentum("bybit", window)

        moms = {}
        if mom_bn is not None: moms["BN"] = mom_bn
        if mom_cb is not None: moms["CB"] = mom_cb
        if mom_by is not None: moms["BY"] = mom_by

        if len(moms) >= min_ex:
            up = sum(1 for v in moms.values() if v >= per_ex)
            dn = sum(1 for v in moms.values() if v <= -per_ex)
            comp_up = sum(v for v in moms.values() if v > 0)
            comp_dn = sum(v for v in moms.values() if v < 0)

            direction = None
            composite = 0
            if up >= min_ex and comp_up >= comp_min:
                direction = "YES"; composite = comp_up
            elif dn >= min_ex and abs(comp_dn) >= comp_min:
                direction = "NO"; composite = abs(comp_dn)

            if direction:
                entry = state.book.best_ask if direction == "YES" else state.book.best_bid
                if min_entry <= entry <= max_entry:
                    combo = next((c for c in state.combos if c.name == "CL_pressure"), None)
                    if combo and not combo.has_position_in_window(ws):
                        dollars = getattr(engine, 'CL_PRESSURE_DOLLARS', CL_PRESSURE_DOLLARS)
                        if composite >= 8.0:
                            dollars = int(dollars * 1.5)
                        mom_str = " ".join("{}{:+.1f}".format(k,v) for k,v in moms.items())
                        print("  {}[CL] XP {} {:.1f}bp [{}] @{:.0f}c ${} T-{:.0f}s{}".format(
                            engine.G if direction == "YES" else engine.R,
                            direction, composite, mom_str, entry*100, dollars,
                            time_remaining, engine.RST))
                        _last_signal_time[0] = now
                        traded.add("XP")
                        engine.execute_paper_trade(combo, direction, composite, time_remaining,
                                                   entry, override_dollars=dollars)

    # ═══ PHASE 2: Certainty Premium (T-60 to T-5) ═══
    if time_remaining <= 60 and "CP" not in traded:
        if state.window_open and state.window_open > 0:
            btc_corrected = (state.binance_price or 0) - state.offset
            z = vol_tracker.get_z_score(btc_corrected, state.window_open, time_remaining)
            abs_z = abs(z)
            min_z = getattr(engine, 'CL_CP_MIN_Z', CL_CP_MIN_Z)

            if abs_z >= min_z:
                if z > 0:
                    direction = "YES"
                    winner_cost = state.book.best_ask
                else:
                    direction = "NO"
                    winner_cost = 1.0 - state.book.best_bid

                max_wp = getattr(engine, 'CL_CP_MAX_WINNER', CL_CP_MAX_WINNER)
                min_wp = getattr(engine, 'CL_CP_MIN_WINNER', CL_CP_MIN_WINNER)

                if min_wp <= winner_cost <= max_wp:
                    entry = state.book.best_ask if direction == "YES" else state.book.best_bid
                    combo = next((c for c in state.combos if c.name == "CL_certainty"), None)
                    if combo and not combo.has_position_in_window(ws):
                        dollars = getattr(engine, 'CL_CERTAINTY_DOLLARS', CL_CERTAINTY_DOLLARS)
                        if abs_z >= 2.0:
                            dollars = int(dollars * 1.5)
                        print("  {}[CL] CP {} z={:.1f} @{:.0f}c ${} T-{:.0f}s{}".format(
                            engine.G if direction == "YES" else engine.R,
                            direction, z, entry*100, dollars,
                            time_remaining, engine.RST))
                        _last_signal_time[0] = now
                        traded.add("CP")
                        engine.execute_paper_trade(combo, direction, abs_z, time_remaining,
                                                   entry, override_dollars=dollars)

    if len(_phase_traded) > 20:
        for k in sorted(_phase_traded.keys())[:-10]:
            del _phase_traded[k]


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "clutch",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "CL_XP_MOMENTUM_WINDOW": CL_XP_MOMENTUM_WINDOW,
        "CL_XP_MIN_EXCHANGES": CL_XP_MIN_EXCHANGES,
        "CL_XP_PER_EX_MIN": CL_XP_PER_EX_MIN,
        "CL_XP_COMPOSITE_MIN": CL_XP_COMPOSITE_MIN,
        "CL_CP_MIN_Z": CL_CP_MIN_Z,
        "CL_CP_MAX_WINNER": CL_CP_MAX_WINNER,
        "CL_CP_MIN_WINNER": CL_CP_MIN_WINNER,
        "CL_MAX_SPREAD": CL_MAX_SPREAD,
        "CL_MIN_BOOK_LEVELS": CL_MIN_BOOK_LEVELS,
        "CL_MIN_ENTRY": CL_MIN_ENTRY,
        "CL_MAX_ENTRY": CL_MAX_ENTRY,
        "CL_COOLDOWN_SEC": CL_COOLDOWN_SEC,
        "CL_PRESSURE_DOLLARS": CL_PRESSURE_DOLLARS,
        "CL_CERTAINTY_DOLLARS": CL_CERTAINTY_DOLLARS,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
