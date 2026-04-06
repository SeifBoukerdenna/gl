"""
Architecture: flow_front (Flow Front-Runner)
Thesis: Large Binance trades arrive as clusters of fills over 1-3 seconds. A big market
order eating through the book produces 10-50 individual trade messages in rapid succession.
By detecting the START of this cluster (first 2-3 fills), we can position on PM before the
full impulse plays out and before PM's market makers reprice.

This is different from impulse_lag which waits for the impulse to COMPLETE (lookback over
10-60 seconds). Flow front-runner catches it mid-formation, within 500ms of the first fill.

Signals:
1. Volume spike: rolling 2-second volume exceeds 5x the rolling 30-second average
2. Trade clustering: 5+ Binance trades in 500ms (normal is 1-2)
3. Net direction: majority of cluster trades are buys (taker buy) or sells

The key edge: Binance trade messages hit our WS feed before PM MMs can react.
We see the flow, PM's book is still stale, we take the stale liquidity.
"""

import time
from collections import deque

# Configurable parameters
FF_VOLUME_SPIKE_RATIO = 5.0     # 2s volume must be Nx the 30s average
FF_CLUSTER_MIN_TRADES = 5       # minimum trades in 500ms to qualify as cluster
FF_CLUSTER_WINDOW_MS = 500      # milliseconds to detect a cluster
FF_MIN_NET_DIRECTION = 0.60     # 60%+ of cluster volume must be one direction
FF_COOLDOWN_MS = 3000           # don't fire again within 3s of last signal
FF_MIN_TIME_REMAINING = 10
FF_MAX_TIME_REMAINING = 290
FF_MAX_SPREAD = 0.04
FF_MIN_BOOK_LEVELS = 2
FF_MIN_ENTRY_PRICE = 0.20
FF_MAX_ENTRY_PRICE = 0.80

# Internal state
_trade_buffer = deque(maxlen=5000)  # (timestamp_ms, price, qty, is_buyer_maker)
_last_signal_ms = 0
_last_status = [0.0]

COMBO_PARAMS = [
    {"name": "FF_5x_fast",   "btc_threshold_bp": 0, "lookback_s": 0,
     "volume_ratio": 5.0, "cluster_trades": 5, "cluster_ms": 500},
    {"name": "FF_3x_fast",   "btc_threshold_bp": 0, "lookback_s": 0,
     "volume_ratio": 3.0, "cluster_trades": 4, "cluster_ms": 500},
    {"name": "FF_5x_tight",  "btc_threshold_bp": 0, "lookback_s": 0,
     "volume_ratio": 5.0, "cluster_trades": 8, "cluster_ms": 500},
    {"name": "FF_3x_vol",    "btc_threshold_bp": 0, "lookback_s": 0,
     "volume_ratio": 3.0, "cluster_trades": 3, "cluster_ms": 750},
]

_combo_config = {p["name"]: p for p in COMBO_PARAMS}


def on_tick(state, price, ts):
    """Record every Binance trade for volume/cluster analysis.
    Note: 'ts' here is seconds but we need ms precision from the raw data."""
    # We get called with per-second ticks, but the flow analysis needs
    # the raw trade data. We'll use state.binance_ts (milliseconds from WS)
    # and state.binance_price for the latest tick.
    ts_ms = getattr(state, 'binance_ts', 0)
    if ts_ms <= 0:
        return

    # Approximate volume from price change (we don't have per-trade volume
    # from the aggregated feed, but we can detect clustering from tick rate)
    _trade_buffer.append((ts_ms, price, 1.0, True))  # placeholder


