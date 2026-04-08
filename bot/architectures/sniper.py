"""
Architecture: sniper — Conviction-Weighted Signal Stacking Engine

Two-phase trading within each 5-minute window:
  Phase 1 (Momentum): T-295 to T-210 — Stacks signals from cross_pressure,
    volume_shift, impulse_confirmed, and flow_front. Vol-normalized thresholds.
    Conviction-weighted position sizing.
  Dead Zone: T-210 to T-90 — NO TRADES
  Phase 2 (Convergence): T-90 to T-5 — certainty_premium and lottery_fade
    signals for near-certain outcomes.

Key innovations over individual architectures:
  - Vol-normalized thresholds (sigma-based, not static bp)
  - Conviction scoring (weighted sum of independent signal sources)
  - Dynamic sizing ($75-$400 based on conviction level)
  - Two-phase structure (momentum early, convergence late, nothing mid)
  - Regime detection (chop multiplier raises thresholds automatically)
  - YES-only by default (configurable via ALLOWED_DIRECTIONS)
"""

import time
import math
import threading
import json as _json
from collections import deque

from bot.shared.volatility import vol_tracker

# ═══ Signal weights (based on historical $/trade) ═══
WEIGHT_VOLUME_SHIFT = 0.30       # +$38.7/trade — predictive, detects before move
WEIGHT_CROSS_PRESSURE = 0.35     # +$3.7/trade — 3-exchange info asymmetry
WEIGHT_IMPULSE_CONFIRMED = 0.20  # +$1.4/trade — multi-exchange confirmed
WEIGHT_FLOW_FRONT = 0.15         # +$0.4/trade — mid-formation cluster

# ═══ Conviction & sizing ═══
SNP_MIN_CONVICTION = 0.30
SNP_TIER2_CONVICTION = 0.50
SNP_TIER3_CONVICTION = 0.70
SNP_BASE_DOLLARS = 75
SNP_TIER2_DOLLARS = 200
SNP_TIER3_DOLLARS = 400

# ═══ Phase boundaries (time_remaining in seconds) ═══
SNP_PHASE1_END = 210       # Phase 1 runs T-295 to T-210
SNP_PHASE2_START = 90      # Phase 2 runs T-90 to T-5

# ═══ Vol normalization ═══
SNP_MIN_SIGMA = 1.2        # min sigma multiplier to consider a move significant

# ═══ Cross-pressure params (compressed from 5s to 2s) ═══
SNP_XP_MOMENTUM_WINDOW = 2
SNP_XP_MIN_EXCHANGES = 3
SNP_XP_PER_EXCHANGE_MIN_BPS = 1.5

# ══��� Volume shift params ═══
SNP_VS_SHORT_WINDOW = 2
SNP_VS_LONG_WINDOW = 30
SNP_VS_MIN_RATIO = 2.5
SNP_VS_MIN_TICKS = 6
SNP_VS_MIN_NET_DIRECTION = 0.60

# ═══ Impulse confirmed params (short lookbacks: 3s, 5s, 10s) ═══
SNP_IC_CONFIRM_MIN_BPS = 1.0

# ══��� Flow front params ═══
SNP_FF_MIN_SPIKE_RATIO = 3.0
SNP_FF_MIN_TICKS_2S = 3

# ═══ Convergence params ═══
SNP_CERT_MIN_Z = 1.5
SNP_CERT_MAX_WINNER_PRICE = 0.95
SNP_LF_MIN_Z = 2.0
SNP_LF_MIN_OVERPRICE = 0.03

# ═══ General ═══
SNP_COOLDOWN_SEC = 10
SNP_MAX_SPREAD = 0.04
SNP_MIN_BOOK_LEVELS = 2
SNP_MIN_ENTRY_PRICE = 0.15
SNP_MAX_ENTRY_PRICE = 0.85

# ═══ Combos: one per phase ═══
COMBO_PARAMS = [
    {"name": "SNP_momentum",    "btc_threshold_bp": 0, "lookback_s": 0},
    {"name": "SNP_convergence", "btc_threshold_bp": 0, "lookback_s": 0},
]

