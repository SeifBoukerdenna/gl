#!/usr/bin/env python3
"""
Realistic backtester with slippage, partial fills, book depth penalty,
and no-look-ahead PM price interpolation.

Tests 5 architectures (impulse_lag, delta_sniper, gamma_snipe, lottery_fade,
certainty_premium) with configurable slippage levels (0c, 1c, 2c, 3c) and
reports per-architecture, per-combo, per-direction, per-period breakdowns.

Usage:  python3 analysis/backtest_realistic.py
Output: stdout + output/backtest_realistic_results.txt
"""

import csv
import math
import os
import random
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

# Try full data first (from fetch_data.py), fall back to 60-day originals
KLINES_FULL = DATA / "binance_klines_full.parquet"
SETTLEMENTS_FULL = DATA / "settlements_full.parquet"
PRICE_HIST_FULL = DATA / "price_history_full.parquet"

KLINES_60D = DATA / "binance_klines_60d.parquet"
SETTLEMENTS_60D = DATA / "polymarket_settlements_60d.parquet"
PRICE_HIST_60D = DATA / "price_history_60d.parquet"

DELTA_TABLE_PATH = DATA / "delta_table_corrected.csv"
RESULTS_PATH = OUTPUT / "backtest_realistic_results.txt"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FEE_RATE = 0.072
FILL_RATE = 0.90  # 90% of signals get filled
DEFAULT_SLIPPAGE = 0.01  # 1 cent
EXTREME_EXTRA_SLIPPAGE = 0.01  # extra 1c at extreme prices (>90c or <10c)
EXTREME_THRESHOLD_HIGH = 0.90
EXTREME_THRESHOLD_LOW = 0.10

# Seed for reproducibility (fill rate randomness)
random.seed(12345)
np.random.seed(12345)


# ---------------------------------------------------------------------------
# Normal CDF
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
        d = 0.3989422804014327
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
        return 0.5 + sign * (0.5 - d * math.exp(-0.5 * x * x) * poly)


# ---------------------------------------------------------------------------
# Delta table
# ---------------------------------------------------------------------------
def load_delta_table():
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
    if delta_bps < -15:
        return "<-15"
    if delta_bps >= 15:
        return ">15"
    edges = [(-15, -10), (-10, -7), (-7, -5), (-5, -3), (-3, -1),
             (-1, 1), (1, 3), (3, 5), (5, 7), (7, 10), (10, 15)]
    for lo, hi in edges:
        if lo <= delta_bps < hi:
            return "[{},{})".format(lo, hi)
    return ">15"


def snap_time_bucket(time_remaining):
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
# PM price interpolation -- CAUSAL only (look-back, no future peeking)
# ---------------------------------------------------------------------------
def build_pm_interpolator_causal(snapshots):
    """
    Given a list of (timestamp, pm_yes_price) sorted by timestamp,
    return a function that returns the most recent pm_yes_price BEFORE time t.
    This is causal: we cannot see future snapshots.
    """
    if not snapshots:
        return lambda t: None
    ts_arr = np.array([s[0] for s in snapshots], dtype=np.float64)
    px_arr = np.array([s[1] for s in snapshots], dtype=np.float64)

    def interp(t):
        if t < ts_arr[0]:
            return None  # no data before first snapshot
        # Find the last snapshot at or before t
        idx = np.searchsorted(ts_arr, t, side='right') - 1
        if idx < 0:
            return None
        return float(px_arr[idx])
    return interp


# ---------------------------------------------------------------------------
# Realized volatility
# ---------------------------------------------------------------------------
def compute_realized_vol_from_prices(prices, vol_window=120):
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
    return max(dollar_vol, 0.5)


# ---------------------------------------------------------------------------
# Realistic execution model
# ---------------------------------------------------------------------------
def apply_slippage(entry_price, direction, slippage, pm_yes_price):
    """
    Apply slippage to entry price.
    For YES buys: fill_price = entry_price + slippage (pay more)
    For NO buys: fill_price = entry_price - slippage (worse fill on bids)
    Also add book depth penalty for extreme prices.
    """
    extra = 0.0
    if pm_yes_price > EXTREME_THRESHOLD_HIGH or pm_yes_price < EXTREME_THRESHOLD_LOW:
        extra = EXTREME_EXTRA_SLIPPAGE

    total_slip = slippage + extra

    if direction == "YES":
        fill = entry_price + total_slip
    else:
        # NO side: entry_price = 1 - pm_yes. Worse fill means paying more for NO = lower pm_yes equivalent
        fill = entry_price + total_slip

    # Clamp to valid range
    fill = max(0.01, min(0.99, fill))
    return fill


