"""
Discover and fetch PM price history for 5-min BTC markets going back 60 days.
Extends existing 14-day data.

Saves:
  data/historical_markets_60d.csv
  data/price_history_60d.parquet
  data/polymarket_settlements_60d.parquet
"""

import asyncio
import json
import time

import httpx
import numpy as np
import polars as pl
from pathlib import Path

EXISTING_MARKETS = Path("data/historical_markets.csv")
EXISTING_PH = Path("data/price_history_with_gaps.parquet")
EXISTING_SETT = Path("data/polymarket_settlements.parquet")
BINANCE_60D = Path("data/binance_klines_60d.parquet")

OUT_MARKETS = Path("data/historical_markets_60d.csv")
OUT_PH = Path("data/price_history_60d.parquet")
OUT_SETT = Path("data/polymarket_settlements_60d.parquet")

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com/prices-history"

CONCURRENCY = 5
DELAY = 0.3

TARGET_DAYS = 60
WINDOW_SECONDS = 300

TIME_BUCKETS = [270, 240, 210, 180, 150, 120, 90, 60, 30, 10]


def get_delta_bucket(delta_bps):
    if delta_bps < -15: return "<-15"
    elif delta_bps < -10: return "[-15,-10)"
    elif delta_bps < -7: return "[-10,-7)"
    elif delta_bps < -5: return "[-7,-5)"
    elif delta_bps < -3: return "[-5,-3)"
    elif delta_bps < -1: return "[-3,-1)"
    elif delta_bps < 1: return "[-1,1)"
    elif delta_bps < 3: return "[1,3)"
    elif delta_bps < 5: return "[3,5)"
    elif delta_bps < 7: return "[5,7)"
    elif delta_bps < 10: return "[7,10)"
    elif delta_bps < 15: return "[10,15)"
    else: return ">15"


def snap_time_bucket(secs):
    return min(TIME_BUCKETS, key=lambda b: abs(b - secs))


