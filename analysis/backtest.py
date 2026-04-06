#!/usr/bin/env python3
"""
Backtest 5 trading architectures against 60 days of historical BTC/Polymarket data.

Replays impulse_lag, delta_sniper, gamma_snipe, lottery_fade, and certainty_premium
across ~11,000 settled 5-minute windows using 1-second BTC klines and PM snapshots.

Usage:  python3 analysis/backtest.py
Output: stdout summary + output/backtest_results.txt
"""

import csv
import math
import os
import sys
import time
from collections import defaultdict
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths (relative to repo root)
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
OUTPUT = BASE / "output"
OUTPUT.mkdir(exist_ok=True)

KLINES_PATH = DATA / "binance_klines_60d.parquet"
PRICE_HIST_PATH = DATA / "price_history_60d.parquet"
SETTLEMENTS_PATH = DATA / "polymarket_settlements_60d.parquet"
DELTA_TABLE_PATH = DATA / "delta_table_corrected.csv"
RESULTS_PATH = OUTPUT / "backtest_results.txt"

# ---------------------------------------------------------------------------
# Fee / sizing model
# ---------------------------------------------------------------------------
FEE_RATE = 0.072  # 7.2% of notional risk


def compute_fee(entry_price):
    """PM fee = entry_price * (1 - entry_price) * 0.072"""
    return entry_price * (1.0 - entry_price) * FEE_RATE


def compute_shares(entry_price, budget=100.0, cap=500):
    """Position size: $budget / entry_price, capped at `cap` shares."""
    if entry_price <= 0:
        return 0
    return min(budget / entry_price, cap)


def compute_pnl(direction, entry_price, outcome, shares):
    """
    Compute PnL for one trade.
    YES trade: buy at entry_price, settle to 1.0 if Up else 0.
    NO  trade: buy at (1-yes_price), settle to 1.0 if Down else 0.
    """
    fee = compute_fee(entry_price) * shares
    if direction == "YES":
        if outcome == "Up":
            return (1.0 - entry_price) * shares - fee
        else:
            return -entry_price * shares - fee
    else:  # NO
        no_cost = 1.0 - entry_price  # entry_price is the pm_yes_price
        if outcome == "Down":
            return (1.0 - no_cost) * shares - fee
        else:
            return -no_cost * shares - fee


# ---------------------------------------------------------------------------
# Normal CDF approximation (Abramowitz & Stegun 26.2.17)
# ---------------------------------------------------------------------------
try:
    from scipy.stats import norm as _norm
    def normal_cdf(x):
        return float(_norm.cdf(x))
except ImportError:
    def normal_cdf(x):
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


# ---------------------------------------------------------------------------
# Delta table loader
# ---------------------------------------------------------------------------
def load_delta_table():
    """Load the delta_table_corrected.csv into a dict: (delta_bucket, time_bucket) -> win_rate."""
    table = {}
    with open(DELTA_TABLE_PATH) as f:
        reader = csv.DictReader(f)
        for row in reader:
            db = row["delta_bucket"]
            tb = int(row["time_bucket"])
            wr = float(row["win_rate"])
            table[(db, tb)] = wr
    return table


def get_delta_bucket(delta_bps):
    """Map delta bps to the bucket string used in the delta table."""
    if delta_bps < -15:
        return "<-15"
    if delta_bps >= 15:
        return ">15"
    edges = [(-15, -10), (-10, -7), (-7, -5), (-5, -3), (-3, -1), (-1, 1), (1, 3), (3, 5), (5, 7), (7, 10), (10, 15)]
    for lo, hi in edges:
        if lo <= delta_bps < hi:
            return "[{},{})".format(lo, hi)
    return ">15"


def snap_time_bucket(time_remaining):
    """Snap time_remaining to the nearest bucket (10,30,60,...,270)."""
    buckets = [10, 30, 60, 90, 120, 150, 180, 210, 240, 270]
    best = 10
    best_dist = abs(time_remaining - 10)
    for b in buckets:
        d = abs(time_remaining - b)
        if d < best_dist:
            best = b
            best_dist = d
    return best