def compute_fee(entry_price):
    return entry_price * (1.0 - entry_price) * FEE_RATE


def compute_shares(entry_price, budget=100.0, cap=500):
    if entry_price <= 0:
        return 0
    return min(budget / entry_price, cap)


def compute_pnl_realistic(direction, pm_yes_price, fill_price, outcome, shares):
    """
    Compute PnL with realistic fill price (after slippage).
    fill_price is the actual price paid for the share (YES cost or NO cost).
    """
    fee = compute_fee(fill_price) * shares
    if direction == "YES":
        if outcome == "Up":
            return (1.0 - fill_price) * shares - fee
        else:
            return -fill_price * shares - fee
    else:  # NO
        if outcome == "Down":
            return (1.0 - fill_price) * shares - fee
        else:
            return -fill_price * shares - fee


def should_fill():
    """Model 90% fill rate."""
    return random.random() < FILL_RATE


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------
class Trade:
    __slots__ = ('architecture', 'combo', 'direction', 'entry_price', 'fill_price',
                 'shares', 'pnl', 'fee', 'outcome', 'window_start', 'time_remaining',
                 'signal_value', 'slippage_applied')

    def __init__(self, architecture, combo, direction, entry_price, fill_price, shares,
                 pnl, fee, outcome, window_start, time_remaining, signal_value=0.0,
                 slippage_applied=0.0):
        self.architecture = architecture
        self.combo = combo
        self.direction = direction
        self.entry_price = entry_price
        self.fill_price = fill_price
        self.shares = shares
        self.pnl = pnl
        self.fee = fee
        self.outcome = outcome
        self.window_start = window_start
        self.time_remaining = time_remaining
        self.signal_value = signal_value
        self.slippage_applied = slippage_applied


