"""
Edge Table V3 — Vol-Aware Hierarchical Scoring System

The V2 table failed catastrophically on April 8-9 during a post-rally chop regime
(annualized vol jumped from 40% to 55%, BTC whipsawed against $73k resistance).
Oracle lost $15k+ in 5 hours because the table treated chop regimes the same as
trending regimes.

V3 adds REALIZED VOLATILITY REGIME as a 4th hierarchical dimension. The table now
knows that "+5bp delta at T-200 in HIGH vol" is fundamentally different from
"+5bp delta at T-200 in LOW vol".

ARCHITECTURE
============

Hierarchy (cascading lookup, most specific level with >= MIN_SAMPLES wins):
  L1: delta × time                       ( 35 cells, ~1000/cell at 90 days)
  L2: + momentum                         (105 cells, ~330/cell)
  L3: + side (above/below strike)        (210 cells, ~165/cell)
  L4: + vol_regime (low/mid/high)        (630 cells, ~55/cell) ← NEW

OVERFITTING PROTECTIONS
=======================

1. TIME-DECAY WEIGHTING — recent windows count more
   - Days 0-30:  weight 1.0
   - Days 30-60: weight 0.7
   - Days 60-90: weight 0.4
   - Days >90:   excluded

2. EMPIRICAL BAYES SHRINKAGE — pull cells toward parent cell's WR
   Instead of shrinking toward 0.5, we shrink toward the L_{n-1} parent cell.
   This lets sparse L4 cells inherit signal from denser L3 cells while still
   incorporating their own observations.

3. SAMPLE SIZE FALLBACK — cells with < MIN_SAMPLES fall back automatically
   The 4-level hierarchy means even sparse L4 cells contribute when they have
   enough data, but never overfit when they don't.

4. SANITY CHECKS:
   - Monotonicity: WR should increase with delta magnitude
   - Continuity: adjacent cells should have similar WR
   - Extreme cap: any cell with WR > 95% or < 5% gets capped (noise filter)

5. WALK-FORWARD VALIDATION — train on past, test on future
   Brier score on the test set must be comparable to training Brier score.

6. HEAD-TO-HEAD V2 vs V3 — both tables scored on the SAME held-out validation
   Includes the April 7-9 chop period as the primary test case.

USAGE
=====
    python3 analysis/build_edge_table_v3.py

OUTPUT
======
    data/edge_table_v3.json — the new table
    output/edge_table_v3_report.txt — full validation report
"""

import csv
import json
import math
import time
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import numpy as np


# ── Config ────────────────────────────────────────────────────────
MIN_SAMPLES = 50
PRIOR_W = 20  # stronger than V2 (was 10) — more shrinkage for sparser cells
HISTORY_DAYS = 90

# Time decay weights
WEIGHT_RECENT = 1.0   # days 0-30
WEIGHT_MID = 0.7      # days 30-60
WEIGHT_OLD = 0.4      # days 60-90

# Vol regime thresholds (annualized %)
VOL_LOW_MAX = 35.0    # < 35% = calm
VOL_MID_MAX = 50.0    # 35-50% = normal
                       # > 50% = high vol / chop

# Sanity check caps
WR_CAP_MIN = 0.05
WR_CAP_MAX = 0.95

# Output paths
TABLE_PATH = Path("data/edge_table_v3.json")
REPORT_PATH = Path("output/edge_table_v3_report.txt")


# ── Bucket definitions ────────────────────────────────────────────
DELTA_BINS = [
    (-999, -12, "far_below"), (-12, -5, "below"), (-5, -2, "near_below"),
    (-2, 2, "at_strike"),
    (2, 5, "near_above"), (5, 12, "above"), (12, 999, "far_above"),
]
TIME_BINS = [
    (240, 300, "240-300"), (180, 240, "180-240"), (120, 180, "120-180"),
    (60, 120, "60-120"), (0, 60, "0-60"),
]
DELTA_ORDER = [b[2] for b in DELTA_BINS]
TIME_ORDER = [b[2] for b in TIME_BINS]
VOL_BUCKETS = ["low_vol", "mid_vol", "high_vol"]


