"""
Test 4 trading heuristics against live Polymarket data.
The delta table approach failed (41% win rate). These heuristics
exploit PM behavioral weaknesses instead.

Uses: data/live_validation.csv (2240 resolved snapshots, 40 windows)
"""

import polars as pl
from pathlib import Path

DATA_PATH = Path("data/live_validation.csv")


# ── PnL helpers ───────────────────────────────────────────────────
def pnl_buy_yes(ask_price, outcome):
    """Buy YES at ask. Win if Up."""
    if outcome == "Up":
        return 1.0 - ask_price
    return -ask_price


def pnl_buy_no(bid_price, outcome):
    """Sell YES at bid (= buy NO). Win if Down."""
    if outcome == "Down":
        return bid_price
    return -(1.0 - bid_price)


def summarize_trades(trades):
    """Compute stats from list of (won: bool, pnl: float) tuples."""
    if not trades:
        return 0, 0, 0, 0
    n = len(trades)
    wins = sum(1 for w, _ in trades if w)
    wr = wins / n
    avg = sum(p for _, p in trades) / n
    total = sum(p for _, p in trades)
    return n, wr, avg, total


# ── Load and prepare data ─────────────────────────────────────────
def load_data():
    df = pl.read_csv(DATA_PATH)

    # Filter to resolved windows with valid book data
    df = df.filter(pl.col("outcome").is_not_null())
    df = df.sort(["window_start", "timestamp"])

    # Compute lagged values within each window
    # Shift by 1 row = ~5 seconds, 2 rows = ~10s, 3 rows = ~15s
    for lag_rows, label in [(1, "5s"), (2, "10s"), (3, "15s")]:
        df = df.with_columns(
            (
                pl.col("delta_bps")
                - pl.col("delta_bps").shift(lag_rows).over("window_start")
            ).alias("delta_change_{}".format(label)),
            (
                pl.col("mid")
                - pl.col("mid").shift(lag_rows).over("window_start")
            ).alias("pm_mid_change_{}".format(label)),
        )

    # For longer lookbacks (4 rows = ~20s, 6 rows = ~30s)
    for lag_rows, label in [(4, "20s"), (6, "30s")]:
        df = df.with_columns(
            (
                pl.col("delta_bps")
                - pl.col("delta_bps").shift(lag_rows).over("window_start")
            ).alias("delta_change_{}".format(label)),
        )

    return df


# ── Estimate Binance→PM ratio ─────────────────────────────────────
def estimate_bps_to_pm_ratio(df):
    """Regress PM mid changes on delta_bps changes to get the scaling factor."""
    pairs = df.filter(
        pl.col("delta_change_5s").is_not_null()
        & pl.col("pm_mid_change_5s").is_not_null()
        & (pl.col("delta_change_5s").abs() > 0.1)
    )
    if pairs.height < 20:
        return 0.05  # fallback: 1bp ≈ 5 cents PM move

    # Median ratio: pm_change / delta_change
    ratios = (pairs["pm_mid_change_5s"] / pairs["delta_change_5s"]).drop_nulls()
    ratios = ratios.filter(ratios.is_finite())
    if ratios.len() < 10:
        return 0.05
    return ratios.median()


