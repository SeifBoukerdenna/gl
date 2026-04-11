"""
Backtest the oracle_omega hypothesis specifically.

oracle_omega = oracle_1 + chrono blocks + overnight skip + trend-aligned sizing

The trend-aligned sizing is the novel piece. For each trade that passes the
filters, we compute BTC's 60-second trend at fire time and size accordingly:

  Strongly aligned (>3bp matching direction):    1.5x
  Weakly aligned (1-3bp):                        1.2x
  Flat (|trend| < 1bp):                          1.0x
  Weakly against (1-3bp opposing):               0.7x
  Strongly against (>3bp opposing):              0.4x

We approximate "what would the new PnL be" by:
  pnl_per_share = original pnl / original filled_size
  new_size_shares = (base_dollars * trend_mult) / fill_price
  new_pnl = pnl_per_share * new_size_shares

This is a linear approximation but it captures the essential dynamics.
"""

import csv
import math
import time
from datetime import datetime
from pathlib import Path
import pandas as pd

DATA_DIR = Path("data")


def load_binance():
    """Load Binance klines as a DataFrame indexed by ts_sec."""
    df = pd.read_parquet("data/binance_klines_60d.parquet")
    df["ts_sec"] = (df["timestamp"] / 1000).astype(int)
    return df.set_index("ts_sec").sort_index()


def get_btc_price_at(df, ts):
    """Lookup BTC close at given epoch second (with 5s tolerance)."""
    try:
        nearby = df.loc[ts:ts + 5]
        if len(nearby) > 0:
            return float(nearby.iloc[0]["close"])
    except (KeyError, IndexError):
        pass
    return None


def compute_trend_bps(df_binance, fire_ts, lookback_sec=60):
    """Compute BTC trend over the last N seconds in bps. Returns None if no data."""
    btc_now = get_btc_price_at(df_binance, fire_ts)
    btc_then = get_btc_price_at(df_binance, fire_ts - lookback_sec)
    if btc_now is None or btc_then is None or btc_then <= 0:
        return None
    return (btc_now - btc_then) / btc_then * 10000


def trend_size_multiplier(trend_bps, direction):
    """Return the sizing multiplier for a trade given trend and direction."""
    if trend_bps is None:
        return 1.0  # no data, default
    # Convert trend into "alignment with direction"
    if direction == "YES":
        aligned_bps = trend_bps  # positive trend = aligned with YES
    else:
        aligned_bps = -trend_bps  # negative trend = aligned with NO

    if aligned_bps > 3:
        return 1.5
    elif aligned_bps > 1:
        return 1.2
    elif aligned_bps > -1:
        return 1.0
    elif aligned_bps > -3:
        return 0.7
    else:
        return 0.4


def load_trades(name):
    """Load oracle_1 trades into list of dicts."""
    p = DATA_DIR / name / "trades.csv"
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for row in csv.DictReader(f):
            try:
                out.append({
                    "ts": float(row["timestamp"]),
                    "pnl": float(row["pnl_taker"]),
                    "result": row["result"],
                    "direction": row["direction"],
                    "delta_bps": float(row.get("delta_bps", 0)),
                    "abs_delta": abs(float(row.get("delta_bps", 0))),
                    "time_remaining": float(row["time_remaining"]),
                    "fill_price": float(row["fill_price"]),
                    "filled_size": float(row.get("filled_size", 0)),
                    "true_entry": float(row["fill_price"]) if row["direction"] == "YES" else 1.0 - float(row["fill_price"]),
                    "btc_price": float(row.get("btc_price", 0)),
                    "hour_et": datetime.fromtimestamp(float(row["timestamp"])).hour,
                })
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


def chrono_bucket(tr):
    if tr >= 270: return "270-300"
    if tr >= 240: return "240-270"
    if tr >= 210: return "210-240"
    if tr >= 180: return "180-210"
    if tr >= 150: return "150-180"
    if tr >= 120: return "120-150"
    if tr >= 90:  return "90-120"
    if tr >= 60:  return "60-90"
    if tr >= 30:  return "30-60"
    if tr >= 5:   return "5-30"
    return "0-5"


CHRONO_BLOCKED = {("60-90", "NO"), ("5-30", "NO"), ("270-300", "NO")}
CHRONO_BOOSTED = {("210-240", "NO"), ("150-180", "YES"), ("180-210", "YES")}


