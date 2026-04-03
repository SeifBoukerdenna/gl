"""
Fetch Polymarket's Chainlink settlement prices (priceToBeat, finalPrice)
for all 5-minute BTC markets in our date range.

Uses: data/historical_markets.csv (for the list of slugs)
Saves: data/polymarket_settlements.parquet
"""

import asyncio
import json

import httpx
import polars as pl
from pathlib import Path

MARKETS_PATH = Path("data/historical_markets.csv")
OUTPUT_PATH = Path("data/polymarket_settlements.parquet")
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CONCURRENCY = 5
REQUEST_DELAY = 0.2


async def fetch_all_settlements(slugs_and_ws):
    results = []
    semaphore = asyncio.Semaphore(CONCURRENCY)
    fetched = 0
    missing = 0
    errors = 0
    total = len(slugs_and_ws)

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": "btc-delta-research/1.0"},
        limits=httpx.Limits(max_connections=CONCURRENCY + 2),
    ) as client:

        async def fetch_one(slug, ws, outcome):
            nonlocal fetched, missing, errors

            async with semaphore:
                for attempt in range(3):
                    try:
                        resp = await client.get(
                            GAMMA_EVENTS_URL,
                            params={"slug": slug, "limit": 1},
                        )

                        if resp.status_code == 429:
                            await asyncio.sleep(2 ** attempt + 1)
                            continue

                        resp.raise_for_status()
                        events = resp.json()

                        if not events:
                            missing += 1
                            return None

                        event = events[0]
                        meta_raw = event.get("eventMetadata", "{}")
                        if isinstance(meta_raw, str):
                            meta = json.loads(meta_raw) if meta_raw else {}
                        else:
                            meta = meta_raw or {}

                        ptb = meta.get("priceToBeat")
                        fp = meta.get("finalPrice")

                        if ptb is None:
                            missing += 1
                            return None

                        fetched += 1
                        return {
                            "window_start": ws,
                            "chainlink_open": float(ptb),
                            "chainlink_close": float(fp) if fp is not None else None,
                            "outcome": outcome,
                        }

                    except httpx.TimeoutException:
                        if attempt < 2:
                            await asyncio.sleep(1)
                            continue
                        errors += 1
                        return None
                    except Exception:
                        errors += 1
                        return None
                    finally:
                        await asyncio.sleep(REQUEST_DELAY)

                errors += 1
                return None

        batch_size = 50
        for i in range(0, total, batch_size):
            batch = slugs_and_ws[i : i + batch_size]
            tasks = [fetch_one(s, ws, o) for s, ws, o in batch]
            batch_results = await asyncio.gather(*tasks)
            for r in batch_results:
                if r is not None:
                    results.append(r)

            done = min(i + batch_size, total)
            print(
                "  [{}/{}] fetched={} missing={} errors={}".format(
                    done, total, fetched, missing, errors
                ),
                end="\r",
                flush=True,
            )

    print(
        "\n  Done: {} fetched, {} missing metadata, {} errors".format(
            fetched, missing, errors
        )
    )
    return results


def main():
    markets = pl.read_csv(
        MARKETS_PATH,
        schema_overrides={"yes_token_id": pl.Utf8, "no_token_id": pl.Utf8},
    )
    print(
        "Fetching Chainlink settlement prices for {} markets...".format(
            markets.height
        )
    )

    slugs_and_ws = [
        (row["slug"], row["window_start"], row["outcome"])
        for row in markets.iter_rows(named=True)
    ]

    results = asyncio.run(fetch_all_settlements(slugs_and_ws))

    if not results:
        print("No settlement data retrieved!")
        return

    df = pl.DataFrame(
        results,
        schema={
            "window_start": pl.Int64,
            "chainlink_open": pl.Float64,
            "chainlink_close": pl.Float64,
            "outcome": pl.Utf8,
        },
    )
    df = df.sort("window_start")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(OUTPUT_PATH)

    has_close = df.filter(pl.col("chainlink_close").is_not_null())
    print("\nSaved {} settlements to {}".format(df.height, OUTPUT_PATH))
    print("  With both open+close: {}".format(has_close.height))
    print("  Open only (no close): {}".format(df.height - has_close.height))

    if has_close.height > 0:
        # Quick stats
        mean_open = has_close["chainlink_open"].mean()
        mean_close = has_close["chainlink_close"].mean()
        print("  Mean Chainlink open:  ${:,.2f}".format(mean_open))
        print("  Mean Chainlink close: ${:,.2f}".format(mean_close))


if __name__ == "__main__":
    main()
