"""
Fetch 14 days of BTC/USDT 1-second kline data from Binance.
Saves to data/binance_klines.parquet
"""

import time
import requests
import polars as pl
from pathlib import Path

SYMBOL = "BTCUSDT"
INTERVAL = "1s"
ENDPOINT = "https://api.binance.com/api/v3/klines"
MAX_CANDLES = 1000
OUTPUT_PATH = Path("data/binance_klines.parquet")
DAYS = 14

# Binance kline columns we care about
# [open_time, open, high, low, close, volume, close_time, ...]
# We keep: open_time as timestamp, open, high, low, close, volume


def fetch_klines(start_ms: int, end_ms: int) -> list[list]:
    """Fetch up to 1000 1s klines starting from start_ms."""
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
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (DAYS * 24 * 60 * 60 * 1000)
    end_ms = now_ms

    all_rows = []
    cursor = start_ms
    total_seconds = DAYS * 24 * 60 * 60
    request_count = 0

    print(f"Fetching {DAYS} days of 1s BTC/USDT klines...")
    print(f"  From: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start_ms / 1000))} UTC")
    print(f"  To:   {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(end_ms / 1000))} UTC")
    print(f"  Expected ~{total_seconds:,} candles, ~{total_seconds // MAX_CANDLES + 1} requests")
    print()

    while cursor < end_ms:
        try:
            data = fetch_klines(cursor, end_ms)
        except requests.exceptions.RequestException as e:
            print(f"  Request error: {e}, retrying in 5s...")
            time.sleep(5)
            continue

        if not data:
            break

        request_count += 1
        for row in data:
            all_rows.append(
                {
                    "timestamp": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )

        last_ts = int(data[-1][0])
        cursor = last_ts + 1000  # next second

        fetched = len(all_rows)
        pct = fetched / total_seconds * 100
        print(f"  [{pct:5.1f}%] {fetched:>10,} candles | request #{request_count}", end="\r")

        # Conservative rate limiting: ~10 requests/sec (well under 1200/min)
        if request_count % 10 == 0:
            time.sleep(1.0)
        else:
            time.sleep(0.1)

    print()
    print(f"Downloaded {len(all_rows):,} candles in {request_count} requests.")

    df = pl.DataFrame(all_rows)
    df = df.sort("timestamp")
    df = df.unique(subset=["timestamp"], keep="first")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(OUTPUT_PATH)
    print(f"Saved to {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  Rows: {df.height:,}")
    print(f"  Date range: {df['timestamp'].min()} — {df['timestamp'].max()}")


if __name__ == "__main__":
    main()
