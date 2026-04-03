"""
Analyze the delta table and generate visualizations.
Outputs:
  - Formatted delta table grid to stdout
  - output/delta_table_heatmap.png
  - output/edge_by_time.png
"""

import polars as pl
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

DELTA_TABLE_PATH = Path("data/delta_table.csv")
OUTPUT_DIR = Path("output")

# Ordered delta bucket labels (from most negative to most positive)
DELTA_ORDER = ["<-15", "[-15,-10)", "[-10,-7)", "[-7,-5)", "[-5,-3)", "[-3,-1)",
               "[-1,1)", "[1,3)", "[3,5)", "[5,7)", "[7,10)", "[10,15)", ">15"]

TIME_ORDER = [270, 240, 210, 180, 150, 120, 90, 60, 30, 10]

MIN_OBS = 50  # Minimum observations for a cell to be considered reliable


def print_grid(pivot_wr, pivot_count):
    """Print the delta table as a formatted ASCII grid."""
    print("\n" + "=" * 120)
    print("DELTA TABLE — Historical Win Rate (% chance window closes UP)")
    print("Rows: delta from open (basis points) | Columns: seconds remaining in window")
    print("=" * 120)

    # Header
    header = f"{'Delta (bps)':>14}"
    for t in TIME_ORDER:
        header += f" | {t:>5}s"
    print(header)
    print("-" * len(header))

    for delta in DELTA_ORDER:
        row = f"{delta:>14}"
        for t in TIME_ORDER:
            wr = pivot_wr.get((delta, t))
            count = pivot_count.get((delta, t), 0)
            if wr is not None and count >= MIN_OBS:
                pct = wr * 100
                marker = ""
                if pct >= 60 or pct <= 40:
                    marker = " **"
                elif pct >= 55 or pct <= 45:
                    marker = " *"
                row += f" | {pct:5.1f}{marker}"
            elif wr is not None:
                row += f" | ({wr * 100:4.1f})"  # Parentheses = low count
            else:
                row += f" |     -"
        print(row)

    print("-" * len(header))
    print("** = strong signal (>60% or <40%)  * = moderate signal  () = <50 observations")
    print()


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading delta table...")
    dt = pl.read_csv(DELTA_TABLE_PATH)
    print(f"  Loaded {dt.height} cells")

    # Build lookup dicts
    pivot_wr = {}
    pivot_count = {}
    for row in dt.iter_rows(named=True):
        key = (row["delta_bucket"], row["time_bucket"])
        pivot_wr[key] = row["win_rate"]
        pivot_count[key] = row["count"]

    # Print formatted grid
    print_grid(pivot_wr, pivot_count)

    # Summary statistics
    print("=" * 80)
    print("SUMMARY STATISTICS")
    print("=" * 80)

    reliable = dt.filter(pl.col("count") >= MIN_OBS)
    total_cells = reliable.height
    total_obs = reliable["count"].sum()

    strong = reliable.filter(
        (pl.col("win_rate") > 0.60) | (pl.col("win_rate") < 0.40)
    )
    moderate = reliable.filter(
        ((pl.col("win_rate") > 0.55) | (pl.col("win_rate") < 0.45))
        & (pl.col("win_rate") <= 0.60)
        & (pl.col("win_rate") >= 0.40)
    )

    print(f"\nReliable cells (>={MIN_OBS} observations): {total_cells}")
    print(f"Total observations in reliable cells: {total_obs:,}")

    print(f"\nStrong signal cells (>60% or <40%): {strong.height} / {total_cells}")
    if strong.height > 0:
        strong_obs = strong["count"].sum()
        print(f"  Observations in strong cells: {strong_obs:,} ({strong_obs / total_obs * 100:.1f}% of total)")
        print(f"  These are:")
        for row in strong.sort("win_rate").iter_rows(named=True):
            print(f"    delta={row['delta_bucket']:>14}  time={row['time_bucket']:>3}s  "
                  f"wr={row['win_rate'] * 100:5.1f}%  n={row['count']}")

    print(f"\nModerate signal cells (55-60% or 40-45%): {moderate.height} / {total_cells}")

    # Average absolute deviation from 50%
    if total_cells > 0:
        deviations = reliable.with_columns(
            (pl.col("win_rate") - 0.5).abs().alias("abs_dev")
        )
        avg_dev = deviations["abs_dev"].mean()
        weighted_avg_dev = (
            (deviations["abs_dev"] * deviations["count"]).sum() / deviations["count"].sum()
        )
        print(f"\nAverage absolute deviation from 50%: {avg_dev * 100:.2f}%")
        print(f"Observation-weighted avg deviation:  {weighted_avg_dev * 100:.2f}%")

    # Heatmap
    print("\nGenerating heatmap...")
    heatmap_data = np.full((len(DELTA_ORDER), len(TIME_ORDER)), np.nan)
    for i, delta in enumerate(DELTA_ORDER):
        for j, t in enumerate(TIME_ORDER):
            wr = pivot_wr.get((delta, t))
            count = pivot_count.get((delta, t), 0)
            if wr is not None and count >= MIN_OBS:
                heatmap_data[i, j] = wr * 100

    fig, ax = plt.subplots(figsize=(14, 10))
    cmap = sns.diverging_palette(240, 10, as_cmap=True)  # Blue -> White -> Red
    sns.heatmap(
        heatmap_data,
        ax=ax,
        cmap=cmap,
        center=50,
        vmin=30,
        vmax=70,
        annot=True,
        fmt=".1f",
        xticklabels=[f"{t}s" for t in TIME_ORDER],
        yticklabels=DELTA_ORDER,
        cbar_kws={"label": "Win Rate (%)"},
        linewidths=0.5,
        mask=np.isnan(heatmap_data),
    )
    ax.set_xlabel("Time Remaining (seconds)", fontsize=12)
    ax.set_ylabel("Delta from Open (basis points)", fontsize=12)
    ax.set_title("BTC 5-Min Window: Historical P(UP) by Delta & Time Remaining", fontsize=14)
    plt.tight_layout()
    heatmap_path = OUTPUT_DIR / "delta_table_heatmap.png"
    fig.savefig(heatmap_path, dpi=150)
    plt.close(fig)
    print(f"  Saved {heatmap_path}")

    # Edge by time chart
    print("Generating edge-by-time chart...")
    edge_by_time = []
    for t in TIME_ORDER:
        devs = []
        counts = []
        for delta in DELTA_ORDER:
            wr = pivot_wr.get((delta, t))
            count = pivot_count.get((delta, t), 0)
            if wr is not None and count >= MIN_OBS:
                devs.append(abs(wr - 0.5))
                counts.append(count)
        if devs:
            avg_edge = sum(d * c for d, c in zip(devs, counts)) / sum(counts)
            edge_by_time.append({"time_remaining": t, "avg_edge": avg_edge * 100, "n_buckets": len(devs)})

    if edge_by_time:
        fig2, ax2 = plt.subplots(figsize=(10, 6))
        times = [e["time_remaining"] for e in edge_by_time]
        edges = [e["avg_edge"] for e in edge_by_time]
        bars = ax2.bar(range(len(times)), edges, color="steelblue", edgecolor="navy", alpha=0.8)
        ax2.set_xticks(range(len(times)))
        ax2.set_xticklabels([f"{t}s" for t in times])
        ax2.set_xlabel("Time Remaining (seconds)", fontsize=12)
        ax2.set_ylabel("Average Absolute Edge (%)", fontsize=12)
        ax2.set_title("Signal Strength by Time Remaining in Window", fontsize=14)
        ax2.axhline(y=0, color="gray", linestyle="-", linewidth=0.5)

        for bar, edge in zip(bars, edges):
            ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1,
                     f"{edge:.1f}%", ha="center", va="bottom", fontsize=9)

        plt.tight_layout()
        edge_path = OUTPUT_DIR / "edge_by_time.png"
        fig2.savefig(edge_path, dpi=150)
        plt.close(fig2)
        print(f"  Saved {edge_path}")
    else:
        print("  Not enough reliable data for edge-by-time chart.")

    print("\nDone.")


if __name__ == "__main__":
    main()
