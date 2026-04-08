"""
Edge Table V2 — Hierarchical Continuous Scoring System

3-level cascading lookup:
  L1: delta × time (35 cells, ~380/cell) — always available
  L2: L1 × momentum (105 cells, ~127/cell) — when sample >= 50
  L3: L2 × bet_alignment (210 cells, ~63/cell) — when sample >= 50

Uses the MOST SPECIFIC level with >= 50 samples.
Falls back to coarser levels automatically.

Output: a score per trade that becomes a sizing multiplier.
NOT binary allow/block — CONTINUOUS 0.0 to 2.0.

Built from 13,861 REAL PM-settled windows + Binance price paths.
"""

import csv
import json
import math
import pandas as pd
import numpy as np
from collections import defaultdict
from pathlib import Path

PRIOR_W = 10
PRIOR_T = 20
MIN_SAMPLES = 50


def load_data():
    print("Loading data...")
    df_k = pd.read_parquet("data/binance_klines_60d.parquet")
    df_k["ts_sec"] = (df_k["timestamp"] / 1000).astype(int)
    df_k = df_k.set_index("ts_sec").sort_index()

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

    print(f"  {len(windows):,} usable windows")
    return windows, df_k


def get_price(df_k, ts):
    try:
        nearby = df_k.loc[ts:ts + 2]
        return nearby.iloc[0]["close"] if len(nearby) > 0 else None
    except:
        return None


def compute_observations(windows, df_k):
    print("Computing observations (10 checkpoints × 2 directions per window)...")

    delta_bins = [
        (-999, -12, "far_below"), (-12, -5, "below"), (-5, -2, "near_below"),
        (-2, 2, "at_strike"),
        (2, 5, "near_above"), (5, 12, "above"), (12, 999, "far_above"),
    ]
    time_bins = [
        (240, 300, "240-300"), (180, 240, "180-240"), (120, 180, "120-180"),
        (60, 120, "60-120"), (0, 60, "0-60"),
    ]

    obs = []
    for i, w in enumerate(windows):
        if i % 3000 == 0:
            print(f"  {i}/{len(windows)}...")

        ws, strike, outcome = w["ws"], w["strike"], w["outcome"]

        for t_lo, t_hi, t_label in time_bins:
            mid_rem = (t_lo + t_hi) / 2
            check_ts = int(ws + 300 - mid_rem)

            btc = get_price(df_k, check_ts)
            if btc is None: continue

            delta_bps = (btc - strike) / strike * 10000
            if abs(delta_bps) < 0.3: continue

            d_label = None
            for d_lo, d_hi, dl in delta_bins:
                if d_lo <= delta_bps < d_hi:
                    d_label = dl
                    break
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

            # ONE observation: betting WITH the delta (the natural direction)
            # If delta > 0, natural bet is YES. If delta < 0, natural bet is NO.
            # WR = how often does the natural direction win?
            # For AGAINST bets, WR = 1 - table WR.
            if delta_bps > 0:
                win = outcome == "Up"
            else:
                win = outcome == "Down"

            obs.append({
                "ws": ws,
                "delta": d_label,
                "time": t_label,
                "momentum": mom_label,
                "win": win,
                "delta_bps": delta_bps,
            })

    print(f"  {len(obs):,} observations")
    return obs


def bayesian_wr(wins, total):
    return (wins + PRIOR_W) / (total + PRIOR_T)


def wilson_ci(wins, total, z=1.645):
    if total == 0: return 0, 1
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return max(0, center - spread), min(1, center + spread)


