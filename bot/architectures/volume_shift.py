"""
Architecture: volume_shift (Volume Regime Shift Detector)
Thesis: Before a significant BTC move, the RATE of trading on Binance shifts from
normal to elevated. Binance normally processes 5-15 trade messages per second. When
institutional activity hits — algo execution, liquidation cascade, news reaction —
the rate jumps to 30-100+ messages per second.

This volume regime shift happens BEFORE the price fully moves. Flow_front catches
the price move mid-formation. Volume_shift catches the activity spike that precedes
the price move, giving an even earlier entry on PM.

Signal: Count Binance WS messages per second over rolling windows.
- Short window (2s): current activity rate
- Long window (30s): baseline activity rate
- When short/long ratio exceeds threshold, a volume regime shift is underway
- Direction: net price change during the spike tells us which way

Key advantage: we detect the EVENT (surge in trading), not the RESULT (price move).
PM's MMs react to price, not to Binance message rate. We see it first.
"""

import time
import math
from collections import deque

# Parameters
VS_SHORT_WINDOW = 2        # seconds for "current" activity rate
VS_LONG_WINDOW = 30        # seconds for baseline activity rate
VS_MIN_RATIO = 3.0         # short rate must be Nx the long rate
VS_MIN_TICKS_SHORT = 6     # minimum ticks in short window (avoid false positives on low vol)
VS_COOLDOWN_SEC = 10       # seconds between signals
VS_MIN_NET_DIRECTION = 0.6 # 60%+ of short-window ticks must be one direction
VS_MIN_TIME_REMAINING = 10
VS_MAX_TIME_REMAINING = 290
VS_MAX_SPREAD = 0.04
VS_MIN_BOOK_LEVELS = 2
VS_MIN_ENTRY_PRICE = 0.15
VS_MAX_ENTRY_PRICE = 0.85

COMBO_PARAMS = [
    {"name": "VS_3x_6t",  "btc_threshold_bp": 0, "lookback_s": 0,
     "min_ratio": 3.0, "min_ticks": 6},
    {"name": "VS_4x_8t",  "btc_threshold_bp": 0, "lookback_s": 0,
     "min_ratio": 4.0, "min_ticks": 8},
    {"name": "VS_2x_10t", "btc_threshold_bp": 0, "lookback_s": 0,
     "min_ratio": 2.0, "min_ticks": 10},
    {"name": "VS_5x_6t",  "btc_threshold_bp": 0, "lookback_s": 0,
     "min_ratio": 5.0, "min_ticks": 6},
]
_combo_config = {p["name"]: p for p in COMBO_PARAMS}

# Track every Binance tick timestamp and direction
# Each entry: (timestamp_sec, price, direction: +1 for uptick, -1 for downtick)
_tick_log = deque(maxlen=3000)  # ~30 seconds at 100 ticks/sec max
_last_price = [0.0]
_last_signal_time = [0.0]
_last_status = [0.0]


def on_tick(state, price, ts):
    """Called once per second — not used for tick counting."""
    pass


def on_raw_tick(state, price, ts_ms):
    """Called on EVERY Binance WS message — counts actual trading rate."""
    now = time.time()
    direction = 1 if price >= _last_price[0] else -1
    _tick_log.append((now, price, direction))
    _last_price[0] = price


def check_signals(state, now_s):
    """Detect volume regime shifts and trade before PM reprices."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < VS_MIN_BOOK_LEVELS or len(state.book.asks) < VS_MIN_BOOK_LEVELS:
        return
    if state.book.spread > VS_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < VS_MIN_TIME_REMAINING or time_remaining > VS_MAX_TIME_REMAINING:
        return

    if not state.window_open or state.window_open <= 0:
        return
    if state.binance_price is None:
        return

    now = time.time()

    # Cooldown
    if now - _last_signal_time[0] < VS_COOLDOWN_SEC:
        return

    # Book freshness
    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    # Count ticks in short and long windows
    short_ticks = []
    long_ticks = []
    for ts, px, d in _tick_log:
        age = now - ts
        if age <= VS_SHORT_WINDOW:
            short_ticks.append((ts, px, d))
        if age <= VS_LONG_WINDOW:
            long_ticks.append((ts, px, d))

    if len(long_ticks) < 10:
        return  # not enough baseline data

    # Compute rates (ticks per second)
    short_rate = len(short_ticks) / VS_SHORT_WINDOW if VS_SHORT_WINDOW > 0 else 0
    long_rate = len(long_ticks) / VS_LONG_WINDOW if VS_LONG_WINDOW > 0 else 0

    if long_rate <= 0:
        return

    ratio = short_rate / long_rate

    # Net direction in short window
    if short_ticks:
        up_ticks = sum(1 for _, _, d in short_ticks if d > 0)
        total_short = len(short_ticks)
        up_pct = up_ticks / total_short if total_short > 0 else 0.5

        # Price change during spike
        if len(short_ticks) >= 2:
            price_start = short_ticks[0][1]
            price_end = short_ticks[-1][1]
            spike_bps = (price_end - price_start) / price_start * 10000 if price_start > 0 else 0
        else:
            spike_bps = 0
    else:
        up_pct = 0.5
        spike_bps = 0
        total_short = 0

    # Status
    if now - _last_status[0] >= 15:
        print("  {}{}{} {}T-{:>3.0f}s{} | ticks: {:.0f}/s (short) {:.0f}/s (long) ratio={:.1f}x | dir={:.0f}%up spike={:+.1f}bp".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            short_rate, long_rate, ratio, up_pct * 100, spike_bps))
        _last_status[0] = now

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_ratio = cfg.get("min_ratio", 3.0)
        min_ticks = cfg.get("min_ticks", 6)

        if ratio < min_ratio:
            continue
        if total_short < min_ticks:
            continue

        # Determine direction from tick imbalance and price change
        if up_pct >= VS_MIN_NET_DIRECTION and spike_bps > 0:
            direction = "YES"
        elif (1 - up_pct) >= VS_MIN_NET_DIRECTION and spike_bps < 0:
            direction = "NO"
        else:
            continue  # no clear direction despite volume spike

        entry = state.book.best_ask if direction == "YES" else state.book.best_bid
        _min_entry = getattr(engine, 'VS_MIN_ENTRY_PRICE', VS_MIN_ENTRY_PRICE)
        _max_entry = getattr(engine, 'VS_MAX_ENTRY_PRICE', VS_MAX_ENTRY_PRICE)
        if entry < _min_entry or entry > _max_entry:
            continue

        print("  [VS] FIRE {} {} ratio={:.1f}x ticks={} dir={:.0f}%up spike={:+.1f}bp T-{:.0f}s".format(
            combo.name, direction, ratio, total_short, up_pct * 100, spike_bps, time_remaining))

        _last_signal_time[0] = now
        engine.execute_paper_trade(combo, direction, ratio, time_remaining, entry)
        break


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


ARCH_SPEC = {
    "name": "volume_shift",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "VS_MIN_ENTRY_PRICE": VS_MIN_ENTRY_PRICE,
        "VS_MAX_ENTRY_PRICE": VS_MAX_ENTRY_PRICE,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
    "on_raw_tick": on_raw_tick,
}