# ── HEURISTIC C: Binance impulse → PM lag ─────────────────────────
def test_heuristic_c(df, ratio):
    print("\n" + "=" * 70)
    print("HEURISTIC C: Binance impulse -> PM lag")
    print("  Ratio: 1bp BTC move ~ {:.4f} PM mid move".format(ratio))
    print("=" * 70)

    print(
        "  {:>8} | {:>6} | {:>6} | {:>5} | {:>8} | {:>9}".format(
            "Lookback", "Thresh", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 55)

    best = {"wr": 0, "avg": -999, "label": ""}

    for lookback in ["5s", "10s", "15s"]:
        dc_col = "delta_change_{}".format(lookback)
        pm_col = "pm_mid_change_{}".format(lookback)

        sub = df.filter(
            pl.col(dc_col).is_not_null()
            & pl.col(pm_col).is_not_null()
            & pl.col("best_bid").is_not_null()
            & pl.col("best_ask").is_not_null()
            & (pl.col("time_remaining") > 30)
        )

        # Compute lag for each snapshot
        sub = sub.with_columns(
            (pl.col(dc_col) * ratio - pl.col(pm_col)).alias("lag")
        )

        for thresh in [0.005, 0.01, 0.02, 0.03]:
            trades = []
            seen_windows = set()

            for row in sub.iter_rows(named=True):
                ws = row["window_start"]
                if ws in seen_windows:
                    continue

                lag = row["lag"]
                if lag is None:
                    continue

                if lag > thresh:
                    # PM behind on upward move → buy YES
                    pnl = pnl_buy_yes(row["best_ask"], row["outcome"])
                    won = row["outcome"] == "Up"
                    trades.append((won, pnl))
                    seen_windows.add(ws)
                elif lag < -thresh:
                    # PM behind on downward move → buy NO
                    pnl = pnl_buy_no(row["best_bid"], row["outcome"])
                    won = row["outcome"] == "Down"
                    trades.append((won, pnl))
                    seen_windows.add(ws)

            n, wr, avg, total = summarize_trades(trades)
            print(
                "  {:>8} | {:>6.3f} | {:>6} | {:>4.1f}% | ${:>7.4f} | ${:>8.2f}".format(
                    lookback, thresh, n, wr * 100, avg, total
                )
                if n > 0
                else "  {:>8} | {:>6.3f} | {:>6} |   N/A |      N/A |       N/A".format(
                    lookback, thresh, 0
                )
            )

            if n >= 10 and avg > best["avg"]:
                best = {
                    "wr": wr,
                    "avg": avg,
                    "n": n,
                    "label": "lookback={}, thresh={:.3f}".format(lookback, thresh),
                }

    if best["avg"] > -999:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best.get("n", 0)
            )
        )
    return best


# ── HEURISTIC A: Fade PM extreme confidence near expiry ───────────
def test_heuristic_a(df):
    print("\n" + "=" * 70)
    print("HEURISTIC A: Fade PM extreme confidence near expiry")
    print("=" * 70)

    print(
        "  {:>9} | {:>6} | {:>5} | {:>6} | {:>5} | {:>8} | {:>9}".format(
            "Time", "PM Thr", "DCap", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 65)

    best = {"wr": 0, "avg": -999, "label": ""}

    valid = df.filter(
        pl.col("best_bid").is_not_null() & pl.col("best_ask").is_not_null()
    )

    for t_min, t_max in [(10, 60), (10, 30), (30, 90)]:
        for hi, lo in [(0.90, 0.10), (0.85, 0.15), (0.80, 0.20)]:
            for dcap in [5, 7, 10]:
                time_filt = valid.filter(
                    (pl.col("time_remaining") >= t_min)
                    & (pl.col("time_remaining") <= t_max)
                )

                trades = []
                seen_windows = set()

                for row in time_filt.iter_rows(named=True):
                    ws = row["window_start"]
                    if ws in seen_windows:
                        continue

                    ask = row["best_ask"]
                    bid = row["best_bid"]
                    delta = abs(row["delta_bps"])

                    if delta >= dcap:
                        continue  # BTC genuinely moved, don't fade

                    if ask >= hi:
                        # PM says high chance UP, but delta is moderate → fade, buy NO
                        pnl = pnl_buy_no(bid, row["outcome"])
                        won = row["outcome"] == "Down"
                        trades.append((won, pnl))
                        seen_windows.add(ws)
                    elif bid <= lo:
                        # PM says high chance DOWN, but delta moderate → fade, buy YES
                        pnl = pnl_buy_yes(ask, row["outcome"])
                        won = row["outcome"] == "Up"
                        trades.append((won, pnl))
                        seen_windows.add(ws)

                n, wr, avg, total = summarize_trades(trades)
                time_label = "{}-{}s".format(t_min, t_max)
                pm_label = "{:.0f}/{:.0f}".format(hi * 100, lo * 100)

                if n > 0:
                    print(
                        "  {:>9} | {:>6} | {:>4}bp | {:>6} | {:>4.1f}% | ${:>7.4f} | ${:>8.2f}".format(
                            time_label, pm_label, dcap, n, wr * 100, avg, total
                        )
                    )
                else:
                    print(
                        "  {:>9} | {:>6} | {:>4}bp | {:>6} |   N/A |      N/A |       N/A".format(
                            time_label, pm_label, dcap, 0
                        )
                    )

                if n >= 5 and avg > best["avg"]:
                    best = {
                        "wr": wr,
                        "avg": avg,
                        "n": n,
                        "label": "time={}, pm={}, dcap={}bp".format(
                            time_label, pm_label, dcap
                        ),
                    }

    if best["avg"] > -999:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best.get("n", 0)
            )
        )
    return best


