"""
Fetch historical YES token prices from Polymarket CLOB API for each
discovered 5-minute BTC market. Cross-reference with Binance kline data
and the delta table to compute the gap (fair prob - market price).

Saves to: data/price_history_with_gaps.parquet
"""

import asyncio
import time

import httpx
import numpy as np
import polars as pl
from pathlib import Path

MARKETS_PATH = Path("data/historical_markets.csv")
BINANCE_PATH = Path("data/binance_klines.parquet")
DELTA_TABLE_PATH = Path("data/delta_table.csv")
OUTPUT_PATH = Path("data/price_history_with_gaps.parquet")

CLOB_URL = "https://clob.polymarket.com/prices-history"
WINDOW_SECONDS = 300
CONCURRENCY = 5
REQUEST_DELAY = 0.25
FIDELITY = 1  # 1 = max granularity (~1 data point/minute)

TIME_BUCKETS = [270, 240, 210, 180, 150, 120, 90, 60, 30, 10]


def get_delta_bucket(delta_bps):
    if delta_bps < -15:
        return "<-15"
    elif delta_bps < -10:
        return "[-15,-10)"
    elif delta_bps < -7:
        return "[-10,-7)"
    elif delta_bps < -5:
        return "[-7,-5)"
    elif delta_bps < -3:
        return "[-5,-3)"
    elif delta_bps < -1:
        return "[-3,-1)"
    elif delta_bps < 1:
        return "[-1,1)"
    elif delta_bps < 3:
        return "[1,3)"
    elif delta_bps < 5:
        return "[3,5)"
    elif delta_bps < 7:
        return "[5,7)"
    elif delta_bps < 10:
        return "[7,10)"
    elif delta_bps < 15:
        return "[10,15)"
    else:
        return ">15"


def snap_time_bucket(time_remaining_s):
    """Snap to nearest time bucket within 15s tolerance."""
    best = None
    best_dist = float("inf")
    for tb in TIME_BUCKETS:
        dist = abs(time_remaining_s - tb)
        if dist < best_dist:
            best_dist = dist
            best = tb
    return best if best_dist <= 15 else None


async def fetch_all_histories(markets_rows, btc_timestamps, btc_prices, open_prices, delta_lookup):
    observations = []
    semaphore = asyncio.Semaphore(CONCURRENCY)
    fetched = 0
    empty = 0
    errors = 0
    total = len(markets_rows)

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": "btc-delta-research/1.0"},
        limits=httpx.Limits(max_connections=CONCURRENCY + 2),
    ) as client:

        async def fetch_one(row):
            nonlocal fetched, empty, errors
            ws = row["window_start"]
            token_id = row["yes_token_id"]
            outcome = row["outcome"]
            window_open = open_prices[ws]
            window_end = ws + WINDOW_SECONDS

            async with semaphore:
                for attempt in range(3):
                    try:
                        resp = await client.get(
                            CLOB_URL,
                            params={
                                "market": token_id,
                                "startTs": ws,
                                "endTs": window_end,
                                "fidelity": FIDELITY,
                            },
                        )

                        if resp.status_code == 429:
                            await asyncio.sleep(2 ** attempt + 1)
                            continue

                        resp.raise_for_status()
                        data = resp.json()
                        history = data.get("history", [])

                        if not history:
                            empty += 1
                            return []

                        fetched += 1
                        obs = []
                        for point in history:
                            t = point["t"]
                            p = point["p"]

                            time_remaining = window_end - t
                            if time_remaining <= 0 or time_remaining > 300:
                                continue

                            # Snap to time bucket
                            time_bucket = snap_time_bucket(time_remaining)
                            if time_bucket is None:
                                continue

                            # Find closest BTC price via binary search
                            t_ms = t * 1000
                            idx = np.searchsorted(btc_timestamps, t_ms, side="left")
                            if idx >= len(btc_timestamps):
                                idx = len(btc_timestamps) - 1
                            if (
                                idx > 0
                                and abs(btc_timestamps[idx - 1] - t_ms)
                                < abs(btc_timestamps[idx] - t_ms)
                            ):
                                idx = idx - 1

                            # Skip if Binance timestamp is too far (>5s)
                            if abs(btc_timestamps[idx] - t_ms) > 5000:
                                continue

                            btc_price = float(btc_prices[idx])
                            delta_bps = (btc_price - window_open) / window_open * 10000
                            delta_bucket = get_delta_bucket(delta_bps)

                            fair_prob = delta_lookup.get((delta_bucket, time_bucket))
                            if fair_prob is None:
                                continue

                            gap = fair_prob - p

                            obs.append(
                                {
                                    "timestamp": t,
                                    "window_start": ws,
                                    "btc_price": btc_price,
                                    "window_open_price": float(window_open),
                                    "delta_bps": float(delta_bps),
                                    "delta_bucket": delta_bucket,
                                    "time_remaining": int(time_remaining),
                                    "time_bucket": int(time_bucket),
                                    "polymarket_yes_price": float(p),
                                    "delta_table_fair_prob": float(fair_prob),
                                    "gap": float(gap),
                                    "outcome": outcome,
                                }
                            )

                        return obs

                    except httpx.TimeoutException:
                        if attempt < 2:
                            await asyncio.sleep(1)
                            continue
                        errors += 1
                        return []
                    except Exception:
                        errors += 1
                        return []
                    finally:
                        await asyncio.sleep(REQUEST_DELAY)

                errors += 1
                return []

        # Process in batches
        batch_size = 50
        for i in range(0, total, batch_size):
            batch = markets_rows[i : i + batch_size]
            tasks = [fetch_one(r) for r in batch]
            batch_results = await asyncio.gather(*tasks)
            for obs_list in batch_results:
                observations.extend(obs_list)

            done = min(i + batch_size, total)
            print(
                "  [{}/{}] fetched={} empty={} errors={} obs={}".format(
                    done, total, fetched, empty, errors, len(observations)
                ),
                end="\r",
                flush=True,
            )

    print(
        "\n  Done: {} with data, {} empty, {} errors".format(fetched, empty, errors)
    )
    return observations


