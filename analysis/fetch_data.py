#!/usr/bin/env python3
"""
Data fetcher: fills the gap from March 31 to April 5, 2026.

Part 1a: Fetch Binance 1-second klines (BTCUSDT) for the gap period.
Part 1b: Fetch/synthesize Polymarket settlements for 5-min BTC windows.
Part 1c: Synthesize PM price snapshots for the gap period using Binance data + delta table.

Usage:  python3 analysis/fetch_data.py
Output: data/binance_klines_full.parquet
        data/settlements_full.parquet
        data/price_history_full.parquet
"""

import csv
import math
import os
import random
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

EXISTING_KLINES = DATA / "binance_klines_60d.parquet"
EXISTING_SETTLEMENTS = DATA / "polymarket_settlements_60d.parquet"
EXISTING_PRICE_HIST = DATA / "price_history_60d.parquet"
DELTA_TABLE_PATH = DATA / "delta_table_corrected.csv"

OUT_KLINES = DATA / "binance_klines_full.parquet"
OUT_SETTLEMENTS = DATA / "settlements_full.parquet"
OUT_PRICE_HIST = DATA / "price_history_full.parquet"

# Gap period
# Last timestamp in existing data: March 31 23:43:06 UTC = 1775014986000 ms
GAP_START_MS = 1775014986000 + 1000  # start 1 second after last existing
GAP_START_SEC = GAP_START_MS // 1000

# April 1 13:40:00 UTC 2026 (first PM window in gap)
# 2026-04-01 00:00:00 UTC = 1775059200
APRIL_1_MIDNIGHT = 1775059200
PM_GAP_START = 1775109600  # April 1 14:00:00 UTC (approximate, adjusted below)

# We'll compute the current time dynamically
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
PM_GAMMA_URL = "https://gamma-api.polymarket.com/events"


# ---------------------------------------------------------------------------
# Normal CDF (for fair price synthesis)
# ---------------------------------------------------------------------------
try:
    from scipy.stats import norm as _norm
    def normal_cdf(x):
        return float(_norm.cdf(x))
except ImportError:
    def normal_cdf(x):
        if x < -8:
            return 0.0
        if x > 8:
            return 1.0
        sign = 1 if x >= 0 else -1
        x = abs(x)
        t = 1.0 / (1.0 + 0.2316419 * x)
        d = 0.3989422804014327
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
        return 0.5 + sign * (0.5 - d * math.exp(-0.5 * x * x) * poly)


# ---------------------------------------------------------------------------
# Delta table
# ---------------------------------------------------------------------------
def load_delta_table():
    table = {}
    with open(DELTA_TABLE_PATH) as f:
        reader = csv.DictReader(f)
        for row in reader:
            db = row["delta_bucket"]
            tb = int(row["time_bucket"])
            wr = float(row["win_rate"])
            table[(db, tb)] = wr
    return table


def get_delta_bucket(delta_bps):
    if delta_bps < -15:
        return "<-15"
    if delta_bps >= 15:
        return ">15"
    edges = [(-15, -10), (-10, -7), (-7, -5), (-5, -3), (-3, -1), (-1, 1), (1, 3), (3, 5), (5, 7), (7, 10), (10, 15)]
    for lo, hi in edges:
        if lo <= delta_bps < hi:
            return "[{},{})".format(lo, hi)
    return ">15"


def snap_time_bucket(time_remaining):
    buckets = [10, 30, 60, 90, 120, 150, 180, 210, 240, 270]
    best = 10
    best_dist = abs(time_remaining - 10)
    for b in buckets:
        d = abs(time_remaining - b)
        if d < best_dist:
            best = b
            best_dist = d
    return best