# ---------------------------------------------------------------------------
# PM price interpolation (nearest-neighbor, stepwise)
# ---------------------------------------------------------------------------
def build_pm_interpolator(snapshots):
    """
    Given a list of (timestamp, pm_yes_price) sorted by timestamp,
    return a function that returns pm_yes_price for any timestamp
    using nearest-neighbor interpolation.
    """
    if not snapshots:
        return lambda t: None
    ts_arr = np.array([s[0] for s in snapshots], dtype=np.float64)
    px_arr = np.array([s[1] for s in snapshots], dtype=np.float64)

    def interp(t):
        if t <= ts_arr[0]:
            return float(px_arr[0])
        if t >= ts_arr[-1]:
            return float(px_arr[-1])
        idx = np.searchsorted(ts_arr, t)
        # nearest neighbor
        if idx == 0:
            return float(px_arr[0])
        if idx >= len(ts_arr):
            return float(px_arr[-1])
        if (t - ts_arr[idx - 1]) <= (ts_arr[idx] - t):
            return float(px_arr[idx - 1])
        return float(px_arr[idx])
    return interp


# ---------------------------------------------------------------------------
# Realized volatility from kline prices
# ---------------------------------------------------------------------------
def compute_realized_vol_from_prices(prices, vol_window=120):
    """
    Given an array of BTC close prices (1 per second), compute realized volatility
    as dollar vol per sqrt(second) using the last `vol_window` prices.
    Returns None if not enough data.
    """
    if len(prices) < 10:
        return None
    window = prices[-vol_window:] if len(prices) > vol_window else prices
    if len(window) < 10:
        return None
    log_returns = np.diff(np.log(window))
    if len(log_returns) < 5:
        return None
    vol_per_sqrt_sec = np.std(log_returns, ddof=1)
    dollar_vol = vol_per_sqrt_sec * window[-1]
    return max(dollar_vol, 0.5)  # floor $0.50


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------
class Trade:
    __slots__ = ('architecture', 'combo', 'direction', 'entry_price', 'shares',
                 'pnl', 'fee', 'outcome', 'window_start', 'time_remaining', 'signal_value')

    def __init__(self, architecture, combo, direction, entry_price, shares, pnl, fee,
                 outcome, window_start, time_remaining, signal_value=0.0):
        self.architecture = architecture
        self.combo = combo
        self.direction = direction
        self.entry_price = entry_price
        self.shares = shares
        self.pnl = pnl
        self.fee = fee
        self.outcome = outcome
        self.window_start = window_start
        self.time_remaining = time_remaining
        self.signal_value = signal_value


# ---------------------------------------------------------------------------
# Architecture signal functions
# Each returns a list of (combo_name, direction, entry_yes_price, time_remaining, signal_val)
# for the given second. At most one trade per combo per window.
# ---------------------------------------------------------------------------

# ---- impulse_lag combos ----
IL_COMBOS = [
    {"name": "A_5bp_30s",  "threshold": 5,  "lookback": 30},
    {"name": "B_7bp_30s",  "threshold": 7,  "lookback": 30},
    {"name": "C_10bp_30s", "threshold": 10, "lookback": 30},
    {"name": "D_15bp_15s", "threshold": 15, "lookback": 15},
    {"name": "E_5bp_15s",  "threshold": 5,  "lookback": 15},
    {"name": "F_7bp_15s",  "threshold": 7,  "lookback": 15},
    {"name": "G_3bp_10s",  "threshold": 3,  "lookback": 10},
    {"name": "H_5bp_10s",  "threshold": 5,  "lookback": 10},
    {"name": "I_10bp_15s", "threshold": 10, "lookback": 15},
    {"name": "J_7bp_45s",  "threshold": 7,  "lookback": 45},
    {"name": "K_5bp_60s",  "threshold": 5,  "lookback": 60},
    {"name": "L_10bp_60s", "threshold": 10, "lookback": 60},
]
IL_MAX_IMPULSE = 25
IL_DEAD_ZONE_START = 90
IL_DEAD_ZONE_END = 210