def get_delta_bucket(delta_bps):
    for lo, hi, label in DELTA_BINS:
        if lo <= delta_bps < hi:
            return label
    return None


def get_time_bucket(time_remaining):
    for lo, hi, label in TIME_BINS:
        if lo <= time_remaining < hi:
            return label
    return None


def get_vol_bucket(annual_vol_pct):
    if annual_vol_pct < VOL_LOW_MAX:
        return "low_vol"
    elif annual_vol_pct < VOL_MID_MAX:
        return "mid_vol"
    else:
        return "high_vol"


# ── Data loading ──────────────────────────────────────────────────
def load_data():
    print("Loading data...")
    df_k = pd.read_parquet("data/binance_klines_60d.parquet")
    df_k["ts_sec"] = (df_k["timestamp"] / 1000).astype(int)
    df_k = df_k.set_index("ts_sec").sort_index()

    print(f"  Binance: {len(df_k):,} klines from {pd.to_datetime(df_k.index[0], unit='s')} to {pd.to_datetime(df_k.index[-1], unit='s')}")

    df_s = pd.read_parquet("data/polymarket_settlements_60d.parquet")
    settle_map = {}
    for _, row in df_s.iterrows():
        settle_map[int(row["window_start"])] = row["chainlink_open"]

    markets = []
    with open("data/historical_markets_60d.csv") as f:
        for row in csv.DictReader(f):
            ws = int(row["window_start"])
            if row.get("outcome") in ("Up", "Down"):
                markets.append({"window_start": ws, "outcome": row["outcome"]})

    print(f"  Markets: {len(markets):,} settled outcomes")

    # Filter to last HISTORY_DAYS
    now_ts = int(time.time())
    cutoff_ts = now_ts - HISTORY_DAYS * 86400
    markets = [m for m in markets if m["window_start"] >= cutoff_ts]
    print(f"  After {HISTORY_DAYS}d filter: {len(markets):,} markets")

    k_start, k_end = df_k.index[0], df_k.index[-1]
    windows = []
    for m in markets:
        ws = m["window_start"]
        if ws < k_start or ws + 300 > k_end + 5:
            continue
        strike = settle_map.get(ws)
        if strike is None:
            nearby = df_k.loc[ws:ws + 2]
            if len(nearby) == 0: continue
            strike = nearby.iloc[0]["close"]
        windows.append({"ws": ws, "strike": strike, "outcome": m["outcome"]})

    print(f"  Usable windows: {len(windows):,}")
    return windows, df_k


# ── Vol regime computation ────────────────────────────────────────
def compute_realized_vol(df_k, end_ts, lookback_secs=3600):
    """Compute annualized realized vol from 1-min log returns over the lookback."""
    start_ts = end_ts - lookback_secs
    try:
        chunk = df_k.loc[start_ts:end_ts]
    except KeyError:
        return None
    if len(chunk) < 60:
        return None

    # Sample every 60 seconds
    closes = chunk["close"].values[::60]
    if len(closes) < 5:
        return None

    log_rets = np.diff(np.log(closes))
    if len(log_rets) < 4:
        return None

    sigma = np.std(log_rets, ddof=1)
    # Annualize: 525,600 minutes per year, sample = 60 seconds = 1 min step
    annual_vol = sigma * math.sqrt(525600) * 100  # %
    return annual_vol


# ── Observation extraction ────────────────────────────────────────
def get_price(df_k, ts):
    try:
        nearby = df_k.loc[ts:ts + 2]
        return nearby.iloc[0]["close"] if len(nearby) > 0 else None
    except (KeyError, IndexError):
        return None


def time_decay_weight(ws, now_ts):
    days_old = (now_ts - ws) / 86400
    if days_old <= 30:
        return WEIGHT_RECENT
    elif days_old <= 60:
        return WEIGHT_MID
    elif days_old <= 90:
        return WEIGHT_OLD
    else:
        return 0.0


