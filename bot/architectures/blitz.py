"""
Architecture: blitz (Greatest Hits — Maximum Trade Volume)

Thesis: The 3 proven profitable architectures each work in different parts of
the window. Run ALL of them in one session, cover the ENTIRE window:
  - Phase 1 (T-295 to T-120): Impulse confirmed logic (proven +$5.7/trade, 66% WR)
  - Phase 2 (T-120 to T-60): Cross-pressure logic (proven +$3.6/trade, 62% WR)
  - Phase 3 (T-60 to T-5): Certainty premium logic (proven +$3.9/trade, 93% WR)

Max 1 trade per phase = up to 3 trades per window.

What's different from sniper:
  - NO conviction scoring (each signal stands on its own proven merits)
  - NO regime detection (the proven archs make money through ALL regimes)
  - NO YES-only filter (proven archs are positive on BOTH directions)
  - NO book staleness gate (proven archs trade on fresh books)
  - NO vol-normalization (proven archs use their original thresholds)
  - BOTH directions (impulse_confirmed YES PnL +$783, NO PnL +$482)
  - BIGGER sizing (proven edge, scale it up)

This is NOT trying to be clever. It's taking EXACTLY what works and running
more of it per window. The 5 proven sessions average $4-5/trade. Blitz tries
to get 3 of those trades per window instead of 1.
"""

import time
import math
import threading
import json as _json
from collections import deque

from bot.shared.volatility import vol_tracker

# ═══ Phase boundaries ═══
BZ_PHASE1_END = 120    # impulse_confirmed: T-295 to T-120
BZ_PHASE2_END = 60     # cross_pressure: T-120 to T-60
                        # certainty_premium: T-60 to T-5

# ═══ Impulse confirmed params (Phase 1) ═══
BZ_IC_THRESHOLD_BP = 3     # G_3bp_10s is the proven best combo
BZ_IC_LOOKBACK = 10        # 10-second lookback
BZ_IC_CONFIRM_BPS = 1.0
BZ_IC_MAX_IMPULSE = 25

# ═══ Cross-pressure params (Phase 2) ═══
BZ_XP_MOMENTUM_WINDOW = 5
BZ_XP_MIN_EXCHANGES = 2
BZ_XP_PER_EX_MIN = 2.0
BZ_XP_COMPOSITE_MIN = 5.0

# ═══ Certainty premium params (Phase 3) ═══
BZ_CP_MIN_Z = 1.5
BZ_CP_MAX_WINNER_PRICE = 0.95

# ═══ General ═══
BZ_MAX_SPREAD = 0.03
BZ_MIN_BOOK_LEVELS = 2
BZ_MIN_ENTRY = 0.20
BZ_MAX_ENTRY = 0.85
BZ_COOLDOWN_SEC = 8
BZ_BASE_DOLLARS = 200

COMBO_PARAMS = [
    {"name": "BZ_impulse",   "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "BZ_pressure",  "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "BZ_certainty", "btc_threshold_bp": 0, "lookback_s": 0},
]

# ═══ Multi-exchange feeds ═══
_exchange_prices = {
    "binance":  deque(maxlen=120),
    "coinbase": deque(maxlen=120),
    "bybit":    deque(maxlen=120),
}
_lock = threading.Lock()
_threads_started = False
_last_signal_time = [0.0]
_last_status = [0.0]
_phase_traded = {}  # window_start -> set of phases traded


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
        t = threading.Thread(target=target, name="bz-" + name, daemon=True)
        t.start()
    print("  [BZ] Started Coinbase + Bybit feeds")


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


# ═══ Phase 1: Impulse Confirmed ═══

