"""
Extended 60-day backtest of Heuristic C (PM lag).
Validates the 73% win rate found on 14 days across different market regimes.

Uses: data/binance_klines_60d.parquet, data/price_history_60d.parquet
"""

import time as _time
import numpy as np
import polars as pl
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime, timezone

PH_PATH = Path("data/price_history_60d.parquet")
BINANCE_PATH = Path("data/binance_klines_60d.parquet")
OUTPUT_DIR = Path("output")

# Fallbacks if 60d files don't exist yet
PH_FALLBACK = Path("data/price_history_with_gaps.parquet")
BINANCE_FALLBACK = Path("data/binance_klines.parquet")


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


def build_dataset(ph_path, binance_path):
    print("Loading data...")
    pm = pl.read_parquet(ph_path)
    binance = pl.read_parquet(binance_path).sort("timestamp")

    print("  PM observations: {:,}".format(pm.height))
    print("  PM windows: {:,}".format(pm["window_start"].n_unique()))
    print("  Binance klines: {:,}".format(binance.height))

    btc_ts = binance["timestamp"].to_numpy()
    btc_px = binance["close"].to_numpy()

    pm = pm.filter(pl.col("outcome").is_not_null())
    pm = pm.sort(["window_start", "timestamp"])

    # Also compute daily BTC stats for regime analysis
    # Convert binance to daily stats
    binance_daily = binance.with_columns(
        (pl.col("timestamp") // 86_400_000 * 86_400_000).alias("day_ms")
    ).group_by("day_ms").agg([
        pl.col("close").first().alias("day_open"),
        pl.col("close").last().alias("day_close"),
        pl.col("high").max().alias("day_high"),
        pl.col("low").min().alias("day_low"),
    ])
    binance_daily = binance_daily.with_columns([
        ((pl.col("day_high") - pl.col("day_low")) / pl.col("day_low") * 100).alias("daily_range_pct"),
        ((pl.col("day_close") - pl.col("day_open")) / pl.col("day_open") * 100).alias("daily_return_pct"),
    ])
    daily_stats = {
        int(row["day_ms"]): row
        for row in binance_daily.iter_rows(named=True)
    }

    print("\nBuilding aligned dataset...")
    rows = []
    windows = pm["window_start"].unique().sort().to_list()
    done = 0

    for ws in windows:
        window_pm = pm.filter(pl.col("window_start") == ws).sort("timestamp")
        outcome = window_pm["outcome"][0]

        prev_pm_price = None
        prev_pm_ts = None

        for row in window_pm.iter_rows(named=True):
            t = row["timestamp"]
            pm_price = row["polymarket_yes_price"]
            tr = row["time_remaining"]

            t_ms = t * 1000
            idx = np.searchsorted(btc_ts, t_ms, side="left")
            if idx >= len(btc_ts):
                idx = len(btc_ts) - 1
            if idx > 0 and abs(btc_ts[idx - 1] - t_ms) < abs(btc_ts[idx] - t_ms):
                idx -= 1
            if abs(btc_ts[idx] - t_ms) > 5000:
                continue

            btc_now = float(btc_px[idx])

            rec = {
                "window_start": ws,
                "timestamp": t,
                "time_remaining": tr,
                "pm_price": pm_price,
                "btc_now": btc_now,
                "outcome": outcome,
            }

            for lb_s in [5, 10, 15, 30, 60]:
                lb_ms = (t - lb_s) * 1000
                if lb_ms < ws * 1000:
                    rec["btc_change_{}s".format(lb_s)] = None
                else:
                    lb_idx = np.searchsorted(btc_ts, lb_ms, side="left")
                    if lb_idx >= len(btc_ts):
                        lb_idx = len(btc_ts) - 1
                    if lb_idx > 0 and abs(btc_ts[lb_idx - 1] - lb_ms) < abs(btc_ts[lb_idx] - lb_ms):
                        lb_idx -= 1
                    btc_then = float(btc_px[lb_idx])
                    if btc_then > 0:
                        rec["btc_change_{}s".format(lb_s)] = (btc_now - btc_then) / btc_then * 10000
                    else:
                        rec["btc_change_{}s".format(lb_s)] = None

            if prev_pm_price is not None:
                rec["pm_change"] = pm_price - prev_pm_price
            else:
                rec["pm_change"] = None

            # Daily regime info
            day_ms = (ws * 1000) // 86_400_000 * 86_400_000
            ds = daily_stats.get(day_ms)
            if ds:
                rec["daily_range_pct"] = ds["daily_range_pct"]
                rec["daily_return_pct"] = ds["daily_return_pct"]
            else:
                rec["daily_range_pct"] = None
                rec["daily_return_pct"] = None

            # Hour of day
            rec["hour_utc"] = (ws % 86400) // 3600

            prev_pm_price = pm_price
            prev_pm_ts = t
            rows.append(rec)

        done += 1
        if done % 1000 == 0:
            print("  {}/{} windows...".format(done, len(windows)))

    df = pl.DataFrame(rows)
    print("  Built {:,} rows across {:,} windows".format(df.height, len(windows)))

    date_min = _time.strftime("%Y-%m-%d", _time.gmtime(df["window_start"].min()))
    date_max = _time.strftime("%Y-%m-%d", _time.gmtime(df["window_start"].max()))
    days = (df["window_start"].max() - df["window_start"].min()) / 86400
    print("  Date range: {} to {} ({:.0f} days)".format(date_min, date_max, days))

    return df


def run_method3(df, label=""):
    """Run Method 3 (magnitude only) across parameter grid."""
    sub = df.filter(pl.col("time_remaining") > 30)
    results = []

    for lookback in ["10s", "15s", "30s", "60s"]:
        col = "btc_change_{}".format(lookback)
        lb_sub = sub.filter(pl.col(col).is_not_null())

        for btc_thr in [3, 5, 7, 10, 15]:
            trades = []
            seen = set()

            for row in lb_sub.iter_rows(named=True):
                ws = row["window_start"]
                if ws in seen:
                    continue
                move = row[col]
                if move > btc_thr:
                    pnl = pnl_buy_yes(row["pm_price"], row["outcome"])
                    trades.append((row["outcome"] == "Up", pnl, ws))
                    seen.add(ws)
                elif move < -btc_thr:
                    pnl = pnl_buy_no(row["pm_price"], row["outcome"])
                    trades.append((row["outcome"] == "Down", pnl, ws))
                    seen.add(ws)

            n, wr, avg, total = summarize([(w, p) for w, p, _ in trades])
            results.append({
                "btc_thr": btc_thr, "lookback": lookback,
                "n": n, "wr": wr, "avg": avg, "total": total,
                "trades_detail": trades,
            })

    return results


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ph_path = PH_PATH if PH_PATH.exists() else PH_FALLBACK
    bn_path = BINANCE_PATH if BINANCE_PATH.exists() else BINANCE_FALLBACK

    df = build_dataset(ph_path, bn_path)
    n_windows = df["window_start"].n_unique()

    outcomes = df.group_by("outcome").agg(pl.col("window_start").n_unique())
    print("\n" + "=" * 70)
    print("DATA SUMMARY")
    print("=" * 70)
    print("  Windows: {:,}".format(n_windows))
    print("  Observations: {:,}".format(df.height))
    for row in outcomes.iter_rows(named=True):
        print("  {} windows: {:,}".format(row["outcome"], row["window_start"]))

    # ── Method 3 ──────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("METHOD 3: MAGNITUDE ONLY")
    print("=" * 70)

    results = run_method3(df)

    print(
        "  {:>7} | {:>8} | {:>7} | {:>5} | {:>9} | {:>10}".format(
            "BTC Thr", "Lookback", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 58)

    best = {"wr": 0, "avg": -999, "n": 0, "label": "", "trades_detail": []}

    for r in results:
        if r["n"] > 0:
            print(
                "  {:>5}bp | {:>8} | {:>7,} | {:>4.1f}% | ${:>8.4f} | ${:>9.2f}".format(
                    r["btc_thr"], r["lookback"], r["n"], r["wr"] * 100,
                    r["avg"], r["total"],
                )
            )
        if r["n"] >= 100 and r["avg"] > best["avg"]:
            best = r
            best["label"] = "btc={}bp, lb={}".format(r["btc_thr"], r["lookback"])

    if best["n"] > 0:
        print(
            "\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {:,} trades".format(
                best["label"], best["wr"] * 100, best["avg"], best["n"]
            )
        )

    # ── Method 2 (impulse) for comparison ─────────────────────────
    print("\n" + "=" * 70)
    print("METHOD 2: IMPULSE AT PM SNAPSHOT (comparison)")
    print("=" * 70)

    sub2 = df.filter(
        pl.col("pm_change").is_not_null() & (pl.col("time_remaining") > 30)
    )
    # Estimate ratio
    ratio_pairs = sub2.filter(
        pl.col("btc_change_60s").is_not_null() & (pl.col("btc_change_60s").abs() > 0.5)
    )
    ratios = (ratio_pairs["pm_change"] / ratio_pairs["btc_change_60s"]).drop_nulls()
    ratios = ratios.filter(ratios.is_finite())
    ratio = ratios.median() if ratios.len() > 10 else 0.015

    print("  BTC->PM ratio: {:.4f}".format(ratio))
    print(
        "  {:>7} | {:>8} | {:>7} | {:>5} | {:>9} | {:>10}".format(
            "Impulse", "Lookback", "Trades", "Win%", "Avg PnL", "Total PnL"
        )
    )
    print("  " + "-" * 58)

    best_m2 = {"wr": 0, "avg": -999, "n": 0, "label": ""}

    for lookback in ["10s", "15s", "30s", "60s"]:
        col = "btc_change_{}".format(lookback)
        lb_sub = sub2.filter(pl.col(col).is_not_null())

        for impulse_thr in [3, 5, 7, 10]:
            trades = []
            seen = set()
            for row in lb_sub.iter_rows(named=True):
                ws = row["window_start"]
                if ws in seen:
                    continue
                impulse = row[col]
                pm_move = abs(row["pm_change"])
                expected = abs(impulse) * abs(ratio)
                if pm_move >= expected * 0.8:
                    continue
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
                    "  {:>5}bp | {:>8} | {:>7,} | {:>4.1f}% | ${:>8.4f} | ${:>9.2f}".format(
                        impulse_thr, lookback, n, wr * 100, avg, total
                    )
                )
            if n >= 100 and avg > best_m2["avg"]:
                best_m2 = {"wr": wr, "avg": avg, "n": n,
                           "label": "impulse={}bp, lb={}".format(impulse_thr, lookback)}

    if best_m2["n"] > 0:
        print("\n  Best: {} -> {:.1f}% win, ${:.4f} avg, {:,} trades".format(
            best_m2["label"], best_m2["wr"] * 100, best_m2["avg"], best_m2["n"]
        ))

    # ── Stability analysis (best Method 3 combo) ─────────────────
    if not best["trades_detail"]:
        print("\nNo trades for stability analysis.")
        return

    trades_detail = best["trades_detail"]  # (won, pnl, window_start)

    print("\n" + "=" * 70)
    print("STABILITY ANALYSIS — {}".format(best["label"]))
    print("  {} trades, {:.1f}% win rate".format(best["n"], best["wr"] * 100))
    print("=" * 70)

    # A. By week
    print("\n  By week:")
    print("  {:>20} | {:>6} | {:>5} | {:>8}".format("Week", "Trades", "Win%", "Avg PnL"))
    print("  " + "-" * 48)

    trade_ws_list = [(w, p, ws) for w, p, ws in trades_detail]
    trade_ws_list.sort(key=lambda x: x[2])

    if trade_ws_list:
        first_ws = trade_ws_list[0][2]
        for week_num in range(20):
            week_start = first_ws + week_num * 7 * 86400
            week_end = week_start + 7 * 86400
            week_trades = [(w, p) for w, p, ws in trade_ws_list if week_start <= ws < week_end]
            if not week_trades:
                continue
            n, wr, avg, _ = summarize(week_trades)
            label = _time.strftime("%m/%d", _time.gmtime(week_start))
            print("  {:>20} | {:>6} | {:>4.1f}% | ${:>7.4f}".format(
                "Week {} ({})".format(week_num + 1, label), n, wr * 100, avg
            ))

    # B. By volatility regime
    print("\n  By volatility regime:")
    print("  {:>10} | {:>6} | {:>5} | {:>8}".format("Regime", "Trades", "Win%", "Avg PnL"))
    print("  " + "-" * 38)

    # Get daily range for each trade
    for regime_name, lo, hi in [("Low (<2%)", 0, 2), ("Med (2-4%)", 2, 4), ("High (>4%)", 4, 100)]:
        regime_trades = []
        for w, p, ws in trades_detail:
            day_ms = (ws * 1000) // 86_400_000 * 86_400_000
            # Find daily range from df
            obs = df.filter(
                (pl.col("window_start") == ws) & pl.col("daily_range_pct").is_not_null()
            )
            if obs.height > 0:
                dr = obs["daily_range_pct"][0]
                if dr is not None and lo <= dr < hi:
                    regime_trades.append((w, p))

        n, wr, avg, _ = summarize(regime_trades)
        if n > 0:
            print("  {:>10} | {:>6} | {:>4.1f}% | ${:>7.4f}".format(regime_name, n, wr * 100, avg))

    # C. By trend
    print("\n  By trend:")
    print("  {:>10} | {:>6} | {:>5} | {:>8}".format("Trend", "Trades", "Win%", "Avg PnL"))
    print("  " + "-" * 38)

    for trend_name, lo, hi in [("Bear (<-1%)", -100, -1), ("Neutral", -1, 1), ("Bull (>1%)", 1, 100)]:
        trend_trades = []
        for w, p, ws in trades_detail:
            obs = df.filter(
                (pl.col("window_start") == ws) & pl.col("daily_return_pct").is_not_null()
            )
            if obs.height > 0:
                ret = obs["daily_return_pct"][0]
                if ret is not None and lo <= ret < hi:
                    trend_trades.append((w, p))

        n, wr, avg, _ = summarize(trend_trades)
        if n > 0:
            print("  {:>10} | {:>6} | {:>4.1f}% | ${:>7.4f}".format(trend_name, n, wr * 100, avg))

    # D. By hour
    print("\n  By hour (UTC):")
    print("  {:>7} | {:>6} | {:>5}".format("Hour", "Trades", "Win%"))
    print("  " + "-" * 24)

    for h_start in range(0, 24, 2):
        h_trades = []
        for w, p, ws in trades_detail:
            hour = (ws % 86400) // 3600
            if h_start <= hour < h_start + 2:
                h_trades.append((w, p))
        n, wr, _, _ = summarize(h_trades)
        if n > 0:
            print("  {:>5}-{} | {:>6} | {:>4.1f}%".format(h_start, h_start + 2, n, wr * 100))

    # E. Drawdown analysis
    print("\n  Drawdown ($1/trade on $100 bankroll):")
    pnls = [p for _, p, _ in trades_detail]
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    drawdown = peak - cum
    max_dd = drawdown.max()
    max_dd_pct = max_dd / (100 + peak[np.argmax(drawdown)]) * 100 if peak[np.argmax(drawdown)] > 0 else 0

    # Longest losing streak
    streak = 0
    max_streak = 0
    for w, _, _ in trades_detail:
        if not w:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    print("    Max drawdown: ${:.2f} ({:.1f}%)".format(max_dd, max_dd_pct))
    print("    Longest losing streak: {} trades".format(max_streak))
    print("    Final PnL: ${:.2f}".format(cum[-1]))

    # Plot cumulative PnL
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(cum, linewidth=1, color="steelblue")
    ax.axhline(0, color="gray", linewidth=0.5)
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Cumulative PnL ($)")
    ax.set_title("Heuristic C — Cumulative PnL ({})".format(best["label"]))
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "cumulative_pnl_60d.png", dpi=150)
    plt.close(fig)
    print("    Saved output/cumulative_pnl_60d.png")

    # F. Rolling win rate
    window_size = min(100, len(trades_detail) // 3)
    if window_size >= 20:
        wins_arr = np.array([1.0 if w else 0.0 for w, _, _ in trades_detail])
        rolling_wr = np.convolve(wins_arr, np.ones(window_size) / window_size, mode="valid")

        print("\n  Rolling {}-trade win rate:".format(window_size))
        print("    Min: {:.1f}%  Max: {:.1f}%  Std: {:.1f}%".format(
            rolling_wr.min() * 100, rolling_wr.max() * 100, rolling_wr.std() * 100
        ))

        fig2, ax2 = plt.subplots(figsize=(12, 5))
        ax2.plot(rolling_wr * 100, linewidth=1, color="steelblue")
        ax2.axhline(50, color="red", linewidth=0.5, linestyle="--", label="50%")
        ax2.axhline(best["wr"] * 100, color="green", linewidth=0.5, linestyle="--",
                     label="Overall {:.1f}%".format(best["wr"] * 100))
        ax2.set_xlabel("Trade #")
        ax2.set_ylabel("Win Rate (%)")
        ax2.set_title("Rolling {}-trade Win Rate".format(window_size))
        ax2.legend()
        ax2.grid(True, alpha=0.3)
        plt.tight_layout()
        fig2.savefig(OUTPUT_DIR / "rolling_winrate_60d.png", dpi=150)
        plt.close(fig2)
        print("    Saved output/rolling_winrate_60d.png")

    # ── Final comparison ──────────────────────────────────────────
    print("\n" + "=" * 70)
    print("14-DAY vs EXTENDED COMPARISON")
    print("=" * 70)

    days_total = (df["window_start"].max() - df["window_start"].min()) / 86400

    print("\n  {:>20} {:>12} {:>12}".format("", "14-day", "Extended"))
    print("  " + "-" * 45)

    # 14-day reference (from backtest_heuristics.py output for 5bp/30s)
    print("  {:>20} {:>11.1f}% {:>11.1f}%".format(
        "Win rate:", 73.2, best["wr"] * 100
    ))
    print("  {:>20} ${:>10.4f} ${:>10.4f}".format(
        "Avg PnL:", 0.2073, best["avg"]
    ))
    print("  {:>20} {:>12,} {:>12,}".format(
        "Trades:", 1825, best["n"]
    ))
    print("  {:>20} {:>12} {:>11.0f}".format(
        "Days:", 14, days_total
    ))
    print("  {:>20} {:>12.0f} {:>12.0f}".format(
        "Trades/day:", 1825 / 14, best["n"] / max(1, days_total)
    ))

    # Verdict
    if best["wr"] > 0.60 and best["avg"] > 0.05:
        verdict = "CONFIRMED"
        detail = "Edge holds across regimes and time periods."
    elif best["wr"] > 0.55 and best["avg"] > 0:
        verdict = "CONFIRMED (weaker)"
        detail = "Edge exists but smaller than 14-day estimate."
    elif best["wr"] > 0.52 and best["avg"] > 0:
        verdict = "MARGINAL"
        detail = "Small edge, may not survive execution costs."
    else:
        verdict = "NOISE"
        detail = "60-day results don't support the 14-day findings."

    print("\n  VERDICT: {}".format(verdict))
    print("  {}".format(detail))
    print("=" * 70)


if __name__ == "__main__":
    main()