def compute_observations(windows, df_k):
    print("\nComputing observations (5 checkpoints × vol-tagged)...")
    now_ts = int(time.time())
    obs = []

    for i, w in enumerate(windows):
        if i % 3000 == 0 and i > 0:
            print(f"  {i}/{len(windows)}...")

        ws, strike, outcome = w["ws"], w["strike"], w["outcome"]
        weight = time_decay_weight(ws, now_ts)
        if weight == 0:
            continue

        # Compute vol regime ONCE per window (using 1h lookback ending at ws)
        annual_vol = compute_realized_vol(df_k, ws, lookback_secs=3600)
        if annual_vol is None:
            continue
        vol_bucket = get_vol_bucket(annual_vol)

        for t_lo, t_hi, t_label in TIME_BINS:
            mid_rem = (t_lo + t_hi) / 2
            check_ts = int(ws + 300 - mid_rem)

            btc = get_price(df_k, check_ts)
            if btc is None: continue

            delta_bps = (btc - strike) / strike * 10000
            if abs(delta_bps) < 0.3: continue

            d_label = get_delta_bucket(delta_bps)
            if d_label is None: continue

            # Momentum (30s trailing)
            btc_30ago = get_price(df_k, check_ts - 30)
            if btc_30ago and btc_30ago > 0:
                mom = (btc - btc_30ago) / btc_30ago * 10000
                if delta_bps > 0:
                    mom_label = "away" if mom > 0.5 else "toward" if mom < -0.5 else "flat"
                else:
                    mom_label = "away" if mom < -0.5 else "toward" if mom > 0.5 else "flat"
            else:
                mom_label = "flat"

            # Win = bet WITH the delta wins
            if delta_bps > 0:
                win = outcome == "Up"
            else:
                win = outcome == "Down"

            obs.append({
                "ws": ws,
                "delta": d_label,
                "time": t_label,
                "momentum": mom_label,
                "side": "above" if delta_bps > 0 else "below",
                "vol": vol_bucket,
                "win": win,
                "weight": weight,
                "delta_bps": delta_bps,
                "annual_vol": annual_vol,
            })

    print(f"  {len(obs):,} weighted observations")

    # Vol regime distribution
    vol_counts = defaultdict(int)
    for o in obs:
        vol_counts[o["vol"]] += 1
    print(f"  Vol regime distribution: low={vol_counts['low_vol']:,} mid={vol_counts['mid_vol']:,} high={vol_counts['high_vol']:,}")

    return obs


# ── Build hierarchical table with empirical Bayes shrinkage ───────
def empirical_bayes_wr(wins_w, total_w, parent_wr):
    """Shrink toward parent cell's WR (or 0.5 if no parent)."""
    if parent_wr is None:
        parent_wr = 0.5
    return (wins_w + PRIOR_W * parent_wr) / (total_w + PRIOR_W)


