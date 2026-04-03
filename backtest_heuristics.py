"""
Backtest heuristics C (PM lag) and B (reversal) against 14 days
of historical data (~4,000 windows). Validates the 70% win rate
found on 40 live windows.
"""

import numpy as np
import polars as pl
from pathlib import Path

PM_PATH = Path("data/price_history_with_gaps.parquet")
BINANCE_PATH = Path("data/binance_klines.parquet")
SETTLEMENTS_PATH = Path("data/polymarket_settlements.parquet")
MARKETS_PATH = Path("data/historical_markets.csv")


# ── PnL helpers ───────────────────────────────────────────────────
def pnl_buy_yes(price, outcome):
    if outcome == "Up":
        return 1.0 - price
    return -price


def pnl_buy_no(price, outcome):
    if outcome == "Down":
        return price
    return -(1.0 - price)


def summarize(trades):
    if not trades:
        return 0, 0, 0, 0
    n = len(trades)
    wins = sum(1 for w, _ in trades if w)
    wr = wins / n
    avg = sum(p for _, p in trades) / n
    total = sum(p for _, p in trades)
    return n, wr, avg, total


# ── Build aligned dataset ─────────────────────────────────────────
def build_dataset():
    print("Loading data...")
    pm = pl.read_parquet(PM_PATH)
    binance = pl.read_parquet(BINANCE_PATH).sort("timestamp")
    settlements = pl.read_parquet(SETTLEMENTS_PATH)

    print("  PM observations: {:,}".format(pm.height))
    print("  Binance klines: {:,}".format(binance.height))
    print("  Settlements: {:,}".format(settlements.height))

    # Numpy arrays for fast binary search
    btc_ts = binance["timestamp"].to_numpy()  # milliseconds
    btc_px = binance["close"].to_numpy()

    def find_btc_price(epoch_s):
        """Find Binance close price nearest to epoch_s (seconds)."""
        t_ms = epoch_s * 1000
        idx = np.searchsorted(btc_ts, t_ms, side="left")
        if idx >= len(btc_ts):
            idx = len(btc_ts) - 1
        if idx > 0 and abs(btc_ts[idx - 1] - t_ms) < abs(btc_ts[idx] - t_ms):
            idx -= 1
        if abs(btc_ts[idx] - t_ms) > 5000:
            return None
        return float(btc_px[idx])

    # Sort PM data
    pm = pm.sort(["window_start", "timestamp"])

    # Filter to windows with outcomes and time_remaining > 5
    pm = pm.filter(
        pl.col("outcome").is_not_null() & (pl.col("time_remaining") > 5)
    )

    print("\nBuilding aligned dataset with BTC lookbacks...")
    rows = []
    windows = pm["window_start"].unique().sort().to_list()
    done = 0

    for ws in windows:
        window_pm = pm.filter(pl.col("window_start") == ws).sort("timestamp")
        outcome = window_pm["outcome"][0]

        prev_pm_price = None
        prev_pm_ts = None

        for row in window_pm.iter_rows(named=True):
            t = row["timestamp"]  # seconds
            pm_price = row["polymarket_yes_price"]
            tr = row["time_remaining"]

            # Current BTC price
            btc_now = find_btc_price(t)
            if btc_now is None:
                continue

            # BTC at various lookbacks
            rec = {
                "window_start": ws,
                "timestamp": t,
                "time_remaining": tr,
                "pm_price": pm_price,
                "btc_now": btc_now,
                "outcome": outcome,
            }

            for lb_s in [5, 10, 15, 30, 60]:
                lb_t = t - lb_s
                # Don't look back past window start
                if lb_t < ws:
                    rec["btc_change_{}s".format(lb_s)] = None
                else:
                    btc_then = find_btc_price(lb_t)
                    if btc_then and btc_then > 0:
                        rec["btc_change_{}s".format(lb_s)] = (
                            (btc_now - btc_then) / btc_then * 10000
                        )
                    else:
                        rec["btc_change_{}s".format(lb_s)] = None

            # PM change from previous observation
            if prev_pm_price is not None and prev_pm_ts is not None:
                rec["pm_change"] = pm_price - prev_pm_price
                rec["pm_dt"] = t - prev_pm_ts
            else:
                rec["pm_change"] = None
                rec["pm_dt"] = None

            prev_pm_price = pm_price
            prev_pm_ts = t
            rows.append(rec)

        done += 1
        if done % 500 == 0:
            print("  {}/{} windows...".format(done, len(windows)))

    df = pl.DataFrame(rows)
    print("  Built {} rows across {} windows".format(df.height, len(windows)))
    return df


