"""
Shared BTC volatility module for architectures B, C, D.
Computes rolling realized volatility from Binance 1-second tick data.

Used by: lottery_fade, overreaction_fade, certainty_premium, gamma_snipe

Usage:
    from bot.shared.volatility import vol_tracker
    vol_tracker.record_tick(price, epoch_second)
    z = vol_tracker.get_z_score(btc_price, strike_price, seconds_remaining)
    move = vol_tracker.get_typical_move(seconds_remaining)
    sigma = vol_tracker.get_sigma()
"""

import math
from collections import deque


def _normal_cdf(x):
    """Standard normal CDF. Abramowitz & Stegun 26.2.17, max error < 1.5e-7."""
    if x < -8: return 0.0
    if x > 8: return 1.0
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return 0.5 + sign * (0.5 - d * math.exp(-0.5 * x * x) * poly)


class VolatilityTracker:
    """Rolling BTC realized volatility from 1-second ticks."""

    def __init__(self, lookback_seconds=7200, min_ticks=60):
        self.lookback = lookback_seconds  # default 2 hours
        self.min_ticks = min_ticks
        self._ticks = deque(maxlen=lookback_seconds + 100)
        self._sigma_cache = None  # (timestamp, sigma_per_sqrt_sec_dollars)
        self._cache_ttl = 3  # recompute every 3 seconds (catch vol spikes faster)

    def record_tick(self, price, epoch_second):
        """Record a BTC price tick. Call once per second from on_tick."""
        self._ticks.append((epoch_second, price))

    def get_sigma(self):
        """Get current realized volatility in $/sqrt(second).
        This is the core vol measure: multiply by sqrt(T) to get typical move over T seconds."""
        import time
        now = time.time()

        # Cache to avoid recomputing on every call
        if self._sigma_cache and (now - self._sigma_cache[0]) < self._cache_ttl:
            return self._sigma_cache[1]

        if len(self._ticks) < self.min_ticks:
            # Fallback: BTC typically moves ~$3-5 per sqrt(second) in normal conditions
            return 3.0

        # Get ticks from lookback window
        cutoff = now - self.lookback
        prices = [(ts, px) for ts, px in self._ticks if ts >= cutoff]
        if len(prices) < self.min_ticks:
            return 3.0

        # Compute 1-second log returns
        returns = []
        for i in range(1, len(prices)):
            dt = prices[i][0] - prices[i - 1][0]
            if dt <= 0 or dt > 5:  # skip gaps > 5s
                continue
            log_ret = math.log(prices[i][1] / prices[i - 1][1])
            returns.append(log_ret / math.sqrt(dt))

        if len(returns) < 30:
            return 3.0

        # Standard deviation of per-sqrt-second returns
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        vol_per_sqrt_sec = math.sqrt(var) if var > 0 else 0

        # Convert to dollar volatility
        current_price = prices[-1][1]
        sigma = vol_per_sqrt_sec * current_price

        # Floor: even in dead markets, BTC can jitter ~$0.50/sqrt(s)
        sigma = max(sigma, 0.5)

        self._sigma_cache = (now, sigma)
        return sigma

    def get_typical_move(self, seconds_remaining):
        """Expected BTC move in dollars over the next `seconds_remaining` seconds.
        = sigma * sqrt(T)"""
        if seconds_remaining <= 0:
            return 0.0
        sigma = self.get_sigma()
        return sigma * math.sqrt(seconds_remaining)

    def get_z_score(self, btc_price, strike_price, seconds_remaining):
        """How many standard deviations BTC is from the strike.
        Positive = BTC above strike (favors UP), negative = below (favors DOWN)."""
        if seconds_remaining <= 0:
            return float('inf') if btc_price > strike_price else float('-inf')
        typical_move = self.get_typical_move(seconds_remaining)
        if typical_move <= 0:
            return 0.0
        return (btc_price - strike_price) / typical_move

    def get_fair_probability(self, btc_price, strike_price, seconds_remaining):
        """P(BTC > strike at settlement) using normal CDF model.
        = Φ(z_score)"""
        z = self.get_z_score(btc_price, strike_price, seconds_remaining)
        return _normal_cdf(z)

    def get_binance_volume_ratio(self, window_seconds=60):
        """Ratio of recent tick count to trailing average. > 3 = unusual activity."""
        import time
        now = time.time()
        recent = sum(1 for ts, _ in self._ticks if now - ts <= window_seconds)
        trailing = sum(1 for ts, _ in self._ticks if now - ts <= 3600)  # last hour
        if trailing == 0:
            return 1.0
        avg_per_window = trailing * window_seconds / 3600
        return recent / avg_per_window if avg_per_window > 0 else 1.0

    def tick_count(self):
        return len(self._ticks)


# Singleton instance — shared across architectures within the same process
vol_tracker = VolatilityTracker()