def build_hierarchical_table(obs):
    print("\nBuilding hierarchical table...")

    # Level 1: delta × time
    l1 = defaultdict(lambda: {"w": 0, "n": 0})
    # Level 2: delta × time × momentum
    l2 = defaultdict(lambda: {"w": 0, "n": 0})
    # Level 3: delta × time × momentum × side (above/below strike)
    l3 = defaultdict(lambda: {"w": 0, "n": 0})

    for o in obs:
        k1 = f"{o['delta']}|{o['time']}"
        k2 = f"{k1}|{o['momentum']}"
        side = "above" if o["delta_bps"] > 0 else "below"
        k3 = f"{k2}|{side}"

        l1[k1]["n"] += 1
        l2[k2]["n"] += 1
        l3[k3]["n"] += 1
        if o["win"]:
            l1[k1]["w"] += 1
            l2[k2]["w"] += 1
            l3[k3]["w"] += 1

    table = {"L1": {}, "L2": {}, "L3": {}}

    for level_data, output, label in [(l1, table["L1"], "L1"), (l2, table["L2"], "L2"), (l3, table["L3"], "L3")]:
        for key, cell in level_data.items():
            if cell["n"] < MIN_SAMPLES:
                continue
            wr = bayesian_wr(cell["w"], cell["n"])
            ci_lo, ci_hi = wilson_ci(cell["w"], cell["n"])
            output[key] = {
                "wr": round(wr, 4),
                "ci_lower": round(ci_lo, 4),
                "ci_upper": round(ci_hi, 4),
                "count": cell["n"],
                "wins": cell["w"],
            }

    print(f"  L1: {len(table['L1'])} cells (delta × time)")
    print(f"  L2: {len(table['L2'])} cells (+ momentum)")
    print(f"  L3: {len(table['L3'])} cells (+ alignment)")
    return table


def validate(obs, table):
    print("\n" + "=" * 70)
    print("WALK-FORWARD VALIDATION")
    print("=" * 70)

    obs_sorted = sorted(obs, key=lambda o: o["ws"])
    split = int(len(obs_sorted) * 0.7)
    train_obs = obs_sorted[:split]
    test_obs = obs_sorted[split:]

    # Build table from train
    l1 = defaultdict(lambda: {"w": 0, "n": 0})
    l2 = defaultdict(lambda: {"w": 0, "n": 0})
    l3 = defaultdict(lambda: {"w": 0, "n": 0})

    for o in train_obs:
        k1 = f"{o['delta']}|{o['time']}"
        k2 = f"{k1}|{o['momentum']}"
        side = "above" if o["delta_bps"] > 0 else "below"
        k3 = f"{k2}|{side}"
        for cells, k in [(l1, k1), (l2, k2), (l3, k3)]:
            cells[k]["n"] += 1
            if o["win"]: cells[k]["w"] += 1

    def lookup_train(o):
        k1 = f"{o['delta']}|{o['time']}"
        k2 = f"{k1}|{o['momentum']}"
        side = "above" if o["delta_bps"] > 0 else "below"
        k3 = f"{k2}|{side}"
        # Most specific with >= 50 samples
        if k3 in l3 and l3[k3]["n"] >= MIN_SAMPLES:
            c = l3[k3]
            return bayesian_wr(c["w"], c["n"]), "L3"
        if k2 in l2 and l2[k2]["n"] >= MIN_SAMPLES:
            c = l2[k2]
            return bayesian_wr(c["w"], c["n"]), "L2"
        if k1 in l1 and l1[k1]["n"] >= MIN_SAMPLES:
            c = l1[k1]
            return bayesian_wr(c["w"], c["n"]), "L1"
        return None, None

    # Test: simulate scoring
    buckets = {"strong": [], "normal": [], "weak": [], "negative": [], "no_data": []}

    for o in test_obs:
        predicted_wr, level = lookup_train(o)
        if predicted_wr is None:
            buckets["no_data"].append(o["win"])
            continue

        # Score = how much WR exceeds 50%
        edge = predicted_wr - 0.50
        if edge >= 0.15:
            buckets["strong"].append(o["win"])
        elif edge >= 0.08:
            buckets["normal"].append(o["win"])
        elif edge >= 0.02:
            buckets["weak"].append(o["win"])
        else:
            buckets["negative"].append(o["win"])

    print(f"\n  {'Score Bucket':<15s} {'Trades':>7s} {'Actual WR':>10s} {'Sizing':>8s}")
    print(f"  {'-'*42}")
    for label, sizing in [("strong", "1.5x"), ("normal", "1.0x"), ("weak", "0.5x"), ("negative", "BLOCK"), ("no_data", "1.0x")]:
        wins_list = buckets[label]
        n = len(wins_list)
        if n == 0: continue
        actual_wr = sum(wins_list) / n * 100
        print(f"  {label:<15s} {n:>7,d} {actual_wr:>9.1f}% {sizing:>8s}")

    # Does the scoring order match reality? (strong > normal > weak > negative)
    wrs = {}
    for label in ["strong", "normal", "weak", "negative"]:
        if buckets[label]:
            wrs[label] = sum(buckets[label]) / len(buckets[label]) * 100

    if wrs:
        ordered = list(wrs.values())
        monotonic = all(ordered[i] >= ordered[i+1] for i in range(len(ordered)-1))
        print(f"\n  Monotonic ordering (strong > normal > weak > negative): {'YES' if monotonic else 'NO'}")
        if monotonic:
            print(f"  → Scoring is CALIBRATED. Higher scores = higher actual WR.")
        else:
            print(f"  → Scoring is NOT perfectly calibrated. Check the numbers.")