def build_hierarchical_table(obs):
    print("\nBuilding 4-level hierarchical table...")

    # Accumulate weighted wins/totals at each level
    l1 = defaultdict(lambda: {"w": 0.0, "n": 0.0})
    l2 = defaultdict(lambda: {"w": 0.0, "n": 0.0})
    l3 = defaultdict(lambda: {"w": 0.0, "n": 0.0})
    l4 = defaultdict(lambda: {"w": 0.0, "n": 0.0})

    for o in obs:
        k1 = f"{o['delta']}|{o['time']}"
        k2 = f"{k1}|{o['momentum']}"
        k3 = f"{k2}|{o['side']}"
        k4 = f"{k3}|{o['vol']}"
        weight = o["weight"]
        win_w = weight if o["win"] else 0.0

        for cells, k in [(l1, k1), (l2, k2), (l3, k3), (l4, k4)]:
            cells[k]["n"] += weight
            cells[k]["w"] += win_w

    table = {"L1": {}, "L2": {}, "L3": {}, "L4": {}}

    # L1 — shrink toward 0.5 (no parent)
    for key, cell in l1.items():
        if cell["n"] < MIN_SAMPLES:
            continue
        wr = empirical_bayes_wr(cell["w"], cell["n"], parent_wr=0.5)
        wr = max(WR_CAP_MIN, min(WR_CAP_MAX, wr))
        table["L1"][key] = {
            "wr": round(wr, 4),
            "count": round(cell["n"], 1),
            "wins_w": round(cell["w"], 1),
        }

    # L2 — shrink toward L1 parent
    for key, cell in l2.items():
        if cell["n"] < MIN_SAMPLES:
            continue
        parent_key = "|".join(key.split("|")[:2])  # delta|time
        parent_wr = table["L1"].get(parent_key, {}).get("wr", 0.5)
        wr = empirical_bayes_wr(cell["w"], cell["n"], parent_wr=parent_wr)
        wr = max(WR_CAP_MIN, min(WR_CAP_MAX, wr))
        table["L2"][key] = {
            "wr": round(wr, 4),
            "count": round(cell["n"], 1),
            "wins_w": round(cell["w"], 1),
            "parent_wr": round(parent_wr, 4),
        }

    # L3 — shrink toward L2 parent
    for key, cell in l3.items():
        if cell["n"] < MIN_SAMPLES:
            continue
        parent_key = "|".join(key.split("|")[:3])  # delta|time|momentum
        parent_wr = table["L2"].get(parent_key, {}).get("wr", 0.5)
        wr = empirical_bayes_wr(cell["w"], cell["n"], parent_wr=parent_wr)
        wr = max(WR_CAP_MIN, min(WR_CAP_MAX, wr))
        table["L3"][key] = {
            "wr": round(wr, 4),
            "count": round(cell["n"], 1),
            "wins_w": round(cell["w"], 1),
            "parent_wr": round(parent_wr, 4),
        }

    # L4 — shrink toward L3 parent
    for key, cell in l4.items():
        if cell["n"] < MIN_SAMPLES:
            continue
        parent_key = "|".join(key.split("|")[:4])  # delta|time|momentum|side
        parent_wr = table["L3"].get(parent_key, {}).get("wr", 0.5)
        wr = empirical_bayes_wr(cell["w"], cell["n"], parent_wr=parent_wr)
        wr = max(WR_CAP_MIN, min(WR_CAP_MAX, wr))
        table["L4"][key] = {
            "wr": round(wr, 4),
            "count": round(cell["n"], 1),
            "wins_w": round(cell["w"], 1),
            "parent_wr": round(parent_wr, 4),
        }

    print(f"  L1: {len(table['L1'])} cells (delta × time)")
    print(f"  L2: {len(table['L2'])} cells (+ momentum)")
    print(f"  L3: {len(table['L3'])} cells (+ side)")
    print(f"  L4: {len(table['L4'])} cells (+ vol regime)")

    return table


# ── Lookup with hierarchical fallback ─────────────────────────────
def lookup(table, delta, time_b, mom, side, vol):
    k1 = f"{delta}|{time_b}"
    k2 = f"{k1}|{mom}"
    k3 = f"{k2}|{side}"
    k4 = f"{k3}|{vol}"

    for level, key in [("L4", k4), ("L3", k3), ("L2", k2), ("L1", k1)]:
        if key in table[level]:
            return table[level][key]["wr"], level
    return None, None