def apply_omega_filters(trades, df_binance):
    """Apply chrono blocks + overnight skip. Returns filtered trades.
    Each trade gets a `_trend_bps` and `_size_mult` field added."""
    kept = []
    for t in trades:
        # Filter 1: chrono block
        bucket = chrono_bucket(t["time_remaining"])
        if (bucket, t["direction"]) in CHRONO_BLOCKED:
            continue
        # Filter 2: skip overnight 12-5AM ET
        if 0 <= t["hour_et"] < 5:
            continue

        # Trend lookup for sizing
        trend_bps = compute_trend_bps(df_binance, int(t["ts"]), lookback_sec=60)
        size_mult = trend_size_multiplier(trend_bps, t["direction"])

        # Chrono boost (existing logic)
        chrono_boost = 1.3 if (bucket, t["direction"]) in CHRONO_BOOSTED else 1.0

        # Combined multiplier
        combined_mult = size_mult * chrono_boost

        # Scale PnL: pnl_per_share × (new_size_dollars / fill_price)
        # Existing PnL was at SOME size — to compare apples to apples we
        # treat the new pnl as: existing_pnl × combined_mult (linear scaling)
        new_pnl = t["pnl"] * combined_mult

        kept.append({
            **t,
            "_trend_bps": trend_bps,
            "_size_mult": size_mult,
            "_chrono_boost": chrono_boost,
            "_combined_mult": combined_mult,
            "_new_pnl": new_pnl,
        })
    return kept


def compute_metrics(trades, pnl_field="pnl"):
    """Compute viability metrics from a list of trades."""
    if len(trades) < 30:
        return None

    pnls = [t[pnl_field] for t in trades]
    n = len(pnls)
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n * 100

    # Cum + drawdown
    sorted_t = sorted(trades, key=lambda x: x["ts"])
    cum_series = []
    c = 0.0
    for t in sorted_t:
        c += t[pnl_field]
        cum_series.append((t["ts"], c))

    peak = 0
    max_dd = 0
    for _, c in cum_series:
        if c > peak: peak = c
        dd = peak - c
        if dd > max_dd: max_dd = dd

    # R²
    times = [t for t, _ in cum_series]
    cums = [c for _, c in cum_series]
    t0 = times[0]
    norm_t = [t - t0 for t in times]
    n_pts = len(norm_t)
    sum_t = sum(norm_t)
    sum_p = sum(cums)
    sum_tt = sum(t * t for t in norm_t)
    sum_tp = sum(t * p for t, p in zip(norm_t, cums))
    mean_t = sum_t / n_pts
    mean_p = sum_p / n_pts
    denom = sum_tt - n_pts * mean_t * mean_t
    if denom <= 0:
        r2 = 0
    else:
        slope = (sum_tp - n_pts * mean_t * mean_p) / denom
        intercept = mean_p - slope * mean_t
        ss_tot = sum((p - mean_p) ** 2 for p in cums)
        ss_res = sum((p - (intercept + slope * t)) ** 2 for t, p in zip(norm_t, cums))
        r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        if slope < 0: r2 = -abs(r2)

    # Rolling 6h windows
    duration_h = (cum_series[-1][0] - cum_series[0][0]) / 3600
    rolling_pct = None
    worst_6h = None
    if duration_h >= 12:
        positive = 0
        total = 0
        worst = float("inf")
        first_ts = cum_series[0][0]
        last_ts = cum_series[-1][0]
        t = first_ts
        while t + 6 * 3600 <= last_ts:
            ws, we = t, t + 6 * 3600
            sp = ep = None
            for ts, c in cum_series:
                if sp is None and ts >= ws:
                    sp = c
                if ts <= we:
                    ep = c
                else:
                    break
            if sp is not None and ep is not None:
                wpnl = ep - sp
                if wpnl > 0: positive += 1
                if wpnl < worst: worst = wpnl
                total += 1
            t += 3600
        if total > 0:
            rolling_pct = positive / total * 100
            worst_6h = worst

    calmar = total_pnl / max_dd if max_dd > 0 else None

    return {
        "n": n, "wr": wr, "pnl": total_pnl, "max_dd": max_dd,
        "r2": r2, "rolling_pct": rolling_pct, "worst_6h": worst_6h, "calmar": calmar,
    }