# ════════════════════════════════════════���══════════════════════
# INTERNAL STATE
# ═══════════════════════════════════════════════════════════════

# Multi-exchange feeds
_exchange_prices = {
    "binance":  deque(maxlen=120),
    "coinbase": deque(maxlen=120),
    "bybit":    deque(maxlen=120),
}
_lock = threading.Lock()
_threads_started = False

# Volume shift raw tick log
_tick_log = deque(maxlen=3000)  # ~30s at 100 ticks/sec
_last_price = [0.0]

# Cooldown & display
_last_signal_time = [0.0]
_last_status = [0.0]


# ═══════════════════════════════════════════════════════════════
# EXCHANGE FEED THREADS
# ═══════════════════════════════════════════════════════════════

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
        t = threading.Thread(target=target, name="snp-" + name, daemon=True)
        t.start()
    print("  [SNP] Started Coinbase + Bybit feeds")


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _get_momentum(exchange, window_sec=2):
    """Price momentum in bps over last N seconds."""
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


def _get_sigma_bps(state, seconds):
    """Vol-normalized expected move in bps for a given time window."""
    sigma = vol_tracker.get_sigma()
    if sigma and state.binance_price and state.binance_price > 0:
        return max((sigma * math.sqrt(max(seconds, 0.1)) / state.binance_price) * 10000, 0.5)
    return 5.0


def _get_regime_multiplier(state):
    """Regime detection from recent window outcomes.
    Returns 1.0 (trending) to 2.0 (choppy). Choppy = raise all thresholds."""
    outcomes = list(getattr(state, 'recent_outcomes', []))
    if len(outcomes) < 4:
        return 1.0
    alternations = sum(1 for i in range(1, len(outcomes)) if outcomes[i] != outcomes[i - 1])
    alt_rate = alternations / (len(outcomes) - 1)
    if alt_rate <= 0.35:
        return 1.0
    if alt_rate >= 0.80:
        return 2.0
    return 1.0 + (alt_rate - 0.35) / 0.45


# ═══════════════════════════════════════════════════════════════
# SIGNAL DETECTORS — each returns (direction, strength) or None
# ═══════════════════════════════════════════════════════════════

def _check_cross_pressure(state, regime_mult):
    """3-exchange momentum agreement with vol-normalized thresholds."""
    import bot.paper_trade_v2 as engine

    window = getattr(engine, 'SNP_XP_MOMENTUM_WINDOW', SNP_XP_MOMENTUM_WINDOW)
    min_ex = getattr(engine, 'SNP_XP_MIN_EXCHANGES', SNP_XP_MIN_EXCHANGES)
    base_per_ex = getattr(engine, 'SNP_XP_PER_EXCHANGE_MIN_BPS', SNP_XP_PER_EXCHANGE_MIN_BPS)

    mom_bn = _get_momentum("binance", window)
    mom_cb = _get_momentum("coinbase", window)
    mom_by = _get_momentum("bybit", window)

    momentums = {}
    if mom_bn is not None:
        momentums["BN"] = mom_bn
    if mom_cb is not None:
        momentums["CB"] = mom_cb
    if mom_by is not None:
        momentums["BY"] = mom_by

    if len(momentums) < min_ex:
        return None

    # Vol-normalize: threshold = max(static_floor, sigma-based)
    sigma_bps = _get_sigma_bps(state, window)
    min_sigma = getattr(engine, 'SNP_MIN_SIGMA', SNP_MIN_SIGMA)
    per_ex_threshold = max(base_per_ex, min_sigma * sigma_bps) * regime_mult

    up_count = sum(1 for v in momentums.values() if v >= per_ex_threshold)
    down_count = sum(1 for v in momentums.values() if v <= -per_ex_threshold)

    composite_up = sum(v for v in momentums.values() if v > 0)
    composite_down = sum(v for v in momentums.values() if v < 0)

    # Composite threshold also vol-normalized
    composite_threshold = max(5.0, min_sigma * sigma_bps * min_ex * 0.6) * regime_mult

    if up_count >= min_ex and composite_up >= composite_threshold:
        return ("YES", composite_up)
    elif down_count >= min_ex and abs(composite_down) >= composite_threshold:
        return ("NO", abs(composite_down))
    return None


