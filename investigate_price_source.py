"""
Investigate what BTC price Polymarket uses for 5-minute market settlement.
Compares Chainlink settlement prices (from event metadata) to Binance
for a sample of resolved windows.

Prints: offset statistics, outcome disagreement rate, and impact assessment.
"""

import json
import time

import httpx
import numpy as np
import polars as pl
from pathlib import Path

MARKETS_PATH = Path("data/historical_markets.csv")
BINANCE_PATH = Path("data/binance_klines.parquet")
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
SAMPLE_SIZE = 30


def main():
    markets = pl.read_csv(
        MARKETS_PATH,
        schema_overrides={"yes_token_id": pl.Utf8, "no_token_id": pl.Utf8},
    )
    binance = pl.read_parquet(BINANCE_PATH).sort("timestamp")
    btc_timestamps = binance["timestamp"].to_numpy()
    btc_prices = binance["close"].to_numpy()

    # Sample resolved markets spread across the date range
    step = max(1, markets.height // SAMPLE_SIZE)
    sample_indices = list(range(0, markets.height, step))[:SAMPLE_SIZE]
    sample = markets[sample_indices]

    print("=" * 80)
    print("PRICE SOURCE INVESTIGATION")
    print("Comparing Polymarket settlement prices to Binance BTC/USDT")
    print("=" * 80)

    print("\nFetching event metadata for {} markets...".format(SAMPLE_SIZE))

    comparisons = []
    with httpx.Client(
        timeout=15.0, headers={"User-Agent": "btc-delta-research/1.0"}
    ) as client:
        for i, row in enumerate(sample.iter_rows(named=True)):
            ws = row["window_start"]
            slug = row["slug"]

            try:
                resp = client.get(
                    GAMMA_EVENTS_URL, params={"slug": slug, "limit": 1}
                )
                resp.raise_for_status()
                events = resp.json()

                if not events:
                    continue

                event = events[0]
                meta_raw = event.get("eventMetadata", "{}")
                if isinstance(meta_raw, str):
                    meta = json.loads(meta_raw) if meta_raw else {}
                else:
                    meta = meta_raw or {}

                ptb = meta.get("priceToBeat")
                fp = meta.get("finalPrice")
                if ptb is None or fp is None:
                    continue

                ptb = float(ptb)
                fp = float(fp)

                # Resolution source (from first successful fetch)
                if i == 0:
                    res_src = event.get("resolutionSource", "not found")
                    desc = event.get("description", "")[:200]
                    print("\n  Resolution source field: {}".format(res_src))
                    print("  Description excerpt: {}...".format(desc[:150]))

                # Find Binance prices at window start and end
                ws_ms = ws * 1000
                we_ms = (ws + 300) * 1000

                idx_open = np.searchsorted(btc_timestamps, ws_ms, side="left")
                idx_close = np.searchsorted(btc_timestamps, we_ms, side="left")
                if idx_close >= len(btc_timestamps):
                    idx_close = len(btc_timestamps) - 1

                if abs(btc_timestamps[idx_open] - ws_ms) > 2000:
                    continue

                binance_open = float(btc_prices[idx_open])
                binance_close = float(btc_prices[idx_close])

                comparisons.append(
                    {
                        "window_start": ws,
                        "chainlink_open": ptb,
                        "chainlink_close": fp,
                        "binance_open": binance_open,
                        "binance_close": binance_close,
                        "open_diff": binance_open - ptb,
                        "close_diff": binance_close - fp,
                        "open_diff_bps": (binance_open - ptb) / ptb * 10000,
                        "close_diff_bps": (binance_close - fp) / fp * 10000,
                        "chainlink_outcome": "Up" if fp >= ptb else "Down",
                        "binance_outcome": "Up"
                        if binance_close >= binance_open
                        else "Down",
                    }
                )

            except Exception as e:
                print("  Error fetching {}: {}".format(slug, e))

            time.sleep(0.4)
            print(
                "  Fetched {}/{}".format(i + 1, sample.height),
                end="\r",
                flush=True,
            )

    if not comparisons:
        print("\nCould not fetch any settlement data!")
        return

    df = pl.DataFrame(comparisons)

    # Print sample
    print("\n\nSample comparisons (first 10):")
    print(
        "  {:>12} | {:>12} {:>12} | {:>12} {:>12} | {:>8} {:>8} | {}".format(
            "Window",
            "CL Open",
            "CL Close",
            "BN Open",
            "BN Close",
            "OpenD$",
            "CloseD$",
            "Agree?",
        )
    )
    print("  " + "-" * 105)
    for row in df.head(10).iter_rows(named=True):
        agree = "YES" if row["chainlink_outcome"] == row["binance_outcome"] else "NO **"
        print(
            "  {:>12} | ${:>11,.2f} ${:>11,.2f} | ${:>11,.2f} ${:>11,.2f} | {:>+8.2f} {:>+8.2f} | {}".format(
                row["window_start"],
                row["chainlink_open"],
                row["chainlink_close"],
                row["binance_open"],
                row["binance_close"],
                row["open_diff"],
                row["close_diff"],
                agree,
            )
        )

    # Statistics
    print("\n" + "=" * 80)
    print("OFFSET STATISTICS ({} windows)".format(df.height))
    print("=" * 80)

    all_diffs = df["open_diff"].to_list() + df["close_diff"].to_list()
    all_bps = df["open_diff_bps"].to_list() + df["close_diff_bps"].to_list()
    abs_diffs = [abs(d) for d in all_diffs]

    print("\n  Price difference (Binance - Chainlink):")
    print("    Mean:     ${:.2f}".format(sum(all_diffs) / len(all_diffs)))
    print("    Median:   ${:.2f}".format(sorted(all_diffs)[len(all_diffs) // 2]))
    print("    Mean |diff|: ${:.2f}".format(sum(abs_diffs) / len(abs_diffs)))
    print("    Min:      ${:.2f}".format(min(all_diffs)))
    print("    Max:      ${:.2f}".format(max(all_diffs)))
    print("  As basis points:")
    print("    Mean:     {:.2f} bp".format(sum(all_bps) / len(all_bps)))
    print("    Median:   {:.2f} bp".format(sorted(all_bps)[len(all_bps) // 2]))

    # Outcome disagreements
    disagree = df.filter(pl.col("chainlink_outcome") != pl.col("binance_outcome"))
    print(
        "\n  Outcome disagreements: {}/{} ({:.1f}%)".format(
            disagree.height, df.height, disagree.height / df.height * 100
        )
    )
    if disagree.height > 0:
        print(
            "  These windows had opposite UP/DOWN results between Binance and Chainlink"
        )

    # Verdict
    mean_offset_bps = abs(sum(all_bps) / len(all_bps))
    disagree_pct = disagree.height / df.height * 100

    print("\n" + "=" * 80)
    print("=== PRICE SOURCE INVESTIGATION RESULTS ===")
    print("=" * 80)
    print("\n  Settlement oracle: Chainlink BTC/USD Data Stream")
    print("  Source: https://data.chain.link/streams/btc-usd")
    print(
        "  Settlement fields: eventMetadata.priceToBeat (open), eventMetadata.finalPrice (close)"
    )
    print("  Price chaining: finalPrice of window N = priceToBeat of window N+1")

    print("\n  IMPACT ASSESSMENT:")
    print("    Binance-Chainlink offset: ~{:.1f} basis points".format(mean_offset_bps))
    print("    Outcome disagreement rate: {:.1f}%".format(disagree_pct))

    if disagree_pct > 3 or mean_offset_bps > 5:
        print("\n    VERDICT: REBUILD NEEDED")
        print(
            "    {:.1f}% of outcomes disagree — this corrupts the delta table win rates.".format(
                disagree_pct
            )
        )
        print("    Run: python fetch_reference_prices.py")
        print("         python rebuild_delta_table.py")
        print("         python rerun_gap_analysis.py")
    elif disagree_pct > 1:
        print("\n    VERDICT: MINOR CORRECTION RECOMMENDED")
    else:
        print("\n    VERDICT: OK — original delta table is approximately correct")


if __name__ == "__main__":
    main()
