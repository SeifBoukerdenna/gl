"""
Phase 1 — Feature Discovery on Live Trade Data

Goal: Find which features (if any) actually distinguish WIN trades from LOSS trades.
This tells us whether the table-based approach can fix oracle's chop failure,
or whether we need to change oracle's structural logic instead.

Approach:
1. Load every live trade across all 9 sessions (~2,500 trades)
2. For each trade, compute candidate features:
   - Intra-window BTC range at fire time
   - Strike crossings count
   - BTC velocity (30s)
   - 1h annualized vol
   - Distance from 1h high/low
   - Book spread, age, entry price, delta_bps, time_remaining
3. Split trades into WIN and LOSS classes
4. For each feature, compute discriminating power:
   - Mean WIN vs mean LOSS
   - AUC (probability a random WIN has higher feature value than random LOSS)
   - KS statistic
5. Rank features by AUC
6. Highlight features with AUC > 0.60 (real signal)

Output: output/feature_discovery_report.txt
"""

import csv
import math
import time
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd
import numpy as np


DATA_DIR = Path("data")
SESSIONS = ["oracle_1", "oracle_safe", "oracle_2", "oracle_stale",
            "test_ic_wide", "blitz_1", "edge_hunter", "test_xp", "test_cp_normal"]


def load_all_trades():
    print("Loading live trade data...")
    trades = []
    for name in SESSIONS:
        csv_path = DATA_DIR / name / "trades.csv"
        if not csv_path.exists():
            continue
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    trades.append({
                        "ts": float(row["timestamp"]),
                        "window_start": int(float(row["window_start"])),
                        "session": name,
                        "architecture": row.get("architecture", ""),
                        "combo": row.get("combo", ""),
                        "direction": row["direction"],
                        "delta_bps": float(row.get("delta_bps", 0)),
                        "time_remaining": float(row.get("time_remaining", 0)),
                        "fill_price": float(row.get("fill_price", 0)),
                        "spread": float(row.get("spread", 0)),
                        "slippage": float(row.get("slippage", 0)),
                        "btc_price": float(row.get("btc_price", 0)),
                        "book_age_ms": float(row.get("book_age_ms", 0)),
                        "impulse_bps": float(row.get("impulse_bps", 0)),
                        "result": row["result"],
                        "pnl": float(row.get("pnl_taker", 0)),
                    })
                except (ValueError, KeyError):
                    continue
    print(f"  Loaded {len(trades):,} trades from {len(SESSIONS)} sessions")
    return trades


def load_binance():
    print("Loading Binance klines...")
    df = pd.read_parquet("data/binance_klines_60d.parquet")
    df["ts_sec"] = (df["timestamp"] / 1000).astype(int)
    df = df.set_index("ts_sec").sort_index()
    print(f"  {len(df):,} klines from {pd.to_datetime(df.index[0], unit='s')} to {pd.to_datetime(df.index[-1], unit='s')}")
    return df


def get_price(df, ts):
    """Get BTC close at exact second."""
    try:
        nearby = df.loc[ts:ts + 2]
        return float(nearby.iloc[0]["close"]) if len(nearby) > 0 else None
    except (KeyError, IndexError):
        return None


def get_range_in(df, start_ts, end_ts):
    """BTC high/low/open/close in a time range."""
    try:
        chunk = df.loc[start_ts:end_ts]
        if len(chunk) == 0:
            return None
        return {
            "open": float(chunk.iloc[0]["close"]),
            "close": float(chunk.iloc[-1]["close"]),
            "high": float(chunk["high"].max()),
            "low": float(chunk["low"].min()),
            "range": float(chunk["high"].max() - chunk["low"].min()),
        }
    except (KeyError, IndexError):
        return None