def _check_impulse(state, now_s, time_remaining):
    """Binance impulse + multi-exchange confirmation. Proven +$5.7/trade."""
    import bot.paper_trade_v2 as engine

    price_now = state.binance_price
    if price_now is None or len(state.price_buffer) < 5:
        return None

    lookback = getattr(engine, 'BZ_IC_LOOKBACK', BZ_IC_LOOKBACK)
    threshold = getattr(engine, 'BZ_IC_THRESHOLD_BP', BZ_IC_THRESHOLD_BP)
    max_imp = getattr(engine, 'BZ_IC_MAX_IMPULSE', BZ_IC_MAX_IMPULSE)
    confirm_bps = getattr(engine, 'BZ_IC_CONFIRM_BPS', BZ_IC_CONFIRM_BPS)

    price_ago = engine.find_price_at(state.price_buffer, now_s - lookback)
    if price_ago is None or price_ago <= 0:
        return None

    impulse = (price_now - price_ago) / price_ago * 10000
    if abs(impulse) < threshold or abs(impulse) > max_imp:
        return None

    direction = "YES" if impulse > 0 else "NO"

    # Multi-exchange confirmation
    mom_cb = _get_momentum("coinbase", min(lookback, 5))
    mom_by = _get_momentum("bybit", min(lookback, 5))

    confirms = 0
    if mom_cb is not None:
        if (direction == "YES" and mom_cb >= confirm_bps) or \
           (direction == "NO" and mom_cb <= -confirm_bps):
            confirms += 1
    if mom_by is not None:
        if (direction == "YES" and mom_by >= confirm_bps) or \
           (direction == "NO" and mom_by <= -confirm_bps):
            confirms += 1

    if confirms < 1:
        return None

    return (direction, abs(impulse), confirms)


# ═══ Phase 2: Cross Pressure ═══

def _check_pressure(state, time_remaining):
    """3-exchange momentum agreement. Proven +$3.6/trade."""
    import bot.paper_trade_v2 as engine

    window = getattr(engine, 'BZ_XP_MOMENTUM_WINDOW', BZ_XP_MOMENTUM_WINDOW)
    min_ex = getattr(engine, 'BZ_XP_MIN_EXCHANGES', BZ_XP_MIN_EXCHANGES)
    per_ex = getattr(engine, 'BZ_XP_PER_EX_MIN', BZ_XP_PER_EX_MIN)
    comp_min = getattr(engine, 'BZ_XP_COMPOSITE_MIN', BZ_XP_COMPOSITE_MIN)

    mom_bn = _get_momentum("binance", window)
    mom_cb = _get_momentum("coinbase", window)
    mom_by = _get_momentum("bybit", window)

    moms = {}
    if mom_bn is not None: moms["BN"] = mom_bn
    if mom_cb is not None: moms["CB"] = mom_cb
    if mom_by is not None: moms["BY"] = mom_by

    if len(moms) < min_ex:
        return None

    up = sum(1 for v in moms.values() if v >= per_ex)
    dn = sum(1 for v in moms.values() if v <= -per_ex)
    comp_up = sum(v for v in moms.values() if v > 0)
    comp_dn = sum(v for v in moms.values() if v < 0)

    if up >= min_ex and comp_up >= comp_min:
        return ("YES", comp_up, up)
    elif dn >= min_ex and abs(comp_dn) >= comp_min:
        return ("NO", abs(comp_dn), dn)
    return None


# ═══ Phase 3: Certainty Premium ═══

def _check_certainty(state, time_remaining):
    """Near-certain outcomes at discount. Proven +$3.9/trade, 93% WR."""
    import bot.paper_trade_v2 as engine

    if not state.window_open or state.window_open <= 0:
        return None

    btc_corrected = (state.binance_price or 0) - state.offset
    strike = state.window_open

    z = vol_tracker.get_z_score(btc_corrected, strike, time_remaining)
    abs_z = abs(z)
    min_z = getattr(engine, 'BZ_CP_MIN_Z', BZ_CP_MIN_Z)
    max_wp = getattr(engine, 'BZ_CP_MAX_WINNER_PRICE', BZ_CP_MAX_WINNER_PRICE)

    if abs_z < min_z:
        return None

    if z > 0:
        direction = "YES"
        winner_cost = state.book.best_ask
    else:
        direction = "NO"
        winner_cost = 1.0 - state.book.best_bid

    if winner_cost > max_wp or winner_cost < 0.80:
        return None

    return (direction, abs_z, winner_cost)