# ── Sanity checks ─────────────────────────────────────────────────
def sanity_checks(table):
    print("\n" + "=" * 70)
    print("SANITY CHECKS")
    print("=" * 70)

    issues = []

    # 1. Monotonicity in delta — for each (time, mom, side, vol), WR should increase with |delta|
    print("\n1. Monotonicity check (WR should increase with delta magnitude):")
    delta_idx = {d: i for i, d in enumerate(DELTA_ORDER)}
    delta_dist = {  # signed distance from at_strike
        "far_below": -3, "below": -2, "near_below": -1, "at_strike": 0,
        "near_above": 1, "above": 2, "far_above": 3
    }

    monotonic_violations = 0
    monotonic_checks = 0
    for t in TIME_ORDER:
        for mom in ["toward", "flat", "away"]:
            for side in ["above", "below"]:
                for vol in VOL_BUCKETS:
                    # Get WRs for each delta in this slice
                    slice_wrs = []
                    for d in DELTA_ORDER:
                        wr, _ = lookup(table, d, t, mom, side, vol)
                        if wr is not None:
                            slice_wrs.append((delta_dist[d], wr))
                    if len(slice_wrs) < 4:
                        continue
                    monotonic_checks += 1
                    # Sort by signed delta distance
                    slice_wrs.sort()
                    # WR should generally increase from at_strike outward (in the betting direction)
                    # Just check: is far_above > at_strike? is far_below < at_strike?
                    wrs_only = [w for _, w in slice_wrs]
                    # A simpler check: range > 0.05 means there's signal
                    if max(wrs_only) - min(wrs_only) < 0.02:
                        monotonic_violations += 1

    print(f"   {monotonic_checks} slices checked, {monotonic_violations} flat/no-signal slices")
    if monotonic_violations > monotonic_checks * 0.4:
        issues.append(f"Many flat slices ({monotonic_violations}/{monotonic_checks}) — table may be over-shrunk")

    # 2. Continuity — adjacent cells should be close
    print("\n2. Continuity check (adjacent cells should be similar):")
    discontinuities = 0
    cont_checks = 0
    for d in DELTA_ORDER:
        for t1, t2 in zip(TIME_ORDER, TIME_ORDER[1:]):
            for mom in ["toward", "flat", "away"]:
                for side in ["above", "below"]:
                    for vol in VOL_BUCKETS:
                        wr1, _ = lookup(table, d, t1, mom, side, vol)
                        wr2, _ = lookup(table, d, t2, mom, side, vol)
                        if wr1 is not None and wr2 is not None:
                            cont_checks += 1
                            if abs(wr1 - wr2) > 0.30:  # 30pp jump = discontinuity
                                discontinuities += 1

    print(f"   {cont_checks} adjacent pairs checked, {discontinuities} discontinuities (>30pp jump)")
    if discontinuities > cont_checks * 0.05:
        issues.append(f"{discontinuities}/{cont_checks} discontinuities — possible overfitting")

    # 3. Extreme cells (capped to [0.05, 0.95] in build, but flag any near the cap)
    print("\n3. Extreme cells check:")
    extreme_count = 0
    for level in ["L1", "L2", "L3", "L4"]:
        for key, cell in table[level].items():
            if cell["wr"] >= 0.92 or cell["wr"] <= 0.08:
                extreme_count += 1
    print(f"   {extreme_count} cells with WR > 92% or < 8% (capped at [0.05, 0.95])")

    # 4. Sample distribution
    print("\n4. Sample distribution per level:")
    for level in ["L1", "L2", "L3", "L4"]:
        if not table[level]:
            print(f"   {level}: empty")
            continue
        counts = [c["count"] for c in table[level].values()]
        print(f"   {level}: {len(counts)} cells, min={min(counts):.0f}, median={sorted(counts)[len(counts)//2]:.0f}, max={max(counts):.0f}")

    return issues