def count_crossings(df, start_ts, end_ts, reference_price, threshold_pct=0.0003):
    """Count how many times BTC crossed the reference price (with threshold to ignore noise)."""
    try:
        chunk = df.loc[start_ts:end_ts]
        if len(chunk) < 2:
            return 0
        prices = chunk["close"].values
        # Was BTC above or below reference at each point?
        above = prices > reference_price * (1 + threshold_pct)
        below = prices < reference_price * (1 - threshold_pct)
        crossings = 0
        last_state = None
        for a, b in zip(above, below):
            state = "above" if a else "below" if b else last_state
            if last_state is not None and state is not None and state != last_state:
                crossings += 1
            if state is not None:
                last_state = state
        return crossings
    except (KeyError, IndexError):
        return 0


def compute_realized_vol(df, end_ts, lookback_secs=3600):
    """Annualized realized vol from 1-min returns."""
    start_ts = end_ts - lookback_secs
    try:
        chunk = df.loc[start_ts:end_ts]
    except KeyError:
        return None
    if len(chunk) < 60:
        return None
    closes = chunk["close"].values[::60]
    if len(closes) < 5:
        return None
    log_rets = np.diff(np.log(closes))
    if len(log_rets) < 4:
        return None
    sigma = float(np.std(log_rets, ddof=1))
    annual_vol = sigma * math.sqrt(525600) * 100
    return annual_vol


def tag_features(trades, df_binance):
    """Add computed features to each trade."""
    print("\nTagging features for each trade...")
    tagged = []
    skipped = 0

    for i, t in enumerate(trades):
        if i % 500 == 0 and i > 0:
            print(f"  {i}/{len(trades)}...")

        fire_ts = int(t["ts"])
        ws = t["window_start"]

        # Reference strike: BTC at window open
        strike_price = get_price(df_binance, ws)
        if strike_price is None:
            skipped += 1
            continue

        # 1. Intra-window range so far (window_start → fire_ts)
        win_so_far = get_range_in(df_binance, ws, fire_ts)
        if win_so_far is None:
            skipped += 1
            continue

        # 2. Strike crossings in window so far
        crossings = count_crossings(df_binance, ws, fire_ts, strike_price)

        # 3. BTC velocity (30s before fire)
        btc_now = get_price(df_binance, fire_ts)
        btc_30ago = get_price(df_binance, fire_ts - 30)
        if btc_now and btc_30ago:
            velocity_30s_bps = (btc_now - btc_30ago) / btc_30ago * 10000
        else:
            velocity_30s_bps = 0.0

        # 4. 1h realized vol
        vol_1h = compute_realized_vol(df_binance, fire_ts, lookback_secs=3600)
        if vol_1h is None:
            vol_1h = 0.0

        # 5. Distance from 1h high/low (in $)
        hour_range = get_range_in(df_binance, fire_ts - 3600, fire_ts)
        if hour_range:
            dist_from_high = hour_range["high"] - btc_now if btc_now else 0
            dist_from_low = btc_now - hour_range["low"] if btc_now else 0
            hour_range_dollars = hour_range["range"]
        else:
            dist_from_high = 0
            dist_from_low = 0
            hour_range_dollars = 0

        # CRITICAL FIX: fill_price is ALWAYS the YES token price.
        # For NO trades, actual cost = 1 - fill_price.
        # A NO trade with fill_price=0.30 means you paid 0.70 per NO share.
        true_entry_cost = t["fill_price"] if t["direction"] == "YES" else (1.0 - t["fill_price"])

        # Combine
        tagged.append({
            **t,
            "win": t["result"] == "WIN",
            "true_entry_cost": true_entry_cost,
            # New features
            "intra_win_range_bps": (win_so_far["range"] / strike_price * 10000) if strike_price else 0,
            "intra_win_range_dollars": win_so_far["range"],
            "strike_crossings": crossings,
            "velocity_30s_bps": velocity_30s_bps,
            "abs_velocity_30s_bps": abs(velocity_30s_bps),
            "vol_1h_pct": vol_1h,
            "dist_from_1h_high": dist_from_high,
            "dist_from_1h_low": dist_from_low,
            "range_1h_dollars": hour_range_dollars,
            # Existing features
            "abs_delta_bps": abs(t["delta_bps"]),
            "spread_pct": t["spread"] * 100,
            # Boolean: was BTC moving WITH the bet direction at fire time?
            "velocity_aligned": (
                (velocity_30s_bps > 0 and t["direction"] == "YES")
                or (velocity_30s_bps < 0 and t["direction"] == "NO")
            ),
        })

    print(f"  Tagged {len(tagged):,} trades, skipped {skipped} (missing price data)")
    return tagged


