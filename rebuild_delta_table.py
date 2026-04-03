"""
Rebuild the delta table using Polymarket's actual Chainlink-based outcomes
instead of Binance-derived outcomes.

The delta values (basis points from open) stay the same — Binance and Chainlink
move in lockstep so the relative deltas are nearly identical. Only the outcome
labeling (UP/DOWN) changes, since the settlement reference price differs.

Uses:
  - data/observations.parquet (Phase 1 per-observation data)
  - data/historical_markets.csv (Polymarket outcomes from Chainlink settlement)

Saves:
  - data/delta_table_corrected.csv
"""

import polars as pl
from pathlib import Path

OBSERVATIONS_PATH = Path("data/observations.parquet")
MARKETS_PATH = Path("data/historical_markets.csv")
ORIGINAL_DT_PATH = Path("data/delta_table.csv")
CORRECTED_DT_PATH = Path("data/delta_table_corrected.csv")


def main():
    print("Loading data...")
    obs = pl.read_parquet(OBSERVATIONS_PATH)
    print("  Observations: {:,} ({} windows)".format(obs.height, obs["window_id"].n_unique()))

    markets = pl.read_csv(
        MARKETS_PATH,
        schema_overrides={"yes_token_id": pl.Utf8, "no_token_id": pl.Utf8},
    )
    print("  Markets (Polymarket outcomes): {}".format(markets.height))

    original_dt = pl.read_csv(ORIGINAL_DT_PATH)
    print("  Original delta table: {} cells".format(original_dt.height))

    # Convert observations window_id from ms to seconds for joining
    obs = obs.with_columns(
        (pl.col("window_id") // 1000).alias("window_start_s")
    )

    # Create Polymarket outcome lookup: window_start -> outcome_up (bool)
    pm_outcomes = markets.select(
        [
            pl.col("window_start"),
            (pl.col("outcome") == "Up").alias("pm_outcome_up"),
        ]
    )

    # Join observations with Polymarket outcomes
    obs_joined = obs.join(
        pm_outcomes,
        left_on="window_start_s",
        right_on="window_start",
        how="left",
    )

    matched = obs_joined.filter(pl.col("pm_outcome_up").is_not_null())
    unmatched = obs_joined.filter(pl.col("pm_outcome_up").is_null())

    print("\nOutcome matching:")
    print(
        "  Matched to Polymarket: {:,} obs ({} windows)".format(
            matched.height, matched["window_start_s"].n_unique()
        )
    )
    print(
        "  Unmatched (Binance only): {:,} obs ({} windows)".format(
            unmatched.height, unmatched["window_start_s"].n_unique()
        )
    )

    # Count outcome disagreements
    disagree = matched.filter(pl.col("outcome_up") != pl.col("pm_outcome_up"))
    disagree_windows = disagree["window_start_s"].n_unique()
    total_matched_windows = matched["window_start_s"].n_unique()

    print("\nOutcome disagreements (Binance vs Chainlink):")
    print(
        "  {}/{} windows ({:.1f}%)".format(
            disagree_windows,
            total_matched_windows,
            disagree_windows / total_matched_windows * 100
            if total_matched_windows > 0
            else 0,
        )
    )

    # Show which delta buckets the disagreements fall in
    if disagree.height > 0:
        disagree_by_delta = (
            disagree.group_by("delta_bucket").len().sort("len", descending=True)
        )
        print("  Disagreements by delta bucket:")
        for row in disagree_by_delta.iter_rows(named=True):
            print("    {}: {} obs".format(row["delta_bucket"], row["len"]))

    # Rebuild delta table using ONLY matched observations (Chainlink outcomes)
    print("\nRebuilding delta table with corrected outcomes...")
    new_dt = (
        matched.group_by(["delta_bucket", "time_bucket"])
        .agg(
            [
                pl.len().alias("count"),
                pl.col("pm_outcome_up").mean().alias("win_rate"),
            ]
        )
        .sort(["delta_bucket", "time_bucket"])
    )

    new_dt.write_csv(CORRECTED_DT_PATH)
    print("  Saved corrected delta table to {}".format(CORRECTED_DT_PATH))
    print("  Cells: {}".format(new_dt.height))
    print("  Total observations: {:,}".format(new_dt["count"].sum()))

    # ======================================================================
    # Compare original vs corrected
    # ======================================================================
    print("\n" + "=" * 80)
    print("=== DELTA TABLE COMPARISON ===")
    print("=" * 80)

    orig_strong = original_dt.filter(
        (pl.col("win_rate") > 0.60) | (pl.col("win_rate") < 0.40)
    )
    new_strong = new_dt.filter(
        (pl.col("win_rate") > 0.60) | (pl.col("win_rate") < 0.40)
    )

    orig_dev = (original_dt["win_rate"] - 0.5).abs().mean()
    new_dev = (new_dt["win_rate"] - 0.5).abs().mean()

    print("\n  Original (Binance-based):")
    print("    Cells: {}".format(original_dt.height))
    print(
        "    Strong signal cells (>60% or <40%): {}/{}".format(
            orig_strong.height, original_dt.height
        )
    )
    print("    Avg absolute deviation from 50%: {:.2f}%".format(orig_dev * 100))

    print("\n  Corrected (Chainlink outcomes):")
    print("    Cells: {}".format(new_dt.height))
    print(
        "    Strong signal cells (>60% or <40%): {}/{}".format(
            new_strong.height, new_dt.height
        )
    )
    print("    Avg absolute deviation from 50%: {:.2f}%".format(new_dev * 100))

    # Per-cell comparison
    merged = original_dt.join(
        new_dt, on=["delta_bucket", "time_bucket"], suffix="_corrected"
    )
    if merged.height > 0:
        merged = merged.with_columns(
            (pl.col("win_rate") - pl.col("win_rate_corrected"))
            .abs()
            .alias("wr_change")
        )
        print("\n  Per-cell win rate change ({} matched cells):".format(merged.height))
        print(
            "    Mean |change|: {:.2f}%".format(merged["wr_change"].mean() * 100)
        )
        print(
            "    Max |change|:  {:.2f}%".format(merged["wr_change"].max() * 100)
        )
        big_changes = merged.filter(pl.col("wr_change") > 0.05)
        print("    Cells with >5% change: {}".format(big_changes.height))

        if big_changes.height > 0:
            print("\n    Largest changes:")
            top = big_changes.sort("wr_change", descending=True).head(10)
            for row in top.iter_rows(named=True):
                print(
                    "      delta={:>14} time={:>3}s  orig={:.1f}% -> corr={:.1f}%  (delta={:+.1f}%)".format(
                        row["delta_bucket"],
                        row["time_bucket"],
                        row["win_rate"] * 100,
                        row["win_rate_corrected"] * 100,
                        (row["win_rate_corrected"] - row["win_rate"]) * 100,
                    )
                )


if __name__ == "__main__":
    main()