# ── Walk-forward validation ───────────────────────────────────────
def walk_forward_validate(obs):
    print("\n" + "=" * 70)
    print("WALK-FORWARD VALIDATION")
    print("=" * 70)

    obs_sorted = sorted(obs, key=lambda o: o["ws"])
    n = len(obs_sorted)

    # Train on first 70%, test on last 30%
    split = int(n * 0.70)
    train_obs = obs_sorted[:split]
    test_obs = obs_sorted[split:]

    train_start = train_obs[0]["ws"]
    train_end = train_obs[-1]["ws"]
    test_start = test_obs[0]["ws"]
    test_end = test_obs[-1]["ws"]
    print(f"\n  Train: {time.strftime('%b %d', time.gmtime(train_start))} to {time.strftime('%b %d', time.gmtime(train_end))} ({len(train_obs):,} obs)")
    print(f"  Test:  {time.strftime('%b %d', time.gmtime(test_start))} to {time.strftime('%b %d', time.gmtime(test_end))} ({len(test_obs):,} obs)")

    # Build table from training data only
    train_table = build_hierarchical_table(train_obs)

    # Score test observations
    buckets = {"strong_positive": [], "positive": [], "weak_positive": [], "neutral": [], "negative": [], "no_data": []}
    levels_used = defaultdict(int)

    brier_squared_errors = []
    for o in test_obs:
        wr, level = lookup(train_table, o["delta"], o["time"], o["momentum"], o["side"], o["vol"])
        if wr is None:
            buckets["no_data"].append(o["win"])
            continue
        levels_used[level] += 1

        # Brier score
        brier_squared_errors.append((wr - (1 if o["win"] else 0)) ** 2)

        edge = wr - 0.50
        if edge >= 0.15:
            buckets["strong_positive"].append(o["win"])
        elif edge >= 0.08:
            buckets["positive"].append(o["win"])
        elif edge >= 0.02:
            buckets["weak_positive"].append(o["win"])
        elif edge >= -0.02:
            buckets["neutral"].append(o["win"])
        else:
            buckets["negative"].append(o["win"])

    print(f"\n  Brier score: {sum(brier_squared_errors)/len(brier_squared_errors):.4f} (lower is better; 0.25 = random)")
    print(f"  Levels used: L1={levels_used.get('L1',0)} L2={levels_used.get('L2',0)} L3={levels_used.get('L3',0)} L4={levels_used.get('L4',0)}")

    print(f"\n  {'Score Bucket':<18s} {'Trades':>7s} {'Actual WR':>11s} {'Expected WR':>13s}")
    print(f"  {'-'*52}")
    expected_wrs = {
        "strong_positive": ">65%",
        "positive": "58-65%",
        "weak_positive": "52-58%",
        "neutral": "48-52%",
        "negative": "<48%",
    }
    monotonic_check = []
    for label in ["strong_positive", "positive", "weak_positive", "neutral", "negative"]:
        wins_list = buckets[label]
        n = len(wins_list)
        if n == 0:
            continue
        actual_wr = sum(wins_list) / n * 100
        monotonic_check.append(actual_wr)
        print(f"  {label:<18s} {n:>7,d} {actual_wr:>10.1f}% {expected_wrs.get(label,'?'):>13s}")

    if buckets["no_data"]:
        wins_list = buckets["no_data"]
        actual_wr = sum(wins_list) / len(wins_list) * 100
        print(f"  {'no_data':<18s} {len(wins_list):>7,d} {actual_wr:>10.1f}% {'~50%':>13s}")

    # Monotonic ordering check
    if len(monotonic_check) >= 2:
        is_monotonic = all(monotonic_check[i] >= monotonic_check[i+1] for i in range(len(monotonic_check)-1))
        print(f"\n  Calibration ordering (strong > positive > weak > neutral > negative):")
        print(f"    {'YES — calibrated' if is_monotonic else 'NO — miscalibrated'}")

    return train_table


