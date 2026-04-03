"""
Discover resolved 5-minute BTC prediction markets on Polymarket.
Generates slugs for all 300s-aligned windows in the Binance data range,
queries Gamma API for each, and collects market metadata + token IDs.

Saves to: data/historical_markets.csv
"""

import asyncio
import json
import sys
import time

import httpx
import polars as pl
from pathlib import Path

BINANCE_PATH = Path("data/binance_klines.parquet")
OUTPUT_PATH = Path("data/historical_markets.csv")
GAMMA_URL = "https://gamma-api.polymarket.com/markets"

CONCURRENCY = 5
REQUEST_DELAY = 0.2  # seconds between requests per worker


async def fetch_all_markets(window_starts):
    results = []
    semaphore = asyncio.Semaphore(CONCURRENCY)
    found = 0
    checked = 0
    errors = 0
    total = len(window_starts)

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": "btc-delta-research/1.0"},
        limits=httpx.Limits(max_connections=CONCURRENCY + 2),
    ) as client:

        async def fetch_one(ts):
            nonlocal found, checked, errors
            slug = "btc-updown-5m-{}".format(ts)

            async with semaphore:
                for attempt in range(3):
                    try:
                        resp = await client.get(
                            GAMMA_URL, params={"slug": slug, "limit": 1}
                        )

                        if resp.status_code == 429:
                            await asyncio.sleep(2 ** attempt + 1)
                            continue

                        resp.raise_for_status()
                        data = resp.json()
                        checked += 1

                        if data and len(data) > 0:
                            market = data[0]

                            # Verify the slug matches (API might return partial matches)
                            if market.get("slug") != slug:
                                return None

                            token_ids = json.loads(
                                market.get("clobTokenIds", "[]")
                            )
                            outcomes = json.loads(
                                market.get("outcomes", "[]")
                            )
                            outcome_prices = json.loads(
                                market.get("outcomePrices", "[]")
                            )

                            yes_token = token_ids[0] if len(token_ids) > 0 else None
                            no_token = token_ids[1] if len(token_ids) > 1 else None

                            # Determine resolved outcome: the outcome whose price is "1"
                            outcome = None
                            if outcome_prices:
                                for o, p in zip(outcomes, outcome_prices):
                                    if p == "1":
                                        outcome = o

                            # Skip unresolved markets
                            if outcome is None:
                                return None

                            found += 1
                            return {
                                "window_start": ts,
                                "slug": slug,
                                "yes_token_id": yes_token,
                                "no_token_id": no_token,
                                "outcome": outcome,
                                "volume": float(market.get("volume", 0) or 0),
                            }
                        return None

                    except httpx.TimeoutException:
                        if attempt < 2:
                            await asyncio.sleep(1)
                            continue
                        checked += 1
                        errors += 1
                        return None
                    except Exception:
                        checked += 1
                        errors += 1
                        return None
                    finally:
                        await asyncio.sleep(REQUEST_DELAY)

                checked += 1
                return None

        # Process in batches for progress reporting
        batch_size = 50
        for i in range(0, total, batch_size):
            batch = window_starts[i : i + batch_size]
            tasks = [fetch_one(ts) for ts in batch]
            batch_results = await asyncio.gather(*tasks)
            for r in batch_results:
                if r is not None:
                    results.append(r)

            done = min(i + batch_size, total)
            print(
                "  [{}/{}] found={} errors={}".format(done, total, found, errors),
                end="\r",
                flush=True,
            )

    print(
        "\n  Done: {} checked, {} found, {} errors".format(checked, found, errors)
    )
    return results


def main():
    print("Loading Binance data for time range...")
    df = pl.read_parquet(BINANCE_PATH)
    min_ts_ms = df["timestamp"].min()
    max_ts_ms = df["timestamp"].max()

    # Generate 300s-aligned window starts (epoch seconds)
    start_s = min_ts_ms // 1000
    end_s = max_ts_ms // 1000
    start_s = start_s - (start_s % 300)

    window_starts = list(range(start_s, end_s, 300))
    print(
        "Time range: {} — {} UTC".format(
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(start_s)),
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(end_s)),
        )
    )
    print("Generated {} window timestamps to check".format(len(window_starts)))

    results = asyncio.run(fetch_all_markets(window_starts))

    if not results:
        print("No markets found! The API may not have data for this date range.")
        sys.exit(1)

    df_out = pl.DataFrame(results)
    df_out = df_out.sort("window_start")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df_out.write_csv(OUTPUT_PATH)

    print("\nSaved {} markets to {}".format(len(results), OUTPUT_PATH))
    first_ts = df_out["window_start"].min()
    last_ts = df_out["window_start"].max()
    print(
        "  Date range: {} — {}".format(
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(first_ts)),
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(last_ts)),
        )
    )

    outcomes = df_out.group_by("outcome").len()
    print("  Outcomes:")
    for row in outcomes.iter_rows(named=True):
        print("    {}: {}".format(row["outcome"], row["len"]))


if __name__ == "__main__":
    main()
