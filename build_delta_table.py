"""
Build the delta table from Binance 1s kline data.
Segments data into 5-minute windows, computes deltas and win rates.
Outputs:
  - data/delta_table.csv
  - data/observations.parquet
"""

import polars as pl
import numpy as np
from pathlib import Path

INPUT_PATH = Path("data/binance_klines.parquet")
DELTA_TABLE_PATH = Path("data/delta_table.csv")
OBSERVATIONS_PATH = Path("data/observations.parquet")

WINDOW_MS = 300_000  # 5 minutes in ms

# Sample every 10 seconds within each window
SAMPLE_INTERVAL_S = 10

# Delta buckets in basis points
DELTA_EDGES = [-float("inf"), -15, -10, -7, -5, -3, -1, 1, 3, 5, 7, 10, 15, float("inf")]

# Time remaining buckets in seconds
TIME_BUCKETS = [270, 240, 210, 180, 150, 120, 90, 60, 30, 10]


def bucket_delta(delta_bps: float) -> str:
    """Assign a delta value to its bucket label."""
    for i in range(len(DELTA_EDGES) - 1):
        if delta_bps < DELTA_EDGES[i + 1]:
            lo = DELTA_EDGES[i]
            hi = DELTA_EDGES[i + 1]
            if lo == -float("inf"):
                return f"<{hi:.0f}"
            if hi == float("inf"):
                return f">{lo:.0f}"
            return f"[{lo:.0f},{hi:.0f})"
    return f">{DELTA_EDGES[-2]:.0f}"


def closest_time_bucket(time_remaining_s: int):
    """Snap time_remaining to the nearest time bucket, within 5s tolerance."""
    best = None
    best_dist = float("inf")
    for tb in TIME_BUCKETS:
        dist = abs(time_remaining_s - tb)
        if dist < best_dist:
            best_dist = dist
            best = tb
    return best if best_dist <= 5 else None


def main():
    print("Loading kline data...")
    df = pl.read_parquet(INPUT_PATH)
    print(f"  Loaded {df.height:,} candles")

    # Compute window assignment
    df = df.with_columns(
        (pl.col("timestamp") - (pl.col("timestamp") % WINDOW_MS)).alias("window_start")
    )

    # Get unique windows
    windows = df.select("window_start").unique().sort("window_start")
    window_starts = windows["window_start"].to_list()
    print(f"  Found {len(window_starts):,} 5-minute windows")

    # For each window, determine open_price and outcome
    # open_price = close of the first 1s candle in the window
    # outcome = UP if last candle close >= open_price
    print("Computing window open prices and outcomes...")

    window_first = (
        df.sort("timestamp")
        .group_by("window_start")
        .first()
        .select(["window_start", pl.col("close").alias("open_price")])
    )

    window_last = (
        df.sort("timestamp")
        .group_by("window_start")
        .last()
        .select(["window_start", pl.col("close").alias("close_price")])
    )

    window_info = window_first.join(window_last, on="window_start")
    window_info = window_info.with_columns(
        (pl.col("close_price") >= pl.col("open_price")).alias("outcome_up")
    )

    # Filter out windows that don't have enough data (e.g., partial windows at edges)
    window_candle_counts = df.group_by("window_start").len()
    # Keep windows with at least 200 candles (out of 300 expected)
    valid_windows = window_candle_counts.filter(pl.col("len") >= 200).select("window_start")
    window_info = window_info.join(valid_windows, on="window_start")
    print(f"  Valid windows (>=200 candles): {window_info.height:,}")

    # Join open_price back to main dataframe
    df = df.join(window_info.select(["window_start", "open_price", "outcome_up"]), on="window_start")

    # Compute time_remaining and delta for each candle
    df = df.with_columns(
        ((pl.col("window_start") + WINDOW_MS - pl.col("timestamp")) / 1000)
        .cast(pl.Int32)
        .alias("time_remaining_s"),
        (
            (pl.col("close") - pl.col("open_price")) / pl.col("open_price") * 10000
        ).alias("delta_bps"),
    )

    # Sample: keep observations at ~10s intervals
    # time_remaining should be close to one of the TIME_BUCKETS
    print("Sampling observations at 10s intervals...")
    sample_times_ms = set()
    for tb in TIME_BUCKETS:
        sample_times_ms.add(tb)

    # Filter to rows where time_remaining_s matches a time bucket
    observations = df.filter(pl.col("time_remaining_s").is_in(TIME_BUCKETS))

    print(f"  Sampled observations: {observations.height:,}")

    # Build observation records
    obs_records = observations.select([
        pl.col("window_start").alias("window_id"),
        pl.col("delta_bps"),
        pl.col("time_remaining_s").alias("time_remaining"),
        pl.col("outcome_up"),
    ])

    # Assign delta buckets
    delta_bps_values = obs_records["delta_bps"].to_list()
    delta_bucket_labels = [bucket_delta(d) for d in delta_bps_values]
    obs_records = obs_records.with_columns(
        pl.Series("delta_bucket", delta_bucket_labels)
    )
    obs_records = obs_records.with_columns(
        pl.col("time_remaining").alias("time_bucket")
    )

    # Save raw observations
    OBSERVATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    obs_records.write_parquet(OBSERVATIONS_PATH)
    print(f"  Saved observations to {OBSERVATIONS_PATH}")

    # Build the delta table: for each (delta_bucket, time_bucket), compute count and win_rate
    print("Building delta table...")
    delta_table = (
        obs_records.group_by(["delta_bucket", "time_bucket"])
        .agg([
            pl.len().alias("count"),
            pl.col("outcome_up").mean().alias("win_rate"),
        ])
        .sort(["delta_bucket", "time_bucket"])
    )

    delta_table.write_csv(DELTA_TABLE_PATH)
    print(f"  Saved delta table to {DELTA_TABLE_PATH}")
    print(f"  Total cells: {delta_table.height}")
    print(f"  Total observations: {delta_table['count'].sum():,}")

    # Quick preview
    print("\nDelta table preview (first 20 rows):")
    print(delta_table.head(20))

    # Summary stats
    strong = delta_table.filter(
        (pl.col("win_rate") > 0.60) | (pl.col("win_rate") < 0.40)
    )
    print(f"\nStrong signal cells (>60% or <40%): {strong.height} / {delta_table.height}")
    if strong.height > 0:
        print(f"  Observations in strong cells: {strong['count'].sum():,}")


if __name__ == "__main__":
    main()
