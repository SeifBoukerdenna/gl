"""
Architecture: micro_scalp
Thesis: Take every small-edge signal across multiple dimensions simultaneously. Instead of
waiting for one large dislocation, combine weak signals: if 2+ of these are true at once,
there's a trade: (1) BTC delta confirms direction by >2bp, (2) book imbalance >1.5x favors
that side, (3) delta table fair value > book price by >2c. Each signal alone is marginal.
Combined, they compound into a reliable edge. This architecture trades 5-10x more than the
others by accepting smaller per-trade edges with higher frequency. Think of it as the
"shotgun" approach vs the "sniper" approach of the other architectures.
"""

import csv
import time
from collections import deque
from pathlib import Path

# Thresholds — deliberately loose to maximize trade frequency
MS_MIN_DELTA_BPS = 1          # very low bar for BTC delta confirmation
MS_MIN_IMBALANCE = 1.5        # mild book imbalance
MS_MIN_FAIR_GAP = 0.02        # 2c gap between fair value and book
MS_MAX_SPREAD = 0.04
MS_MIN_BOOK_LEVELS = 2
MS_MIN_TIME_REMAINING = 10
MS_MAX_TIME_REMAINING = 290
MS_MIN_ENTRY_PRICE = 0.10
MS_MAX_ENTRY_PRICE = 0.95

# Delta table
_delta_table = {}
_table_loaded = False
MS_DELTA_TABLE_PATH = "data/delta_table_corrected.csv"

# Book imbalance tracking
_imbalance_history = deque(maxlen=10)


def _load_delta_table():
    global _delta_table, _table_loaded
    path = Path(MS_DELTA_TABLE_PATH)
    if not path.exists():
        _table_loaded = True
        return
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            _delta_table[(row["delta_bucket"], int(row["time_bucket"]))] = float(row["win_rate"])
    _table_loaded = True


def _get_delta_bucket(delta_bps):
    if delta_bps < -15: return "<-15"
    if delta_bps > 15: return ">15"
    for lo, hi in [(-15,-7),(-7,-5),(-5,-3),(-3,-1),(-1,1),(1,3),(3,5),(5,7),(7,15)]:
        if lo <= delta_bps < hi:
            return "[{},{})".format(lo, hi)
    return "[7,15)"


def _snap_time_bucket(t):
    buckets = [10, 30, 60, 90, 120, 150, 180, 210, 240, 270]
    return min(buckets, key=lambda b: abs(t - b))


COMBO_PARAMS = [
    # Combined signal combos: require N signals to agree
    {"name": "MS_2sig",   "btc_threshold_bp": 0, "lookback_s": 0, "min_signals": 2},
    {"name": "MS_3sig",   "btc_threshold_bp": 0, "lookback_s": 0, "min_signals": 3},
    # Delta-only fast: just delta table + any confirmation
    {"name": "MS_delta",  "btc_threshold_bp": 0, "lookback_s": 0, "min_signals": 1, "require_delta": True},
    # Impulse + imbalance: BTC moved AND book leans that way
    {"name": "MS_impulse_imb", "btc_threshold_bp": 0, "lookback_s": 0, "min_signals": 2, "require_impulse": True, "require_imbalance": True},
]

_combo_config = {p["name"]: p for p in COMBO_PARAMS}
_last_status = [0.0]


def on_tick(state, price, ts):
    """Track book imbalance."""
    if not state.book.bids or not state.book.asks:
        return
    bid_size = sum(s for _, s in state.book.bids[:5])
    ask_size = sum(s for _, s in state.book.asks[:5])
    ratio = bid_size / ask_size if ask_size > 0 else 99.0
    _imbalance_history.append((ts, ratio))


