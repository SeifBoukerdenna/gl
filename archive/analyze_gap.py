"""
Analyze the gap between delta table fair probabilities and Polymarket
YES token prices. Core question: does Polymarket misprice these markets?

Outputs:
  - stdout: formatted statistics and final verdict
  - output/gap_by_time.png
  - output/gap_heatmap.png
"""

import polars as pl
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

DATA_PATH = Path("data/price_history_with_gaps.parquet")
OUTPUT_DIR = Path("output")

DELTA_ORDER = [
    "<-15", "[-15,-10)", "[-10,-7)", "[-7,-5)", "[-5,-3)", "[-3,-1)",
    "[-1,1)", "[1,3)", "[3,5)", "[5,7)", "[7,10)", "[10,15)", ">15",
]
TIME_ORDER = [270, 240, 210, 180, 150, 120, 90, 60, 30, 10]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading price history with gaps...")
    df = pl.read_parquet(DATA_PATH)
    print("  Total observations: {:,}".format(df.height))
    print("  Unique windows: {}".format(df["window_start"].n_unique()))

    df = df.with_columns(pl.col("gap").abs().alias("abs_gap"))

    # ==================================================================
    # Analysis 1: Overall gap distribution
    # ==================================================================
    print("\n" + "=" * 80)
    print("ANALYSIS 1: Overall Gap Distribution")
    print("  gap = delta_table_fair_prob - polymarket_yes_price")
    print("  Positive gap = delta table says more likely UP than PM prices")
    print("=" * 80)

    print("\n  Count:      {:,}".format(df.height))
    print("  Mean gap:   {:.4f}".format(df["gap"].mean()))
    print("  Median gap: {:.4f}".format(df["gap"].median()))
    print("  Std dev:    {:.4f}".format(df["gap"].std()))
    print("  Mean |gap|: {:.4f}".format(df["abs_gap"].mean()))
    print("  Median |gap|: {:.4f}".format(df["abs_gap"].median()))

    for thresh in [0.01, 0.02, 0.03, 0.05, 0.10]:
        n = df.filter(pl.col("abs_gap") > thresh).height
        frac = n / df.height
        print("  |gap| > {:.2f}: {:.1f}% ({:,} obs)".format(thresh, frac * 100, n))

    # ==================================================================
    # Analysis 2: Gap by signal strength
    # ==================================================================
    print("\n" + "=" * 80)
    print("ANALYSIS 2: Gap by Signal Strength")
    print("=" * 80)

    strong = df.filter(
        (pl.col("delta_table_fair_prob") > 0.60)
        | (pl.col("delta_table_fair_prob") < 0.40)
    )
    weak = df.filter(
        (pl.col("delta_table_fair_prob") >= 0.40)
        & (pl.col("delta_table_fair_prob") <= 0.60)
    )

    for label, subset in [
        ("Strong signal (fair prob >60% or <40%)", strong),
        ("Weak signal (40%-60%)", weak),
    ]:
        print("\n  {}:".format(label))
        if subset.height == 0:
            print("    No observations")
            continue
        print("    Count:      {:,}".format(subset.height))
        print("    Mean gap:   {:.4f}".format(subset["gap"].mean()))
        print("    Mean |gap|: {:.4f}".format(subset["abs_gap"].mean()))
        print("    Median |gap|: {:.4f}".format(subset["abs_gap"].median()))
        for thresh in [0.03, 0.05, 0.10]:
            n = subset.filter(pl.col("abs_gap") > thresh).height
            frac = n / subset.height
            print("    |gap| > {:.2f}: {:.1f}%".format(thresh, frac * 100))

    # ==================================================================
    # Analysis 3: Gap by time remaining
    # ==================================================================
    print("\n" + "=" * 80)
    print("ANALYSIS 3: Gap by Time Remaining")
    print("=" * 80)

    time_stats = (
        df.group_by("time_bucket")
        .agg(
            [
                pl.len().alias("count"),
                pl.col("abs_gap").mean().alias("mean_abs_gap"),
                pl.col("gap").mean().alias("mean_signed_gap"),
                pl.col("abs_gap").median().alias("median_abs_gap"),
            ]
        )
        .sort("time_bucket", descending=True)
    )

    print(
        "\n  {:>8} | {:>7} | {:>10} | {:>10} | {:>10}".format(
            "Time", "Count", "Mean |gap|", "Mean gap", "Med |gap|"
        )
    )
    print("  " + "-" * 58)
    for row in time_stats.iter_rows(named=True):
        print(
            "  {:>6}s | {:>7,} | {:>10.4f} | {:>10.4f} | {:>10.4f}".format(
                row["time_bucket"],
                row["count"],
                row["mean_abs_gap"],
                row["mean_signed_gap"],
                row["median_abs_gap"],
            )
        )

    # Chart
    if time_stats.height > 0:
        ts_sorted = time_stats.sort("time_bucket", descending=True)
        times = ts_sorted["time_bucket"].to_list()
        mean_gaps = ts_sorted["mean_abs_gap"].to_list()

        fig, ax = plt.subplots(figsize=(10, 6))
        bars = ax.bar(
            range(len(times)), mean_gaps, color="steelblue", edgecolor="navy", alpha=0.8
        )
        ax.set_xticks(range(len(times)))
        ax.set_xticklabels(["{}s".format(t) for t in times])
        ax.set_xlabel("Time Remaining (seconds)", fontsize=12)
        ax.set_ylabel("Mean |gap| (fair prob - PM price)", fontsize=12)
        ax.set_title("Polymarket Mispricing by Time Remaining", fontsize=14)

        for bar, g in zip(bars, mean_gaps):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.001,
                "{:.3f}".format(g),
                ha="center",
                va="bottom",
                fontsize=9,
            )

        plt.tight_layout()
        fig.savefig(OUTPUT_DIR / "gap_by_time.png", dpi=150)
        plt.close(fig)
        print("\n  Saved output/gap_by_time.png")

    # ==================================================================
    # Analysis 4: Gap direction and symmetry
    # ==================================================================
    print("\n" + "=" * 80)
    print("ANALYSIS 4: Gap Direction & Symmetry")
    print("=" * 80)

    up_likely = df.filter(pl.col("delta_table_fair_prob") > 0.55)
    down_likely = df.filter(pl.col("delta_table_fair_prob") < 0.45)

    print("\n  UP-likely zones (fair prob > 55%):")
    if up_likely.height > 0:
        print("    Count: {:,}".format(up_likely.height))
        print("    Mean signed gap: {:.4f}".format(up_likely["gap"].mean()))
        print("    (Positive = PM underpricing the UP outcome)")

    print("\n  DOWN-likely zones (fair prob < 45%):")
    if down_likely.height > 0:
        print("    Count: {:,}".format(down_likely.height))
        print("    Mean signed gap: {:.4f}".format(down_likely["gap"].mean()))
        print("    (Negative = PM overpricing UP / underpricing DOWN)")

    overall_bias = df["gap"].mean()
    print("\n  Overall signed gap: {:.4f}".format(overall_bias))
    if abs(overall_bias) < 0.005:
        print("  -> Roughly symmetric (no systematic bias)")
    elif overall_bias > 0:
        print("  -> PM tends to underprice YES (UP)")
    else:
        print("  -> PM tends to overprice YES (UP)")

    # ==================================================================
    # Analysis 5: Theoretical PnL simulation
    # ==================================================================
    print("\n" + "=" * 80)
    print("ANALYSIS 5: Theoretical PnL Simulation")
    print("  Buy YES when gap > threshold, buy NO when gap < -threshold")
    print("  Binary payout: $1 if correct, $0 if wrong")
    print("=" * 80)

    print(
        "\n  {:>8} | {:>7} | {:>6} | {:>15} | {:>15} | {:>10}".format(
            "Thresh", "Trades", "Win%", "Avg PnL(maker)", "Avg PnL(taker)", "Total PnL"
        )
    )
    print("  " + "-" * 78)

    best_threshold = None
    best_avg_pnl = -999
    best_win_rate = 0
    best_n_trades = 0
    best_avg_pnl_taker = 0

    for thresh in [0.02, 0.03, 0.05, 0.07, 0.10]:
        # Buy YES when delta table says more UP than PM prices
        buy_yes = df.filter(pl.col("gap") > thresh)
        # Buy NO when delta table says more DOWN than PM prices
        buy_no = df.filter(pl.col("gap") < -thresh)

        pnl_list = []

        # YES trades
        for row in buy_yes.iter_rows(named=True):
            entry = row["polymarket_yes_price"]
            won = row["outcome"] == "Up"
            gross = (1.0 - entry) if won else -entry
            gross_taker = (1.0 - entry) * 0.982 if won else -entry
            pnl_list.append((won, gross, gross_taker))

        # NO trades
        for row in buy_no.iter_rows(named=True):
            entry = 1.0 - row["polymarket_yes_price"]
            won = row["outcome"] == "Down"
            gross = (1.0 - entry) if won else -entry
            gross_taker = (1.0 - entry) * 0.982 if won else -entry
            pnl_list.append((won, gross, gross_taker))

        n_trades = len(pnl_list)
        if n_trades == 0:
            print(
                "  {:>8.2f} | {:>7} | {:>6} | {:>15} | {:>15} | {:>10}".format(
                    thresh, 0, "N/A", "N/A", "N/A", "N/A"
                )
            )
            continue

        wins = sum(1 for w, _, _ in pnl_list if w)
        win_rate = wins / n_trades
        avg_pnl = sum(g for _, g, _ in pnl_list) / n_trades
        avg_pnl_taker = sum(gt for _, _, gt in pnl_list) / n_trades
        total_pnl = sum(g for _, g, _ in pnl_list)

        print(
            "  {:>8.2f} | {:>7,} | {:>5.1f}% | ${:>14.4f} | ${:>14.4f} | ${:>9.2f}".format(
                thresh, n_trades, win_rate * 100, avg_pnl, avg_pnl_taker, total_pnl
            )
        )

        if avg_pnl > best_avg_pnl and n_trades >= 20:
            best_avg_pnl = avg_pnl
            best_threshold = thresh
            best_win_rate = win_rate
            best_n_trades = n_trades
            best_avg_pnl_taker = avg_pnl_taker

    # ==================================================================
    # Analysis 6: Gap heatmap
    # ==================================================================
    print("\n" + "=" * 80)
    print("ANALYSIS 6: Gap Heatmap")
    print("=" * 80)

    gap_by_cell = df.group_by(["delta_bucket", "time_bucket"]).agg(
        [
            pl.col("abs_gap").mean().alias("mean_abs_gap"),
            pl.len().alias("count"),
        ]
    )

    gap_pivot = {}
    count_pivot = {}
    for row in gap_by_cell.iter_rows(named=True):
        key = (row["delta_bucket"], row["time_bucket"])
        gap_pivot[key] = row["mean_abs_gap"]
        count_pivot[key] = row["count"]

    heatmap_data = np.full((len(DELTA_ORDER), len(TIME_ORDER)), np.nan)
    for i, delta in enumerate(DELTA_ORDER):
        for j, t in enumerate(TIME_ORDER):
            val = gap_pivot.get((delta, t))
            count = count_pivot.get((delta, t), 0)
            if val is not None and count >= 5:
                heatmap_data[i, j] = val * 100

    fig, ax = plt.subplots(figsize=(14, 10))
    cmap = sns.color_palette("YlOrRd", as_cmap=True)
    sns.heatmap(
        heatmap_data,
        ax=ax,
        cmap=cmap,
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
    ax.set_xlabel("Time Remaining (seconds)", fontsize=12)
    ax.set_ylabel("Delta from Open (basis points)", fontsize=12)
    ax.set_title("Polymarket Mispricing: Mean |Gap| by Delta & Time", fontsize=14)
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "gap_heatmap.png", dpi=150)
    plt.close(fig)
    print("  Saved output/gap_heatmap.png")

    # ==================================================================
    # VERDICT
    # ==================================================================
    print("\n" + "=" * 80)
    print("=== VERDICT ===")
    print("=" * 80)

    total_obs = df.height
    strong_obs = strong.height
    median_abs_gap = df["abs_gap"].median()
    strong_median_gap = strong["abs_gap"].median() if strong.height > 0 else 0

    print("\n  Total observations with Polymarket price data: {:,}".format(total_obs))
    print("  Observations in strong-signal zones: {:,}".format(strong_obs))
    print("\n  Median |gap| overall: ${:.3f}".format(median_abs_gap))
    print("  Median |gap| in strong-signal zones: ${:.3f}".format(strong_median_gap))

    if best_threshold is not None:
        print("\n  Best theoretical threshold: {:.2f}".format(best_threshold))
        print("    Win rate: {:.1f}%".format(best_win_rate * 100))
        print("    Avg PnL per trade (maker): ${:.4f}".format(best_avg_pnl))
        print("    Avg PnL per trade (taker): ${:.4f}".format(best_avg_pnl_taker))
        print("    Number of trades (14 days): {}".format(best_n_trades))

    # Assessment
    if (
        strong.height > 0
        and strong_median_gap > 0.03
        and best_threshold is not None
        and best_win_rate > 0.55
        and best_n_trades >= 100
    ):
        assessment = "EDGE EXISTS"
    elif strong.height > 0 and (
        strong_median_gap > 0.02
        or (best_threshold is not None and best_win_rate > 0.52)
    ):
        assessment = "MARGINAL"
    else:
        assessment = "NO EDGE"

    print("\n  ASSESSMENT: {}".format(assessment))
    print("  Criteria:")
    print(
        "    - EDGE EXISTS: median |gap| in strong zones > $0.03 AND win rate > 55% AND 100+ trades"
    )
    print(
        "    - MARGINAL: some gaps exist but small (<$0.03) or low trade count"
    )
    print(
        "    - NO EDGE: gaps consistently < $0.02 — market is efficient"
    )

    print("\nDone.")


if __name__ == "__main__":
    main()
