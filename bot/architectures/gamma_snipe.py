"""
Architecture: gamma_snipe
Thesis: In the last 60 seconds of a 5-min binary market, the contract's gamma (sensitivity
of fair value to underlying price changes) explodes. A tiny BTC move of 0.05% can shift fair
value from 55c to 92c — but PM's book doesn't reprice that fast. We compute a proper
probability model P(UP) = Φ((BTC - PTB) / (σ * √T)) using realized volatility from the
Binance tick stream, compare it to PM's book price, and trade when the gap exceeds fees.
This works especially well when BTC is FLAT — low realized vol means even a 1-2bp delta
becomes a near-certainty at T-15s, but PM's book still quotes 70-80c.
"""

import math
import time
from collections import deque

# Configurable parameters
GS_MAX_TIME = 60           # only active in last 60s
GS_MIN_TIME = 3            # don't trade in last 3s (settlement risk)
GS_MIN_GAP_CENTS = 5       # minimum fair-vs-book gap to trade (5c)
GS_MAX_SPREAD = 0.05       # wider spread tolerance (books thin near expiry)
GS_MIN_BOOK_LEVELS = 1     # accept thin books
GS_MIN_FAIR_PROB = 0.55    # don't trade when fair value is near 50% (no edge)
GS_VOL_WINDOW = 120        # seconds of BTC data to estimate volatility
GS_MIN_ENTRY_PRICE = 0.10  # accept cheap entries (high gamma makes them valuable)
GS_MAX_ENTRY_PRICE = 0.97  # near-certain outcomes

# Volatility tracking
_tick_prices = deque(maxlen=300)  # (timestamp, price) for vol estimation
_last_status = [0.0]


def _normal_cdf(x):
    """Approximate Φ(x) — standard normal CDF. Max error < 1.5e-7."""
    # Abramowitz and Stegun approximation 26.2.17
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return 0.5 + sign * (0.5 - d * math.exp(-0.5 * x * x) * poly)


def _compute_realized_vol(price_buffer, window_seconds=120):
    """Compute annualized realized volatility from recent BTC prices.
    Returns vol per second (not annualized) for direct use in the model."""
    if len(price_buffer) < 10:
        return None

    now = time.time()
    # Get prices from the last `window_seconds`
    prices = [(ts, px) for ts, px in price_buffer if now - ts <= window_seconds]
    if len(prices) < 10:
        return None

    # Compute per-second log returns
    returns = []
    for i in range(1, len(prices)):
        dt = prices[i][0] - prices[i - 1][0]
        if dt <= 0:
            continue
        r = math.log(prices[i][1] / prices[i - 1][1])
        returns.append(r / math.sqrt(dt))  # normalize by sqrt(dt) for per-second vol

    if len(returns) < 5:
        return None

    # Standard deviation of per-second returns = vol per sqrt(second)
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    vol_per_sqrt_sec = math.sqrt(var) if var > 0 else 0

    # Convert to dollar vol: multiply by current price
    current_price = prices[-1][1]
    dollar_vol_per_sqrt_sec = vol_per_sqrt_sec * current_price

    # Floor: even in dead markets, BTC can move ~$1 in 60s
    min_vol = 0.5  # $0.50 per sqrt(second) minimum
    return max(dollar_vol_per_sqrt_sec, min_vol)


def _compute_fair_value(btc_price, ptb, vol_per_sqrt_sec, time_remaining):
    """Compute P(UP) using normal CDF model.
    P(UP) = Φ((BTC - PTB) / (σ * √T))
    where σ is dollar volatility per √second, T is seconds remaining."""
    if time_remaining <= 0 or vol_per_sqrt_sec <= 0 or ptb <= 0:
        return 0.5

    delta_dollars = btc_price - ptb
    uncertainty = vol_per_sqrt_sec * math.sqrt(time_remaining)

    if uncertainty <= 0:
        return 1.0 if delta_dollars > 0 else 0.0

    z = delta_dollars / uncertainty
    return _normal_cdf(z)