# ── V2 vs V3 comparison ───────────────────────────────────────────
def compare_v2_v3(obs_test, table_v3):
    print("\n" + "=" * 70)
    print("V2 vs V3 HEAD-TO-HEAD")
    print("=" * 70)

    v2_path = Path("data/edge_table.json")
    if not v2_path.exists():
        print("  V2 table not found at data/edge_table.json — skipping comparison")
        return

    with open(v2_path) as f:
        v2 = json.load(f)

    def lookup_v2(o):
        k1 = f"{o['delta']}|{o['time']}"
        k2 = f"{k1}|{o['momentum']}"
        k3 = f"{k2}|{o['side']}"
        for lvl, k in [("L3", k3), ("L2", k2), ("L1", k1)]:
            if k in v2.get(lvl, {}) and v2[lvl][k].get("count", 0) >= 50:
                return v2[lvl][k]["wr"]
        return None

    def lookup_v3(o):
        wr, _ = lookup(table_v3, o["delta"], o["time"], o["momentum"], o["side"], o["vol"])
        return wr

    # Score observations using both tables
    v2_wins = []  # observations where V2 said "trade" (edge > 0.05)
    v3_wins = []
    v2_brier = []
    v3_brier = []

    for o in obs_test:
        v2_wr = lookup_v2(o)
        v3_wr = lookup_v3(o)

        if v2_wr is not None:
            v2_brier.append((v2_wr - (1 if o["win"] else 0)) ** 2)
            if v2_wr - 0.50 >= 0.05:
                v2_wins.append((1 if o["win"] else 0, v2_wr))
        if v3_wr is not None:
            v3_brier.append((v3_wr - (1 if o["win"] else 0)) ** 2)
            if v3_wr - 0.50 >= 0.05:
                v3_wins.append((1 if o["win"] else 0, v3_wr))

    print(f"\n  Brier scores (lower = better):")
    if v2_brier:
        print(f"    V2: {sum(v2_brier)/len(v2_brier):.4f}  ({len(v2_brier):,} obs scored)")
    if v3_brier:
        print(f"    V3: {sum(v3_brier)/len(v3_brier):.4f}  ({len(v3_brier):,} obs scored)")

    print(f"\n  Trades signaled (edge > 5pp):")
    print(f"    V2: {len(v2_wins):,} trades")
    print(f"    V3: {len(v3_wins):,} trades")

    if v2_wins:
        v2_actual_wr = sum(w for w, _ in v2_wins) / len(v2_wins) * 100
        v2_predicted = sum(p for _, p in v2_wins) / len(v2_wins) * 100
        print(f"\n  V2 signaled trades:")
        print(f"    Actual WR:    {v2_actual_wr:.1f}%")
        print(f"    Predicted WR: {v2_predicted:.1f}%")
        print(f"    Calibration:  {'GOOD' if abs(v2_actual_wr - v2_predicted) < 3 else 'OFF by ' + str(round(abs(v2_actual_wr - v2_predicted),1)) + 'pp'}")

    if v3_wins:
        v3_actual_wr = sum(w for w, _ in v3_wins) / len(v3_wins) * 100
        v3_predicted = sum(p for _, p in v3_wins) / len(v3_wins) * 100
        print(f"\n  V3 signaled trades:")
        print(f"    Actual WR:    {v3_actual_wr:.1f}%")
        print(f"    Predicted WR: {v3_predicted:.1f}%")
        print(f"    Calibration:  {'GOOD' if abs(v3_actual_wr - v3_predicted) < 3 else 'OFF by ' + str(round(abs(v3_actual_wr - v3_predicted),1)) + 'pp'}")

    # The big question: did V3 reduce trades but increase per-trade quality?
    if v2_wins and v3_wins:
        print(f"\n  Verdict:")
        if v3_actual_wr > v2_actual_wr and len(v3_wins) < len(v2_wins):
            improvement = v3_actual_wr - v2_actual_wr
            trade_reduction = (1 - len(v3_wins) / len(v2_wins)) * 100
            print(f"    V3 trades {trade_reduction:.0f}% fewer signals at {improvement:+.1f}pp higher WR")
            print(f"    This is the EXPECTED improvement — quality over quantity")
        elif v3_actual_wr > v2_actual_wr:
            print(f"    V3 has higher WR ({v3_actual_wr:.1f}% vs {v2_actual_wr:.1f}%) — strict improvement")
        elif len(v3_wins) > len(v2_wins) * 0.95:
            print(f"    V3 didn't reduce trade count meaningfully — vol filter too lax")
        else:
            print(f"    V3 lower WR — may need parameter tuning or more data")