# ── HEURISTIC B: Reversal detector ───────────────────────────────
def test_heuristic_b(df):
    print("\n" + "=" * 70)
    print("HEURISTIC B: Reversal detector")
    print("=" * 70)

    print(
        "  {:>8} | {:>6} | {:>6} | {:>6} | {:>5} | {:>8} | {:>9}".format(
            "Trend LB", "TrThr", "RevThr", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 65)

    best = {"wr": 0, "avg": -999, "label": ""}

    valid = df.filter(
        pl.col("best_bid").is_not_null()
        & pl.col("best_ask").is_not_null()
        & (pl.col("time_remaining") > 30)
    )

    for trend_lb in ["15s", "20s", "30s"]:
        trend_col = "delta_change_{}".format(trend_lb)

        sub = valid.filter(
            pl.col(trend_col).is_not_null()
            & pl.col("delta_change_5s").is_not_null()
        )

        for trend_thr in [2, 3, 5]:
            for rev_thr in [1, 2, 3]:
                trades = []
                seen_windows = set()

                for row in sub.iter_rows(named=True):
                    ws = row["window_start"]
                    if ws in seen_windows:
                        continue

                    trend = row[trend_col]
                    rev = row["delta_change_5s"]

                    # Trending up, just reversed down
                    if trend > trend_thr and rev < -rev_thr:
                        pnl = pnl_buy_no(row["best_bid"], row["outcome"])
                        won = row["outcome"] == "Down"
                        trades.append((won, pnl))
                        seen_windows.add(ws)
                    # Trending down, just reversed up
                    elif trend < -trend_thr and rev > rev_thr:
                        pnl = pnl_buy_yes(row["best_ask"], row["outcome"])
                        won = row["outcome"] == "Up"
                        trades.append((won, pnl))
                        seen_windows.add(ws)

                n, wr, avg, total = summarize_trades(trades)

                if n > 0:
                    print(
                        "  {:>8} | {:>5}bp | {:>5}bp | {:>6} | {:>4.1f}% | ${:>7.4f} | ${:>8.2f}".format(
                            trend_lb, trend_thr, rev_thr, n, wr * 100, avg, total
                        )
                    )
                else:
                    print(
                        "  {:>8} | {:>5}bp | {:>5}bp | {:>6} |   N/A |      N/A |       N/A".format(
                            trend_lb, trend_thr, rev_thr, 0
                        )
                    )

                if n >= 5 and avg > best["avg"]:
                    best = {
                        "wr": wr,
                        "avg": avg,
                        "n": n,
                        "label": "trend_lb={}, trend={}bp, rev={}bp".format(
                            trend_lb, trend_thr, rev_thr
                        ),
                    }

    if best["avg"] > -999:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best.get("n", 0)
            )
        )
    return best


# ── HEURISTIC E: Depth-weighted direction ─────────────────────────
def test_heuristic_e(df):
    print("\n" + "=" * 70)
    print("HEURISTIC E: Depth-weighted direction")
    print("=" * 70)

    print(
        "  {:>9} | {:>6} | {:>6} | {:>5} | {:>8} | {:>9}".format(
            "Time", "Ratio", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 55)

    best = {"wr": 0, "avg": -999, "label": ""}

    valid = df.filter(
        pl.col("best_bid").is_not_null()
        & pl.col("best_ask").is_not_null()
        & pl.col("bid_depth_3").is_not_null()
        & pl.col("ask_depth_3").is_not_null()
        & (pl.col("bid_depth_3") > 0)
        & (pl.col("ask_depth_3") > 0)
    )

    for t_min, t_max in [(60, 240), (30, 180), (90, 270)]:
        time_filt = valid.filter(
            (pl.col("time_remaining") >= t_min)
            & (pl.col("time_remaining") <= t_max)
        )

        # Compute depth ratio
        time_filt = time_filt.with_columns(
            (pl.col("bid_depth_3") / pl.col("ask_depth_3")).alias("depth_ratio")
        )

        for ratio_thr in [1.5, 2.0, 2.5, 3.0]:
            trades = []
            seen_windows = set()

            for row in time_filt.iter_rows(named=True):
                ws = row["window_start"]
                if ws in seen_windows:
                    continue

                dr = row["depth_ratio"]

                if dr > ratio_thr:
                    # Bid depth dominates → buy YES
                    pnl = pnl_buy_yes(row["best_ask"], row["outcome"])
                    won = row["outcome"] == "Up"
                    trades.append((won, pnl))
                    seen_windows.add(ws)
                elif dr < 1.0 / ratio_thr:
                    # Ask depth dominates → buy NO
                    pnl = pnl_buy_no(row["best_bid"], row["outcome"])
                    won = row["outcome"] == "Down"
                    trades.append((won, pnl))
                    seen_windows.add(ws)

            n, wr, avg, total = summarize_trades(trades)
            time_label = "{}-{}s".format(t_min, t_max)

            if n > 0:
                print(
                    "  {:>9} | {:>5.1f}x | {:>6} | {:>4.1f}% | ${:>7.4f} | ${:>8.2f}".format(
                        time_label, ratio_thr, n, wr * 100, avg, total
                    )
                )
            else:
                print(
                    "  {:>9} | {:>5.1f}x | {:>6} |   N/A |      N/A |       N/A".format(
                        time_label, ratio_thr, 0
                    )
                )

            if n >= 5 and avg > best["avg"]:
                best = {
                    "wr": wr,
                    "avg": avg,
                    "n": n,
                    "label": "time={}, ratio={:.1f}x".format(time_label, ratio_thr),
                }

    if best["avg"] > -999:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best.get("n", 0)
            )
        )
    return best