def print_row(label, m):
    if m is None:
        print(f"  {label:<32} (insufficient data)")
        return
    print(f"  {label:<32} n={m['n']:<5} pnl=${m['pnl']:>+8,.0f}  R²={m['r2']:.3f}  6h+={m['rolling_pct']:>5.1f}%  w6h=${m['worst_6h']:>+7,.0f}  Cal={m['calmar']:.2f}")


def main():
    print("=" * 100)
    print("  ORACLE_OMEGA BACKTEST")
    print("  Comparing: oracle_1 baseline → +chrono → +overnight → +trend sizing (omega)")
    print("=" * 100)

    print("\n  Loading Binance klines...")
    df_binance = load_binance()
    print(f"  Loaded {len(df_binance):,} klines")

    print("\n  Loading oracle_1 trades...")
    trades = load_trades("oracle_1")
    print(f"  Loaded {len(trades):,} trades")

    # Stage 1: Baseline (no filters, no sizing changes)
    baseline_metrics = compute_metrics(trades, pnl_field="pnl")

    # Stage 2: Chrono only
    chrono_only = []
    for t in trades:
        bucket = chrono_bucket(t["time_remaining"])
        if (bucket, t["direction"]) in CHRONO_BLOCKED:
            continue
        chrono_only.append(t)
    chrono_metrics = compute_metrics(chrono_only, pnl_field="pnl")

    # Stage 3: Chrono + overnight (sizing unchanged)
    chrono_overnight = []
    for t in trades:
        bucket = chrono_bucket(t["time_remaining"])
        if (bucket, t["direction"]) in CHRONO_BLOCKED:
            continue
        if 0 <= t["hour_et"] < 5:
            continue
        chrono_overnight.append(t)
    overnight_metrics = compute_metrics(chrono_overnight, pnl_field="pnl")

    # Stage 4: Omega = chrono + overnight + trend sizing
    print("\n  Computing trend at fire time for each surviving trade...")
    omega_trades = apply_omega_filters(trades, df_binance)
    print(f"  Computed trends for {len(omega_trades):,} trades")

    # Stats on size multipliers
    mults = [t["_size_mult"] for t in omega_trades if t["_size_mult"] is not None]
    if mults:
        from collections import Counter
        bins = Counter()
        for m in mults:
            if m == 1.5: bins["1.5x (strong align)"] += 1
            elif m == 1.2: bins["1.2x (weak align)"] += 1
            elif m == 1.0: bins["1.0x (flat)"] += 1
            elif m == 0.7: bins["0.7x (weak against)"] += 1
            else: bins["0.4x (strong against)"] += 1
        print("\n  Trend-sizing distribution:")
        for k, v in bins.most_common():
            print(f"    {k:<24} {v:>4} trades ({v/len(mults)*100:.0f}%)")

    omega_metrics = compute_metrics(omega_trades, pnl_field="_new_pnl")

    print("\n")
    print("=" * 100)
    print("  RESULTS")
    print("=" * 100)
    print()
    print(f"  {'Stage':<32} {'Trades':<7} {'PnL':<12} {'R²':<7} {'6h+':<8} {'Worst 6h':<12} {'Calmar':<7}")
    print("  " + "-" * 90)
    print_row("oracle_1 baseline", baseline_metrics)
    print_row("+ chrono blocks (V1)", chrono_metrics)
    print_row("+ overnight skip", overnight_metrics)
    print_row("+ trend sizing (OMEGA)", omega_metrics)

    print("\n  Viability targets: R² ≥ 0.85, 6h+ ≥ 75%, w6h ≥ -$500, Calmar ≥ 2.0")
    print()

    # Verdict
    if omega_metrics:
        passes = 0
        if omega_metrics["r2"] >= 0.85: passes += 1
        if omega_metrics["rolling_pct"] >= 75: passes += 1
        if omega_metrics["worst_6h"] >= -500: passes += 1
        if omega_metrics["calmar"] >= 2.0: passes += 1
        print(f"  oracle_omega passes {passes}/4 VIABLE thresholds")
        if passes == 4:
            print("  → VIABLE — would deploy live")
        elif passes >= 3:
            print("  → PROMISING — best candidate so far")
        else:
            print("  → not yet — more iteration needed")


if __name__ == "__main__":
    main()