# ===================================================================
# PART 1a: Fetch Binance 1-second klines
# ===================================================================
def fetch_binance_klines():
    """Fetch 1-second klines from Binance for the gap period."""
    print("=" * 70)
    print("PART 1a: Fetching Binance 1-second klines")
    print("=" * 70)

    # Load existing data
    if EXISTING_KLINES.exists():
        existing = pd.read_parquet(EXISTING_KLINES)
        print(f"  Existing klines: {len(existing):,} rows")
        print(f"  Last timestamp: {existing['timestamp'].max()} ms")
    else:
        existing = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        print("  No existing klines found.")

    start_ms = GAP_START_MS
    now_ms = int(time.time() * 1000)
    total_seconds = (now_ms - start_ms) // 1000
    print(f"  Gap period: {total_seconds:,} seconds ({total_seconds/3600:.1f} hours)")
    print(f"  Fetching in chunks of 1000...")
    print()

    all_new = []
    current_ms = start_ms
    chunk_count = 0
    max_retries = 3

    while current_ms < now_ms:
        end_ms = min(current_ms + 1000 * 1000, now_ms)  # 1000 seconds

        for attempt in range(max_retries):
            try:
                resp = requests.get(BINANCE_KLINE_URL, params={
                    "symbol": "BTCUSDT",
                    "interval": "1s",
                    "startTime": current_ms,
                    "endTime": end_ms,
                    "limit": 1000,
                }, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.RequestException, ValueError) as e:
                if attempt < max_retries - 1:
                    print(f"    Retry {attempt+1} for chunk at {current_ms}: {e}")
                    time.sleep(2 ** attempt)
                else:
                    print(f"    FAILED chunk at {current_ms}: {e}")
                    data = []

        if not data:
            current_ms = end_ms
            continue

        rows = []
        for k in data:
            rows.append({
                "timestamp": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        all_new.extend(rows)
        chunk_count += 1

        if chunk_count % 50 == 0:
            print(f"    Chunks: {chunk_count}, rows: {len(all_new):,}, "
                  f"latest ts: {rows[-1]['timestamp']}")

        # Advance past the last returned timestamp
        current_ms = int(data[-1][0]) + 1000

        # Rate limit: Binance allows 1200 req/min for klines, be conservative
        time.sleep(0.1)

    if all_new:
        new_df = pd.DataFrame(all_new)
        print(f"\n  Fetched {len(new_df):,} new kline rows in {chunk_count} chunks")

        # Combine
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        print(f"  Combined: {len(combined):,} total rows")
    else:
        print("\n  No new klines fetched (API may be unavailable).")
        combined = existing.copy()
        print(f"  Using existing data only: {len(combined):,} rows")

    combined.to_parquet(OUT_KLINES, index=False)
    print(f"  Saved to {OUT_KLINES}")
    return combined


# ===================================================================
# PART 1b: Fetch/synthesize Polymarket settlements
# ===================================================================
def fetch_settlements(kline_df):
    """Fetch PM settlements for gap period, falling back to synthesis from Binance data."""
    print()
    print("=" * 70)
    print("PART 1b: Fetching/synthesizing Polymarket settlements")
    print("=" * 70)

    # Load existing
    if EXISTING_SETTLEMENTS.exists():
        existing = pd.read_parquet(EXISTING_SETTLEMENTS)
        print(f"  Existing settlements: {len(existing):,} windows")
        last_ws = existing["window_start"].max()
        print(f"  Last window_start: {last_ws}")
    else:
        existing = pd.DataFrame(columns=["window_start", "chainlink_open", "chainlink_close", "outcome"])
        last_ws = 0

    # Build kline lookup for synthesis
    kline_ts = (kline_df["timestamp"] // 1000).values.astype(np.int64)
    kline_close = kline_df["close"].values
    if len(kline_ts) > 0:
        kl_min = int(kline_ts[0])
        kl_max = int(kline_ts[-1])
        kl_range = kl_max - kl_min + 1
        kl_arr = np.full(kl_range, np.nan, dtype=np.float64)
        kl_arr[kline_ts - kl_min] = kline_close
        mask = np.isnan(kl_arr)
        if mask.any():
            idx_a = np.arange(len(kl_arr))
            valid = ~mask
            if valid.any():
                kl_arr[mask] = np.interp(idx_a[mask], idx_a[valid], kl_arr[valid])
    else:
        kl_min = kl_max = 0
        kl_arr = np.array([])

    def get_btc_price(ts_sec):
        """Get BTC close price at given second."""
        if len(kl_arr) == 0:
            return None
        idx = ts_sec - kl_min
        if idx < 0 or idx >= len(kl_arr):
            return None
        v = kl_arr[idx]
        return float(v) if not np.isnan(v) else None

    # Generate window starts for gap period
    # Windows are every 300 seconds. Find the first window after existing data.
    # PM windows appear to align to 300-second boundaries
    if last_ws > 0:
        first_new_ws = last_ws + 300
    else:
        first_new_ws = GAP_START_SEC - (GAP_START_SEC % 300)

    now_sec = int(time.time())
    new_windows = []
    ws = first_new_ws
    while ws + 300 <= now_sec:
        new_windows.append(ws)
        ws += 300

    print(f"  New windows to process: {len(new_windows):,}")

    # Try to fetch from Polymarket Gamma API
    fetched_from_pm = 0
    pm_results = {}

    print("  Attempting Polymarket Gamma API fetch...")
    # Try a sample to see if API is available
    sample_ws = new_windows[0] if new_windows else None
    pm_api_available = False

    if sample_ws:
        try:
            slug = f"btc-updown-5m-{sample_ws}"
            resp = requests.get(PM_GAMMA_URL, params={"slug": slug}, timeout=10)
            if resp.status_code == 200 and resp.json():
                pm_api_available = True
                print("  Polymarket API is available, fetching settlements...")
            else:
                print(f"  Polymarket API returned empty/error for sample slug, will synthesize.")
        except Exception as e:
            print(f"  Polymarket API not available: {e}")
            print("  Will synthesize settlements from Binance data.")

    if pm_api_available:
        batch_size = 50
        for i in range(0, len(new_windows), batch_size):
            batch = new_windows[i:i+batch_size]
            for ws_val in batch:
                try:
                    slug = f"btc-updown-5m-{ws_val}"
                    resp = requests.get(PM_GAMMA_URL, params={"slug": slug}, timeout=10)
                    if resp.status_code == 200:
                        events = resp.json()
                        if events and len(events) > 0:
                            event = events[0]
                            # Try to extract outcome from outcomePrices
                            outcome_prices = event.get("outcomePrices", "")
                            # outcomePrices is typically "[price_up, price_down]"
                            # "1" means that outcome won
                            if outcome_prices:
                                try:
                                    prices = eval(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                                    if float(prices[0]) == 1.0:
                                        outcome = "Up"
                                    elif float(prices[1]) == 1.0:
                                        outcome = "Down"
                                    else:
                                        outcome = None
                                except:
                                    outcome = None
                            else:
                                outcome = None

                            # Get chainlink open from Binance
                            open_price = get_btc_price(ws_val)
                            close_price = get_btc_price(ws_val + 300)

                            if outcome and open_price:
                                pm_results[ws_val] = {
                                    "window_start": ws_val,
                                    "chainlink_open": open_price,
                                    "chainlink_close": close_price if close_price else np.nan,
                                    "outcome": outcome,
                                }
                                fetched_from_pm += 1
                except Exception:
                    pass
                time.sleep(0.05)  # rate limit

            if (i // batch_size) % 10 == 0 and i > 0:
                print(f"    Processed {i}/{len(new_windows)} windows, fetched {fetched_from_pm}")

    print(f"  Fetched {fetched_from_pm} settlements from Polymarket API")

    # Synthesize remaining from Binance data
    synthesized = 0
    new_rows = []
    for ws_val in new_windows:
        if ws_val in pm_results:
            new_rows.append(pm_results[ws_val])
            continue

        open_price = get_btc_price(ws_val)
        close_price = get_btc_price(ws_val + 300)

        if open_price is None:
            continue

        if close_price is not None:
            outcome = "Up" if close_price >= open_price else "Down"
        else:
            # Window not complete yet, skip
            continue

        new_rows.append({
            "window_start": ws_val,
            "chainlink_open": open_price,
            "chainlink_close": close_price,
            "outcome": outcome,
        })
        synthesized += 1

    print(f"  Synthesized {synthesized} settlements from Binance data")

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["window_start"]).sort_values("window_start").reset_index(drop=True)
    else:
        combined = existing.copy()

    print(f"  Combined: {len(combined):,} total settlements")
    combined.to_parquet(OUT_SETTLEMENTS, index=False)
    print(f"  Saved to {OUT_SETTLEMENTS}")
    return combined


# ===================================================================
# PART 1c: Synthesize PM price snapshots for gap period
# ===================================================================
def synthesize_price_history(kline_df, settlements_df):
    """Build price history with real PM prices for existing period + synthetic for gap."""
    print()
    print("=" * 70)
    print("PART 1c: Building full price history")
    print("=" * 70)

    delta_table = load_delta_table()
    print(f"  Delta table: {len(delta_table)} entries")

    # Load existing price history
    if EXISTING_PRICE_HIST.exists():
        existing = pd.read_parquet(EXISTING_PRICE_HIST)
        print(f"  Existing price history: {len(existing):,} snapshots")
        existing_ws = set(existing["window_start"].unique())
    else:
        existing = pd.DataFrame(columns=[
            "timestamp", "window_start", "btc_price", "window_open_price",
            "delta_bps", "delta_bucket", "time_remaining", "time_bucket",
            "polymarket_yes_price", "outcome"
        ])
        existing_ws = set()

    # Build kline lookup
    kline_ts = (kline_df["timestamp"] // 1000).values.astype(np.int64)
    kline_close = kline_df["close"].values
    if len(kline_ts) > 0:
        kl_min = int(kline_ts[0])
        kl_max = int(kline_ts[-1])
        kl_range = kl_max - kl_min + 1
        kl_arr = np.full(kl_range, np.nan, dtype=np.float64)
        kl_arr[kline_ts - kl_min] = kline_close
        mask = np.isnan(kl_arr)
        if mask.any():
            idx_a = np.arange(len(kl_arr))
            valid = ~mask
            if valid.any():
                kl_arr[mask] = np.interp(idx_a[mask], idx_a[valid], kl_arr[valid])
    else:
        kl_min = kl_max = 0
        kl_arr = np.array([])

    def get_btc_price(ts_sec):
        if len(kl_arr) == 0:
            return None
        idx = ts_sec - kl_min
        if idx < 0 or idx >= len(kl_arr):
            return None
        v = kl_arr[idx]
        return float(v) if not np.isnan(v) else None

    # For new windows, synthesize ~6 snapshots per window (similar to existing data density)
    # Existing data has ~69k snapshots for ~11.6k windows = ~6 per window
    new_windows = settlements_df[~settlements_df["window_start"].isin(existing_ws)]
    print(f"  New windows needing price synthesis: {len(new_windows):,}")

    # Snapshot times within each 300s window: roughly at 20, 60, 120, 180, 240, 280 seconds in
    snapshot_offsets = [20, 60, 120, 180, 240, 280]

    new_rows = []
    random.seed(42)  # reproducible noise

    for i, (_, wrow) in enumerate(new_windows.iterrows()):
        ws = int(wrow["window_start"])
        ptb = float(wrow["chainlink_open"])
        outcome = wrow["outcome"]

        if np.isnan(ptb) or ptb <= 0:
            continue

        for offset in snapshot_offsets:
            ts = ws + offset
            btc = get_btc_price(ts)
            if btc is None:
                continue

            delta_bps_val = (btc - ptb) / ptb * 10000
            d_bucket = get_delta_bucket(delta_bps_val)
            time_remaining = 300 - offset
            t_bucket = snap_time_bucket(time_remaining)

            # Look up fair probability from delta table
            fair = delta_table.get((d_bucket, t_bucket))
            if fair is None:
                fair = 0.5

            # Add noise to simulate PM price deviating from fair
            noise = random.gauss(0, 0.02)
            pm_yes = max(0.01, min(0.99, fair + noise))

            new_rows.append({
                "timestamp": ts,
                "window_start": ws,
                "btc_price": btc,
                "window_open_price": ptb,
                "delta_bps": delta_bps_val,
                "delta_bucket": d_bucket,
                "time_remaining": time_remaining,
                "time_bucket": t_bucket,
                "polymarket_yes_price": round(pm_yes, 3),
                "outcome": outcome,
            })

        if (i + 1) % 1000 == 0:
            print(f"    Synthesized {i+1}/{len(new_windows)} windows ({len(new_rows):,} snapshots)")

    print(f"  Synthesized {len(new_rows):,} new snapshots")

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp", "window_start"]).sort_values(
            ["window_start", "timestamp"]).reset_index(drop=True)
    else:
        combined = existing.copy()

    print(f"  Combined: {len(combined):,} total snapshots")
    combined.to_parquet(OUT_PRICE_HIST, index=False)
    print(f"  Saved to {OUT_PRICE_HIST}")
    return combined


# ===================================================================
# MAIN
# ===================================================================
def main():
    t0 = time.time()
    print("DATA FETCHER: Filling gap from March 31 to current time")
    print(f"Current time: {int(time.time())} ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})")
    print()

    # Part 1a: Klines
    klines = fetch_binance_klines()

    # Part 1b: Settlements
    settlements = fetch_settlements(klines)

    # Part 1c: Price history
    price_hist = synthesize_price_history(klines, settlements)

    elapsed = time.time() - t0
    print()
    print("=" * 70)
    print(f"DATA FETCH COMPLETE in {elapsed:.1f}s")
    print(f"  Klines:      {len(klines):,} rows -> {OUT_KLINES}")
    print(f"  Settlements: {len(settlements):,} windows -> {OUT_SETTLEMENTS}")
    print(f"  Price hist:  {len(price_hist):,} snapshots -> {OUT_PRICE_HIST}")
    print("=" * 70)


if __name__ == "__main__":
    main()