# ── Step 1: Discover markets ─────────────────────────────────────
async def discover_new_markets(existing_windows):
    now = int(time.time())
    start = now - (TARGET_DAYS * 86400)
    start = start - (start % 300)

    new_timestamps = [
        ts for ts in range(start, now, 300) if ts not in existing_windows
    ]
    print("New windows to check: {:,}".format(len(new_timestamps)))

    results = []
    semaphore = asyncio.Semaphore(CONCURRENCY)
    found = 0
    checked = 0
    errors = 0
    consecutive_empty = 0

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": "btc-backtest/1.0"},
        limits=httpx.Limits(max_connections=CONCURRENCY + 2),
    ) as client:

        async def fetch_one(ts):
            nonlocal found, checked, errors, consecutive_empty
            slug = "btc-updown-5m-{}".format(ts)

            async with semaphore:
                for attempt in range(3):
                    try:
                        # Fetch event (for settlement data)
                        resp = await client.get(
                            "{}/events".format(GAMMA_URL),
                            params={"slug": slug, "limit": 1},
                        )
                        if resp.status_code == 429:
                            await asyncio.sleep(2 ** attempt + 1)
                            continue

                        resp.raise_for_status()
                        events = resp.json()
                        checked += 1

                        if not events:
                            consecutive_empty += 1
                            return None

                        event = events[0]
                        if event.get("slug") != slug:
                            consecutive_empty += 1
                            return None

                        consecutive_empty = 0

                        # Get token IDs from markets
                        markets = event.get("markets", [])
                        if not markets:
                            return None

                        m = markets[0]
                        tokens_raw = m.get("clobTokenIds", "[]")
                        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else (tokens_raw or [])
                        outcomes = json.loads(m.get("outcomes", "[]"))
                        outcome_prices = json.loads(m.get("outcomePrices", "[]"))

                        yes_token = tokens[0] if len(tokens) > 0 else None
                        no_token = tokens[1] if len(tokens) > 1 else None

                        outcome = None
                        if outcome_prices:
                            for o, p in zip(outcomes, outcome_prices):
                                if str(p) == "1":
                                    outcome = o

                        if outcome is None:
                            return None

                        # Get settlement data
                        meta_raw = event.get("eventMetadata")
                        if isinstance(meta_raw, dict):
                            meta = meta_raw
                        elif isinstance(meta_raw, str) and meta_raw:
                            meta = json.loads(meta_raw)
                        else:
                            meta = {}

                        ptb = float(meta["priceToBeat"]) if meta.get("priceToBeat") else None
                        fp = float(meta["finalPrice"]) if meta.get("finalPrice") else None

                        found += 1
                        return {
                            "window_start": ts,
                            "slug": slug,
                            "yes_token_id": yes_token,
                            "no_token_id": no_token,
                            "outcome": outcome,
                            "volume": float(m.get("volume", 0) or 0),
                            "chainlink_open": ptb,
                            "chainlink_close": fp,
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
                        await asyncio.sleep(DELAY)

                return None

        # Process newest first (most likely to exist)
        new_timestamps.sort(reverse=True)
        batch_size = 50

        for i in range(0, len(new_timestamps), batch_size):
            batch = new_timestamps[i: i + batch_size]
            tasks = [fetch_one(ts) for ts in batch]
            batch_results = await asyncio.gather(*tasks)
            for r in batch_results:
                if r is not None:
                    results.append(r)

            done = min(i + batch_size, len(new_timestamps))
            print(
                "  [{}/{}] found={} errors={} consec_empty={}".format(
                    done, len(new_timestamps), found, errors, consecutive_empty
                ),
                end="\r", flush=True,
            )

            # If we've hit 200+ consecutive empties, markets don't exist further back
            if consecutive_empty > 200:
                print("\n  Hit {} consecutive empties — reached market launch boundary.".format(
                    consecutive_empty
                ))
                break

    print("\n  Done: {} checked, {} found, {} errors".format(checked, found, errors))
    return results


# ── Step 2: Fetch price history for new markets ──────────────────
async def fetch_price_histories(markets_list, btc_ts, btc_px):
    observations = []
    semaphore = asyncio.Semaphore(CONCURRENCY)
    fetched = 0
    empty = 0
    errors = 0
    total = len(markets_list)

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": "btc-backtest/1.0"},
        limits=httpx.Limits(max_connections=CONCURRENCY + 2),
    ) as client:

        async def fetch_one(mkt):
            nonlocal fetched, empty, errors
            ws = mkt["window_start"]
            token_id = mkt["yes_token_id"]
            outcome = mkt["outcome"]
            window_end = ws + WINDOW_SECONDS

            if not token_id:
                return []

            # Find window open price from Binance
            ws_ms = ws * 1000
            idx = np.searchsorted(btc_ts, ws_ms, side="left")
            if idx >= len(btc_ts) or abs(btc_ts[idx] - ws_ms) > 5000:
                return []
            window_open = float(btc_px[idx])

            async with semaphore:
                for attempt in range(3):
                    try:
                        resp = await client.get(
                            CLOB_URL,
                            params={
                                "market": token_id,
                                "startTs": ws,
                                "endTs": window_end,
                                "fidelity": 1,
                            },
                        )
                        if resp.status_code == 429:
                            await asyncio.sleep(2 ** attempt + 1)
                            continue

                        resp.raise_for_status()
                        history = resp.json().get("history", [])

                        if not history:
                            empty += 1
                            return []

                        fetched += 1
                        obs = []
                        for point in history:
                            t = point["t"]
                            p = point["p"]
                            tr = window_end - t
                            if tr <= 0 or tr > 300:
                                continue

                            tb = snap_time_bucket(tr)

                            t_ms = t * 1000
                            bidx = np.searchsorted(btc_ts, t_ms, side="left")
                            if bidx >= len(btc_ts):
                                bidx = len(btc_ts) - 1
                            if bidx > 0 and abs(btc_ts[bidx - 1] - t_ms) < abs(btc_ts[bidx] - t_ms):
                                bidx -= 1
                            if abs(btc_ts[bidx] - t_ms) > 5000:
                                continue

                            btc_price = float(btc_px[bidx])
                            delta_bps = (btc_price - window_open) / window_open * 10000

                            obs.append({
                                "timestamp": t,
                                "window_start": ws,
                                "btc_price": btc_price,
                                "window_open_price": window_open,
                                "delta_bps": round(delta_bps, 4),
                                "delta_bucket": get_delta_bucket(delta_bps),
                                "time_remaining": int(tr),
                                "time_bucket": int(tb),
                                "polymarket_yes_price": float(p),
                                "outcome": outcome,
                            })
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
                        await asyncio.sleep(DELAY)
                return []

        batch_size = 50
        for i in range(0, total, batch_size):
            batch = markets_list[i: i + batch_size]
            tasks = [fetch_one(m) for m in batch]
            batch_results = await asyncio.gather(*tasks)
            for obs_list in batch_results:
                observations.extend(obs_list)

            done = min(i + batch_size, total)
            if done % 200 < batch_size:
                print(
                    "  [{}/{}] fetched={} empty={} errors={} obs={:,}".format(
                        done, total, fetched, empty, errors, len(observations)
                    ),
                    end="\r", flush=True,
                )

    print("\n  Done: {} with data, {} empty, {} errors, {:,} observations".format(
        fetched, empty, errors, len(observations)
    ))
    return observations