# ═══ Main hooks ═══

def on_tick(state, price, ts):
    _start_feeds()
    with _lock:
        _exchange_prices["binance"].append((time.time(), price))
    vol_tracker.record_tick(price, ts)


def check_signals(state, now_s):
    """Run 3 proven signals across 3 phases of the window."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    min_levels = getattr(engine, 'BZ_MIN_BOOK_LEVELS', BZ_MIN_BOOK_LEVELS)
    if len(state.book.bids) < min_levels or len(state.book.asks) < min_levels:
        return
    if state.book.spread > getattr(engine, 'BZ_MAX_SPREAD', BZ_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < 5 or time_remaining > 295:
        return

    if state.binance_price is None:
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'BZ_COOLDOWN_SEC', BZ_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    ws = state.window_start
    if ws not in _phase_traded:
        _phase_traded[ws] = set()
    traded = _phase_traded[ws]

    phase1_end = getattr(engine, 'BZ_PHASE1_END', BZ_PHASE1_END)
    phase2_end = getattr(engine, 'BZ_PHASE2_END', BZ_PHASE2_END)
    dollars = getattr(engine, 'BZ_BASE_DOLLARS', BZ_BASE_DOLLARS)
    min_entry = getattr(engine, 'BZ_MIN_ENTRY', BZ_MIN_ENTRY)
    max_entry = getattr(engine, 'BZ_MAX_ENTRY', BZ_MAX_ENTRY)

    # Status
    if now - _last_status[0] >= 15:
        btc = state.binance_price - state.offset
        delta = (btc - state.window_open) / state.window_open * 10000 if state.window_open else 0
        dc = engine.G if delta > 0 else engine.R if delta < 0 else engine.RST
        phase = "P1:IC" if time_remaining > phase1_end else "P2:XP" if time_remaining > phase2_end else "P3:CP"
        phases_done = ",".join(sorted(traded)) if traded else "none"
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC ${:,.2f} {}{:+.1f}bp{} | {} | traded:[{}]".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            btc, dc, delta, engine.RST, phase, phases_done))
        _last_status[0] = now

    # ═══ PHASE 1: Impulse Confirmed (T-295 to T-120) ═══
    if time_remaining > phase1_end and "P1" not in traded:
        result = _check_impulse(state, now_s, time_remaining)
        if result:
            direction, impulse, confirms = result
            entry = state.book.best_ask if direction == "YES" else state.book.best_bid
            if min_entry <= entry <= max_entry:
                # Dead zone check (T-90 to T-210 was bad for impulse)
                if not (90 <= time_remaining <= 210):
                    combo = next((c for c in state.combos if c.name == "BZ_impulse"), None)
                    if combo and not combo.has_position_in_window(ws):
                        p1_dollars = dollars
                        if confirms >= 2:
                            p1_dollars = int(dollars * 1.5)
                        print("  {}[BZ] P1 IMPULSE {} {:.1f}bp {}ex @{:.0f}c ${} T-{:.0f}s{}".format(
                            engine.G if direction == "YES" else engine.R,
                            direction, impulse, confirms, entry*100, p1_dollars,
                            time_remaining, engine.RST))
                        _last_signal_time[0] = now
                        traded.add("P1")
                        engine.execute_paper_trade(combo, direction, impulse, time_remaining,
                                                   entry, override_dollars=p1_dollars)

    # ═══ PHASE 2: Cross Pressure (T-120 to T-60) ═══
    if phase2_end < time_remaining <= phase1_end and "P2" not in traded:
        result = _check_pressure(state, time_remaining)
        if result:
            direction, composite, n_ex = result
            entry = state.book.best_ask if direction == "YES" else state.book.best_bid
            if min_entry <= entry <= max_entry:
                combo = next((c for c in state.combos if c.name == "BZ_pressure"), None)
                if combo and not combo.has_position_in_window(ws):
                    p2_dollars = dollars
                    if composite >= 8.0:
                        p2_dollars = int(dollars * 1.5)
                    print("  {}[BZ] P2 PRESSURE {} {:.1f}bp {}ex @{:.0f}c ${} T-{:.0f}s{}".format(
                        engine.G if direction == "YES" else engine.R,
                        direction, composite, n_ex, entry*100, p2_dollars,
                        time_remaining, engine.RST))
                    _last_signal_time[0] = now
                    traded.add("P2")
                    engine.execute_paper_trade(combo, direction, composite, time_remaining,
                                               entry, override_dollars=p2_dollars)

    # ═══ PHASE 3: Certainty Premium (T-60 to T-5) ═══
    if time_remaining <= phase2_end and "P3" not in traded:
        result = _check_certainty(state, time_remaining)
        if result:
            direction, z_score, winner_cost = result
            entry = state.book.best_ask if direction == "YES" else state.book.best_bid
            combo = next((c for c in state.combos if c.name == "BZ_certainty"), None)
            if combo and not combo.has_position_in_window(ws):
                p3_dollars = dollars
                if z_score >= 2.0:
                    p3_dollars = int(dollars * 1.5)
                print("  {}[BZ] P3 CERTAINTY {} z={:.1f} @{:.0f}c ${} T-{:.0f}s{}".format(
                    engine.G if direction == "YES" else engine.R,
                    direction, z_score, entry*100, p3_dollars,
                    time_remaining, engine.RST))
                _last_signal_time[0] = now
                traded.add("P3")
                engine.execute_paper_trade(combo, direction, z_score, time_remaining,
                                           entry, override_dollars=p3_dollars)

    # Cleanup old windows from _phase_traded
    if len(_phase_traded) > 20:
        old_keys = sorted(_phase_traded.keys())[:-10]
        for k in old_keys:
            del _phase_traded[k]


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "blitz",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "BZ_PHASE1_END": BZ_PHASE1_END,
        "BZ_PHASE2_END": BZ_PHASE2_END,
        "BZ_IC_THRESHOLD_BP": BZ_IC_THRESHOLD_BP,
        "BZ_IC_LOOKBACK": BZ_IC_LOOKBACK,
        "BZ_IC_CONFIRM_BPS": BZ_IC_CONFIRM_BPS,
        "BZ_IC_MAX_IMPULSE": BZ_IC_MAX_IMPULSE,
        "BZ_XP_MOMENTUM_WINDOW": BZ_XP_MOMENTUM_WINDOW,
        "BZ_XP_MIN_EXCHANGES": BZ_XP_MIN_EXCHANGES,
        "BZ_XP_PER_EX_MIN": BZ_XP_PER_EX_MIN,
        "BZ_XP_COMPOSITE_MIN": BZ_XP_COMPOSITE_MIN,
        "BZ_CP_MIN_Z": BZ_CP_MIN_Z,
        "BZ_CP_MAX_WINNER_PRICE": BZ_CP_MAX_WINNER_PRICE,
        "BZ_MAX_SPREAD": BZ_MAX_SPREAD,
        "BZ_MIN_BOOK_LEVELS": BZ_MIN_BOOK_LEVELS,
        "BZ_MIN_ENTRY": BZ_MIN_ENTRY,
        "BZ_MAX_ENTRY": BZ_MAX_ENTRY,
        "BZ_COOLDOWN_SEC": BZ_COOLDOWN_SEC,
        "BZ_BASE_DOLLARS": BZ_BASE_DOLLARS,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
        "ONE_TRADE_PER_WINDOW": False,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