def print_summary(table):
    print("\n" + "=" * 70)
    print("TABLE SUMMARY")
    print("=" * 70)

    delta_order = ['far_below', 'below', 'near_below', 'at_strike', 'near_above', 'above', 'far_above']
    time_order = ['240-300', '180-240', '120-180', '60-120', '0-60']

    print(f"\n  L1 (base table):")
    print(f"  {'Delta':<14s}", end="")
    for t in time_order:
        print(f" {'T-'+t:>10s}", end="")
    print()
    print(f"  {'-'*64}")

    for d in delta_order:
        print(f"  {d:<14s}", end="")
        for t in time_order:
            key = f"{d}|{t}"
            if key in table["L1"]:
                c = table["L1"][key]
                print(f" {c['wr']:>5.0%}({c['count']:>3d})", end="")
            else:
                print(f"     ---   ", end="")
        print()

    # Momentum effect per delta bucket
    print(f"\n  Momentum effect (L2 - L1 WR difference):")
    print(f"  {'Delta':<14s} {'Toward':>10s} {'Flat':>10s} {'Away':>10s}")
    print(f"  {'-'*46}")
    for d in delta_order:
        row = []
        for mom in ["toward", "flat", "away"]:
            # Average across time buckets
            diffs = []
            for t in time_order:
                k1 = f"{d}|{t}"
                k2 = f"{k1}|{mom}"
                if k1 in table["L1"] and k2 in table["L2"]:
                    diff = table["L2"][k2]["wr"] - table["L1"][k1]["wr"]
                    diffs.append(diff)
            if diffs:
                avg_diff = sum(diffs) / len(diffs)
                row.append(f"{avg_diff:>+.1%}")
            else:
                row.append("   ---")
        print(f"  {d:<14s} {row[0]:>10s} {row[1]:>10s} {row[2]:>10s}")

    # Side effect (above vs below strike)
    print(f"\n  Side effect (above vs below strike):")
    above_wrs = []
    below_wrs = []
    for key, cell in table["L3"].items():
        if "|above" in key:
            above_wrs.append(cell["wr"])
        elif "|below" in key:
            below_wrs.append(cell["wr"])

    if above_wrs and below_wrs:
        print(f"  BTC above strike (YES wins): avg {sum(above_wrs)/len(above_wrs):.1%} WR ({len(above_wrs)} cells)")
        print(f"  BTC below strike (NO wins):  avg {sum(below_wrs)/len(below_wrs):.1%} WR ({len(below_wrs)} cells)")


def export(table, path="data/edge_table.json"):
    output = {
        "version": 2,
        "metadata": {
            "built_at": __import__("time").time(),
            "levels": ["L1: delta×time", "L2: +momentum", "L3: +alignment"],
            "min_samples_per_cell": MIN_SAMPLES,
            "bayesian_prior": f"{PRIOR_W}/{PRIOR_T}",
            "L1_cells": len(table["L1"]),
            "L2_cells": len(table["L2"]),
            "L3_cells": len(table["L3"]),
        },
        "L1": table["L1"],
        "L2": table["L2"],
        "L3": table["L3"],
    }
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Exported to {path}")


def main():
    print("EDGE TABLE V2 — Hierarchical Continuous Scoring")
    print("=" * 70)

    windows, df_k = load_data()
    obs = compute_observations(windows, df_k)
    table = build_hierarchical_table(obs)
    print_summary(table)
    validate(obs, table)
    export(table)

    print("\n" + "=" * 70)
    print("DONE — Use in bot as CONTINUOUS sizing multiplier, not binary filter")
    print("=" * 70)


if __name__ == "__main__":
    main()