def _check_volume_shift(state, regime_mult):
    """Binance trading rate spike — predictive signal."""
    import bot.paper_trade_v2 as engine

    now = time.time()
    short_win = getattr(engine, 'SNP_VS_SHORT_WINDOW', SNP_VS_SHORT_WINDOW)
    long_win = getattr(engine, 'SNP_VS_LONG_WINDOW', SNP_VS_LONG_WINDOW)
    min_ratio = getattr(engine, 'SNP_VS_MIN_RATIO', SNP_VS_MIN_RATIO) * regime_mult
    min_ticks = getattr(engine, 'SNP_VS_MIN_TICKS', SNP_VS_MIN_TICKS)
    min_dir = getattr(engine, 'SNP_VS_MIN_NET_DIRECTION', SNP_VS_MIN_NET_DIRECTION)

    short_ticks = []
    long_count = 0
    for ts, px, d in _tick_log:
        age = now - ts
        if age <= short_win:
            short_ticks.append((ts, px, d))
        if age <= long_win:
            long_count += 1

    if long_count < 10 or len(short_ticks) < min_ticks:
        return None

    short_rate = len(short_ticks) / short_win
    long_rate = long_count / long_win
    if long_rate <= 0:
        return None

    ratio = short_rate / long_rate
    if ratio < min_ratio:
        return None

    up_ticks = sum(1 for _, _, d in short_ticks if d > 0)
    total = len(short_ticks)
    up_pct = up_ticks / total if total > 0 else 0.5

    if len(short_ticks) >= 2:
        spike_bps = (short_ticks[-1][1] - short_ticks[0][1]) / short_ticks[0][1] * 10000
    else:
        spike_bps = 0

    if up_pct >= min_dir and spike_bps > 0:
        return ("YES", ratio)
    elif (1 - up_pct) >= min_dir and spike_bps < 0:
        return ("NO", ratio)
    return None


def _check_impulse_confirmed(state, now_s, regime_mult):
    """Binance impulse + multi-exchange confirmation, short lookbacks (3/5/10s)."""
    import bot.paper_trade_v2 as engine

    price_now = state.binance_price
    if price_now is None or len(state.price_buffer) < 5:
        return None

    min_sigma = getattr(engine, 'SNP_MIN_SIGMA', SNP_MIN_SIGMA)
    confirm_bps = getattr(engine, 'SNP_IC_CONFIRM_MIN_BPS', SNP_IC_CONFIRM_MIN_BPS)

    for lookback_s in [3, 5, 10]:
        price_ago = engine.find_price_at(state.price_buffer, now_s - lookback_s)
        if price_ago is None or price_ago <= 0:
            continue

        impulse_bps = (price_now - price_ago) / price_ago * 10000

        # Vol-normalized threshold
        sigma_bps = _get_sigma_bps(state, lookback_s)
        threshold = min_sigma * sigma_bps * regime_mult

        if abs(impulse_bps) < threshold or abs(impulse_bps) > 25:
            continue

        direction = "YES" if impulse_bps > 0 else "NO"

        # Require at least 1 other exchange to confirm
        mom_cb = _get_momentum("coinbase", min(lookback_s, 5))
        mom_by = _get_momentum("bybit", min(lookback_s, 5))

        confirms = 0
        if mom_cb is not None:
            if (direction == "YES" and mom_cb >= confirm_bps) or \
               (direction == "NO" and mom_cb <= -confirm_bps):
                confirms += 1
        if mom_by is not None:
            if (direction == "YES" and mom_by >= confirm_bps) or \
               (direction == "NO" and mom_by <= -confirm_bps):
                confirms += 1

        if confirms >= 1:
            return (direction, abs(impulse_bps))

    return None