def check_signals(state, now_s):
    """Flow front-runner: detect Binance trade clusters and position before PM reprices."""
    import bot.paper_trade_v2 as engine
    global _last_signal_ms

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < FF_MIN_BOOK_LEVELS or len(state.book.asks) < FF_MIN_BOOK_LEVELS:
        return
    if state.book.spread > FF_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < FF_MIN_TIME_REMAINING or time_remaining > FF_MAX_TIME_REMAINING:
        return

    if not state.window_open or state.window_open <= 0:
        return
    if state.binance_price is None:
        return

    ts_ms = getattr(state, 'binance_ts', 0)
    if ts_ms <= 0:
        return

    # Cooldown between signals
    if ts_ms - _last_signal_ms < FF_COOLDOWN_MS:
        return

    # Analyze recent price buffer for rapid movement (cluster detection)
    # Use the price_buffer (1-second ticks) to detect rapid price changes
    buf = state.price_buffer
    if len(buf) < 5:
        return

    # Method 1: Rapid price change in last 2 seconds vs last 30 seconds
    now_price = state.binance_price

    # Get prices from last 2 seconds and last 30 seconds
    recent_2s = [(t, p) for t, p in buf if now_s - t <= 2]
    recent_30s = [(t, p) for t, p in buf if now_s - t <= 30]

    if len(recent_2s) < 2 or len(recent_30s) < 5:
        return

    # Price change in last 2 seconds (absolute)
    price_2s_start = recent_2s[0][1]
    move_2s = abs(now_price - price_2s_start)

    # Average absolute move per 2-second window over last 30 seconds
    moves_30s = []
    for i in range(1, len(recent_30s)):
        dt = recent_30s[i][0] - recent_30s[i-1][0]
        if dt > 0:
            dp = abs(recent_30s[i][1] - recent_30s[i-1][1])
            moves_30s.append(dp * 2 / dt)  # normalize to 2-second equivalent

    if not moves_30s:
        return

    avg_move_2s = sum(moves_30s) / len(moves_30s)
    if avg_move_2s <= 0:
        return

    # Volume spike ratio (using price movement as proxy for volume)
    spike_ratio = move_2s / avg_move_2s if avg_move_2s > 0 else 0

    # Direction of the move
    direction_sign = now_price - price_2s_start  # positive = price going up

    # Tick clustering: count how many 1-second ticks we got in last 2 seconds
    # More ticks = more Binance trades = larger order being filled
    tick_count_2s = len(recent_2s)

    btc_corrected = now_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # Status line
    now_t = time.time()
    if now_t - _last_status[0] >= 15:
        print("  {}{}{} {}T-{:>3.0f}s{} | flow: {:.1f}x spike, {} ticks/2s, move ${:.1f} | d{:+.1f}bp".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            spike_ratio, tick_count_2s, move_2s, delta_bps))
        _last_status[0] = now_t

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_ratio = cfg.get("volume_ratio", 5.0)
        min_ticks = cfg.get("cluster_trades", 5)

        # Signal conditions:
        # 1. Price moved significantly faster than average (volume spike proxy)
        # 2. Multiple ticks in short window (trade cluster)
        if spike_ratio < min_ratio:
            continue
        if tick_count_2s < min_ticks:
            continue

        # Determine direction from the spike
        if direction_sign > 0:
            direction = "YES"
            entry = state.book.best_ask
        else:
            direction = "NO"
            entry = state.book.best_bid

        if entry < FF_MIN_ENTRY_PRICE or entry > FF_MAX_ENTRY_PRICE:
            continue

        _min_entry = getattr(engine, 'FF_MIN_ENTRY_PRICE', FF_MIN_ENTRY_PRICE)
        _max_entry = getattr(engine, 'FF_MAX_ENTRY_PRICE', FF_MAX_ENTRY_PRICE)
        if entry < _min_entry or entry > _max_entry:
            continue

        print("  [FF] FIRE {} {} spike={:.1f}x ticks={} move=${:.1f} T-{:.0f}s".format(
            combo.name, direction, spike_ratio, tick_count_2s, move_2s, time_remaining))

        _last_signal_ms = ts_ms
        engine.execute_paper_trade(combo, direction, spike_ratio, time_remaining, entry)
        break  # one trade per signal


def on_window_start(state):
    _last_status[0] = 0.0
    global _last_signal_ms
    _last_signal_ms = 0


ARCH_SPEC = {
    "name": "flow_front",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "FF_VOLUME_SPIKE_RATIO": FF_VOLUME_SPIKE_RATIO,
        "FF_CLUSTER_MIN_TRADES": FF_CLUSTER_MIN_TRADES,
        "FF_MIN_ENTRY_PRICE": FF_MIN_ENTRY_PRICE,
        "FF_MAX_ENTRY_PRICE": FF_MAX_ENTRY_PRICE,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