# ---------------------------------------------------------------------------
# Architecture signal functions (identical logic to backtest.py)
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
                        traded_combos, dead_zone=True, entry_range=None):
    """
    entry_range: None (default 10-90) or tuple (lo, hi) e.g. (0.10, 0.40) for cheap.
    """
    signals = []
    if sec_idx < 1:
        return signals
    time_remaining = 300 - sec_idx
    if time_remaining < 5 or time_remaining > 295:
        return signals
    if dead_zone and IL_DEAD_ZONE_START <= time_remaining <= IL_DEAD_ZONE_END:
        return signals

    price_now = btc_prices[sec_idx]
    pm_yes = pm_interp(window_start + sec_idx)
    if pm_yes is None:
        return signals

    lo_bound = 0.10
    hi_bound = 0.90
    if entry_range:
        lo_bound, hi_bound = entry_range

    for combo in IL_COMBOS:
        cname = combo["name"]
        prefix = "IL_"
        if not dead_zone:
            prefix = "IL_nodead_"
        if entry_range:
            range_tag = f"_{int(lo_bound*100)}_{int(hi_bound*100)}"
            prefix = f"IL{'_nodead' if not dead_zone else ''}{range_tag}_"
        key = prefix + cname
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
        entry = pm_yes
        if entry < lo_bound or entry > hi_bound:
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
        gap_yes = fair_prob - pm_yes
        if gap_yes >= min_gap and fair_prob >= min_fair:
            if 0.30 <= pm_yes <= 0.95:
                signals.append((cname, "YES", pm_yes, time_remaining, gap_yes * 100))
                traded_combos.add(cname)
                continue
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
    if time_remaining > 60 or time_remaining < 3:
        return signals
    btc_now = btc_prices[sec_idx]
    if ptb <= 0:
        return signals
    start_idx = max(0, sec_idx - 120)
    price_slice = btc_prices[start_idx:sec_idx + 1]
    vol = compute_realized_vol_from_prices(price_slice, vol_window=120)
    if vol is None:
        return signals
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
        gap_yes = fair - pm_yes
        if gap_yes >= min_gap:
            if 0.10 <= pm_yes <= 0.97:
                signals.append((cname, "YES", pm_yes, time_remaining, gap_yes * 100))
                traded_combos.add(cname)
                continue
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
    if time_remaining > 120 or time_remaining < 3:
        return signals
    btc_now = btc_prices[sec_idx]
    if ptb <= 0:
        return signals
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
    if z > 0:
        winning_dir = "YES"
        loser_fair = 1.0 - fair_up
        loser_market = 1.0 - pm_yes
    elif z < 0:
        winning_dir = "NO"
        loser_fair = fair_up
        loser_market = pm_yes
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
    if time_remaining > 60 or time_remaining < 3:
        return signals
    btc_now = btc_prices[sec_idx]
    if ptb <= 0:
        return signals
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
# Execute a signal with realistic model
# ---------------------------------------------------------------------------
def execute_signal(arch_name, cname, direction, pm_yes_price, time_remaining,
                   sig_val, outcome, window_start, slippage, budget=100.0, cap=500):
    """
    Apply fill rate, slippage, fees and return a Trade or None (if not filled).
    """
    if not should_fill():
        return None  # 10% random no-fill

    # Determine entry cost (pre-slippage)
    if direction == "YES":
        raw_entry = pm_yes_price
    else:
        raw_entry = 1.0 - pm_yes_price

    # Apply slippage
    fill_price = apply_slippage(raw_entry, direction, slippage, pm_yes_price)

    # Position sizing
    shares = compute_shares(fill_price, budget, cap)
    if shares <= 0:
        return None

    # Fee
    fee = compute_fee(fill_price) * shares

    # PnL
    pnl = compute_pnl_realistic(direction, pm_yes_price, fill_price, outcome, shares)

    return Trade(
        architecture=arch_name,
        combo=cname,
        direction=direction,
        entry_price=raw_entry,
        fill_price=fill_price,
        shares=shares,
        pnl=pnl,
        fee=fee,
        outcome=outcome,
        window_start=window_start,
        time_remaining=time_remaining,
        signal_value=sig_val,
        slippage_applied=slippage,
    )


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------
def analyze_trades(trades, label, output_fn, combo_detail=True):
    """Analyze and print trade stats."""
    if not trades:
        output_fn(f"  No trades for {label}")
        output_fn("")
        return

    # Per-combo breakdown
    combo_trades = defaultdict(list)
    for t in trades:
        combo_trades[t.combo].append(t)

    combo_names = sorted(combo_trades.keys())

    if combo_detail:
        output_fn(f"{'Combo':<30} {'N':>5} {'Win%':>6} {'PnL':>10} {'$/Trade':>8} "
                  f"{'AvgWin':>8} {'AvgLoss':>9} {'R:R':>6} {'Sharpe':>7} {'MaxDD':>9}")
        output_fn("-" * 110)

    arch_total_pnl = 0.0
    arch_total_trades = 0
    arch_total_wins = 0
    highlighted = []

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

        if n > 1:
            std_pnl = np.std(pnls, ddof=1)
            sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0
        else:
            sharpe = 0

        cum = np.cumsum(pnls)
        running_max = np.maximum.accumulate(cum)
        dd = running_max - cum
        max_dd = np.max(dd) if len(dd) > 0 else 0

        marker = ""
        if total_pnl > 0 and n >= 50:
            marker = " ***"
            highlighted.append((cn, n, win_rate, total_pnl, avg_pnl))

        if combo_detail:
            output_fn(f"{cn:<30} {n:>5} {win_rate:>5.1f}% ${total_pnl:>9.2f} ${avg_pnl:>7.2f} "
                      f"${avg_win:>7.2f} ${avg_loss:>8.2f} {rr_str:>6} {sharpe:>7.3f} ${max_dd:>8.2f}{marker}")

        arch_total_pnl += total_pnl
        arch_total_trades += n
        arch_total_wins += len(wins)

    output_fn("")
    arch_wr = arch_total_wins / arch_total_trades * 100 if arch_total_trades > 0 else 0
    avg_per_trade = arch_total_pnl / arch_total_trades if arch_total_trades > 0 else 0
    output_fn(f"  AGGREGATE: {arch_total_trades:,} trades | {arch_wr:.1f}% win rate | "
              f"${arch_total_pnl:,.2f} total PnL | ${avg_per_trade:.2f}/trade")

    # YES vs NO split
    yes_trades = [t for t in trades if t.direction == "YES"]
    no_trades = [t for t in trades if t.direction == "NO"]
    yes_pnl = sum(t.pnl for t in yes_trades)
    no_pnl = sum(t.pnl for t in no_trades)
    yes_wr = sum(1 for t in yes_trades if t.pnl > 0) / len(yes_trades) * 100 if yes_trades else 0
    no_wr = sum(1 for t in no_trades if t.pnl > 0) / len(no_trades) * 100 if no_trades else 0
    output_fn(f"  YES: {len(yes_trades):,} trades, {yes_wr:.1f}% WR, ${yes_pnl:,.2f} PnL")
    output_fn(f"  NO:  {len(no_trades):,} trades, {no_wr:.1f}% WR, ${no_pnl:,.2f} PnL")

    return arch_total_pnl, arch_total_trades, highlighted