# ── Heuristic C Method 1: Inter-observation lag ───────────────────
def test_c_method1(df, ratio):
    print("\n" + "=" * 70)
    print("HEURISTIC C: PM LAG — Method 1 (inter-observation)")
    print("  When BTC moves between PM obs but PM barely changes")
    print("  BTC→PM ratio: {:.4f}".format(ratio))
    print("=" * 70)

    sub = df.filter(
        pl.col("pm_change").is_not_null()
        & pl.col("pm_dt").is_not_null()
        & (pl.col("time_remaining") > 30)
    )

    # Compute BTC change over same period as PM change
    # pm_dt is roughly 60s, use btc_change_60s as approximation
    sub = sub.filter(pl.col("btc_change_60s").is_not_null())

    print(
        "  {:>7} | {:>6} | {:>6} | {:>5} | {:>9} | {:>10}".format(
            "BTC Thr", "PM Max", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 55)

    best = {"wr": 0, "avg": -999, "n": 0, "label": ""}

    for btc_thr in [2, 3, 5, 7, 10]:
        for pm_max in [0.01, 0.02, 0.03]:
            trades = []
            seen = set()

            for row in sub.iter_rows(named=True):
                ws = row["window_start"]
                if ws in seen:
                    continue

                btc_move = row["btc_change_60s"]
                pm_move = abs(row["pm_change"])

                if btc_move > btc_thr and pm_move < pm_max:
                    pnl = pnl_buy_yes(row["pm_price"], row["outcome"])
                    trades.append((row["outcome"] == "Up", pnl))
                    seen.add(ws)
                elif btc_move < -btc_thr and pm_move < pm_max:
                    pnl = pnl_buy_no(row["pm_price"], row["outcome"])
                    trades.append((row["outcome"] == "Down", pnl))
                    seen.add(ws)

            n, wr, avg, total = summarize(trades)
            pm_label = "{:.0f}c".format(pm_max * 100)
            if n > 0:
                print(
                    "  {:>5}bp | {:>6} | {:>6} | {:>4.1f}% | ${:>8.4f} | ${:>9.2f}".format(
                        btc_thr, pm_label, n, wr * 100, avg, total
                    )
                )
            else:
                print(
                    "  {:>5}bp | {:>6} | {:>6} |   N/A |       N/A |        N/A".format(
                        btc_thr, pm_label, 0
                    )
                )

            if n >= 50 and avg > best["avg"]:
                best = {
                    "wr": wr, "avg": avg, "n": n,
                    "label": "btc={}bp, pm_max={}".format(btc_thr, pm_label),
                }

    if best["n"] > 0:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best["n"]
            )
        )
    return best


# ── Heuristic C Method 2: BTC impulse at PM snapshot ─────────────
def test_c_method2(df, ratio):
    print("\n" + "=" * 70)
    print("HEURISTIC C: PM LAG — Method 2 (BTC impulse at PM snapshot)")
    print("  Large BTC move in seconds before PM price was recorded")
    print("=" * 70)

    sub = df.filter(
        pl.col("pm_change").is_not_null() & (pl.col("time_remaining") > 30)
    )

    print(
        "  {:>7} | {:>8} | {:>6} | {:>5} | {:>9} | {:>10}".format(
            "Impulse", "Lookback", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 58)

    best = {"wr": 0, "avg": -999, "n": 0, "label": ""}

    for lookback in ["10s", "15s", "30s", "60s"]:
        col = "btc_change_{}".format(lookback)
        lb_sub = sub.filter(pl.col(col).is_not_null())

        for impulse_thr in [3, 5, 7, 10]:
            trades = []
            seen = set()

            for row in lb_sub.iter_rows(named=True):
                ws = row["window_start"]
                if ws in seen:
                    continue

                impulse = row[col]
                pm_move = abs(row["pm_change"])

                # Require PM hasn't fully repriced
                expected_pm = abs(impulse) * abs(ratio)
                if pm_move >= expected_pm * 0.8:
                    continue  # PM already caught up

                if impulse > impulse_thr:
                    pnl = pnl_buy_yes(row["pm_price"], row["outcome"])
                    trades.append((row["outcome"] == "Up", pnl))
                    seen.add(ws)
                elif impulse < -impulse_thr:
                    pnl = pnl_buy_no(row["pm_price"], row["outcome"])
                    trades.append((row["outcome"] == "Down", pnl))
                    seen.add(ws)

            n, wr, avg, total = summarize(trades)
            if n > 0:
                print(
                    "  {:>5}bp | {:>8} | {:>6} | {:>4.1f}% | ${:>8.4f} | ${:>9.2f}".format(
                        impulse_thr, lookback, n, wr * 100, avg, total
                    )
                )

            if n >= 50 and avg > best["avg"]:
                best = {
                    "wr": wr, "avg": avg, "n": n,
                    "label": "impulse={}bp, lb={}".format(impulse_thr, lookback),
                }

    if best["n"] > 0:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best["n"]
            )
        )
    return best