def impulse_lag_signals(sec_idx, btc_prices, pm_interp, window_start, ptb, outcome,
                        traded_combos, dead_zone=True):
    """
    Evaluate impulse_lag combos at second `sec_idx` within the window.
    btc_prices: array of 300 close prices (index 0 = second 0 of window).
    """
    signals = []
    if sec_idx < 1:
        return signals
    time_remaining = 300 - sec_idx
    if time_remaining < 5 or time_remaining > 295:
        return signals
    # Dead zone filter
    if dead_zone and IL_DEAD_ZONE_START <= time_remaining <= IL_DEAD_ZONE_END:
        return signals

    price_now = btc_prices[sec_idx]
    pm_yes = pm_interp(window_start + sec_idx)
    if pm_yes is None:
        return signals

    for combo in IL_COMBOS:
        cname = combo["name"]
        if dead_zone:
            key = "IL_" + cname
        else:
            key = "IL_nodead_" + cname
        if key in traded_combos:
            continue
        lookback = combo["lookback"]
        if sec_idx < lookback:
            continue
        price_ago = btc_prices[sec_idx - lookback]
        if price_ago <= 0:
            continue
        impulse_bps = (price_now - price_ago) / price_ago * 10000
        if abs(impulse_bps) < combo["threshold"]:
            continue
        if abs(impulse_bps) > IL_MAX_IMPULSE:
            continue
        direction = "YES" if impulse_bps > 0 else "NO"
        entry = pm_yes if direction == "YES" else pm_yes  # we store pm_yes_price always
        if entry < 0.10 or entry > 0.90:
            continue
        signals.append((key, direction, entry, time_remaining, impulse_bps))
        traded_combos.add(key)
    return signals


# ---- delta_sniper combos ----
DS_COMBOS = [
    {"name": "DS_2c_gap",     "min_gap": 0.02, "min_fair": 0.55},
    {"name": "DS_3c_gap",     "min_gap": 0.03, "min_fair": 0.55},
    {"name": "DS_5c_gap",     "min_gap": 0.05, "min_fair": 0.60},
    {"name": "DS_3c_strong",  "min_gap": 0.03, "min_fair": 0.70},
    {"name": "DS_5c_strong",  "min_gap": 0.05, "min_fair": 0.70},
    {"name": "DS_2c_extreme", "min_gap": 0.02, "min_fair": 0.80},
]


def delta_sniper_signals(sec_idx, btc_prices, pm_interp, window_start, ptb, outcome,
                          traded_combos, delta_table):
    signals = []
    time_remaining = 300 - sec_idx
    if time_remaining < 10 or time_remaining > 280:
        return signals

    btc_now = btc_prices[sec_idx]
    if ptb <= 0:
        return signals
    delta_bps = (btc_now - ptb) / ptb * 10000
    delta_bucket = get_delta_bucket(delta_bps)
    time_bucket = snap_time_bucket(time_remaining)
    fair_prob = delta_table.get((delta_bucket, time_bucket))
    if fair_prob is None:
        return signals

    pm_yes = pm_interp(window_start + sec_idx)
    if pm_yes is None:
        return signals

    for combo in DS_COMBOS:
        cname = combo["name"]
        if cname in traded_combos:
            continue
        min_gap = combo["min_gap"]
        min_fair = combo["min_fair"]

        # Check YES: fair_prob - pm_yes >= min_gap
        gap_yes = fair_prob - pm_yes
        if gap_yes >= min_gap and fair_prob >= min_fair:
            if 0.30 <= pm_yes <= 0.95:
                signals.append((cname, "YES", pm_yes, time_remaining, gap_yes * 100))
                traded_combos.add(cname)
                continue

        # Check NO: (1-fair_prob) - (1-pm_yes) >= min_gap
        no_fair = 1.0 - fair_prob
        no_cost = 1.0 - pm_yes
        gap_no = no_fair - no_cost
        if gap_no >= min_gap and no_fair >= min_fair:
            if 0.30 <= no_cost <= 0.95:
                signals.append((cname, "NO", pm_yes, time_remaining, gap_no * 100))
                traded_combos.add(cname)

    return signals


# ---- gamma_snipe combos ----
GS_COMBOS = [
    {"name": "GS_2c_10s", "min_gap": 0.02, "max_time": 10},
    {"name": "GS_2c_20s", "min_gap": 0.02, "max_time": 20},
    {"name": "GS_2c_30s", "min_gap": 0.02, "max_time": 30},
    {"name": "GS_3c_45s", "min_gap": 0.03, "max_time": 45},
    {"name": "GS_3c_60s", "min_gap": 0.03, "max_time": 60},
    {"name": "GS_5c_45s", "min_gap": 0.05, "max_time": 45},
    {"name": "GS_5c_60s", "min_gap": 0.05, "max_time": 60},
    {"name": "GS_8c_60s", "min_gap": 0.08, "max_time": 60},
]