def analyze_periods(trades, periods, output_fn):
    """Print period stability analysis."""
    output_fn("")
    output_fn(f"  Period stability:")
    output_fn(f"  {'Period':<25} {'N':>6} {'Win%':>6} {'TotalPnL':>10} {'$/Trade':>8}")
    for pname, pstart, pend in periods:
        pt = [t for t in trades if pstart <= t.window_start < pend]
        pn = len(pt)
        ppnl = sum(t.pnl for t in pt)
        pwr = sum(1 for t in pt if t.pnl > 0) / pn * 100 if pn > 0 else 0
        pavg = ppnl / pn if pn > 0 else 0
        output_fn(f"    {pname:<23} {pn:>6} {pwr:>5.1f}% ${ppnl:>9.2f} ${pavg:>7.2f}")
    output_fn("")


# ---------------------------------------------------------------------------
# Main simulation for one slippage level
# ---------------------------------------------------------------------------
def run_simulation(kline_arr, kline_min_ts, kline_max_ts, valid_windows, pm_snaps,
                   delta_table, slippage, test_il_ranges=False,
                   test_lf_sizing=False, test_cp_sizing=False):
    """
    Run the full simulation for a given slippage level.
    Returns dict: architecture_name -> list of Trade objects.
    """
    all_trades = defaultdict(list)
    n_windows = len(valid_windows)

    # Reset random seed for consistent fill rate across slippage levels
    random.seed(12345)

    for wi, row in valid_windows.iterrows():
        ws = int(row["window_start"])
        ptb = float(row["chainlink_open"])
        outcome = row["outcome"]

        if wi > 0 and wi % 2000 == 0:
            total_so_far = sum(len(v) for v in all_trades.values())
            sys.stdout.write(f"\r  Window {wi:,}/{n_windows:,} ({wi/n_windows*100:.0f}%) - "
                           f"{total_so_far:,} trades")
            sys.stdout.flush()

        # Extract 300 seconds of BTC prices
        offset = ws - kline_min_ts
        if offset < 0 or offset + 300 > len(kline_arr):
            continue
        btc_prices = kline_arr[offset:offset + 300]
        if np.isnan(btc_prices).any():
            continue

        # Build causal PM interpolator
        snaps = pm_snaps.get(ws)
        if not snaps:
            continue
        pm_interp = build_pm_interpolator_causal(snaps)

        # Tracked combos (one trade per combo per window)
        traded_il = set()
        traded_il_nodead = set()
        traded_ds = set()
        traded_gs = set()
        traded_lf = set()
        traded_cp = set()

        # Additional IL entry range tracked sets
        traded_il_cheap = set()
        traded_il_mid = set()
        traded_il_exp = set()

        # Additional sizing tracked sets
        traded_lf_small = set()
        traded_cp_small = set()

        for sec in range(300):
            # ---- impulse_lag (with dead zone) ----
            sigs = impulse_lag_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                       traded_il, dead_zone=True)
            for (cname, direction, pm_yes, t_rem, sig_val) in sigs:
                trade = execute_signal("impulse_lag", cname, direction, pm_yes,
                                       t_rem, sig_val, outcome, ws, slippage)
                if trade:
                    all_trades["impulse_lag"].append(trade)

            # ---- impulse_lag (no dead zone) ----
            sigs = impulse_lag_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                       traded_il_nodead, dead_zone=False)
            for (cname, direction, pm_yes, t_rem, sig_val) in sigs:
                trade = execute_signal("impulse_lag_nodead", cname, direction, pm_yes,
                                       t_rem, sig_val, outcome, ws, slippage)
                if trade:
                    all_trades["impulse_lag_nodead"].append(trade)

            # ---- IL entry range variants (with dead zone) ----
            if test_il_ranges:
                for range_name, entry_range in [
                    ("IL_cheap_10_40", (0.10, 0.40)),
                    ("IL_mid_40_60", (0.40, 0.60)),
                    ("IL_exp_60_90", (0.60, 0.90)),
                ]:
                    tracked = {"IL_cheap_10_40": traded_il_cheap,
                               "IL_mid_40_60": traded_il_mid,
                               "IL_exp_60_90": traded_il_exp}[range_name]
                    sigs = impulse_lag_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                               tracked, dead_zone=True, entry_range=entry_range)
                    for (cname, direction, pm_yes, t_rem, sig_val) in sigs:
                        trade = execute_signal(range_name, cname, direction, pm_yes,
                                               t_rem, sig_val, outcome, ws, slippage)
                        if trade:
                            all_trades[range_name].append(trade)

            # ---- delta_sniper ----
            sigs = delta_sniper_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                         traded_ds, delta_table)
            for (cname, direction, pm_yes, t_rem, sig_val) in sigs:
                trade = execute_signal("delta_sniper", cname, direction, pm_yes,
                                       t_rem, sig_val, outcome, ws, slippage)
                if trade:
                    all_trades["delta_sniper"].append(trade)

            # ---- gamma_snipe ----
            sigs = gamma_snipe_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                        traded_gs)
            for (cname, direction, pm_yes, t_rem, sig_val) in sigs:
                trade = execute_signal("gamma_snipe", cname, direction, pm_yes,
                                       t_rem, sig_val, outcome, ws, slippage)
                if trade:
                    all_trades["gamma_snipe"].append(trade)

            # ---- lottery_fade ($100 sizing) ----
            sigs = lottery_fade_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                         traded_lf)
            for (cname, direction, pm_yes, t_rem, sig_val) in sigs:
                trade = execute_signal("lottery_fade", cname, direction, pm_yes,
                                       t_rem, sig_val, outcome, ws, slippage)
                if trade:
                    all_trades["lottery_fade"].append(trade)

            # ---- lottery_fade ($25 sizing) ----
            if test_lf_sizing:
                sigs_lf_small = lottery_fade_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                                      traded_lf_small)
                for (cname, direction, pm_yes, t_rem, sig_val) in sigs_lf_small:
                    trade = execute_signal("lottery_fade_$25", cname, direction, pm_yes,
                                           t_rem, sig_val, outcome, ws, slippage,
                                           budget=25.0, cap=50)
                    if trade:
                        all_trades["lottery_fade_$25"].append(trade)

            # ---- certainty_premium ($100 sizing) ----
            sigs = certainty_premium_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                              traded_cp)
            for (cname, direction, pm_yes, t_rem, sig_val) in sigs:
                trade = execute_signal("certainty_premium", cname, direction, pm_yes,
                                       t_rem, sig_val, outcome, ws, slippage)
                if trade:
                    all_trades["certainty_premium"].append(trade)

            # ---- certainty_premium ($25 sizing) ----
            if test_cp_sizing:
                sigs_cp_small = certainty_premium_signals(sec, btc_prices, pm_interp, ws, ptb, outcome,
                                                           traded_cp_small)
                for (cname, direction, pm_yes, t_rem, sig_val) in sigs_cp_small:
                    trade = execute_signal("certainty_premium_$25", cname, direction, pm_yes,
                                           t_rem, sig_val, outcome, ws, slippage,
                                           budget=25.0, cap=50)
                    if trade:
                        all_trades["certainty_premium_$25"].append(trade)

    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()
    return dict(all_trades)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_backtest():
    t0 = time.time()

    # Use StringIO for dual output
    output = StringIO()

    def pr(msg=""):
        print(msg)
        output.write(msg + "\n")

    pr("=" * 110)
    pr("REALISTIC BACKTEST: Slippage + Fill Rate + Book Depth + Causal PM Prices")
    pr("=" * 110)
    pr()

    # ------------------------------------------------------------------
    # Load data (prefer full, fall back to 60d)
    # ------------------------------------------------------------------
    pr("Loading data...")
    t_load = time.time()

    klines_path = KLINES_FULL if KLINES_FULL.exists() else KLINES_60D
    settlements_path = SETTLEMENTS_FULL if SETTLEMENTS_FULL.exists() else SETTLEMENTS_60D
    price_hist_path = PRICE_HIST_FULL if PRICE_HIST_FULL.exists() else PRICE_HIST_60D

    pr(f"  Klines:      {klines_path.name}")
    pr(f"  Settlements: {settlements_path.name}")
    pr(f"  Price hist:  {price_hist_path.name}")

    klines = pd.read_parquet(klines_path)
    klines["ts_sec"] = klines["timestamp"] // 1000
    pr(f"  Klines: {len(klines):,} rows")

    settlements = pd.read_parquet(settlements_path)
    settlements = settlements.dropna(subset=["chainlink_open"])
    pr(f"  Settlements: {len(settlements):,} windows")

    price_hist = pd.read_parquet(price_hist_path)
    pr(f"  Price history: {len(price_hist):,} snapshots")

    delta_table = load_delta_table()
    pr(f"  Delta table: {len(delta_table)} entries")

    # Build kline dense array
    kline_ts = klines["ts_sec"].values
    kline_close = klines["close"].values
    kline_min_ts = int(kline_ts[0])
    kline_max_ts = int(kline_ts[-1])

    pr("  Building kline index...")
    kline_range = kline_max_ts - kline_min_ts + 1
    kline_arr = np.full(kline_range, np.nan, dtype=np.float64)
    kline_arr[kline_ts.astype(np.int64) - kline_min_ts] = kline_close
    mask = np.isnan(kline_arr)
    if mask.any():
        idx_arr = np.arange(len(kline_arr))
        valid = ~mask
        kline_arr[mask] = np.interp(idx_arr[mask], idx_arr[valid], kline_arr[valid])

    # Build PM snapshot lookup (causal)
    pr("  Building PM snapshot index...")
    pm_snaps = defaultdict(list)
    for _, row in price_hist.iterrows():
        pm_snaps[int(row["window_start"])].append(
            (int(row["timestamp"]), float(row["polymarket_yes_price"]))
        )
    for ws in pm_snaps:
        pm_snaps[ws].sort()

    pr(f"  Data loaded in {time.time() - t_load:.1f}s")
    pr()

    # Filter windows
    valid_windows = settlements[
        (settlements["window_start"] >= kline_min_ts) &
        (settlements["window_start"] + 300 <= kline_max_ts)
    ].copy()
    valid_windows = valid_windows.sort_values("window_start").reset_index(drop=True)
    n_windows = len(valid_windows)
    pr(f"Windows with full kline coverage: {n_windows:,}")

    # Period boundaries (thirds)
    all_ws = valid_windows["window_start"].values
    ws_min, ws_max = int(all_ws[0]), int(all_ws[-1])
    period_len = (ws_max - ws_min) / 3
    periods = [
        ("Days 1-20",  ws_min, ws_min + period_len),
        ("Days 21-40", ws_min + period_len, ws_min + 2 * period_len),
        ("Days 41-60+", ws_min + 2 * period_len, ws_max + 1),
    ]
    pr()

    # ------------------------------------------------------------------
    # Run simulations at multiple slippage levels
    # ------------------------------------------------------------------
    slippage_levels = [0.00, 0.01, 0.02, 0.03]
    results_by_slippage = {}

    for slip in slippage_levels:
        pr(f"Running simulation with slippage={slip:.2f} ({int(slip*100)}c)...")
        t_sim = time.time()

        # Only test IL ranges and LF/CP sizing at default slippage (1c)
        test_extras = (slip == DEFAULT_SLIPPAGE)

        arch_trades = run_simulation(
            kline_arr, kline_min_ts, kline_max_ts, valid_windows, pm_snaps,
            delta_table, slippage=slip,
            test_il_ranges=test_extras,
            test_lf_sizing=test_extras,
            test_cp_sizing=test_extras,
        )

        total_trades = sum(len(v) for v in arch_trades.values())
        sim_time = time.time() - t_sim
        pr(f"  Completed: {n_windows:,} windows in {sim_time:.1f}s | {total_trades:,} trades")
        pr()

        results_by_slippage[slip] = arch_trades

    # ------------------------------------------------------------------
    # Detailed report at default slippage (1c)
    # ------------------------------------------------------------------
    pr()
    pr("=" * 110)
    pr(f"DETAILED RESULTS AT DEFAULT SLIPPAGE ({int(DEFAULT_SLIPPAGE*100)}c)")
    pr("=" * 110)

    default_results = results_by_slippage[DEFAULT_SLIPPAGE]

    arch_order = [
        "impulse_lag", "impulse_lag_nodead",
        "IL_cheap_10_40", "IL_mid_40_60", "IL_exp_60_90",
        "delta_sniper", "gamma_snipe",
        "lottery_fade", "lottery_fade_$25",
        "certainty_premium", "certainty_premium_$25",
    ]

    all_highlighted = []

    for arch in arch_order:
        trades = default_results.get(arch, [])
        if not trades:
            continue

        pr()
        pr("-" * 110)
        pr(f"ARCHITECTURE: {arch} (slippage={int(DEFAULT_SLIPPAGE*100)}c)")
        pr("-" * 110)

        result = analyze_trades(trades, arch, pr)
        if result:
            _, _, highlighted = result
            for h in highlighted:
                all_highlighted.append((arch, *h))

        analyze_periods(trades, periods, pr)

    # ------------------------------------------------------------------
    # Slippage sensitivity
    # ------------------------------------------------------------------
    pr()
    pr("=" * 110)
    pr("SLIPPAGE SENSITIVITY ANALYSIS")
    pr("=" * 110)
    pr()

    # Core architectures only for sensitivity
    core_archs = ["impulse_lag", "impulse_lag_nodead", "delta_sniper",
                  "gamma_snipe", "lottery_fade", "certainty_premium"]

    pr(f"{'Architecture':<25} {'0c PnL':>12} {'1c PnL':>12} {'2c PnL':>12} {'3c PnL':>12} {'Breakeven':>12}")
    pr("-" * 85)

    arch_verdicts = {}

    for arch in core_archs:
        pnls = []
        counts = []
        for slip in slippage_levels:
            trades = results_by_slippage[slip].get(arch, [])
            total_pnl = sum(t.pnl for t in trades)
            pnls.append(total_pnl)
            counts.append(len(trades))

        # Find breakeven slippage via linear interpolation
        breakeven_str = ">3c"
        for i in range(len(pnls) - 1):
            if pnls[i] > 0 and pnls[i + 1] <= 0:
                # Linear interpolation between slippage_levels[i] and [i+1]
                frac = pnls[i] / (pnls[i] - pnls[i + 1]) if (pnls[i] - pnls[i + 1]) != 0 else 0
                be = slippage_levels[i] + frac * (slippage_levels[i + 1] - slippage_levels[i])
                breakeven_str = f"{be*100:.1f}c"
                break
            elif pnls[i] <= 0 and i == 0:
                breakeven_str = "<0c"
                break

        if pnls[-1] > 0:
            breakeven_str = ">3c"

        pr(f"{arch:<25} ${pnls[0]:>10,.2f} ${pnls[1]:>10,.2f} ${pnls[2]:>10,.2f} ${pnls[3]:>10,.2f} {breakeven_str:>12}")

        # Store verdict info
        n_at_1c = counts[1]
        pnl_at_1c = pnls[1]
        avg_at_1c = pnl_at_1c / n_at_1c if n_at_1c > 0 else 0
        arch_verdicts[arch] = {
            "pnl_0c": pnls[0], "pnl_1c": pnls[1], "pnl_2c": pnls[2], "pnl_3c": pnls[3],
            "n_1c": n_at_1c, "avg_1c": avg_at_1c, "breakeven": breakeven_str,
        }

    pr()

    # Per-combo slippage sensitivity for highlighted combos
    pr("Per-combo breakeven (combos with 50+ trades and positive PnL at 0c):")
    pr(f"{'Architecture':<25} {'Combo':<30} {'0c':>10} {'1c':>10} {'2c':>10} {'3c':>10} {'BE':>8}")
    pr("-" * 100)

    # Collect all combos across slippage levels
    all_combos = set()
    for slip in slippage_levels:
        for arch in core_archs:
            for t in results_by_slippage[slip].get(arch, []):
                all_combos.add((arch, t.combo))

    for arch, combo in sorted(all_combos):
        combo_pnls = []
        combo_n = 0
        for slip in slippage_levels:
            trades = [t for t in results_by_slippage[slip].get(arch, []) if t.combo == combo]
            combo_pnls.append(sum(t.pnl for t in trades))
            if slip == 0.0:
                combo_n = len(trades)

        if combo_n < 50 or combo_pnls[0] <= 0:
            continue

        be_str = ">3c"
        for i in range(len(combo_pnls) - 1):
            if combo_pnls[i] > 0 and combo_pnls[i + 1] <= 0:
                frac = combo_pnls[i] / (combo_pnls[i] - combo_pnls[i + 1]) if (combo_pnls[i] - combo_pnls[i + 1]) != 0 else 0
                be = slippage_levels[i] + frac * (slippage_levels[i + 1] - slippage_levels[i])
                be_str = f"{be*100:.1f}c"
                break
            elif combo_pnls[i] <= 0 and i == 0:
                be_str = "<0c"
                break
        if combo_pnls[-1] > 0:
            be_str = ">3c"

        pr(f"{arch:<25} {combo:<30} ${combo_pnls[0]:>8,.2f} ${combo_pnls[1]:>8,.2f} "
           f"${combo_pnls[2]:>8,.2f} ${combo_pnls[3]:>8,.2f} {be_str:>8}")

    # ------------------------------------------------------------------
    # Final verdict
    # ------------------------------------------------------------------
    pr()
    pr("=" * 110)
    pr("FINAL VERDICT")
    pr("=" * 110)
    pr()

    pr("Execution model assumptions:")
    pr(f"  - Slippage: {int(DEFAULT_SLIPPAGE*100)}c default ({DEFAULT_SLIPPAGE:.2f} per share)")
    pr(f"  - Fill rate: {FILL_RATE*100:.0f}% (random)")
    pr(f"  - Extra slippage at extremes (>{EXTREME_THRESHOLD_HIGH*100:.0f}c or <{EXTREME_THRESHOLD_LOW*100:.0f}c): +{int(EXTREME_EXTRA_SLIPPAGE*100)}c")
    pr(f"  - Causal PM price lookup (no future peeking)")
    pr(f"  - Fee: {FEE_RATE*100:.1f}% of notional risk")
    pr()

    # Classify architectures
    pr("Architecture assessment at realistic 1c slippage:")
    pr(f"{'Architecture':<25} {'Trades':>7} {'PnL':>12} {'$/Trade':>9} {'Breakeven':>10} {'Verdict':>15}")
    pr("-" * 85)

    for arch in core_archs:
        v = arch_verdicts.get(arch, {})
        pnl = v.get("pnl_1c", 0)
        n = v.get("n_1c", 0)
        avg = v.get("avg_1c", 0)
        be = v.get("breakeven", "?")

        if pnl > 0 and be in [">3c"] or (be not in ["<0c"] and "c" in be and float(be.replace("c", "")) >= 2.0):
            verdict = "HAS EDGE"
        elif pnl > 0:
            verdict = "MARGINAL"
        else:
            verdict = "NO EDGE"

        pr(f"{arch:<25} {n:>7,} ${pnl:>10,.2f} ${avg:>8.2f} {be:>10} {verdict:>15}")

    pr()
    pr("Robust combos (profitable at 1c slippage across all 3 periods):")
    pr("-" * 85)

    robust_found = False
    default_trades = results_by_slippage[DEFAULT_SLIPPAGE]
    for arch in core_archs:
        trades = default_trades.get(arch, [])
        if not trades:
            continue
        combo_trades = defaultdict(list)
        for t in trades:
            combo_trades[t.combo].append(t)
        for combo in sorted(combo_trades.keys()):
            ct = combo_trades[combo]
            if len(ct) < 50:
                continue
            total_pnl = sum(t.pnl for t in ct)
            if total_pnl <= 0:
                continue
            # Check all periods profitable
            all_profitable = True
            period_info = []
            for pname, pstart, pend in periods:
                pt = [t for t in ct if pstart <= t.window_start < pend]
                ppnl = sum(t.pnl for t in pt)
                pn = len(pt)
                if ppnl <= 0 or pn < 10:
                    all_profitable = False
                period_info.append((pname, pn, ppnl))

            if all_profitable:
                robust_found = True
                avg_pnl = total_pnl / len(ct)
                pr(f"  {arch} / {combo}: {len(ct)} trades, ${total_pnl:.2f} total, ${avg_pnl:.2f}/trade")
                for pname, pn, ppnl in period_info:
                    pr(f"    {pname}: {pn} trades, ${ppnl:.2f}")

    if not robust_found:
        pr("  None found -- no combo is profitable across all 3 periods at 1c slippage.")

    pr()
    pr("Expected realistic PnL per trade (at 1c slippage):")
    all_1c_trades = []
    for arch in core_archs:
        all_1c_trades.extend(default_trades.get(arch, []))
    if all_1c_trades:
        total_pnl = sum(t.pnl for t in all_1c_trades)
        total_n = len(all_1c_trades)
        total_fees = sum(t.fee for t in all_1c_trades)
        total_wr = sum(1 for t in all_1c_trades if t.pnl > 0) / total_n * 100
        pr(f"  All architectures combined: {total_n:,} trades | {total_wr:.1f}% WR | "
           f"${total_pnl:,.2f} PnL | ${total_fees:,.2f} fees | ${total_pnl/total_n:.2f}/trade")
    pr()

    # Best single strategy
    pr("Best strategies at 1c slippage (by total PnL):")
    strategy_pnls = []
    for arch in core_archs:
        trades = default_trades.get(arch, [])
        combo_trades = defaultdict(list)
        for t in trades:
            combo_trades[t.combo].append(t)
        for combo, ct in combo_trades.items():
            if len(ct) >= 50:
                pnl = sum(t.pnl for t in ct)
                strategy_pnls.append((arch, combo, len(ct), pnl, pnl/len(ct)))

    strategy_pnls.sort(key=lambda x: -x[3])
    for i, (arch, combo, n, pnl, avg) in enumerate(strategy_pnls[:10]):
        wr = sum(1 for t in [t for t in default_trades[arch] if t.combo == combo] if t.pnl > 0) / n * 100
        pr(f"  {i+1}. {arch}/{combo}: {n} trades, {wr:.1f}% WR, ${pnl:.2f} total, ${avg:.2f}/trade")

    pr()
    pr("Worst strategies at 1c slippage (biggest losers):")
    for i, (arch, combo, n, pnl, avg) in enumerate(strategy_pnls[-5:]):
        pr(f"  {i+1}. {arch}/{combo}: {n} trades, ${pnl:.2f} total, ${avg:.2f}/trade")

    pr()
    elapsed = time.time() - t0
    pr(f"Total runtime: {elapsed:.1f}s")
    pr("=" * 110)

    # Save
    with open(RESULTS_PATH, "w") as f:
        f.write(output.getvalue())
    print(f"\nResults saved to {RESULTS_PATH}")


if __name__ == "__main__":
    run_backtest()