COMBO_PARAMS = [
    # Aggressive gap thresholds to trade in this low-vol regime
    {"name": "GS_2c_10s", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap": 2,  "max_time": 10},
    {"name": "GS_2c_20s", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap": 2,  "max_time": 20},
    {"name": "GS_2c_30s", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap": 2,  "max_time": 30},
    {"name": "GS_3c_45s", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap": 3,  "max_time": 45},
    {"name": "GS_3c_60s", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap": 3,  "max_time": 60},
    {"name": "GS_5c_45s", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap": 5,  "max_time": 45},
    {"name": "GS_5c_60s", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap": 5,  "max_time": 60},
    {"name": "GS_8c_60s", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap": 8,  "max_time": 60},
]

_combo_config = {p["name"]: p for p in COMBO_PARAMS}


def on_tick(state, price, ts):
    """Record every BTC tick for volatility estimation."""
    _tick_prices.append((ts, price))


def check_signals(state, now_s):
    """Gamma snipe signal detection — fires in the last 60s when fair value diverges from book."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < GS_MIN_BOOK_LEVELS or len(state.book.asks) < GS_MIN_BOOK_LEVELS:
        return
    if state.book.spread > GS_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()

    # Only active in the gamma zone
    if time_remaining > GS_MAX_TIME or time_remaining < GS_MIN_TIME:
        return

    # Need price-to-beat and BTC price
    if not state.window_open or state.window_open <= 0:
        return
    btc = state.binance_price
    if btc is None or btc <= 0:
        return

    # Correct for Binance-PM offset
    btc_corrected = btc - state.offset
    ptb = state.window_open

    # Compute realized volatility
    vol = _compute_realized_vol(state.price_buffer, GS_VOL_WINDOW)
    if vol is None:
        return

    # Compute fair value
    fair = _compute_fair_value(btc_corrected, ptb, vol, time_remaining)

    # Status line
    now_t = time.time()
    if now_t - _last_status[0] >= 5:  # more frequent in gamma zone
        delta_bps = (btc_corrected - ptb) / ptb * 10000 if ptb > 0 else 0
        yes_ask = state.book.best_ask
        no_cost = 1.0 - state.book.best_bid
        no_fair = 1.0 - fair
        gap_yes = (fair - yes_ask) * 100
        gap_no = (no_fair - no_cost) * 100
        side = "YES" if gap_yes > gap_no else "NO"
        best_gap = max(gap_yes, gap_no)
        print("  {}{}{} {}T-{:>2.0f}s{} | fair={:.0f}% vol=${:.1f} | {}: gap {:+.0f}c (YES:{:+.0f}c NO:{:+.0f}c) | d{:+.1f}bp".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            fair * 100, vol, side, best_gap, gap_yes, gap_no, delta_bps))
        _last_status[0] = now_t

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_gap = cfg.get("min_gap", 5) / 100  # cents to decimal
        max_time = cfg.get("max_time", 60)

        if time_remaining > max_time:
            continue

        # Check YES side: fair > PM ask + gap
        yes_cost = state.book.best_ask
        gap_yes = fair - yes_cost

        if gap_yes >= min_gap:
            if GS_MIN_ENTRY_PRICE <= yes_cost <= GS_MAX_ENTRY_PRICE:
                engine.execute_paper_trade(combo, "YES", gap_yes * 100, time_remaining, yes_cost)
                continue

        # Check NO side: (1-fair) > (1-best_bid) + gap
        no_fair = 1.0 - fair
        no_cost = 1.0 - state.book.best_bid
        gap_no = no_fair - no_cost

        if gap_no >= min_gap:
            if GS_MIN_ENTRY_PRICE <= no_cost <= GS_MAX_ENTRY_PRICE:
                engine.execute_paper_trade(combo, "NO", gap_no * 100, time_remaining, state.book.best_bid)


def on_window_start(state):
    """Reset per-window state."""
    _last_status[0] = 0.0


ARCH_SPEC = {
    "name": "gamma_snipe",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "GS_MAX_TIME": GS_MAX_TIME,
        "GS_MIN_TIME": GS_MIN_TIME,
        "GS_MIN_GAP_CENTS": GS_MIN_GAP_CENTS,
        "GS_MAX_SPREAD": GS_MAX_SPREAD,
        "GS_VOL_WINDOW": GS_VOL_WINDOW,
        "GS_MIN_ENTRY_PRICE": GS_MIN_ENTRY_PRICE,
        "GS_MAX_ENTRY_PRICE": GS_MAX_ENTRY_PRICE,
        "MIN_ENTRY_PRICE": 0.01,
        "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0,
        "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