def gamma_snipe_signals(sec_idx, btc_prices, pm_interp, window_start, ptb, outcome,
                         traded_combos):
    signals = []
    time_remaining = 300 - sec_idx
    # Only active in last 60s
    if time_remaining > 60 or time_remaining < 3:
        return signals

    btc_now = btc_prices[sec_idx]
    if ptb <= 0:
        return signals

    # Compute realized vol from prior 120s
    start_idx = max(0, sec_idx - 120)
    price_slice = btc_prices[start_idx:sec_idx + 1]
    vol = compute_realized_vol_from_prices(price_slice, vol_window=120)
    if vol is None:
        return signals

    # Fair value
    delta_dollars = btc_now - ptb
    uncertainty = vol * math.sqrt(time_remaining) if time_remaining > 0 else 0
    if uncertainty <= 0:
        fair = 1.0 if delta_dollars > 0 else 0.0
    else:
        z = delta_dollars / uncertainty
        fair = normal_cdf(z)

    pm_yes = pm_interp(window_start + sec_idx)
    if pm_yes is None:
        return signals

    for combo in GS_COMBOS:
        cname = combo["name"]
        if cname in traded_combos:
            continue
        min_gap = combo["min_gap"]
        max_time = combo["max_time"]
        if time_remaining > max_time:
            continue

        # Check YES
        gap_yes = fair - pm_yes
        if gap_yes >= min_gap:
            if 0.10 <= pm_yes <= 0.97:
                signals.append((cname, "YES", pm_yes, time_remaining, gap_yes * 100))
                traded_combos.add(cname)
                continue

        # Check NO
        no_fair = 1.0 - fair
        no_cost = 1.0 - pm_yes
        gap_no = no_fair - no_cost
        if gap_no >= min_gap:
            if 0.10 <= no_cost <= 0.97:
                signals.append((cname, "NO", pm_yes, time_remaining, gap_no * 100))
                traded_combos.add(cname)

    return signals


# ---- lottery_fade combos ----
LF_COMBOS = [
    {"name": "LF_z2_120s",   "min_z": 2.0, "max_time": 120},
    {"name": "LF_z2_90s",    "min_z": 2.0, "max_time": 90},
    {"name": "LF_z2.5_120s", "min_z": 2.5, "max_time": 120},
    {"name": "LF_z3_120s",   "min_z": 3.0, "max_time": 120},
    {"name": "LF_z2_60s",    "min_z": 2.0, "max_time": 60},
]
LF_MIN_LOSING_OVERPRICE = 0.03


def lottery_fade_signals(sec_idx, btc_prices, pm_interp, window_start, ptb, outcome,
                          traded_combos):
    signals = []
    time_remaining = 300 - sec_idx
    # Active in last 120s
    if time_remaining > 120 or time_remaining < 3:
        return signals

    btc_now = btc_prices[sec_idx]
    if ptb <= 0:
        return signals

    # Vol from prior 120s
    start_idx = max(0, sec_idx - 120)
    price_slice = btc_prices[start_idx:sec_idx + 1]
    vol = compute_realized_vol_from_prices(price_slice, vol_window=120)
    if vol is None:
        return signals

    uncertainty = vol * math.sqrt(time_remaining) if time_remaining > 0 else 0
    if uncertainty <= 0:
        return signals
    z = (btc_now - ptb) / uncertainty
    fair_up = normal_cdf(z)

    pm_yes = pm_interp(window_start + sec_idx)
    if pm_yes is None:
        return signals

    # Determine winning/losing side
    if z > 0:
        winning_dir = "YES"
        loser_fair = 1.0 - fair_up
        loser_market = 1.0 - pm_yes  # NO cost
    elif z < 0:
        winning_dir = "NO"
        loser_fair = fair_up
        loser_market = pm_yes  # YES price = loser cost
    else:
        return signals

    losing_overprice = loser_market - loser_fair

    for combo in LF_COMBOS:
        cname = combo["name"]
        if cname in traded_combos:
            continue
        min_z = combo["min_z"]
        max_time = combo["max_time"]
        if time_remaining > max_time:
            continue
        if abs(z) < min_z:
            continue
        if losing_overprice < LF_MIN_LOSING_OVERPRICE:
            continue

        # Buy winning side
        signals.append((cname, winning_dir, pm_yes, time_remaining, abs(z)))
        traded_combos.add(cname)

    return signals


# ---- certainty_premium combos ----
CP_COMBOS = [
    {"name": "CP_z1.5_60s_95c", "min_z": 1.5, "max_time": 60, "max_winner_price": 0.95},
    {"name": "CP_z2_60s_95c",   "min_z": 2.0, "max_time": 60, "max_winner_price": 0.95},
    {"name": "CP_z1.5_30s_95c", "min_z": 1.5, "max_time": 30, "max_winner_price": 0.95},
    {"name": "CP_z1.5_60s_92c", "min_z": 1.5, "max_time": 60, "max_winner_price": 0.92},
    {"name": "CP_z2_30s_93c",   "min_z": 2.0, "max_time": 30, "max_winner_price": 0.93},
]