# ── Heuristic C Method 3: Magnitude only (ignore PM) ─────────────
def test_c_method3(df):
    print("\n" + "=" * 70)
    print("HEURISTIC C: PM LAG — Method 3 (magnitude only — ignore PM)")
    print("  Just check if BTC had a large move, buy the direction at PM price")
    print("=" * 70)

    sub = df.filter(pl.col("time_remaining") > 30)

    print(
        "  {:>7} | {:>8} | {:>6} | {:>5} | {:>9} | {:>10}".format(
            "BTC Thr", "Lookback", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 58)

    best = {"wr": 0, "avg": -999, "n": 0, "label": ""}

    for lookback in ["5s", "10s", "15s", "30s", "60s"]:
        col = "btc_change_{}".format(lookback)
        lb_sub = sub.filter(pl.col(col).is_not_null())

        for btc_thr in [2, 3, 5, 7, 10, 15]:
            trades = []
            seen = set()

            for row in lb_sub.iter_rows(named=True):
                ws = row["window_start"]
                if ws in seen:
                    continue

                move = row[col]

                if move > btc_thr:
                    pnl = pnl_buy_yes(row["pm_price"], row["outcome"])
                    trades.append((row["outcome"] == "Up", pnl))
                    seen.add(ws)
                elif move < -btc_thr:
                    pnl = pnl_buy_no(row["pm_price"], row["outcome"])
                    trades.append((row["outcome"] == "Down", pnl))
                    seen.add(ws)

            n, wr, avg, total = summarize(trades)
            if n > 0:
                print(
                    "  {:>5}bp | {:>8} | {:>6} | {:>4.1f}% | ${:>8.4f} | ${:>9.2f}".format(
                        btc_thr, lookback, n, wr * 100, avg, total
                    )
                )

            if n >= 50 and avg > best["avg"]:
                best = {
                    "wr": wr, "avg": avg, "n": n,
                    "label": "btc={}bp, lb={}".format(btc_thr, lookback),
                }

    if best["n"] > 0:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best["n"]
            )
        )
    return best


# ── Heuristic B: Reversal detector ───────────────────────────────
def test_b(df):
    print("\n" + "=" * 70)
    print("HEURISTIC B: REVERSAL DETECTOR")
    print("  BTC was trending one way, suddenly reverses — trade the reversal")
    print("=" * 70)

    sub = df.filter(pl.col("time_remaining") > 30)

    print(
        "  {:>9} | {:>7} | {:>6} | {:>6} | {:>6} | {:>5} | {:>9}".format(
            "Trend Thr", "Rev Thr", "T Look", "R Look", "Trades", "Win%", "Avg PnL"
        )
    )
    print("  " + "-" * 65)

    best = {"wr": 0, "avg": -999, "n": 0, "label": ""}

    for t_lb in ["30s", "60s"]:
        t_col = "btc_change_{}".format(t_lb)

        for r_lb in ["5s", "10s"]:
            r_col = "btc_change_{}".format(r_lb)

            lb_sub = sub.filter(
                pl.col(t_col).is_not_null() & pl.col(r_col).is_not_null()
            )

            for trend_thr in [3, 5, 7]:
                for rev_thr in [2, 3, 5]:
                    trades = []
                    seen = set()

                    for row in lb_sub.iter_rows(named=True):
                        ws = row["window_start"]
                        if ws in seen:
                            continue

                        trend = row[t_col]
                        rev = row[r_col]

                        # Trending up, reversed down
                        if trend > trend_thr and rev < -rev_thr:
                            pnl = pnl_buy_no(row["pm_price"], row["outcome"])
                            trades.append((row["outcome"] == "Down", pnl))
                            seen.add(ws)
                        # Trending down, reversed up
                        elif trend < -trend_thr and rev > rev_thr:
                            pnl = pnl_buy_yes(row["pm_price"], row["outcome"])
                            trades.append((row["outcome"] == "Up", pnl))
                            seen.add(ws)

                    n, wr, avg, total = summarize(trades)
                    if n > 0:
                        print(
                            "  {:>7}bp | {:>5}bp | {:>6} | {:>6} | {:>6} | {:>4.1f}% | ${:>8.4f}".format(
                                trend_thr, rev_thr, t_lb, r_lb, n, wr * 100, avg
                            )
                        )

                    if n >= 50 and avg > best["avg"]:
                        best = {
                            "wr": wr, "avg": avg, "n": n,
                            "label": "trend={}bp, rev={}bp, t_lb={}, r_lb={}".format(
                                trend_thr, rev_thr, t_lb, r_lb
                            ),
                        }

    if best["n"] > 0:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best["n"]
            )
        )
    return best