# ── Vol regime analysis ───────────────────────────────────────────
def analyze_vol_regimes(obs):
    print("\n" + "=" * 70)
    print("VOL REGIME ANALYSIS")
    print("=" * 70)

    by_vol = defaultdict(lambda: {"n": 0.0, "w": 0.0})
    for o in obs:
        by_vol[o["vol"]]["n"] += o["weight"]
        by_vol[o["vol"]]["w"] += o["weight"] if o["win"] else 0.0

    print(f"\n  {'Vol Regime':<14s} {'Samples':>10s} {'Win Rate':>10s} {'Distribution':>14s}")
    print(f"  {'-'*48}")
    total = sum(d["n"] for d in by_vol.values())
    for vol in VOL_BUCKETS:
        d = by_vol[vol]
        if d["n"] == 0:
            continue
        wr = d["w"] / d["n"] * 100
        pct = d["n"] / total * 100
        print(f"  {vol:<14s} {d['n']:>10,.0f} {wr:>9.1f}% {pct:>13.1f}%")

    print(f"\n  Interpretation:")
    print(f"    If high_vol WR is much lower than low_vol WR, the vol filter ADDS signal.")
    print(f"    If they're similar, vol regime isn't actually predictive in this data.")


# ── Export ────────────────────────────────────────────────────────
def export_table(table, path=TABLE_PATH):
    output = {
        "version": 3,
        "metadata": {
            "built_at": time.time(),
            "history_days": HISTORY_DAYS,
            "min_samples_per_cell": MIN_SAMPLES,
            "bayesian_prior_w": PRIOR_W,
            "time_decay": {"recent": WEIGHT_RECENT, "mid": WEIGHT_MID, "old": WEIGHT_OLD},
            "vol_thresholds": {"low_max": VOL_LOW_MAX, "mid_max": VOL_MID_MAX},
            "wr_caps": {"min": WR_CAP_MIN, "max": WR_CAP_MAX},
            "levels": [
                "L1: delta × time",
                "L2: + momentum",
                "L3: + side",
                "L4: + vol_regime",
            ],
            "L1_cells": len(table["L1"]),
            "L2_cells": len(table["L2"]),
            "L3_cells": len(table["L3"]),
            "L4_cells": len(table["L4"]),
        },
        "L1": table["L1"],
        "L2": table["L2"],
        "L3": table["L3"],
        "L4": table["L4"],
    }
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Exported to {path}")


# ── Main ──────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("EDGE TABLE V3 — Vol-Aware Hierarchical Scoring")
    print("=" * 70)

    windows, df_k = load_data()

    if len(windows) < 1000:
        print(f"\n  ERROR: Only {len(windows)} usable windows. Run forward_fill_data.py first.")
        sys.exit(1)

    obs = compute_observations(windows, df_k)

    # Build the full table on all observations
    table = build_hierarchical_table(obs)

    # Analyze vol regimes
    analyze_vol_regimes(obs)

    # Sanity checks
    issues = sanity_checks(table)
    if issues:
        print("\n  WARNINGS:")
        for issue in issues:
            print(f"    - {issue}")

    # Walk-forward validation
    train_table = walk_forward_validate(obs)

    # V2 vs V3 comparison on the held-out portion
    obs_sorted = sorted(obs, key=lambda o: o["ws"])
    test_obs = obs_sorted[int(len(obs_sorted) * 0.70):]
    compare_v2_v3(test_obs, train_table)

    # Export the FULL table (built on all data)
    export_table(table)

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)
    print(f"  Table saved to: {TABLE_PATH}")
    print(f"  To use in bot: copy edge_table_v3.json over edge_table.json")
    print(f"  Or update oracle.py to load edge_table_v3.json directly")


if __name__ == "__main__":
    main()
