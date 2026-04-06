"""
Architecture: delta_sniper
Thesis: PM's book mid systematically deviates from the true fair probability derived from
the historical delta table. At any given (delta_bucket, time_remaining) pair, we know the
historical win rate for UP. When PM's YES ask price is significantly below this fair value,
or PM's NO cost is significantly below (1 - fair_value), there's a mispricing to exploit.
This is NOT a momentum strategy — it's a fair-value arbitrage. The edge exists because PM
market makers are slow to reprice to match the delta-implied probability, especially in the
middle of windows (T-60 to T-180) where the delta table is most informative.
"""

import csv
import time
from pathlib import Path

# Delta table: (delta_bucket, time_bucket) -> win_rate
_delta_table = {}
_table_loaded = False

DS_DELTA_TABLE_PATH = "data/delta_table_corrected.csv"
DS_MIN_TIME_REMAINING = 10
DS_MAX_TIME_REMAINING = 280
DS_MAX_SPREAD = 0.04
DS_MIN_BOOK_LEVELS = 2
DS_MIN_ENTRY_PRICE = 0.30   # trade when actual cost > 30c
DS_MAX_ENTRY_PRICE = 0.95


def _load_delta_table():
    """Load the delta table CSV into a lookup dict."""
    global _delta_table, _table_loaded
    path = Path(DS_DELTA_TABLE_PATH)
    if not path.exists():
        print("  [delta_sniper] WARNING: delta table not found at {}".format(path))
        _table_loaded = True
        return
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            db = row["delta_bucket"]
            tb = int(row["time_bucket"])
            wr = float(row["win_rate"])
            _delta_table[(db, tb)] = wr
    _table_loaded = True
    print("  [delta_sniper] Loaded {} delta table entries".format(len(_delta_table)))


def _get_delta_bucket(delta_bps):
    """Map a delta (in bps) to the bucket string used in the delta table."""
    if delta_bps < -15:
        return "<-15"
    if delta_bps > 15:
        return ">15"
    # Buckets: [-15,-7), [-7,-5), [-5,-3), [-3,-1), [-1,1), [1,3), [3,5), [5,7), [7,15)
    edges = [(-15, -7), (-7, -5), (-5, -3), (-3, -1), (-1, 1), (1, 3), (3, 5), (5, 7), (7, 15)]
    for lo, hi in edges:
        if lo <= delta_bps < hi:
            return "[{},{})".format(lo, hi)
    return "[7,15)"


def _snap_time_bucket(time_remaining):
    """Snap time_remaining to the nearest bucket in the delta table (10,30,60,...,270)."""
    buckets = [10, 30, 60, 90, 120, 150, 180, 210, 240, 270]
    best = 10
    best_dist = abs(time_remaining - 10)
    for b in buckets:
        d = abs(time_remaining - b)
        if d < best_dist:
            best = b
            best_dist = d
    return best


COMBO_PARAMS = [
    {"name": "DS_2c_gap",     "btc_threshold_bp": 0, "lookback_s": 0, "min_gap_cents": 2,  "min_fair_prob": 0.55},
    {"name": "DS_3c_gap",     "btc_threshold_bp": 0, "lookback_s": 0, "min_gap_cents": 3,  "min_fair_prob": 0.55},
    {"name": "DS_5c_gap",     "btc_threshold_bp": 0, "lookback_s": 0, "min_gap_cents": 5,  "min_fair_prob": 0.60},
    {"name": "DS_3c_strong",  "btc_threshold_bp": 0, "lookback_s": 0, "min_gap_cents": 3,  "min_fair_prob": 0.70},
    {"name": "DS_5c_strong",  "btc_threshold_bp": 0, "lookback_s": 0, "min_gap_cents": 5,  "min_fair_prob": 0.70},
    {"name": "DS_2c_extreme", "btc_threshold_bp": 0, "lookback_s": 0, "min_gap_cents": 2,  "min_fair_prob": 0.80},
]

# Store per-combo extra params (not in the Combo dataclass)
_combo_config = {p["name"]: p for p in COMBO_PARAMS}

_last_status = [0.0]


def check_signals(state, now_s):
    """Delta sniper signal detection — compare delta table fair value vs PM book price."""
    import bot.paper_trade_v2 as engine

    # Read configurable params from engine (allows config JSON overrides)
    _ds_min_entry = getattr(engine, 'DS_MIN_ENTRY_PRICE', DS_MIN_ENTRY_PRICE)
    _ds_max_entry = getattr(engine, 'DS_MAX_ENTRY_PRICE', DS_MAX_ENTRY_PRICE)

    if not _table_loaded:
        _load_delta_table()
    if not _delta_table:
        return

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < DS_MIN_BOOK_LEVELS or len(state.book.asks) < DS_MIN_BOOK_LEVELS:
        return
    if state.book.spread > DS_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < DS_MIN_TIME_REMAINING or time_remaining > DS_MAX_TIME_REMAINING:
        return

    # Compute current BTC delta from priceToBeat
    if not state.window_open or state.window_open <= 0:
        return
    btc_corrected = (state.binance_price or 0) - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    # Look up fair probability
    delta_bucket = _get_delta_bucket(delta_bps)
    time_bucket = _snap_time_bucket(time_remaining)
    fair_prob = _delta_table.get((delta_bucket, time_bucket))
    if fair_prob is None:
        return

    # Status line
    now_t = time.time()
    if now_t - _last_status[0] >= 15:
        print("  {}{}{} {}T-{:>3.0f}s{} | delta {:+.1f}bp -> fair={:.0f}% | book mid={:.0f}c".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            delta_bps, fair_prob * 100, state.book.mid * 100))
        _last_status[0] = now_t

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_gap = cfg.get("min_gap_cents", 5) / 100
        min_fair = cfg.get("min_fair_prob", 0.60)

        # Check YES side: fair_prob > PM ask + gap => YES is underpriced
        yes_cost = state.book.best_ask
        gap_yes = fair_prob - yes_cost

        if gap_yes >= min_gap and fair_prob >= min_fair:
            if _ds_min_entry <= yes_cost <= _ds_max_entry:
                engine.execute_paper_trade(combo, "YES", gap_yes * 100, time_remaining, yes_cost)
                continue

        # Check NO side: (1 - fair_prob) > (1 - best_bid) + gap => NO is underpriced
        no_fair = 1.0 - fair_prob
        no_cost = 1.0 - state.book.best_bid
        gap_no = no_fair - no_cost

        if gap_no >= min_gap and no_fair >= min_fair:
            if _ds_min_entry <= no_cost <= _ds_max_entry:
                engine.execute_paper_trade(combo, "NO", gap_no * 100, time_remaining, state.book.best_bid)


def on_window_start(state):
    """Reset per-window state."""
    _last_status[0] = 0.0


ARCH_SPEC = {
    "name": "delta_sniper",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "DS_DELTA_TABLE_PATH": DS_DELTA_TABLE_PATH,
        "DS_MIN_TIME_REMAINING": DS_MIN_TIME_REMAINING,
        "DS_MAX_TIME_REMAINING": DS_MAX_TIME_REMAINING,
        "DS_MAX_SPREAD": DS_MAX_SPREAD,
        "DS_MIN_BOOK_LEVELS": DS_MIN_BOOK_LEVELS,
        "DS_MIN_ENTRY_PRICE": DS_MIN_ENTRY_PRICE,
        "DS_MAX_ENTRY_PRICE": DS_MAX_ENTRY_PRICE,
        "MIN_ENTRY_PRICE": 0.01,   # disable the engine's default filter (we do our own)
        "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0,
        "DEAD_ZONE_END": 0,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": None,
}