# ── Additional analysis for winning heuristic ─────────────────────
def analyze_winner(df, name, best_params):
    """If a heuristic wins, break down by time, volatility, hour."""
    print("\n" + "=" * 70)
    print("DETAILED ANALYSIS: {}".format(name))
    print("  Params: {}".format(best_params.get("label", "N/A")))
    print("=" * 70)
    # Placeholder — detailed analysis depends on which heuristic won
    # We'd need to re-run the winning params and tag each trade
    print("  (Run the winning heuristic with tagged trades for breakdown)")


# ── Main ──────────────────────────────────────────────────────────
def main():
    df = build_dataset()

    print("\n" + "=" * 70)
    print("DATA SUMMARY")
    print("=" * 70)
    n_windows = df["window_start"].n_unique()
    print("  Windows with aligned data: {}".format(n_windows))
    print("  Total observations: {:,}".format(df.height))
    outcomes = df.group_by("outcome").agg(pl.col("window_start").n_unique())
    for row in outcomes.iter_rows(named=True):
        print("  {} windows: {}".format(row["outcome"], row["window_start"]))

    # Estimate BTC→PM ratio from inter-observation pairs
    pairs = df.filter(
        pl.col("pm_change").is_not_null()
        & pl.col("btc_change_60s").is_not_null()
        & (pl.col("btc_change_60s").abs() > 0.5)
    )
    ratios = (pairs["pm_change"] / pairs["btc_change_60s"]).drop_nulls()
    ratios = ratios.filter(ratios.is_finite())
    ratio = ratios.median() if ratios.len() > 10 else 0.02
    print("  BTC→PM ratio (1bp ~ {:.4f} PM move)".format(ratio))

    # Run all heuristics
    best_c1 = test_c_method1(df, ratio)
    best_c2 = test_c_method2(df, ratio)
    best_c3 = test_c_method3(df)
    best_b = test_b(df)

    # Final comparison
    print("\n" + "=" * 70)
    print("=== FINAL COMPARISON ===")
    print("=" * 70)

    print(
        "  {:>30} | {:>5} | {:>9} | {:>6} | {:>7}".format(
            "", "Win%", "Avg PnL", "Trades", "Verdict"
        )
    )
    print("  " + "-" * 68)

    results = [
        ("C M1 (inter-obs lag)", best_c1),
        ("C M2 (impulse at snap)", best_c2),
        ("C M3 (magnitude only)", best_c3),
        ("B (reversal detector)", best_b),
    ]

    for name, b in results:
        n = b.get("n", 0)
        if n > 0:
            verdict = "YES" if b["wr"] > 0.52 and b["avg"] > 0 and n >= 100 else "NO"
            print(
                "  {:>30} | {:>4.1f}% | ${:>8.4f} | {:>6} | {:>7}".format(
                    name, b["wr"] * 100, b["avg"], n, verdict
                )
            )
        else:
            print(
                "  {:>30} | {:>5} | {:>9} | {:>6} | {:>7}".format(
                    name, "N/A", "N/A", 0, "NO"
                )
            )

    print(
        "  {:>30} | {:>4.1f}% | ${:>8.4f} | {:>6} | {:>7}".format(
            "(ref) Live C — 40 windows", 70.0, 0.183, 40, "YES"
        )
    )
    print(
        "  {:>30} | {:>4.1f}% | ${:>8.4f} | {:>6} | {:>7}".format(
            "(ref) Delta table — 40 wins", 41.0, -0.059, 1567, "NO"
        )
    )

    print("\n  Verdict: YES if win% > 52% AND avg PnL > 0 AND trades >= 100")

    any_yes = any(
        b.get("n", 0) >= 100 and b.get("wr", 0) > 0.52 and b.get("avg", -1) > 0
        for _, b in results
    )

    print("\n  KEY QUESTION:")
    print("  Does C's 70% win rate on 40 live windows hold across 4,000 historical?")
    if any_yes:
        print("  -> YES. Edge survives at scale. Build the bot.")
        for name, b in results:
            if b.get("n", 0) >= 100 and b.get("wr", 0) > 0.52 and b.get("avg", -1) > 0:
                print("     {} — {}: {:.1f}% on {} trades".format(
                    name, b["label"], b["wr"] * 100, b["n"]
                ))
    else:
        print("  -> NO. The live result was likely noise or the PM data resolution")
        print("     is too coarse to capture the 5-second lag effect historically.")

    print("=" * 70)


if __name__ == "__main__":
    main()