def certainty_premium_signals(sec_idx, btc_prices, pm_interp, window_start, ptb, outcome,
                               traded_combos):
    signals = []
    time_remaining = 300 - sec_idx
    # Active in last 60s
    if time_remaining > 60 or time_remaining < 3:
        return signals

    btc_now = btc_prices[sec_idx]
    if ptb <= 0:
        return signals

    # Vol from prior 120s
    start_idx = max(0, sec_idx - 120)
    price_slice = btc_prices[start_idx:sec_idx + 1]
    vol = compute_realized_vol_from_prices(price_slice, vol_window=120)
    if vol is None:
        return signals

    uncertainty = vol * math.sqrt(time_remaining) if time_remaining > 0 else 0
    if uncertainty <= 0:
        return signals
    z = (btc_now - ptb) / uncertainty

    pm_yes = pm_interp(window_start + sec_idx)
    if pm_yes is None:
        return signals

    # Determine winning side and its cost
    if z > 0:
        winning_dir = "YES"
        winner_cost = pm_yes
    elif z < 0:
        winning_dir = "NO"
        winner_cost = 1.0 - pm_yes
    else:
        return signals

    for combo in CP_COMBOS:
        cname = combo["name"]
        if cname in traded_combos:
            continue
        min_z = combo["min_z"]
        max_time = combo["max_time"]
        max_wp = combo["max_winner_price"]
        if time_remaining > max_time:
            continue
        if abs(z) < min_z:
            continue
        if winner_cost > max_wp:
            continue
        if winner_cost < 0.80:
            continue

        signals.append((cname, winning_dir, pm_yes, time_remaining, abs(z)))
        traded_combos.add(cname)

    return signals


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------
def run_backtest():
    t0 = time.time()
    print("=" * 80)
    print("BACKTEST: 5 architectures x 60 days of historical data")
    print("=" * 80)
    print()

    # ------------------------------------------------------------------
    # Load data
    # ------------------------------------------------------------------
    print("Loading data...")
    t_load = time.time()

    klines = pd.read_parquet(KLINES_PATH)
    # Convert klines timestamp from ms to seconds and index by second
    klines["ts_sec"] = klines["timestamp"] // 1000
    # Build a fast lookup: ts_sec -> close price
    # Since there are 5.2M rows, we use a dict for O(1) lookup
    print(f"  Klines: {len(klines):,} rows")

    settlements = pd.read_parquet(SETTLEMENTS_PATH)
    settlements = settlements.dropna(subset=["chainlink_open"])
    print(f"  Settlements: {len(settlements):,} windows")

    price_hist = pd.read_parquet(PRICE_HIST_PATH)
    print(f"  Price history: {len(price_hist):,} snapshots")

    delta_table = load_delta_table()
    print(f"  Delta table: {len(delta_table)} entries")

    # Build kline lookup: ts_sec -> close price
    # Use numpy arrays for fast slicing
    kline_ts = klines["ts_sec"].values
    kline_close = klines["close"].values
    kline_min_ts = int(kline_ts[0])
    kline_max_ts = int(kline_ts[-1])

    # Build a dense array indexed by (ts - kline_min_ts) for O(1) lookup
    # This uses ~40MB for 5M seconds but is very fast
    print("  Building kline index...")
    kline_range = kline_max_ts - kline_min_ts + 1
    kline_arr = np.full(kline_range, np.nan, dtype=np.float64)
    kline_arr[kline_ts.astype(np.int64) - kline_min_ts] = kline_close
    # Forward-fill NaNs (some seconds may be missing)
    mask = np.isnan(kline_arr)
    if mask.any():
        idx_arr = np.arange(len(kline_arr))
        valid = ~mask
        kline_arr[mask] = np.interp(idx_arr[mask], idx_arr[valid], kline_arr[valid])

    # Build PM snapshot lookup: window_start -> sorted list of (timestamp, pm_yes_price)
    print("  Building PM snapshot index...")
    pm_snaps = defaultdict(list)
    for _, row in price_hist.iterrows():
        pm_snaps[int(row["window_start"])].append((int(row["timestamp"]), float(row["polymarket_yes_price"])))
    for ws in pm_snaps:
        pm_snaps[ws].sort()

    print(f"  Data loaded in {time.time() - t_load:.1f}s")
    print()

    # ------------------------------------------------------------------
    # Filter windows to those with kline coverage
    # ------------------------------------------------------------------
    valid_windows = settlements[
        (settlements["window_start"] >= kline_min_ts) &
        (settlements["window_start"] + 300 <= kline_max_ts)
    ].copy()
    valid_windows = valid_windows.sort_values("window_start").reset_index(drop=True)
    n_windows = len(valid_windows)
    print(f"Windows with full kline coverage: {n_windows}")
    print()

    # ------------------------------------------------------------------
    # Run simulation
    # ------------------------------------------------------------------
    all_trades = []

    print("Running backtest...")
    t_sim = time.time()

    for wi, row in valid_windows.iterrows():
        ws = int(row["window_start"])
        ptb = float(row["chainlink_open"])
        outcome = row["outcome"]

        if wi > 0 and wi % 1000 == 0:
            elapsed = time.time() - t_sim
            rate = wi / elapsed if elapsed > 0 else 0
            print(f"  Window {wi:,}/{n_windows:,} ({wi/n_windows*100:.0f}%) - {rate:.0f} windows/sec - {len(all_trades):,} trades so far")

        # Extract 300 seconds of BTC prices for this window
        offset = ws - kline_min_ts
        if offset < 0 or offset + 300 > len(kline_arr):
            continue
        btc_prices = kline_arr[offset:offset + 300]
        if np.isnan(btc_prices).any():
            continue

        # Build PM interpolator for this window
        snaps = pm_snaps.get(ws)
        if not snaps:
            continue
        pm_interp = build_pm_interpolator(snaps)

        # Track which combos have already traded this window (one trade per combo per window)
        traded_il = set()
        traded_il_nodead = set()
        traded_ds = set()
        traded_gs = set()
        traded_lf = set()
        traded_cp = set()

        # Simulate every second of the 300-second window
        for sec in range(300):
            # impulse_lag (with dead zone)
            sigs = impulse_lag_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                       traded_il, dead_zone=True)
            for (cname, direction, pm_yes_price, t_rem, sig_val) in sigs:
                if direction == "YES":
                    entry_price = pm_yes_price
                else:
                    entry_price = 1.0 - pm_yes_price
                shares = compute_shares(entry_price)
                fee = compute_fee(entry_price) * shares
                pnl = compute_pnl(direction, pm_yes_price, outcome, shares)
                all_trades.append(Trade("impulse_lag", cname, direction, entry_price,
                                        shares, pnl, fee, outcome, ws, t_rem, sig_val))

            # impulse_lag (without dead zone)
            sigs = impulse_lag_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                       traded_il_nodead, dead_zone=False)
            for (cname, direction, pm_yes_price, t_rem, sig_val) in sigs:
                if direction == "YES":
                    entry_price = pm_yes_price
                else:
                    entry_price = 1.0 - pm_yes_price
                shares = compute_shares(entry_price)
                fee = compute_fee(entry_price) * shares
                pnl = compute_pnl(direction, pm_yes_price, outcome, shares)
                all_trades.append(Trade("impulse_lag_nodead", cname, direction, entry_price,
                                        shares, pnl, fee, outcome, ws, t_rem, sig_val))

            # delta_sniper
            sigs = delta_sniper_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                         traded_ds, delta_table)
            for (cname, direction, pm_yes_price, t_rem, sig_val) in sigs:
                if direction == "YES":
                    entry_price = pm_yes_price
                else:
                    entry_price = 1.0 - pm_yes_price
                shares = compute_shares(entry_price)
                fee = compute_fee(entry_price) * shares
                pnl = compute_pnl(direction, pm_yes_price, outcome, shares)
                all_trades.append(Trade("delta_sniper", cname, direction, entry_price,
                                        shares, pnl, fee, outcome, ws, t_rem, sig_val))

            # gamma_snipe
            sigs = gamma_snipe_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                        traded_gs)
            for (cname, direction, pm_yes_price, t_rem, sig_val) in sigs:
                if direction == "YES":
                    entry_price = pm_yes_price
                else:
                    entry_price = 1.0 - pm_yes_price
                shares = compute_shares(entry_price)
                fee = compute_fee(entry_price) * shares
                pnl = compute_pnl(direction, pm_yes_price, outcome, shares)
                all_trades.append(Trade("gamma_snipe", cname, direction, entry_price,
                                        shares, pnl, fee, outcome, ws, t_rem, sig_val))

            # lottery_fade
            sigs = lottery_fade_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                         traded_lf)
            for (cname, direction, pm_yes_price, t_rem, sig_val) in sigs:
                if direction == "YES":
                    entry_price = pm_yes_price
                else:
                    entry_price = 1.0 - pm_yes_price
                shares = compute_shares(entry_price)
                fee = compute_fee(entry_price) * shares
                pnl = compute_pnl(direction, pm_yes_price, outcome, shares)
                all_trades.append(Trade("lottery_fade", cname, direction, entry_price,
                                        shares, pnl, fee, outcome, ws, t_rem, sig_val))

            # certainty_premium
            sigs = certainty_premium_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                              traded_cp)
            for (cname, direction, pm_yes_price, t_rem, sig_val) in sigs:
                if direction == "YES":
                    entry_price = pm_yes_price
                else:
                    entry_price = 1.0 - pm_yes_price
                shares = compute_shares(entry_price)
                fee = compute_fee(entry_price) * shares
                pnl = compute_pnl(direction, pm_yes_price, outcome, shares)
                all_trades.append(Trade("certainty_premium", cname, direction, entry_price,
                                        shares, pnl, fee, outcome, ws, t_rem, sig_val))

    sim_time = time.time() - t_sim
    print(f"\nSimulation complete: {n_windows:,} windows in {sim_time:.1f}s ({n_windows/sim_time:.0f} windows/sec)")
    print(f"Total trades: {len(all_trades):,}")
    print()

    # ------------------------------------------------------------------
    # Analyze results
    # ------------------------------------------------------------------
    output = StringIO()

    def pr(msg=""):
        print(msg)
        output.write(msg + "\n")

    pr("=" * 100)
    pr("BACKTEST RESULTS: 5 Architectures x 60 Days ({:,} windows)".format(n_windows))
    pr("=" * 100)
    pr()

    # Compute sub-period boundaries
    all_ws = valid_windows["window_start"].values
    ws_min, ws_max = int(all_ws[0]), int(all_ws[-1])
    period_len = (ws_max - ws_min) / 3
    periods = [
        ("First 20 days",  ws_min, ws_min + period_len),
        ("Middle 20 days", ws_min + period_len, ws_min + 2 * period_len),
        ("Last 20 days",   ws_min + 2 * period_len, ws_max + 1),
    ]

    # Group trades by architecture
    arch_trades = defaultdict(list)
    for t in all_trades:
        arch_trades[t.architecture].append(t)

    arch_order = ["impulse_lag", "impulse_lag_nodead", "delta_sniper",
                  "gamma_snipe", "lottery_fade", "certainty_premium"]

    highlighted = []  # (arch, combo, n, wr, pnl) for profitable + 100+ trades

    for arch in arch_order:
        trades = arch_trades.get(arch, [])
        if not trades:
            pr("-" * 100)
            pr(f"ARCHITECTURE: {arch}")
            pr("  No trades generated.")
            pr()
            continue

        pr("-" * 100)
        pr(f"ARCHITECTURE: {arch}")
        pr("-" * 100)

        # Per-combo breakdown
        combo_trades = defaultdict(list)
        for t in trades:
            combo_trades[t.combo].append(t)

        # Sort combos by name
        combo_names = sorted(combo_trades.keys())

        pr(f"{'Combo':<25} {'N':>6} {'Win%':>6} {'TotalPnL':>10} {'AvgPnL':>8} {'AvgWin':>8} {'AvgLoss':>9} {'R:R':>6} {'Sharpe':>7} {'MaxDD':>9}")
        pr("-" * 100)

        arch_total_pnl = 0.0
        arch_total_trades = 0
        arch_total_wins = 0

        for cn in combo_names:
            ct = combo_trades[cn]
            n = len(ct)
            pnls = [t.pnl for t in ct]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            total_pnl = sum(pnls)
            avg_pnl = total_pnl / n if n > 0 else 0
            win_rate = len(wins) / n * 100 if n > 0 else 0
            avg_win = sum(wins) / len(wins) if wins else 0
            avg_loss = sum(losses) / len(losses) if losses else 0
            rr = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
            rr_str = f"{rr:.2f}" if rr != float('inf') else "inf"

            # Sharpe: mean / std of per-trade pnl (annualized makes no sense here)
            if n > 1:
                std_pnl = np.std(pnls, ddof=1)
                sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0
            else:
                sharpe = 0

            # Max drawdown
            cum = np.cumsum(pnls)
            running_max = np.maximum.accumulate(cum)
            dd = running_max - cum
            max_dd = np.max(dd) if len(dd) > 0 else 0

            marker = ""
            if total_pnl > 0 and n >= 100:
                marker = " ***"
                highlighted.append((arch, cn, n, win_rate, total_pnl))

            pr(f"{cn:<25} {n:>6} {win_rate:>5.1f}% ${total_pnl:>9.2f} ${avg_pnl:>7.2f} ${avg_win:>7.2f} ${avg_loss:>8.2f} {rr_str:>6} {sharpe:>7.3f} ${max_dd:>8.2f}{marker}")

            arch_total_pnl += total_pnl
            arch_total_trades += n
            arch_total_wins += len(wins)

        pr()
        arch_wr = arch_total_wins / arch_total_trades * 100 if arch_total_trades > 0 else 0
        pr(f"  AGGREGATE: {arch_total_trades:,} trades | {arch_wr:.1f}% win rate | ${arch_total_pnl:,.2f} total PnL | ${arch_total_pnl/arch_total_trades:.2f}/trade" if arch_total_trades > 0 else "  AGGREGATE: 0 trades")

        # YES vs NO split
        yes_trades = [t for t in trades if t.direction == "YES"]
        no_trades = [t for t in trades if t.direction == "NO"]
        yes_pnl = sum(t.pnl for t in yes_trades)
        no_pnl = sum(t.pnl for t in no_trades)
        yes_wr = sum(1 for t in yes_trades if t.pnl > 0) / len(yes_trades) * 100 if yes_trades else 0
        no_wr = sum(1 for t in no_trades if t.pnl > 0) / len(no_trades) * 100 if no_trades else 0
        pr(f"  YES: {len(yes_trades):,} trades | {yes_wr:.1f}% WR | ${yes_pnl:,.2f} PnL")
        pr(f"  NO:  {len(no_trades):,} trades | {no_wr:.1f}% WR | ${no_pnl:,.2f} PnL")

        # Sub-period analysis
        pr()
        pr(f"  {'Period':<20} {'N':>6} {'Win%':>6} {'TotalPnL':>10} {'AvgPnL':>8}")
        for pname, pstart, pend in periods:
            pt = [t for t in trades if pstart <= t.window_start < pend]
            pn = len(pt)
            ppnl = sum(t.pnl for t in pt)
            pwr = sum(1 for t in pt if t.pnl > 0) / pn * 100 if pn > 0 else 0
            pavg = ppnl / pn if pn > 0 else 0
            pr(f"  {pname:<20} {pn:>6} {pwr:>5.1f}% ${ppnl:>9.2f} ${pavg:>7.2f}")
        pr()

    # ------------------------------------------------------------------
    # Summary of highlighted combos
    # ------------------------------------------------------------------
    pr("=" * 100)
    pr("HIGHLIGHTED: Profitable combos with 100+ trades (statistically meaningful)")
    pr("=" * 100)
    if highlighted:
        pr(f"{'Architecture':<25} {'Combo':<25} {'N':>6} {'Win%':>6} {'TotalPnL':>10}")
        pr("-" * 80)
        for (arch, cn, n, wr, pnl) in sorted(highlighted, key=lambda x: -x[4]):
            pr(f"{arch:<25} {cn:<25} {n:>6} {wr:>5.1f}% ${pnl:>9.2f}")
    else:
        pr("  None found.")
    pr()

    # ------------------------------------------------------------------
    # Grand total
    # ------------------------------------------------------------------
    total_pnl = sum(t.pnl for t in all_trades)
    total_fees = sum(t.fee for t in all_trades)
    total_wins = sum(1 for t in all_trades if t.pnl > 0)
    total_n = len(all_trades)
    pr("=" * 100)
    pr(f"GRAND TOTAL: {total_n:,} trades | {total_wins/total_n*100:.1f}% WR | ${total_pnl:,.2f} PnL | ${total_fees:,.2f} fees" if total_n > 0 else "GRAND TOTAL: 0 trades")
    pr(f"Runtime: {time.time() - t0:.1f}s")
    pr("=" * 100)

    # Save to file
    with open(RESULTS_PATH, "w") as f:
        f.write(output.getvalue())
    print(f"\nResults saved to {RESULTS_PATH}")


if __name__ == "__main__":
    run_backtest()