def main():
    # Load existing
    existing_mkts = pl.read_csv(
        EXISTING_MARKETS,
        schema_overrides={"yes_token_id": pl.Utf8, "no_token_id": pl.Utf8},
    )
    existing_windows = set(existing_mkts["window_start"].to_list())
    print("Existing markets: {:,} ({} - {})".format(
        existing_mkts.height,
        time.strftime("%Y-%m-%d", time.gmtime(existing_mkts["window_start"].min())),
        time.strftime("%Y-%m-%d", time.gmtime(existing_mkts["window_start"].max())),
    ))

    # Step 1: Discover new markets
    print("\n--- Step 1: Discovering new markets ---")
    new_markets_raw = asyncio.run(discover_new_markets(existing_windows))

    if not new_markets_raw:
        print("No new markets found!")
        return

    # Separate settlement data
    new_settlements = []
    new_markets = []
    for m in new_markets_raw:
        new_markets.append({
            "window_start": m["window_start"],
            "slug": m["slug"],
            "yes_token_id": m["yes_token_id"],
            "no_token_id": m["no_token_id"],
            "outcome": m["outcome"],
            "volume": m["volume"],
        })
        if m["chainlink_open"] is not None:
            new_settlements.append({
                "window_start": m["window_start"],
                "chainlink_open": m["chainlink_open"],
                "chainlink_close": m["chainlink_close"],
                "outcome": m["outcome"],
            })

    new_mkts_df = pl.DataFrame(new_markets)
    print("\nNew markets found: {:,}".format(new_mkts_df.height))

    # Merge markets
    merged_mkts = pl.concat([
        existing_mkts,
        new_mkts_df.select(existing_mkts.columns),
    ]).sort("window_start").unique(subset=["window_start"], keep="first")

    merged_mkts.write_csv(OUT_MARKETS)
    print("Saved {:,} total markets to {}".format(merged_mkts.height, OUT_MARKETS))
    print("  Range: {} to {}".format(
        time.strftime("%Y-%m-%d", time.gmtime(merged_mkts["window_start"].min())),
        time.strftime("%Y-%m-%d", time.gmtime(merged_mkts["window_start"].max())),
    ))

    # Merge settlements
    if new_settlements:
        new_sett_df = pl.DataFrame(
            new_settlements,
            schema={"window_start": pl.Int64, "chainlink_open": pl.Float64,
                    "chainlink_close": pl.Float64, "outcome": pl.Utf8},
        )
        existing_sett = pl.read_parquet(EXISTING_SETT)
        merged_sett = pl.concat([existing_sett, new_sett_df]).sort("window_start").unique(
            subset=["window_start"], keep="first"
        )
        merged_sett.write_parquet(OUT_SETT)
        print("Saved {:,} settlements to {}".format(merged_sett.height, OUT_SETT))

    # Step 2: Fetch price history
    print("\n--- Step 2: Fetching price history for new markets ---")

    # Load Binance data for BTC price lookups
    if BINANCE_60D.exists():
        binance = pl.read_parquet(BINANCE_60D).sort("timestamp")
    else:
        binance = pl.read_parquet(Path("data/binance_klines.parquet")).sort("timestamp")
    btc_ts = binance["timestamp"].to_numpy()
    btc_px = binance["close"].to_numpy()
    print("Binance klines loaded: {:,}".format(binance.height))

    new_obs = asyncio.run(fetch_price_histories(new_markets, btc_ts, btc_px))

    # Merge price history
    existing_ph = pl.read_parquet(EXISTING_PH)
    if new_obs:
        # Match columns to existing
        new_ph_df = pl.DataFrame(new_obs)
        # Ensure matching columns
        common_cols = [c for c in existing_ph.columns if c in new_ph_df.columns]
        merged_ph = pl.concat([
            existing_ph.select(common_cols),
            new_ph_df.select(common_cols),
        ]).sort(["window_start", "timestamp"]).unique(
            subset=["window_start", "timestamp"], keep="first"
        )
    else:
        merged_ph = existing_ph

    merged_ph.write_parquet(OUT_PH)
    print("\nSaved {:,} total PM observations to {}".format(merged_ph.height, OUT_PH))
    print("  Windows: {:,}".format(merged_ph["window_start"].n_unique()))

    # Summary
    outcomes = merged_mkts.group_by("outcome").len()
    print("\nOutcomes:")
    for row in outcomes.iter_rows(named=True):
        print("  {}: {:,}".format(row["outcome"], row["len"]))


if __name__ == "__main__":
    main()