def check_signals(state, now_s):
    """Micro scalp — fire when multiple weak signals align."""
    import bot.paper_trade_v2 as engine

    # Read configurable params from engine (allows config JSON overrides)
    _min_delta = getattr(engine, 'MS_MIN_DELTA_BPS', MS_MIN_DELTA_BPS)
    _min_imb = getattr(engine, 'MS_MIN_IMBALANCE', MS_MIN_IMBALANCE)
    _min_gap = getattr(engine, 'MS_MIN_FAIR_GAP', MS_MIN_FAIR_GAP)

    if not _table_loaded:
        _load_delta_table()

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < MS_MIN_BOOK_LEVELS or len(state.book.asks) < MS_MIN_BOOK_LEVELS:
        return
    if state.book.spread > MS_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < MS_MIN_TIME_REMAINING or time_remaining > MS_MAX_TIME_REMAINING:
        return

    # Compute all signals
    if not state.window_open or state.window_open <= 0:
        return
    btc_corrected = (state.binance_price or 0) - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # Signal 1: BTC delta confirms direction
    delta_up = delta_bps >= _min_delta
    delta_down = delta_bps <= -_min_delta

    # Signal 2: Book imbalance
    bid_size = sum(s for _, s in state.book.bids[:5])
    ask_size = sum(s for _, s in state.book.asks[:5])
    ratio = bid_size / ask_size if ask_size > 0 else 1.0
    imbalance_up = ratio >= _min_imb
    imbalance_down = (1/ratio if ratio > 0 else 1) >= _min_imb

    # Signal 3: Delta table fair value gap
    fair_gap_up = False
    fair_gap_down = False
    if _delta_table:
        db = _get_delta_bucket(delta_bps)
        tb = _snap_time_bucket(time_remaining)
        fair = _delta_table.get((db, tb))
        if fair is not None:
            yes_ask = state.book.best_ask
            no_cost = 1.0 - state.book.best_bid
            if fair - yes_ask >= _min_gap:
                fair_gap_up = True
            if (1 - fair) - no_cost >= _min_gap:
                fair_gap_down = True

    # Signal 4: Quick impulse (3bp in 15s from price buffer)
    impulse_up = False
    impulse_down = False
    if len(state.price_buffer) >= 5:
        price_now = state.binance_price
        price_15s = engine.find_price_at(state.price_buffer, now_s - 15)
        if price_15s and price_15s > 0:
            imp = (price_now - price_15s) / price_15s * 10000
            impulse_up = imp >= 3
            impulse_down = imp <= -3

    # Count UP signals and DOWN signals
    up_signals = sum([delta_up, imbalance_up, fair_gap_up, impulse_up])
    down_signals = sum([delta_down, imbalance_down, fair_gap_down, impulse_down])

    # Status
    now_t = time.time()
    if now_t - _last_status[0] >= 15:
        print("  {}{}{} {}T-{:>3.0f}s{} | delta {:+.1f}bp | imb {:.1f}x | UP:{} DN:{} | mid={:.0f}c".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            delta_bps, ratio, up_signals, down_signals, state.book.mid * 100))
        _last_status[0] = now_t

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_sigs = cfg.get("min_signals", 2)
        req_delta = cfg.get("require_delta", False)
        req_impulse = cfg.get("require_impulse", False)
        req_imbalance = cfg.get("require_imbalance", False)

        # Check UP
        if up_signals >= min_sigs:
            blocked = False
            if req_delta and not delta_up: blocked = True
            if req_impulse and not impulse_up: blocked = True
            if req_imbalance and not imbalance_up: blocked = True
            if not blocked:
                entry = state.book.best_ask
                actual_cost = entry
                if MS_MIN_ENTRY_PRICE <= actual_cost <= MS_MAX_ENTRY_PRICE:
                    engine.execute_paper_trade(combo, "YES", up_signals, time_remaining, entry)
                    continue

        # Check DOWN
        if down_signals >= min_sigs:
            blocked = False
            if req_delta and not delta_down: blocked = True
            if req_impulse and not impulse_down: blocked = True
            if req_imbalance and not imbalance_down: blocked = True
            if not blocked:
                entry = state.book.best_bid
                actual_cost = 1.0 - entry
                if MS_MIN_ENTRY_PRICE <= actual_cost <= MS_MAX_ENTRY_PRICE:
                    engine.execute_paper_trade(combo, "NO", down_signals, time_remaining, entry)


def on_window_start(state):
    _imbalance_history.clear()
    _last_status[0] = 0.0


ARCH_SPEC = {
    "name": "micro_scalp",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "MS_MIN_DELTA_BPS": MS_MIN_DELTA_BPS,
        "MS_MIN_IMBALANCE": MS_MIN_IMBALANCE,
        "MS_MIN_FAIR_GAP": MS_MIN_FAIR_GAP,
        "MS_MAX_SPREAD": MS_MAX_SPREAD,
        "MS_DELTA_TABLE_PATH": MS_DELTA_TABLE_PATH,
        "MS_MIN_ENTRY_PRICE": MS_MIN_ENTRY_PRICE,
        "MS_MAX_ENTRY_PRICE": MS_MAX_ENTRY_PRICE,
        "MIN_ENTRY_PRICE": 0.01,
        "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0,
        "DEAD_ZONE_END": 0,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