def compute_auc(values_pos, values_neg):
    """Compute AUC (probability a random pos has higher value than random neg)."""
    if not values_pos or not values_neg:
        return 0.5
    # Combine and rank
    all_vals = [(v, 1) for v in values_pos] + [(v, 0) for v in values_neg]
    all_vals.sort(key=lambda x: x[0])
    n_pos = len(values_pos)
    n_neg = len(values_neg)
    rank_sum = 0
    # Average rank for ties
    i = 0
    rank = 1
    while i < len(all_vals):
        j = i
        while j < len(all_vals) and all_vals[j][0] == all_vals[i][0]:
            j += 1
        avg_rank = (rank + rank + (j - i) - 1) / 2
        for k in range(i, j):
            if all_vals[k][1] == 1:
                rank_sum += avg_rank
        rank += (j - i)
        i = j
    auc = (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return auc


def ks_statistic(values_pos, values_neg):
    """Kolmogorov-Smirnov statistic — max distance between CDFs."""
    if not values_pos or not values_neg:
        return 0
    sorted_pos = sorted(values_pos)
    sorted_neg = sorted(values_neg)
    n_pos = len(sorted_pos)
    n_neg = len(sorted_neg)
    all_vals = sorted(set(sorted_pos + sorted_neg))
    max_dist = 0
    for v in all_vals:
        cdf_pos = sum(1 for x in sorted_pos if x <= v) / n_pos
        cdf_neg = sum(1 for x in sorted_neg if x <= v) / n_neg
        dist = abs(cdf_pos - cdf_neg)
        if dist > max_dist:
            max_dist = dist
    return max_dist


def analyze_feature(name, trades, feature_key, lower_is_winning=False):
    """Analyze a single feature's predictive power."""
    win_vals = [t[feature_key] for t in trades if t["win"]]
    loss_vals = [t[feature_key] for t in trades if not t["win"]]

    if not win_vals or not loss_vals:
        return None

    mean_win = sum(win_vals) / len(win_vals)
    mean_loss = sum(loss_vals) / len(loss_vals)
    median_win = sorted(win_vals)[len(win_vals) // 2]
    median_loss = sorted(loss_vals)[len(loss_vals) // 2]

    # AUC: probability LOSS has higher feature value than WIN (since high feature = bad)
    # If lower_is_winning, then WIN should have lower values, so we compute P(LOSS > WIN)
    if lower_is_winning:
        auc = compute_auc(loss_vals, win_vals)
    else:
        auc = compute_auc(win_vals, loss_vals)

    # Always report as "discriminating power" — distance from 0.5 in the predictive direction
    discriminating_power = abs(auc - 0.5)
    ks = ks_statistic(win_vals, loss_vals)

    return {
        "name": name,
        "feature": feature_key,
        "n_win": len(win_vals),
        "n_loss": len(loss_vals),
        "mean_win": mean_win,
        "mean_loss": mean_loss,
        "median_win": median_win,
        "median_loss": median_loss,
        "auc": auc,
        "discriminating_power": discriminating_power,
        "ks": ks,
        "direction": "lower=win" if lower_is_winning else "higher=win",
    }


def main():
    print("=" * 70)
    print("PHASE 1: FEATURE DISCOVERY ON LIVE TRADE DATA")
    print("=" * 70)

    trades = load_all_trades()
    df_binance = load_binance()
    tagged = tag_features(trades, df_binance)

    if not tagged:
        print("No trades tagged. Exiting.")
        return

    n_total = len(tagged)
    n_win = sum(1 for t in tagged if t["win"])
    n_loss = n_total - n_win
    print(f"\n  Total: {n_total:,} trades ({n_win:,} wins, {n_loss:,} losses, {n_win/n_total*100:.1f}% WR)")

    # ── ANALYZE EACH FEATURE ──
    features_to_test = [
        # (display name, feature key, lower_is_winning)
        # NEW features (chop indicators)
        ("Intra-window range (bps)",   "intra_win_range_bps",     True),  # high range = chop = LOSS
        ("Intra-window range ($)",     "intra_win_range_dollars", True),
        ("Strike crossings count",     "strike_crossings",        True),  # more crossings = chop = LOSS
        ("BTC velocity 30s |bps|",     "abs_velocity_30s_bps",    True),  # high vel = momentum chop?
        ("1h realized vol (%)",        "vol_1h_pct",              True),  # high vol = bad
        ("1h BTC range ($)",           "range_1h_dollars",        True),
        ("Distance from 1h high",      "dist_from_1h_high",       False), # closer to high = ?
        ("Distance from 1h low",       "dist_from_1h_low",        False),
        # EXISTING features
        ("|delta_bps| at fire",        "abs_delta_bps",           False), # bigger delta = better
        ("Time remaining (s)",         "time_remaining",          True),  # more time = more risk
        ("Spread (cents)",             "spread_pct",              True),  # wider spread = bad
        ("Slippage",                   "slippage",                True),
        ("Book age (ms)",              "book_age_ms",             True),
        ("YES-token price (raw)",      "fill_price",              False), # raw fill, mixed YES/NO
        ("TRUE entry cost (corrected)", "true_entry_cost",        True),  # lower true cost = win
    ]

    print("\n" + "=" * 70)
    print("FEATURE DISCRIMINATING POWER (sorted by AUC)")
    print("=" * 70)

    results = []
    for display_name, key, lower_is_win in features_to_test:
        r = analyze_feature(display_name, tagged, key, lower_is_winning=lower_is_win)
        if r:
            results.append(r)

    # Sort by discriminating power
    results.sort(key=lambda x: x["discriminating_power"], reverse=True)

    print(f"\n  {'Feature':<30s} {'AUC':>7s} {'KS':>6s} {'Mean WIN':>11s} {'Mean LOSS':>11s} {'Signal':>10s}")
    print("  " + "-" * 80)

    for r in results:
        # AUC > 0.60 = real signal
        # AUC > 0.55 = weak signal
        # AUC ~ 0.50 = no signal
        if r["discriminating_power"] >= 0.10:
            signal = "STRONG"
        elif r["discriminating_power"] >= 0.05:
            signal = "weak"
        else:
            signal = "none"

        print(f"  {r['name']:<30s} {r['auc']:>7.3f} {r['ks']:>6.3f} {r['mean_win']:>11.2f} {r['mean_loss']:>11.2f} {signal:>10s}")

    # ── ZOOM IN ON ORACLE ONLY ──
    print("\n\n" + "=" * 70)
    print("ORACLE-ONLY ANALYSIS (where the failure is)")
    print("=" * 70)

    oracle_trades = [t for t in tagged if "oracle" in t["session"]]
    n_o = len(oracle_trades)
    n_o_win = sum(1 for t in oracle_trades if t["win"])
    print(f"\n  Oracle trades: {n_o:,} ({n_o_win:,} wins, {n_o-n_o_win:,} losses, {n_o_win/n_o*100:.1f}% WR)")

    print(f"\n  {'Feature':<30s} {'AUC':>7s} {'Mean WIN':>11s} {'Mean LOSS':>11s} {'Signal':>10s}")
    print("  " + "-" * 75)

    oracle_results = []
    for display_name, key, lower_is_win in features_to_test:
        r = analyze_feature(display_name, oracle_trades, key, lower_is_winning=lower_is_win)
        if r:
            oracle_results.append(r)
    oracle_results.sort(key=lambda x: x["discriminating_power"], reverse=True)

    for r in oracle_results:
        if r["discriminating_power"] >= 0.10:
            signal = "STRONG"
        elif r["discriminating_power"] >= 0.05:
            signal = "weak"
        else:
            signal = "none"
        print(f"  {r['name']:<30s} {r['auc']:>7.3f} {r['mean_win']:>11.2f} {r['mean_loss']:>11.2f} {signal:>10s}")

    # ── ZOOM IN ON THE CHOP PERIOD (Apr 9, 6PM-10PM) ──
    print("\n\n" + "=" * 70)
    print("CHOP PERIOD ANALYSIS (Apr 9, 6PM-10PM ET — the bloodbath)")
    print("=" * 70)

    # Apr 9 6PM ET = ~22:00 UTC = 1775776800
    chop_start = 1775767200  # Apr 9 6PM ET
    chop_end = 1775793600    # Apr 9 1AM ET
    chop_trades = [t for t in tagged if chop_start <= t["ts"] < chop_end and "oracle" in t["session"]]
    n_c = len(chop_trades)
    if n_c > 30:
        n_c_win = sum(1 for t in chop_trades if t["win"])
        print(f"\n  Oracle trades during chop: {n_c:,} ({n_c_win:,} wins, {n_c-n_c_win:,} losses, {n_c_win/n_c*100:.1f}% WR)")

        print(f"\n  {'Feature':<30s} {'AUC':>7s} {'Mean WIN':>11s} {'Mean LOSS':>11s} {'Signal':>10s}")
        print("  " + "-" * 75)

        chop_results = []
        for display_name, key, lower_is_win in features_to_test:
            r = analyze_feature(display_name, chop_trades, key, lower_is_winning=lower_is_win)
            if r:
                chop_results.append(r)
        chop_results.sort(key=lambda x: x["discriminating_power"], reverse=True)

        for r in chop_results:
            if r["discriminating_power"] >= 0.10:
                signal = "STRONG"
            elif r["discriminating_power"] >= 0.05:
                signal = "weak"
            else:
                signal = "none"
            print(f"  {r['name']:<30s} {r['auc']:>7.3f} {r['mean_win']:>11.2f} {r['mean_loss']:>11.2f} {signal:>10s}")
    else:
        print(f"\n  Not enough oracle trades during chop period ({n_c}). Need ≥30.")

    # ── SUMMARY & RECOMMENDATION ──
    print("\n\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)

    strong_features = [r for r in results if r["discriminating_power"] >= 0.10]
    weak_features = [r for r in results if 0.05 <= r["discriminating_power"] < 0.10]

    if strong_features:
        print(f"\n  STRONG signals found in {len(strong_features)} features:")
        for r in strong_features:
            print(f"    - {r['name']:<30s} AUC={r['auc']:.3f}")
        print("\n  RECOMMENDATION: Build V3.1 using these features.")
        print("  These features measurably distinguish WIN from LOSS in the live data.")
    elif weak_features:
        print(f"\n  Only WEAK signals found in {len(weak_features)} features:")
        for r in weak_features:
            print(f"    - {r['name']:<30s} AUC={r['auc']:.3f}")
        print("\n  RECOMMENDATION: Combine multiple weak features into V3.1.")
        print("  Individually they're marginal but combined may add up to meaningful filtering.")
    else:
        print("\n  NO discriminating features found (all AUC ~0.50).")
        print("\n  RECOMMENDATION: The edge table approach has hit its ceiling.")
        print("  The historical data does not contain the signal needed to predict failures.")
        print("  Pivot to ORACLE STRUCTURAL CHANGES instead:")
        print("    - Add multi-source confirmation (Coinbase + Bybit)")
        print("    - Add a hard halt during high-vol windows at the bot level")
        print("    - Or run oracle only during the historically profitable hours")

    # Save tagged trades for V3.1 builder
    Path("output").mkdir(exist_ok=True)
    out_path = Path("output/tagged_trades.json")
    # Convert non-serializable types
    clean = []
    for t in tagged:
        clean.append({k: (float(v) if isinstance(v, (int, float, np.floating, np.integer))
                          else bool(v) if isinstance(v, (bool, np.bool_))
                          else v) for k, v in t.items()})
    with open(out_path, "w") as f:
        json.dump(clean, f, default=str)
    print(f"\n  Tagged trades saved to {out_path}")


if __name__ == "__main__":
    main()