def _check_flow_front(state, now_s, regime_mult):
    """Rapid price cluster detection — 2s spike vs 30s average."""
    import bot.paper_trade_v2 as engine

    price_now = state.binance_price
    if price_now is None:
        return None

    buf = state.price_buffer
    if len(buf) < 5:
        return None

    recent_2s = [(t, p) for t, p in buf if now_s - t <= 2]
    recent_30s = [(t, p) for t, p in buf if now_s - t <= 30]

    if len(recent_2s) < 2 or len(recent_30s) < 5:
        return None

    move_2s = abs(price_now - recent_2s[0][1])

    moves = []
    for i in range(1, len(recent_30s)):
        dt = recent_30s[i][0] - recent_30s[i - 1][0]
        if dt > 0:
            dp = abs(recent_30s[i][1] - recent_30s[i - 1][1])
            moves.append(dp * 2 / dt)

    if not moves:
        return None

    avg_move = sum(moves) / len(moves)
    if avg_move <= 0:
        return None

    spike_ratio = move_2s / avg_move
    min_ratio = getattr(engine, 'SNP_FF_MIN_SPIKE_RATIO', SNP_FF_MIN_SPIKE_RATIO) * regime_mult

    if spike_ratio < min_ratio or len(recent_2s) < getattr(engine, 'SNP_FF_MIN_TICKS_2S', SNP_FF_MIN_TICKS_2S):
        return None

    direction = "YES" if (price_now - recent_2s[0][1]) > 0 else "NO"
    return (direction, spike_ratio)


def _check_convergence(state, time_remaining):
    """Phase 2: certainty premium + lottery fade near expiry."""
    import bot.paper_trade_v2 as engine

    if not state.window_open or state.window_open <= 0:
        return None

    btc_corrected = (state.binance_price or 0) - state.offset
    strike = state.window_open

    z = vol_tracker.get_z_score(btc_corrected, strike, time_remaining)
    abs_z = abs(z)
    fair_up = vol_tracker.get_fair_probability(btc_corrected, strike, time_remaining)

    # Certainty premium: near-certain outcome at discount
    min_z = getattr(engine, 'SNP_CERT_MIN_Z', SNP_CERT_MIN_Z)
    max_wp = getattr(engine, 'SNP_CERT_MAX_WINNER_PRICE', SNP_CERT_MAX_WINNER_PRICE)

    if abs_z >= min_z:
        if z > 0:
            winner_cost = state.book.best_ask
            direction = "YES"
        else:
            winner_cost = 1.0 - state.book.best_bid
            direction = "NO"

        if 0.80 <= winner_cost <= max_wp:
            return (direction, abs_z, "certainty")

    # Lottery fade: overpriced loser token
    lf_min_z = getattr(engine, 'SNP_LF_MIN_Z', SNP_LF_MIN_Z)
    lf_min_op = getattr(engine, 'SNP_LF_MIN_OVERPRICE', SNP_LF_MIN_OVERPRICE)

    if abs_z >= lf_min_z:
        if z > 0:
            fair_loser = 1.0 - fair_up
            loser_market = 1.0 - state.book.best_bid
            direction = "YES"
        else:
            fair_loser = fair_up
            loser_market = state.book.best_ask
            direction = "NO"

        if (loser_market - fair_loser) >= lf_min_op:
            return (direction, abs_z, "lottery")

    return None


# ═══════════════════════════════════════════════════════════════
# HOOKS
# ═══════════════════════════════════════════════════════════════

def on_raw_tick(state, price, ts_ms):
    """Every Binance WS message — feed tick log for volume rate detection."""
    now = time.time()
    direction = 1 if price >= _last_price[0] else -1
    _tick_log.append((now, price, direction))
    _last_price[0] = price