def main():
    print("Loading data...")
    markets = pl.read_csv(
        MARKETS_PATH,
        schema_overrides={"yes_token_id": pl.Utf8, "no_token_id": pl.Utf8},
    )
    print("  Markets: {}".format(markets.height))

    binance = pl.read_parquet(BINANCE_PATH).sort("timestamp")
    print("  Binance candles: {:,}".format(binance.height))

    # Build numpy arrays for fast binary search
    btc_timestamps = binance["timestamp"].to_numpy()
    btc_prices = binance["close"].to_numpy()

    # Load delta table into lookup dict
    dt = pl.read_csv(DELTA_TABLE_PATH)
    delta_lookup = {}
    for row in dt.iter_rows(named=True):
        delta_lookup[(row["delta_bucket"], row["time_bucket"])] = row["win_rate"]
    print("  Delta table: {} cells".format(len(delta_lookup)))

    # Compute window open prices from Binance data
    # open_price = close of the first 1s candle at or after window_start
    print("\nComputing window open prices from Binance data...")
    open_prices = {}
    for row in markets.iter_rows(named=True):
        ws_ms = row["window_start"] * 1000
        idx = np.searchsorted(btc_timestamps, ws_ms, side="left")
        if idx < len(btc_timestamps) and abs(btc_timestamps[idx] - ws_ms) < 2000:
            open_prices[row["window_start"]] = float(btc_prices[idx])

    print(
        "  Found open prices for {}/{} windows".format(
            len(open_prices), markets.height
        )
    )

    # Filter to markets where we have both Binance data and a token ID
    valid_rows = []
    for row in markets.iter_rows(named=True):
        ws = row["window_start"]
        if ws in open_prices and row["yes_token_id"] is not None:
            valid_rows.append(row)

    print(
        "\nFetching price history for {} valid markets...".format(len(valid_rows))
    )
    all_observations = asyncio.run(
        fetch_all_histories(
            valid_rows, btc_timestamps, btc_prices, open_prices, delta_lookup
        )
    )

    if not all_observations:
        print("No price history data retrieved!")
        print("This could mean:")
        print("  - The CLOB prices-history API returned empty for these markets")
        print("  - Markets had no trades during the 5-minute windows")
        print("  - Rate limiting or network issues")
        return

    df_out = pl.DataFrame(all_observations)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df_out.write_parquet(OUTPUT_PATH)

    print("\nSaved {:,} observations to {}".format(df_out.height, OUTPUT_PATH))
    print("  Unique windows: {}".format(df_out["window_start"].n_unique()))
    print("  Mean gap: {:.4f}".format(df_out["gap"].mean()))
    print("  Mean |gap|: {:.4f}".format(df_out["gap"].abs().mean()))


if __name__ == "__main__":
    main()
