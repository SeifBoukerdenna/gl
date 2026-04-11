"""Extend binance_klines_60d.parquet from its last timestamp to now."""
import pandas as pd
import requests
import time
from pathlib import Path

P = Path("data/binance_klines_60d.parquet")
URL = "https://api.binance.com/api/v3/klines"


def main():
    df = pd.read_parquet(P)
    last_ms = int(df["timestamp"].max())
    print(f"Existing: {len(df):,} rows  last_ts={last_ms}  ({pd.to_datetime(last_ms, unit='ms')})")

    start_ms = last_ms + 1000
    now_ms = int(time.time() * 1000)
    gap_h = (now_ms - start_ms) / 3600 / 1000
    print(f"Gap: {gap_h:.1f}h  → fetching")

    new_rows = []
    cur = start_ms
    chunks = 0
    while cur < now_ms:
        end = min(cur + 1000 * 1000, now_ms)
        for attempt in range(3):
            try:
                r = requests.get(URL, params={
                    "symbol": "BTCUSDT", "interval": "1s",
                    "startTime": cur, "endTime": end, "limit": 1000,
                }, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  fail @{cur}: {e}")
                    data = []
                else:
                    time.sleep(2 ** attempt)
        if not data:
            cur = end
            continue
        for k in data:
            new_rows.append({
                "timestamp": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            })
        chunks += 1
        if chunks % 50 == 0:
            print(f"  chunks={chunks} rows={len(new_rows):,} last={data[-1][0]}")
        cur = int(data[-1][0]) + 1000
        time.sleep(0.05)

    if not new_rows:
        print("nothing new")
        return

    new_df = pd.DataFrame(new_rows)
    print(f"\nFetched {len(new_df):,} new rows")
    combined = pd.concat([df, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    combined.to_parquet(P, index=False)
    print(f"Saved {len(combined):,} total rows to {P}")
    print(f"  range: {pd.to_datetime(combined['timestamp'].min(), unit='ms')} → {pd.to_datetime(combined['timestamp'].max(), unit='ms')}")


if __name__ == "__main__":
    main()