def on_tick(state, price, ts):
    """Once per second — record exchange prices, start feeds, update vol."""
    _start_feeds()
    with _lock:
        _exchange_prices["binance"].append((time.time(), price))
    vol_tracker.record_tick(price, ts)


def check_signals(state, now_s):
    """Two-phase conviction-weighted signal stacking."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return

    min_levels = getattr(engine, 'SNP_MIN_BOOK_LEVELS', SNP_MIN_BOOK_LEVELS)
    if len(state.book.bids) < min_levels or len(state.book.asks) < min_levels:
        return
    if state.book.spread > getattr(engine, 'SNP_MAX_SPREAD', SNP_MAX_SPREAD):
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < 5 or time_remaining > 295:
        return

    if not state.window_open or state.window_open <= 0:
        return
    if state.binance_price is None:
        return

    now = time.time()

    if now - _last_signal_time[0] < getattr(engine, 'SNP_COOLDOWN_SEC', SNP_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    regime_mult = _get_regime_multiplier(state)

    # ── Status line ──
    if now - _last_status[0] >= 15:
        sigma = vol_tracker.get_sigma()
        btc_corrected = state.binance_price - state.offset
        delta = 0
        if state.window_open > 0:
            delta = (btc_corrected - state.window_open) / state.window_open * 10000
        dc = engine.G if delta > 0 else engine.R if delta < 0 else engine.RST
        phase1_end = getattr(engine, 'SNP_PHASE1_END', SNP_PHASE1_END)
        phase2_start = getattr(engine, 'SNP_PHASE2_START', SNP_PHASE2_START)
        phase = "P1" if time_remaining > phase1_end else "DZ" if time_remaining > phase2_start else "P2"
        regime = "CHOP" if regime_mult >= 1.5 else "TREND" if regime_mult <= 1.1 else "MIX"
        outcomes = list(getattr(state, 'recent_outcomes', []))
        n_out = len(outcomes)
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC ${:,.2f} {}{:+.1f}bp{} | sig=${:.1f} | {} {} x{:.1f} ({} wins)".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            btc_corrected, dc, delta, engine.RST,
            sigma, phase, regime, regime_mult, n_out))
        _last_status[0] = now

    phase1_end = getattr(engine, 'SNP_PHASE1_END', SNP_PHASE1_END)
    phase2_start = getattr(engine, 'SNP_PHASE2_START', SNP_PHASE2_START)

    # ═══ PHASE 1: MOMENTUM (T-295 to T-210) ═══
    if time_remaining > phase1_end:
        # HARD STOP in serious chop — only pause when regime is clearly choppy
        if regime_mult < 1.7:
            _phase1_momentum(state, now_s, time_remaining, regime_mult, now)
        return

    # ═══ DEAD ZONE (T-210 to T-90) — hard stop, no trades ═══
    if time_remaining > phase2_start:
        return

    # ═══ PHASE 2: CONVERGENCE (T-90 to T-5) ═══
    _phase2_convergence(state, time_remaining, regime_mult, now)


def _phase1_momentum(state, now_s, time_remaining, regime_mult, now):
    """Stack momentum signals, compute conviction, size dynamically."""
    import bot.paper_trade_v2 as engine

    combo = None
    for c in state.combos:
        if c.name == "SNP_momentum":
            combo = c
            break
    if combo is None or combo.has_position_in_window(state.window_start):
        return

    # Intra-window chop check: if BTC crossed strike both ways, skip momentum
    if state.window_crossed_above and state.window_crossed_below:
        elapsed = time.time() - state.window_start if state.window_start else 0
        if elapsed > 60:
            return

    # ── Check all signal sources ──
    signals = {}

    xp = _check_cross_pressure(state, regime_mult)
    if xp:
        signals['cross_pressure'] = xp

    vs = _check_volume_shift(state, regime_mult)
    if vs:
        signals['volume_shift'] = vs

    ic = _check_impulse_confirmed(state, now_s, regime_mult)
    if ic:
        signals['impulse_confirmed'] = ic

    ff = _check_flow_front(state, now_s, regime_mult)
    if ff:
        signals['flow_front'] = ff

    if not signals:
        return

    # ── Conviction scoring ──
    weights = {
        'cross_pressure': WEIGHT_CROSS_PRESSURE,
        'volume_shift': WEIGHT_VOLUME_SHIFT,
        'impulse_confirmed': WEIGHT_IMPULSE_CONFIRMED,
        'flow_front': WEIGHT_FLOW_FRONT,
    }

    yes_conv = sum(weights[k] for k, (d, _) in signals.items() if d == "YES")
    no_conv = sum(weights[k] for k, (d, _) in signals.items() if d == "NO")

    if yes_conv >= no_conv:
        direction = "YES"
        conviction = yes_conv
    else:
        direction = "NO"
        conviction = no_conv

    # ── Direction filter ──
    allowed = getattr(engine, 'ALLOWED_DIRECTIONS', None)
    if allowed and direction not in allowed:
        # Try the allowed direction if it has any conviction
        for d in allowed:
            alt_conv = yes_conv if d == "YES" else no_conv
            if alt_conv >= getattr(engine, 'SNP_MIN_CONVICTION', SNP_MIN_CONVICTION):
                direction = d
                conviction = alt_conv
                break
        else:
            return

    # ── Conviction gate (regime-adjusted) ──
    min_conv = getattr(engine, 'SNP_MIN_CONVICTION', SNP_MIN_CONVICTION)
    if conviction < min_conv:
        return

    # ── Entry price ──
    entry = state.book.best_ask if direction == "YES" else state.book.best_bid
    min_entry = getattr(engine, 'SNP_MIN_ENTRY_PRICE', SNP_MIN_ENTRY_PRICE)
    max_entry = getattr(engine, 'SNP_MAX_ENTRY_PRICE', SNP_MAX_ENTRY_PRICE)
    if entry < min_entry or entry > max_entry:
        return

    # ── Dynamic sizing ──
    tier2_conv = getattr(engine, 'SNP_TIER2_CONVICTION', SNP_TIER2_CONVICTION)
    tier3_conv = getattr(engine, 'SNP_TIER3_CONVICTION', SNP_TIER3_CONVICTION)

    if conviction >= tier3_conv:
        dollars = getattr(engine, 'SNP_TIER3_DOLLARS', SNP_TIER3_DOLLARS)
    elif conviction >= tier2_conv:
        dollars = getattr(engine, 'SNP_TIER2_DOLLARS', SNP_TIER2_DOLLARS)
    else:
        dollars = getattr(engine, 'SNP_BASE_DOLLARS', SNP_BASE_DOLLARS)

    # ── Fire ──
    sig_parts = []
    for k, (d, s) in signals.items():
        tag = k[:2].upper()
        sig_parts.append("{}{:+.1f}".format(tag, s if d == "YES" else -s))
    sig_str = " ".join(sig_parts)

    print("  {}[SNP] P1 FIRE {} {} conv={:.0f}% ${} [{}] T-{:.0f}s rx{:.1f}{}".format(
        engine.G if direction == "YES" else engine.R,
        combo.name, direction, conviction * 100, dollars,
        sig_str, time_remaining, regime_mult, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, conviction * 10, time_remaining, entry,
                               override_dollars=dollars)


def _phase2_convergence(state, time_remaining, regime_mult, now):
    """Certainty premium + lottery fade near expiry. Smaller sizing."""
    import bot.paper_trade_v2 as engine

    combo = None
    for c in state.combos:
        if c.name == "SNP_convergence":
            combo = c
            break
    if combo is None or combo.has_position_in_window(state.window_start):
        return

    result = _check_convergence(state, time_remaining)
    if result is None:
        return

    direction, z_score, signal_type = result

    # Direction filter
    allowed = getattr(engine, 'ALLOWED_DIRECTIONS', None)
    if allowed and direction not in allowed:
        return

    # Entry price
    if direction == "YES":
        entry = state.book.best_ask
    else:
        entry = state.book.best_bid
    if not (0.01 <= entry <= 0.99):
        return

    # Convergence = smaller sizing (high WR, small payout, risk containment)
    base = getattr(engine, 'SNP_BASE_DOLLARS', SNP_BASE_DOLLARS)
    dollars = max(25, int(base * 0.5))

    print("  {}[SNP] P2 FIRE {} {} z={:.1f} type={} @{:.0f}c ${} T-{:.0f}s{}".format(
        engine.G if direction == "YES" else engine.R,
        combo.name, direction, z_score, signal_type,
        entry * 100, dollars, time_remaining, engine.RST))

    _last_signal_time[0] = now
    engine.execute_paper_trade(combo, direction, z_score, time_remaining, entry,
                               override_dollars=dollars)


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0


# ═══════════════════════════════════════════════════════════════
# ARCH_SPEC
# ═══════════════════════════════════════════════════════════════

ARCH_SPEC = {
    "name": "sniper",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        # Sniper-specific (all overridable via config JSON)
        "SNP_MIN_CONVICTION": SNP_MIN_CONVICTION,
        "SNP_TIER2_CONVICTION": SNP_TIER2_CONVICTION,
        "SNP_TIER3_CONVICTION": SNP_TIER3_CONVICTION,
        "SNP_BASE_DOLLARS": SNP_BASE_DOLLARS,
        "SNP_TIER2_DOLLARS": SNP_TIER2_DOLLARS,
        "SNP_TIER3_DOLLARS": SNP_TIER3_DOLLARS,
        "SNP_PHASE1_END": SNP_PHASE1_END,
        "SNP_PHASE2_START": SNP_PHASE2_START,
        "SNP_MIN_SIGMA": SNP_MIN_SIGMA,
        "SNP_XP_MOMENTUM_WINDOW": SNP_XP_MOMENTUM_WINDOW,
        "SNP_XP_MIN_EXCHANGES": SNP_XP_MIN_EXCHANGES,
        "SNP_XP_PER_EXCHANGE_MIN_BPS": SNP_XP_PER_EXCHANGE_MIN_BPS,
        "SNP_VS_SHORT_WINDOW": SNP_VS_SHORT_WINDOW,
        "SNP_VS_LONG_WINDOW": SNP_VS_LONG_WINDOW,
        "SNP_VS_MIN_RATIO": SNP_VS_MIN_RATIO,
        "SNP_VS_MIN_TICKS": SNP_VS_MIN_TICKS,
        "SNP_VS_MIN_NET_DIRECTION": SNP_VS_MIN_NET_DIRECTION,
        "SNP_IC_CONFIRM_MIN_BPS": SNP_IC_CONFIRM_MIN_BPS,
        "SNP_FF_MIN_SPIKE_RATIO": SNP_FF_MIN_SPIKE_RATIO,
        "SNP_FF_MIN_TICKS_2S": SNP_FF_MIN_TICKS_2S,
        "SNP_CERT_MIN_Z": SNP_CERT_MIN_Z,
        "SNP_CERT_MAX_WINNER_PRICE": SNP_CERT_MAX_WINNER_PRICE,
        "SNP_LF_MIN_Z": SNP_LF_MIN_Z,
        "SNP_LF_MIN_OVERPRICE": SNP_LF_MIN_OVERPRICE,
        "SNP_COOLDOWN_SEC": SNP_COOLDOWN_SEC,
        "SNP_MAX_SPREAD": SNP_MAX_SPREAD,
        "SNP_MIN_BOOK_LEVELS": SNP_MIN_BOOK_LEVELS,
        "SNP_MIN_ENTRY_PRICE": SNP_MIN_ENTRY_PRICE,
        "SNP_MAX_ENTRY_PRICE": SNP_MAX_ENTRY_PRICE,
        # Engine overrides for this architecture
        "ALLOWED_DIRECTIONS": None,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
    "on_raw_tick": on_raw_tick,
}
