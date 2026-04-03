"""
Extend Binance BTC/USDT 1-second klines from 14 days to 60 days.
Fetches backward from existing data start.

Saves: data/binance_klines_60d.parquet
"""

import time
import requests
import polars as pl
from pathlib import Path

SYMBOL = "BTCUSDT"
INTERVAL = "1s"
ENDPOINT = "https://api.binance.com/api/v3/klines"
MAX_CANDLES = 1000
EXISTING_PATH = Path("data/binance_klines.parquet")
OUTPUT_PATH = Path("data/binance_klines_60d.parquet")
TARGET_DAYS = 60


def fetch_klines(start_ms, end_ms):
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": MAX_CANDLES,
    }
    resp = requests.get(ENDPOINT, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main():
    existing = pl.read_parquet(EXISTING_PATH)
    earliest_ms = existing["timestamp"].min()
    latest_ms = existing["timestamp"].max()

    print("Existing data: {} to {}".format(
        time.strftime("%Y-%m-%d %H:%M", time.gmtime(earliest_ms / 1000)),
        time.strftime("%Y-%m-%d %H:%M", time.gmtime(latest_ms / 1000)),
    ))
    print("  Rows: {:,}".format(existing.height))

    target_start_ms = latest_ms - (TARGET_DAYS * 86400 * 1000)
    need_seconds = (earliest_ms - target_start_ms) / 1000
    est_requests = int(need_seconds / MAX_CANDLES) + 1

    print("\nFetching backward to {}".format(
        time.strftime("%Y-%m-%d", time.gmtime(target_start_ms / 1000))
    ))
    print("  ~{:,} seconds = {:.1f} days, ~{:,} requests".format(
        int(need_seconds), need_seconds / 86400, est_requests
    ))

    all_rows = []
    cursor = target_start_ms
    end_ms = earliest_ms - 1
    request_count = 0
    errors = 0

    while cursor < end_ms:
        try:
            data = fetch_klines(cursor, end_ms)
        except Exception as e:
            errors += 1
            if errors > 20:
                print("\n  Too many errors, stopping.")
                break
            print("  Error: {}, retrying in 5s...".format(e))
            time.sleep(5)
            continue

        if not data:
            break

        request_count += 1
        for row in data:
            all_rows.append({
                "timestamp": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        last_ts = int(data[-1][0])
        cursor = last_ts + 1000

        if request_count % 50 == 0:
            pct = (cursor - target_start_ms) / (end_ms - target_start_ms) * 100
            print("  [{:5.1f}%] {:>10,} candles | req #{:,} | errors: {}".format(
                pct, len(all_rows), request_count, errors
            ))

        # Conservative rate limiting
        if request_count % 10 == 0:
            time.sleep(1.0)
        else:
            time.sleep(0.1)

    print("\n  Fetched {:,} new candles in {:,} requests ({} errors)".format(
        len(all_rows), request_count, errors
    ))

    if not all_rows:
        print("No new data fetched!")
        return

    new_df = pl.DataFrame(all_rows)

    # Merge with existing
    print("\nMerging with existing data...")
    merged = pl.concat([new_df, existing])
    merged = merged.sort("timestamp")
    merged = merged.unique(subset=["timestamp"], keep="first")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged.write_parquet(OUTPUT_PATH)

    print("Saved to {} ({:.1f} MB)".format(
        OUTPUT_PATH, OUTPUT_PATH.stat().st_size / 1024 / 1024
    ))
    print("  Total rows: {:,}".format(merged.height))
    print("  From: {}".format(
        time.strftime("%Y-%m-%d %H:%M", time.gmtime(merged["timestamp"].min() / 1000))
    ))
    print("  To:   {}".format(
        time.strftime("%Y-%m-%d %H:%M", time.gmtime(merged["timestamp"].max() / 1000))
    ))
    print("  Days: {:.1f}".format(
        (merged["timestamp"].max() - merged["timestamp"].min()) / 1000 / 86400
    ))


if __name__ == "__main__":
    main()
