"""
Re-run the gap analysis using the corrected delta table (Chainlink outcomes).
Shows before vs after comparison and final verdict.

Uses:
  - data/price_history_with_gaps.parquet (Phase 2 Polymarket price data)
  - data/delta_table.csv (original, Binance-based)
  - data/delta_table_corrected.csv (corrected, Chainlink outcomes)

Saves:
  - data/price_history_corrected.parquet
  - output/gap_comparison_by_time.png
  - output/gap_heatmap_corrected.png
"""

import polars as pl
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

PRICE_HISTORY_PATH = Path("data/price_history_with_gaps.parquet")
ORIGINAL_DT_PATH = Path("data/delta_table.csv")
CORRECTED_DT_PATH = Path("data/delta_table_corrected.csv")
OUTPUT_DIR = Path("output")

DELTA_ORDER = [
    "<-15", "[-15,-10)", "[-10,-7)", "[-7,-5)", "[-5,-3)", "[-3,-1)",
    "[-1,1)", "[1,3)", "[3,5)", "[5,7)", "[7,10)", "[10,15)", ">15",
]
TIME_ORDER = [270, 240, 210, 180, 150, 120, 90, 60, 30, 10]


def load_delta_lookup(path):
    dt = pl.read_csv(path)
    lookup = {}
    for row in dt.iter_rows(named=True):
        lookup[(row["delta_bucket"], row["time_bucket"])] = row["win_rate"]
    return lookup