# ── Main ──────────────────────────────────────────────────────────
def main():
    print("Loading data...")
    df = load_data()
    resolved = df.filter(pl.col("outcome").is_not_null())
    n_windows = resolved["window_start"].n_unique()
    print(
        "  {} snapshots, {} resolved windows".format(resolved.height, n_windows)
    )
    print("  NOTE: 40 windows is a small sample. Results are noisy.")

    # Estimate BTC→PM scaling ratio
    ratio = estimate_bps_to_pm_ratio(resolved)
    print("  BTC→PM ratio: 1bp delta ~ {:.4f} PM mid change".format(ratio))

    # Run all heuristics
    best_c = test_heuristic_c(resolved, ratio)
    best_a = test_heuristic_a(resolved)
    best_b = test_heuristic_b(resolved)
    best_e = test_heuristic_e(resolved)

    # Overall comparison
    print("\n" + "=" * 70)
    print("=== OVERALL COMPARISON ===")
    print("=" * 70)

    print(
        "  {:>25} | {:>9} | {:>10} | {:>6} | {:>7}".format(
            "", "Best Win%", "Best AvgPnL", "Trades", "Verdict"
        )
    )
    print("  " + "-" * 68)

    all_results = [
        ("C: PM lag", best_c),
        ("A: Fade expiry", best_a),
        ("B: Reversal", best_b),
        ("E: Depth ratio", best_e),
    ]

    any_edge = False
    for name, b in all_results:
        if b["avg"] > -999 and b.get("n", 0) > 0:
            n = b.get("n", 0)
            verdict = "YES" if b["wr"] > 0.52 and b["avg"] > 0 and n >= 20 else "NO"
            if verdict == "YES":
                any_edge = True
            print(
                "  {:>25} | {:>8.1f}% | ${:>9.4f} | {:>6} | {:>7}".format(
                    name, b["wr"] * 100, b["avg"], n, verdict
                )
            )
        else:
            print(
                "  {:>25} | {:>9} | {:>10} | {:>6} | {:>7}".format(
                    name, "N/A", "N/A", 0, "NO"
                )
            )

    print(
        "  {:>25} | {:>8.1f}% | ${:>9.4f} | {:>6} | {:>7}".format(
            "(ref) Delta table", 41.0, -0.059, 1567, "NO"
        )
    )

    print("\n  Verdict: YES if win% > 52% AND avg PnL > 0 AND trades >= 20")

    print("\n  RECOMMENDATION:")
    if any_edge:
        for name, b in all_results:
            n = b.get("n", 0)
            if b["avg"] > -999 and b["wr"] > 0.52 and b["avg"] > 0 and n >= 20:
                print("    {} shows edge: {}".format(name, b["label"]))
        print("    BUT: 40 windows is a tiny sample. Validate with more data.")
    else:
        print(
            "    No heuristic beats 52% with positive PnL on 40 windows."
        )
        print(
            "    The market may be too efficient for simple heuristics,"
        )
        print(
            "    or the sample is too small to detect a real edge."
        )

    print("=" * 70)


if __name__ == "__main__":
    main()
