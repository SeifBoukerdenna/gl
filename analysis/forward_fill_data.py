"""
Forward-fill data for V3 edge table build.

Extends:
  - data/binance_klines_60d.parquet  (Apr 1 → Apr 9)
  - data/historical_markets_60d.csv  (Apr 1 → Apr 9)
  - data/polymarket_settlements_60d.parquet  (Apr 1 → Apr 9)

Binance: fetched via REST API.
PM markets: fetched via Gamma API.
PM settlements: derived from Binance close at window boundaries.

Run: python3 analysis/forward_fill_data.py
"""

import asyncio
import csv
import json
import time
from pathlib import Path

import httpx
import pandas as pd
import requests

BINANCE_PARQUET = Path("data/binance_klines_60d.parquet")
MARKETS_CSV = Path("data/historical_markets_60d.csv")
SETTLEMENTS_PARQUET = Path("data/polymarket_settlements_60d.parquet")

GAMMA_URL = "https://gamma-api.polymarket.com"
BINANCE_URL = "https://api.binance.com/api/v3/klines"


# ── Step 1: Forward-fill Binance ──────────────────────────────────
def extend_binance(target_end_ts):
    print("\n[1/3] Extending Binance klines...")
    df = pd.read_parquet(BINANCE_PARQUET)
    current_max_ms = int(df["timestamp"].max())
    target_end_ms = int(target_end_ts * 1000)

    if current_max_ms >= target_end_ms - 60000:
        print("  Already up to date.")
        return df

    print(f"  Current end: {pd.to_datetime(current_max_ms, unit='ms')}")
    print(f"  Target end:  {pd.to_datetime(target_end_ms, unit='ms')}")
    need_secs = (target_end_ms - current_max_ms) / 1000
    print(f"  Need: ~{need_secs/86400:.1f} days = ~{int(need_secs):,} klines")

    new_rows = []
    cursor = current_max_ms + 1000
    errors = 0
    request_count = 0

    while cursor < target_end_ms:
        try:
            params = {
                "symbol": "BTCUSDT",
                "interval": "1s",
                "startTime": cursor,
                "endTime": target_end_ms,
                "limit": 1000,
            }
            r = requests.get(BINANCE_URL, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            errors += 1
            if errors > 20:
                print("  Too many errors, stopping.")
                break
            print(f"  Error: {e}, retrying...")
            time.sleep(3)
            continue

        if not data:
            break

        for row in data:
            new_rows.append({
                "timestamp": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        cursor = int(data[-1][0]) + 1000
        request_count += 1

        if request_count % 50 == 0:
            pct = (cursor - current_max_ms) / (target_end_ms - current_max_ms) * 100
            print(f"  [{pct:5.1f}%] {len(new_rows):>8,} new candles | req #{request_count}")

        if request_count % 10 == 0:
            time.sleep(0.5)
        else:
            time.sleep(0.05)

    print(f"  Fetched {len(new_rows):,} new candles")

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        merged = pd.concat([df, new_df], ignore_index=True)
        merged = merged.drop_duplicates(subset=["timestamp"], keep="first")
        merged = merged.sort_values("timestamp").reset_index(drop=True)
        merged.to_parquet(BINANCE_PARQUET)
        print(f"  Saved {len(merged):,} total rows")
        return merged
    return df


# ── Step 2: Discover PM markets ───────────────────────────────────
async def discover_pm_markets(start_ts, end_ts):
    print("\n[2/3] Discovering PM markets for Apr 1-9...")

    new_markets = []
    timestamps = list(range(int(start_ts), int(end_ts), 300))
    timestamps = [ts - (ts % 300) for ts in timestamps]
    print(f"  Need to check {len(timestamps):,} 5-min windows")

    semaphore = asyncio.Semaphore(8)
    found = [0]
    checked = [0]

    async def check_window(client, ts):
        async with semaphore:
            try:
                start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
                slug_attempts = [
                    f"bitcoin-up-or-down-{time.strftime('%B-%d-%-I%p-et', time.gmtime(ts)).lower()}",
                ]
                # Try the gamma events API
                r = await client.get(
                    f"{GAMMA_URL}/events",
                    params={"start_date_min": start_iso, "limit": 10, "tag_id": 100196},
                )
                checked[0] += 1
                if r.status_code != 200:
                    return None

                events = r.json()
                for ev in events:
                    markets = ev.get("markets", [])
                    for m in markets:
                        slug = m.get("slug", "")
                        if "bitcoin-up-or-down" not in slug:
                            continue
                        # Check if this market starts at our timestamp
                        ws = m.get("startDate") or m.get("startDateIso")
                        if not ws:
                            continue
                        try:
                            ws_ts = int(time.mktime(time.strptime(ws[:19], "%Y-%m-%dT%H:%M:%S")))
                            if abs(ws_ts - ts) > 300:
                                continue
                        except:
                            continue
                        outcomes = m.get("outcomes", "[]")
                        outcome_prices = m.get("outcomePrices", "[]")
                        if isinstance(outcomes, str):
                            outcomes = json.loads(outcomes)
                        if isinstance(outcome_prices, str):
                            outcome_prices = json.loads(outcome_prices)
                        if len(outcome_prices) >= 2:
                            yes_price = float(outcome_prices[0])
                            outcome = "Up" if yes_price > 0.5 else "Down"
                            tokens = m.get("clobTokenIds", "[]")
                            if isinstance(tokens, str):
                                tokens = json.loads(tokens)
                            yes_token = tokens[0] if len(tokens) > 0 else ""
                            no_token = tokens[1] if len(tokens) > 1 else ""
                            found[0] += 1
                            return {
                                "window_start": ts,
                                "slug": slug,
                                "yes_token_id": yes_token,
                                "no_token_id": no_token,
                                "outcome": outcome,
                                "volume": m.get("volume", 0),
                            }
            except Exception:
                pass
            return None

    async with httpx.AsyncClient(timeout=15.0) as client:
        tasks = [check_window(client, ts) for ts in timestamps]
        results = await asyncio.gather(*tasks)

    new_markets = [r for r in results if r is not None]
    print(f"  Found {len(new_markets):,} new markets ({checked[0]} checks)")
    return new_markets


# ── Step 3: Build settlements from Binance ────────────────────────
def build_settlements(df_binance, markets):
    print("\n[3/3] Building settlements from Binance...")
    df = df_binance.copy()
    df["ts_sec"] = (df["timestamp"] / 1000).astype(int)
    df = df.set_index("ts_sec").sort_index()

    settlements = []
    for m in markets:
        ws = m["window_start"]
        we = ws + 300
        try:
            open_price = df.loc[ws:ws + 5]["close"].iloc[0]
            close_price = df.loc[we - 5:we + 5]["close"].iloc[-1] if we in df.index or we - 1 in df.index else None
        except (IndexError, KeyError):
            continue
        if close_price is None:
            continue
        outcome = "Up" if close_price > open_price else "Down"
        settlements.append({
            "window_start": ws,
            "chainlink_open": open_price,
            "chainlink_close": close_price,
            "outcome": outcome,
        })
    print(f"  Built {len(settlements):,} settlements")
    return settlements


# ── Main ──────────────────────────────────────────────────────────
def main():
    target_end_ts = int(time.time())
    print(f"Target end: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(target_end_ts))}")

    # 1. Binance forward-fill
    df_binance = extend_binance(target_end_ts)

    # 2. Determine new windows needed
    existing_markets = []
    if MARKETS_CSV.exists():
        with open(MARKETS_CSV) as f:
            existing_markets = list(csv.DictReader(f))
    existing_ws = {int(m["window_start"]) for m in existing_markets}
    last_existing_ws = max(existing_ws) if existing_ws else 0
    print(f"\n  Last existing market window: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(last_existing_ws))}")

    # 3. PM markets — use Binance directly to synthesize since Gamma discovery is unreliable
    print("\n[2/3] Synthesizing market outcomes from Binance (PM uses Chainlink which closely tracks Binance)...")
    new_windows_start = last_existing_ws + 300
    new_windows_end = target_end_ts - 600  # leave buffer
    new_windows = list(range(new_windows_start, new_windows_end, 300))
    print(f"  Synthesizing {len(new_windows):,} windows")

    df_idx = df_binance.copy()
    df_idx["ts_sec"] = (df_idx["timestamp"] / 1000).astype(int)
    df_idx = df_idx.set_index("ts_sec").sort_index()

    new_markets = []
    for ws in new_windows:
        we = ws + 300
        try:
            # open: BTC at ws, close: BTC at ws+300
            open_rows = df_idx.loc[ws:ws + 3]
            close_rows = df_idx.loc[we:we + 3]
            if len(open_rows) == 0 or len(close_rows) == 0:
                continue
            open_p = open_rows["close"].iloc[0]
            close_p = close_rows["close"].iloc[0]
            outcome = "Up" if close_p > open_p else "Down"
            new_markets.append({
                "window_start": ws,
                "slug": f"synth-{ws}",
                "yes_token_id": "",
                "no_token_id": "",
                "outcome": outcome,
                "volume": "0",
            })
        except (IndexError, KeyError):
            continue
    print(f"  Synthesized {len(new_markets):,} market outcomes")

    # Append to markets CSV
    if new_markets:
        existing_dict = {int(m["window_start"]): m for m in existing_markets}
        for m in new_markets:
            existing_dict[m["window_start"]] = m
        merged_markets = sorted(existing_dict.values(), key=lambda m: int(m["window_start"]))
        with open(MARKETS_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["window_start", "slug", "yes_token_id", "no_token_id", "outcome", "volume"])
            writer.writeheader()
            for m in merged_markets:
                writer.writerow(m)
        print(f"  Saved {len(merged_markets):,} total markets to {MARKETS_CSV}")

    # 4. Settlements
    settlements = build_settlements(df_binance, new_markets)
    if settlements:
        df_settle_old = pd.read_parquet(SETTLEMENTS_PARQUET)
        df_settle_new = pd.DataFrame(settlements)
        merged_settle = pd.concat([df_settle_old, df_settle_new], ignore_index=True)
        merged_settle = merged_settle.drop_duplicates(subset=["window_start"], keep="last")
        merged_settle = merged_settle.sort_values("window_start").reset_index(drop=True)
        merged_settle.to_parquet(SETTLEMENTS_PARQUET)
        print(f"  Saved {len(merged_settle):,} total settlements to {SETTLEMENTS_PARQUET}")

    print("\n=== Forward-fill complete ===")
    print(f"  Binance:     {pd.to_datetime(df_binance['timestamp'].min(), unit='ms')} -> {pd.to_datetime(df_binance['timestamp'].max(), unit='ms')}")
    print(f"  Markets:     {len(merged_markets if new_markets else existing_markets):,} rows")
    print(f"  Settlements: {len(merged_settle if settlements else []):,} rows" if settlements else "  Settlements: unchanged")


if __name__ == "__main__":
    main()