def run_pnl_sim(df, label):
    """Run PnL simulation, print table, return best threshold stats."""
    print("\n  PnL Simulation — {}".format(label))
    print(
        "  {:>8} | {:>7} | {:>6} | {:>15} | {:>15} | {:>10}".format(
            "Thresh", "Trades", "Win%", "AvgPnL(maker)", "AvgPnL(taker)", "TotalPnL"
        )
    )
    print("  " + "-" * 78)

    best = {
        "threshold": None,
        "avg_pnl": -999,
        "win_rate": 0,
        "n_trades": 0,
        "avg_pnl_taker": 0,
    }

    for thresh in [0.02, 0.03, 0.05, 0.07, 0.10]:
        buy_yes = df.filter(pl.col("gap") > thresh)
        buy_no = df.filter(pl.col("gap") < -thresh)

        pnl_list = []
        for row in buy_yes.iter_rows(named=True):
            entry = row["polymarket_yes_price"]
            won = row["outcome"] == "Up"
            gross = (1.0 - entry) if won else -entry
            gross_t = (1.0 - entry) * 0.982 if won else -entry
            pnl_list.append((won, gross, gross_t))

        for row in buy_no.iter_rows(named=True):
            entry = 1.0 - row["polymarket_yes_price"]
            won = row["outcome"] == "Down"
            gross = (1.0 - entry) if won else -entry
            gross_t = (1.0 - entry) * 0.982 if won else -entry
            pnl_list.append((won, gross, gross_t))

        n = len(pnl_list)
        if n == 0:
            print(
                "  {:>8.2f} | {:>7} | {:>6} | {:>15} | {:>15} | {:>10}".format(
                    thresh, 0, "N/A", "N/A", "N/A", "N/A"
                )
            )
            continue

        wins = sum(1 for w, _, _ in pnl_list if w)
        wr = wins / n
        avg = sum(g for _, g, _ in pnl_list) / n
        avg_t = sum(gt for _, _, gt in pnl_list) / n
        total = sum(g for _, g, _ in pnl_list)

        print(
            "  {:>8.2f} | {:>7,} | {:>5.1f}% | ${:>14.4f} | ${:>14.4f} | ${:>9.2f}".format(
                thresh, n, wr * 100, avg, avg_t, total
            )
        )

        if avg > best["avg_pnl"] and n >= 20:
            best = {
                "threshold": thresh,
                "avg_pnl": avg,
                "win_rate": wr,
                "n_trades": n,
                "avg_pnl_taker": avg_t,
            }

    return best


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading data...")
    df = pl.read_parquet(PRICE_HISTORY_PATH)
    print("  Price history observations: {:,}".format(df.height))

    orig_lookup = load_delta_lookup(ORIGINAL_DT_PATH)
    corr_lookup = load_delta_lookup(CORRECTED_DT_PATH)
    print("  Original delta table: {} cells".format(len(orig_lookup)))
    print("  Corrected delta table: {} cells".format(len(corr_lookup)))

    # Recompute fair prob and gap using corrected delta table
    corrected_rows = []
    dropped = 0
    for row in df.iter_rows(named=True):
        key = (row["delta_bucket"], row["time_bucket"])
        fair_prob = corr_lookup.get(key)
        if fair_prob is None:
            dropped += 1
            continue
        new_row = dict(row)
        new_row["delta_table_fair_prob"] = fair_prob
        new_row["gap"] = fair_prob - row["polymarket_yes_price"]
        corrected_rows.append(new_row)

    df_corr = pl.DataFrame(corrected_rows)
    print(
        "  Corrected observations: {:,} (dropped {} unmapped)".format(
            df_corr.height, dropped
        )
    )

    corrected_path = Path("data/price_history_corrected.parquet")
    df_corr.write_parquet(corrected_path)
    print("  Saved to {}".format(corrected_path))

    # Add abs_gap to both
    df_orig = df.with_columns(pl.col("gap").abs().alias("abs_gap"))
    df_corr = df_corr.with_columns(pl.col("gap").abs().alias("abs_gap"))

    # Strong signal subsets
    orig_strong = df_orig.filter(
        (pl.col("delta_table_fair_prob") > 0.60)
        | (pl.col("delta_table_fair_prob") < 0.40)
    )
    corr_strong = df_corr.filter(
        (pl.col("delta_table_fair_prob") > 0.60)
        | (pl.col("delta_table_fair_prob") < 0.40)
    )

    # ======================================================================
    # Side-by-side gap statistics
    # ======================================================================
    print("\n" + "=" * 80)
    print("GAP STATISTICS: Original vs Corrected")
    print("=" * 80)

    print(
        "\n  {:>35} {:>12} {:>12}".format("", "Original", "Corrected")
    )
    print("  " + "-" * 60)
    print(
        "  {:>35} {:>12,} {:>12,}".format(
            "Observations:", df_orig.height, df_corr.height
        )
    )
    print(
        "  {:>35} ${:>11.4f} ${:>11.4f}".format(
            "Mean |gap| overall:",
            df_orig["abs_gap"].mean(),
            df_corr["abs_gap"].mean(),
        )
    )
    print(
        "  {:>35} ${:>11.4f} ${:>11.4f}".format(
            "Median |gap| overall:",
            df_orig["abs_gap"].median(),
            df_corr["abs_gap"].median(),
        )
    )
    print(
        "  {:>35} {:>12,} {:>12,}".format(
            "Strong-signal obs:",
            orig_strong.height,
            corr_strong.height,
        )
    )
    if orig_strong.height > 0 and corr_strong.height > 0:
        print(
            "  {:>35} ${:>11.4f} ${:>11.4f}".format(
                "Mean |gap| strong zones:",
                orig_strong["abs_gap"].mean(),
                corr_strong["abs_gap"].mean(),
            )
        )
        print(
            "  {:>35} ${:>11.4f} ${:>11.4f}".format(
                "Median |gap| strong zones:",
                orig_strong["abs_gap"].median(),
                corr_strong["abs_gap"].median(),
            )
        )

    # Gap thresholds
    print("\n  Fraction of observations exceeding gap thresholds:")
    for thresh in [0.01, 0.02, 0.03, 0.05, 0.10]:
        n_orig = df_orig.filter(pl.col("abs_gap") > thresh).height
        n_corr = df_corr.filter(pl.col("abs_gap") > thresh).height
        print(
            "    |gap| > {:.2f}: {:>5.1f}% orig vs {:>5.1f}% corr".format(
                thresh,
                n_orig / df_orig.height * 100,
                n_corr / df_corr.height * 100,
            )
        )

    # ======================================================================
    # Gap by time remaining
    # ======================================================================
    print("\n" + "=" * 80)
    print("GAP BY TIME REMAINING")
    print("=" * 80)

    time_orig = (
        df_orig.group_by("time_bucket")
        .agg(pl.col("abs_gap").mean().alias("mean_abs_gap"))
        .sort("time_bucket", descending=True)
    )
    time_corr = (
        df_corr.group_by("time_bucket")
        .agg(pl.col("abs_gap").mean().alias("mean_abs_gap"))
        .sort("time_bucket", descending=True)
    )

    time_merged = time_orig.join(
        time_corr, on="time_bucket", suffix="_corr"
    ).sort("time_bucket", descending=True)

    print(
        "\n  {:>8} | {:>12} | {:>12} | {:>8}".format(
            "Time", "Orig |gap|", "Corr |gap|", "Change"
        )
    )
    print("  " + "-" * 48)
    for row in time_merged.iter_rows(named=True):
        change = row["mean_abs_gap_corr"] - row["mean_abs_gap"]
        print(
            "  {:>6}s | {:>12.4f} | {:>12.4f} | {:>+7.4f}".format(
                row["time_bucket"],
                row["mean_abs_gap"],
                row["mean_abs_gap_corr"],
                change,
            )
        )

    # Chart: gap comparison by time
    times_o = time_orig["time_bucket"].to_list()
    gaps_o = time_orig["mean_abs_gap"].to_list()
    times_c = time_corr["time_bucket"].to_list()
    gaps_c = time_corr["mean_abs_gap"].to_list()

    fig, ax = plt.subplots(figsize=(12, 6))
    x = np.arange(len(times_o))
    w = 0.35
    ax.bar(
        x - w / 2, gaps_o, w, label="Original (Binance)", color="steelblue", alpha=0.8
    )
    ax.bar(
        x + w / 2, gaps_c, w, label="Corrected (Chainlink)", color="coral", alpha=0.8
    )
    ax.set_xticks(x)
    ax.set_xticklabels(["{}s".format(t) for t in times_o])
    ax.set_xlabel("Time Remaining (seconds)", fontsize=12)
    ax.set_ylabel("Mean |gap|", fontsize=12)
    ax.set_title(
        "Polymarket Mispricing: Before vs After Price Source Correction", fontsize=14
    )
    ax.legend()
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "gap_comparison_by_time.png", dpi=150)
    plt.close(fig)
    print("\n  Saved output/gap_comparison_by_time.png")

    # Corrected gap heatmap
    gap_by_cell = df_corr.group_by(["delta_bucket", "time_bucket"]).agg(
        [
            pl.col("abs_gap").mean().alias("mean_abs_gap"),
            pl.len().alias("count"),
        ]
    )
    gap_pivot = {}
    count_pivot = {}
    for row in gap_by_cell.iter_rows(named=True):
        gap_pivot[(row["delta_bucket"], row["time_bucket"])] = row["mean_abs_gap"]
        count_pivot[(row["delta_bucket"], row["time_bucket"])] = row["count"]

    heatmap_data = np.full((len(DELTA_ORDER), len(TIME_ORDER)), np.nan)
    for i, d in enumerate(DELTA_ORDER):
        for j, t in enumerate(TIME_ORDER):
            v = gap_pivot.get((d, t))
            c = count_pivot.get((d, t), 0)
            if v is not None and c >= 5:
                heatmap_data[i, j] = v * 100

    fig2, ax2 = plt.subplots(figsize=(14, 10))
    sns.heatmap(
        heatmap_data,
        ax=ax2,
        cmap=sns.color_palette("YlOrRd", as_cmap=True),
        vmin=0,
        vmax=15,
        annot=True,
        fmt=".1f",
        xticklabels=["{}s".format(t) for t in TIME_ORDER],
        yticklabels=DELTA_ORDER,
        cbar_kws={"label": "Mean |gap| (percentage points)"},
        linewidths=0.5,
        mask=np.isnan(heatmap_data),
    )
    ax2.set_xlabel("Time Remaining (seconds)", fontsize=12)
    ax2.set_ylabel("Delta from Open (basis points)", fontsize=12)
    ax2.set_title(
        "CORRECTED: Polymarket Mispricing by Delta & Time", fontsize=14
    )
    plt.tight_layout()
    fig2.savefig(OUTPUT_DIR / "gap_heatmap_corrected.png", dpi=150)
    plt.close(fig2)
    print("  Saved output/gap_heatmap_corrected.png")

    # ======================================================================
    # PnL comparison
    # ======================================================================
    print("\n" + "=" * 80)
    print("PnL SIMULATION COMPARISON")
    print("=" * 80)

    best_orig = run_pnl_sim(df_orig, "Original (Binance)")
    best_corr = run_pnl_sim(df_corr, "Corrected (Chainlink)")

    # ======================================================================
    # FINAL VERDICT
    # ======================================================================
    print("\n" + "=" * 80)
    print("=== BEFORE vs AFTER CORRECTION ===")
    print("=" * 80)

    orig_median = df_orig["abs_gap"].median()
    corr_median = df_corr["abs_gap"].median()
    orig_strong_med = orig_strong["abs_gap"].median() if orig_strong.height > 0 else 0
    corr_strong_med = corr_strong["abs_gap"].median() if corr_strong.height > 0 else 0

    print(
        "\n  {:>35} {:>12} {:>12}".format("", "Original", "Corrected")
    )
    print("  " + "-" * 60)
    print(
        "  {:>35} ${:>11.3f} ${:>11.3f}".format(
            "Median |gap| overall:", orig_median, corr_median
        )
    )
    print(
        "  {:>35} ${:>11.3f} ${:>11.3f}".format(
            "Median |gap| strong:", orig_strong_med, corr_strong_med
        )
    )

    if best_orig["threshold"] is not None:
        print(
            "  {:>35} {:>11.1f}% {:>11.1f}%".format(
                "Win rate (best thresh):",
                best_orig["win_rate"] * 100,
                best_corr["win_rate"] * 100
                if best_corr["threshold"]
                else 0,
            )
        )
        print(
            "  {:>35} ${:>11.4f} ${:>11.4f}".format(
                "Avg PnL/trade (maker):",
                best_orig["avg_pnl"],
                best_corr["avg_pnl"]
                if best_corr["threshold"]
                else 0,
            )
        )
        print(
            "  {:>35} {:>12} {:>12}".format(
                "Trades:",
                best_orig["n_trades"],
                best_corr["n_trades"]
                if best_corr["threshold"]
                else 0,
            )
        )

    # Determine verdict
    if (
        corr_strong_med > 0.03
        and best_corr["threshold"] is not None
        and best_corr["win_rate"] > 0.55
        and best_corr["n_trades"] >= 100
    ):
        verdict = "EDGE STILL EXISTS"
    elif corr_strong_med > 0.02 or (
        best_corr["threshold"] is not None and best_corr["win_rate"] > 0.52
    ):
        verdict = "REDUCED BUT REAL"
    elif corr_strong_med < orig_strong_med * 0.5:
        verdict = "EDGE WAS AN ARTIFACT"
    else:
        verdict = "NO EDGE"

    print("\n  VERDICT: {}".format(verdict))

    if verdict == "EDGE WAS AN ARTIFACT":
        print(
            "  The large gap in Phase 2 was caused by using Binance prices"
        )
        print(
            "  instead of Chainlink. After correction, the gap disappears."
        )
    elif verdict == "EDGE STILL EXISTS":
        print(
            "  Even after correcting for the Chainlink price source,"
        )
        print(
            "  significant mispricing persists in Polymarket 5-min BTC markets."
        )
    elif verdict == "REDUCED BUT REAL":
        print("  The gap shrinks after correction but doesn't vanish.")
        print(
            "  There may be a small exploitable edge, but it's marginal."
        )
    else:
        print("  No significant edge found after correction.")

    print("\nDone.")


if __name__ == "__main__":
    main()
