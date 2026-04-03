"""
Deep analysis of paper_trades.csv.
Gives every dimension needed to make production decisions.

Usage: python analyze_paper.py
"""

import polars as pl
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path
from datetime import datetime

import os

def find_csv():
    """Find the trades CSV, checking env var, then new paths, then old paths."""
    env = os.environ.get("ANALYZE_CSV")
    if env:
        return Path(env)
    # New naming: data/{instance}/trades.csv
    for p in [Path("data/default/trades.csv"), Path("data/paper_trades_v2.csv")]:
        if p.exists():
            return p
    return Path("data/paper_trades_v2.csv")

DATA_PATH = find_csv()
OUTPUT_DIR = Path("output")

G = "\033[32m"
R = "\033[31m"
Y = "\033[33m"
C = "\033[36m"
M = "\033[35m"
W = "\033[97m"
DIM = "\033[2m"
BOLD = "\033[1m"
RST = "\033[0m"


def cpnl(val, fmt="+.0f"):
    return "{}${}{}".format(G if val >= 0 else R, format(val, fmt), RST)


def cwr(wr):
    return "{}{:.1f}%{}".format(G if wr >= 65 else Y if wr >= 55 else R, wr, RST)


def section(num, title):
    print("\n{}{}  {}. {}  {}".format(BOLD, W, num, title, RST))


def divider():
    print("  {}{}{}".format(DIM, "-" * 66, RST))


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    df = pl.read_csv(DATA_PATH, infer_schema_length=10000)
    # Strip whitespace from column names and string values
    df = df.rename({col: col.strip() for col in df.columns})
    for col in df.columns:
        if df[col].dtype == pl.Utf8:
            df = df.with_columns(pl.col(col).str.strip_chars().alias(col))
    # Cast numeric columns
    num_cols = [
        "timestamp", "window_start", "impulse_bps", "time_remaining",
        "best_bid", "best_ask", "mid", "spread", "fill_price",
        "levels_consumed", "slippage", "fee", "effective_entry_taker",
        "effective_entry_maker", "filled_size", "btc_price", "delta_bps",
        "book_age_ms", "pnl_taker", "pnl_maker",
    ]
    for col in num_cols:
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    s = df.filter(pl.col("outcome").is_not_null() & (pl.col("outcome") != ""))

    if s.height == 0:
        print("No settled trades.")
        return

    n_trades = s.height
    n_windows = s["window_start"].n_unique()
    hours = (s["timestamp"].max() - s["timestamp"].min()) / 3600
    combos = sorted(s["combo"].unique().to_list())

    # Derived columns
    s = s.with_columns([
        (pl.col("fill_price") * 100).alias("entry_c"),
        pl.col("impulse_bps").abs().alias("abs_imp"),
        pl.col("slippage").abs().alias("abs_slip"),
    ])

    print("\n{}{}  PAPER TRADING DEEP ANALYSIS  {}{}".format(
        BOLD, M, RST, M + "=" * 40 + RST
    ))
    print("  {} trades | {} windows | {:.1f} hours | {} combos\n".format(
        n_trades, n_windows, hours, len(combos)
    ))

    # ══════════════════════════════════════════════════════════════
    section(1, "PER-COMBO BREAKDOWN")
    # ══════════════════════════════════════════════════════════════
    print("  {}{:>12} {:>5} {:>6} {:>9} {:>9} {:>7} {:>7} {:>5} {:>5} {:>5}{}".format(
        DIM, "Combo", "N", "Win%", "PnL(tk)", "PnL(mk)", "$/trade", "MaxDD", "Slip", "Fee", "Avg$", RST
    ))
    divider()

    combo_stats = {}
    for combo in combos:
        c = s.filter(pl.col("combo") == combo)
        n = c.height
        wins = c.filter(pl.col("result") == "WIN").height
        wr = wins / n * 100
        pnl_tk = c["pnl_taker"].sum()
        pnl_mk = c["pnl_maker"].sum()
        avg_pnl = pnl_tk / n
        avg_slip = c["abs_slip"].mean() * 100
        avg_fee = c["fee"].abs().mean() * 100
        avg_entry = c["fill_price"].mean() * 100

        # Max drawdown
        cum = np.cumsum(c.sort("timestamp")["pnl_taker"].to_list())
        peak = np.maximum.accumulate(cum)
        dd = (peak - cum).max()

        # Win/loss streaks
        results = c.sort("timestamp")["result"].to_list()
        max_win_streak = max_loss_streak = cur = 0
        prev = None
        for r in results:
            if r == prev:
                cur += 1
            else:
                cur = 1
            if r == "WIN":
                max_win_streak = max(max_win_streak, cur)
            else:
                max_loss_streak = max(max_loss_streak, cur)
            prev = r

        combo_stats[combo] = {
            "n": n, "wr": wr, "pnl_tk": pnl_tk, "pnl_mk": pnl_mk,
            "avg_pnl": avg_pnl, "dd": dd, "win_streak": max_win_streak,
            "loss_streak": max_loss_streak,
        }

        print("  {}{:>12}{} {:>5} {} {:>9} {:>9} {:>7} {:>7} {:>4.1f}c {:>4.1f}c {:>4.0f}c".format(
            C, combo, RST, n, cwr(wr),
            cpnl(pnl_tk), cpnl(pnl_mk),
            cpnl(avg_pnl, "+.1f"), cpnl(-dd),
            avg_slip, avg_fee, avg_entry,
        ))

    print()
    print("  {}Streaks:{}".format(DIM, RST))
    for combo in combos:
        cs = combo_stats[combo]
        if cs["n"] > 0:
            print("    {}{:>12}{}: best win streak: {}  worst loss streak: {}{}{}".format(
                C, combo, RST, cs["win_streak"],
                R if cs["loss_streak"] >= 3 else "", cs["loss_streak"], RST,
            ))

    # ══════════════════════════════════════════════════════════════
    section(2, "ENTRY PRICE ANALYSIS")
    # ══════════════════════════════════════════════════════════════
    print("  {}{:>8} {:>5} {:>6} {:>8} {:>9} {:>8} {:>8} {:>8}{}".format(
        DIM, "Entry", "N", "Win%", "AvgPnL", "Total", "AvgWin", "AvgLoss", "Expect", RST
    ))
    divider()

    for lo, hi in [(20, 25), (25, 30), (30, 35), (35, 40), (40, 45), (45, 50),
                    (50, 55), (55, 60), (60, 65), (65, 70), (70, 75), (75, 80)]:
        b = s.filter((pl.col("entry_c") >= lo) & (pl.col("entry_c") < hi))
        if b.height >= 3:
            n = b.height
            wins = b.filter(pl.col("result") == "WIN").height
            wr = wins / n * 100
            avg = b["pnl_taker"].mean()
            total = b["pnl_taker"].sum()
            w_avg = b.filter(pl.col("result") == "WIN")["pnl_taker"].mean() if wins > 0 else 0
            l_avg = b.filter(pl.col("result") == "LOSS")["pnl_taker"].mean() if n - wins > 0 else 0
            # Expected value per dollar risked
            risk = lo / 100  # approximate risk per share
            ev = avg / (risk * b["filled_size"].mean()) if risk > 0 else 0
            print("  {:>3}-{:<3}c {:>5} {} {:>8} {:>9} {:>8} {:>8} {:>7.1f}%".format(
                lo, hi, n, cwr(wr), cpnl(avg, "+.1f"), cpnl(total),
                cpnl(w_avg, "+.0f"), cpnl(l_avg, "+.0f"), ev * 100,
            ))

    # ══════════════════════════════════════════════════════════════
    section(3, "TIME REMAINING ANALYSIS")
    # ══════════════════════════════════════════════════════════════
    print("  {}{:>14} {:>5} {:>6} {:>8} {:>9} {:>8}{}".format(
        DIM, "Time", "N", "Win%", "AvgPnL", "Total", "AvgSize", RST
    ))
    divider()

    for lo, hi in [(270, 300), (240, 270), (210, 240), (180, 210), (150, 180),
                    (120, 150), (90, 120), (60, 90), (30, 60), (0, 30)]:
        b = s.filter((pl.col("time_remaining") >= lo) & (pl.col("time_remaining") < hi))
        if b.height >= 2:
            n = b.height
            wins = b.filter(pl.col("result") == "WIN").height
            wr = wins / n * 100
            avg = b["pnl_taker"].mean()
            total = b["pnl_taker"].sum()
            avg_size = b["filled_size"].mean()
            print("  T-{:>3}-{:<3}s {:>5} {} {:>8} {:>9} {:>7.0f}".format(
                lo, hi, n, cwr(wr), cpnl(avg, "+.1f"), cpnl(total), avg_size,
            ))

    # ══════════════════════════════════════════════════════════════
    section(4, "IMPULSE SIZE ANALYSIS")
    # ══════════════════════════════════════════════════════════════
    print("  {}{:>10} {:>5} {:>6} {:>8} {:>9}{}".format(
        DIM, "Impulse", "N", "Win%", "AvgPnL", "Total", RST
    ))
    divider()

    for lo, hi in [(0, 3), (3, 5), (5, 7), (7, 10), (10, 15), (15, 20), (20, 30), (30, 200)]:
        b = s.filter((pl.col("abs_imp") >= lo) & (pl.col("abs_imp") < hi))
        if b.height >= 2:
            n = b.height
            wins = b.filter(pl.col("result") == "WIN").height
            wr = wins / n * 100
            avg = b["pnl_taker"].mean()
            total = b["pnl_taker"].sum()
            label = "{}+bp".format(lo) if hi >= 200 else "{}-{}bp".format(lo, hi)
            print("  {:>10} {:>5} {} {:>8} {:>9}".format(
                label, n, cwr(wr), cpnl(avg, "+.1f"), cpnl(total),
            ))

    # ══════════════════════════════════════════════════════════════
    section(5, "DIRECTION ANALYSIS")
    # ══════════════════════════════════════════════════════════════

    for d, label in [("YES", "BUY UP  "), ("NO", "BUY DOWN")]:
        b = s.filter(pl.col("direction") == d)
        n = b.height
        wins = b.filter(pl.col("result") == "WIN").height
        wr = wins / n * 100
        pnl = b["pnl_taker"].sum()
        avg_entry = b["fill_price"].mean() * 100
        print("  {} {:>5} trades  {}  PnL: {}  avg entry: {:.0f}c".format(
            label, n, cwr(wr), cpnl(pnl), avg_entry,
        ))

    # ══════════════════════════════════════════════════════════════
    section(6, "CROSS-TAB: ENTRY PRICE x TIME x DIRECTION")
    # ══════════════════════════════════════════════════════════════

    s2 = s.with_columns([
        pl.when(pl.col("entry_c") < 30).then(pl.lit("<30c"))
        .when(pl.col("entry_c") < 40).then(pl.lit("30-40c"))
        .when(pl.col("entry_c") < 50).then(pl.lit("40-50c"))
        .when(pl.col("entry_c") < 60).then(pl.lit("50-60c"))
        .otherwise(pl.lit("60c+")).alias("pb"),
        pl.when(pl.col("time_remaining") >= 240).then(pl.lit("early"))
        .when(pl.col("time_remaining") >= 120).then(pl.lit("mid"))
        .otherwise(pl.lit("late")).alias("tb"),
    ])

    for tb, tb_label in [("early", "Early (T-240 to T-300)"), ("mid", "Mid (T-120 to T-240)"), ("late", "Late (T-0 to T-120)")]:
        print("  {}{}:{}".format(Y, tb_label, RST))
        for pb in ["<30c", "30-40c", "40-50c", "50-60c", "60c+"]:
            cell = s2.filter((pl.col("pb") == pb) & (pl.col("tb") == tb))
            if cell.height >= 3:
                n = cell.height
                wins = cell.filter(pl.col("result") == "WIN").height
                wr = wins / n * 100
                pnl = cell["pnl_taker"].sum()
                # Direction split
                yes_n = cell.filter(pl.col("direction") == "YES").height
                no_n = cell.filter(pl.col("direction") == "NO").height
                print("    {:>6}: {:>3} trades  {}  PnL: {:>10}  ({}UP/{} DOWN)".format(
                    pb, n, cwr(wr), cpnl(pnl), yes_n, no_n,
                ))

    # ══════════════════════════════════════════════════════════════
    section(7, "PER-WINDOW RESULTS")
    # ══════════════════════════════════════════════════════════════

    windows = sorted(s["window_start"].unique().to_list())
    win_results = []
    for ws in windows:
        w = s.filter(pl.col("window_start") == ws)
        outcome = w["outcome"][0]
        n = w.height
        wins = w.filter(pl.col("result") == "WIN").height
        total_pnl = w["pnl_taker"].sum()
        win_results.append((ws, outcome, n, wins, total_pnl))

    # Show worst and best windows
    win_results.sort(key=lambda x: x[4])
    print("  {}Worst 5 windows:{}".format(R, RST))
    for ws, outcome, n, wins, pnl in win_results[:5]:
        t = datetime.fromtimestamp(ws).strftime("%H:%M")
        print("    {} {} {:>3} trades  {}/{} won  {}".format(
            t, outcome, n, wins, n, cpnl(pnl),
        ))

    print("  {}Best 5 windows:{}".format(G, RST))
    for ws, outcome, n, wins, pnl in win_results[-5:]:
        t = datetime.fromtimestamp(ws).strftime("%H:%M")
        print("    {} {} {:>3} trades  {}/{} won  {}".format(
            t, outcome, n, wins, n, cpnl(pnl),
        ))

    # ══════════════════════════════════════════════════════════════
    section(8, "RISK ANALYSIS")
    # ══════════════════════════════════════════════════════════════

    # Per-window PnL distribution
    win_pnls = [r[4] for r in win_results]
    print("  Per-window PnL distribution:")
    print("    Mean:   {}".format(cpnl(np.mean(win_pnls), "+.0f")))
    print("    Median: {}".format(cpnl(np.median(win_pnls), "+.0f")))
    print("    Std:    ${:.0f}".format(np.std(win_pnls)))
    print("    Min:    {}".format(cpnl(min(win_pnls), "+.0f")))
    print("    Max:    {}".format(cpnl(max(win_pnls), "+.0f")))
    neg = sum(1 for p in win_pnls if p < 0)
    print("    Losing windows: {}/{} ({:.0f}%)".format(neg, len(win_pnls), neg / len(win_pnls) * 100))

    # Correlation between combos — do they all lose together?
    print("\n  Correlation risk (windows where ALL active combos lost):")
    all_loss_windows = 0
    for ws, outcome, n, wins, pnl in win_results:
        if wins == 0 and n > 1:
            all_loss_windows += 1
    multi_trade_windows = sum(1 for _, _, n, _, _ in win_results if n > 1)
    print("    {}/{} multi-trade windows had ALL combos lose".format(
        all_loss_windows, multi_trade_windows
    ))

    # Largest single-window loss
    print("    Largest single-window loss: {}".format(cpnl(min(win_pnls))))

    # Sharpe-like ratio (per trade)
    trade_pnls = s["pnl_taker"].to_list()
    if np.std(trade_pnls) > 0:
        sharpe = np.mean(trade_pnls) / np.std(trade_pnls) * np.sqrt(len(trade_pnls) / hours * 24)
        print("    Daily Sharpe (per-trade): {:.2f}".format(sharpe))

    # ══════════════════════════════════════════════════════════════
    section(9, "EXECUTION QUALITY")
    # ══════════════════════════════════════════════════════════════

    print("  Avg slippage:    {:.2f}c".format(s["abs_slip"].mean() * 100))
    print("  Avg fee:         {:.2f}c".format(s["fee"].abs().mean() * 100))
    print("  Avg spread:      {:.2f}c".format(s["spread"].mean() * 100))
    print("  Avg book age:    {:.0f}ms".format(s["book_age_ms"].mean()))
    print("  Avg levels hit:  {:.1f}".format(s["levels_consumed"].mean()))
    total_cost = (s["abs_slip"].mean() + s["fee"].abs().mean()) * 100
    print("  Total cost:      {:.2f}c/trade".format(total_cost))

    # Cost as % of avg PnL for winners
    winners = s.filter(pl.col("result") == "WIN")
    if winners.height > 0:
        avg_win_pnl = winners["pnl_taker"].mean()
        print("  Cost as % of avg win: {:.1f}%".format(total_cost / (avg_win_pnl / winners["filled_size"].mean()) * 100 / 100))

    # ══════════════════════════════════════════════════════════════
    section(10, "WORST TRADES DEEP DIVE")
    # ══════════════════════════════════════════════════════════════

    worst = s.sort("pnl_taker").head(15)
    print("  {}{:>12} {:>4} {:>5} {:>5} {:>7} {:>7} {:>6} {:>6}{}".format(
        DIM, "Combo", "Dir", "Entry", "Size", "PnL", "Imp", "T-rem", "Out", RST
    ))
    divider()
    for row in worst.iter_rows(named=True):
        print("  {}{:>12}{} {:>4} {:>4.0f}c {:>5.0f} {} {:>+5.0f}bp {:>4.0f}s  {}".format(
            C, row["combo"], RST,
            row["direction"], row["fill_price"] * 100, row["filled_size"],
            cpnl(row["pnl_taker"]),
            row["impulse_bps"], row["time_remaining"],
            row["outcome"],
        ))

    # Common patterns in worst trades
    print("\n  {}Patterns in worst 20 trades:{}".format(BOLD, RST))
    w20 = s.sort("pnl_taker").head(20)
    avg_entry_w = w20["fill_price"].mean() * 100
    avg_time_w = w20["time_remaining"].mean()
    avg_imp_w = w20["abs_imp"].mean()
    no_pct = w20.filter(pl.col("direction") == "NO").height / 20 * 100
    print("    Avg entry: {:.0f}c | Avg T-remaining: {:.0f}s | Avg impulse: {:.0f}bp | NO%: {:.0f}%".format(
        avg_entry_w, avg_time_w, avg_imp_w, no_pct
    ))

    # ══════════════════════════════════════════════════════════════
    section(11, "BEST TRADES DEEP DIVE")
    # ══════════════════════════════════════════════════════════════

    best = s.sort("pnl_taker", descending=True).head(15)
    print("  {}{:>12} {:>4} {:>5} {:>5} {:>7} {:>7} {:>6} {:>6}{}".format(
        DIM, "Combo", "Dir", "Entry", "Size", "PnL", "Imp", "T-rem", "Out", RST
    ))
    divider()
    for row in best.iter_rows(named=True):
        print("  {}{:>12}{} {:>4} {:>4.0f}c {:>5.0f} {} {:>+5.0f}bp {:>4.0f}s  {}".format(
            C, row["combo"], RST,
            row["direction"], row["fill_price"] * 100, row["filled_size"],
            cpnl(row["pnl_taker"]),
            row["impulse_bps"], row["time_remaining"],
            row["outcome"],
        ))

    print("\n  {}Patterns in best 20 trades:{}".format(BOLD, RST))
    b20 = s.sort("pnl_taker", descending=True).head(20)
    avg_entry_b = b20["fill_price"].mean() * 100
    avg_time_b = b20["time_remaining"].mean()
    avg_imp_b = b20["abs_imp"].mean()
    no_pct_b = b20.filter(pl.col("direction") == "NO").height / 20 * 100
    print("    Avg entry: {:.0f}c | Avg T-remaining: {:.0f}s | Avg impulse: {:.0f}bp | NO%: {:.0f}%".format(
        avg_entry_b, avg_time_b, avg_imp_b, no_pct_b
    ))

    # ══════════════════════════════════════════════════════════════
    section(12, "HOUR-OF-DAY ANALYSIS")
    # ══════════════════════════════════════════════════════════════

    s_hour = s.with_columns(
        pl.col("timestamp").cast(pl.Int64).map_elements(
            lambda t: datetime.fromtimestamp(t).hour, return_dtype=pl.Int64
        ).alias("hour")
    )
    print("  {}{:>6} {:>5} {:>6} {:>9}{}".format(DIM, "Hour", "N", "Win%", "PnL(tk)", RST))
    divider()
    for h in range(24):
        b = s_hour.filter(pl.col("hour") == h)
        if b.height >= 2:
            n = b.height
            wins = b.filter(pl.col("result") == "WIN").height
            wr = wins / n * 100
            pnl = b["pnl_taker"].sum()
            print("  {:>4}:00 {:>5} {} {:>9}".format(h, n, cwr(wr), cpnl(pnl)))

    # ══════════════════════════════════════════════════════════════
    section(13, "SPREAD AT ENTRY vs OUTCOME")
    # ══════════════════════════════════════════════════════════════

    for spr_label, lo, hi in [("Tight (1c)", 0, 0.015), ("Normal (2c)", 0.015, 0.025), ("Wide (3c+)", 0.025, 1)]:
        b = s.filter((pl.col("spread") >= lo) & (pl.col("spread") < hi))
        if b.height >= 3:
            n = b.height
            wins = b.filter(pl.col("result") == "WIN").height
            wr = wins / n * 100
            pnl = b["pnl_taker"].sum()
            print("  {:>14}: {:>4} trades  {}  PnL: {}".format(
                spr_label, n, cwr(wr), cpnl(pnl),
            ))

    # ══════════════════════════════════════════════════════════════
    section(14, "FILTER OPTIMIZER")
    # ══════════════════════════════════════════════════════════════

    print("  Testing filter combinations (min 15 trades)...\n")

    best_filters = []
    for entry_lo in [20, 25, 30, 35]:
        for entry_hi in [45, 50, 55, 60, 65]:
            if entry_hi <= entry_lo:
                continue
            for time_lo in [0, 60, 120, 240]:
                for time_hi in [120, 180, 240, 300]:
                    if time_hi <= time_lo:
                        continue
                    for imp_cap in [15, 20, 25]:
                        f = s.filter(
                            (pl.col("entry_c") >= entry_lo)
                            & (pl.col("entry_c") < entry_hi)
                            & (pl.col("time_remaining") >= time_lo)
                            & (pl.col("time_remaining") < time_hi)
                            & (pl.col("abs_imp") < imp_cap)
                        )
                        if f.height >= 15:
                            n = f.height
                            wins = f.filter(pl.col("result") == "WIN").height
                            wr = wins / n * 100
                            avg = f["pnl_taker"].mean()
                            total = f["pnl_taker"].sum()
                            best_filters.append({
                                "entry": "{}-{}c".format(entry_lo, entry_hi),
                                "time": "T-{}-{}".format(time_lo, time_hi),
                                "imp_cap": "{}bp".format(imp_cap),
                                "n": n, "wr": wr, "avg": avg, "total": total,
                            })

    # Sort by avg PnL
    best_filters.sort(key=lambda x: x["avg"], reverse=True)

    print("  {}{:>10} {:>10} {:>6} {:>5} {:>6} {:>8} {:>9}{}".format(
        DIM, "Entry", "Time", "ImpCap", "N", "Win%", "AvgPnL", "Total", RST
    ))
    divider()
    for f in best_filters[:15]:
        print("  {:>10} {:>10} {:>6} {:>5} {} {:>8} {:>9}".format(
            f["entry"], f["time"], f["imp_cap"],
            f["n"], cwr(f["wr"]),
            cpnl(f["avg"], "+.1f"), cpnl(f["total"]),
        ))

    if best_filters:
        top = best_filters[0]
        print("\n  {}RECOMMENDED FILTERS:{}".format(BOLD, RST))
        print("    Entry: {}  Time: {}  Impulse cap: {}".format(
            top["entry"], top["time"], top["imp_cap"]
        ))
        print("    {} trades | {} win | {} avg | {} total".format(
            top["n"], cwr(top["wr"]), cpnl(top["avg"], "+.1f"), cpnl(top["total"]),
        ))

    # ══════════════════════════════════════════════════════════════
    section(15, "CHARTS")
    # ══════════════════════════════════════════════════════════════

    # Chart 1: Cumulative PnL per combo (top 4)
    top4 = sorted(combo_stats.items(), key=lambda x: x[1]["pnl_tk"], reverse=True)[:4]
    fig, ax = plt.subplots(figsize=(14, 6))
    colors_list = ["#2ecc71", "#3498db", "#e74c3c", "#f39c12", "#9b59b6"]
    for i, (combo, _) in enumerate(top4):
        c = s.filter(pl.col("combo") == combo).sort("timestamp")
        cum = np.cumsum(c["pnl_taker"].to_list())
        ax.plot(cum, label="{} (${:+,.0f})".format(combo, cum[-1]),
                linewidth=1.5, color=colors_list[i])
    ax.axhline(0, color="gray", linewidth=0.5)
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Cumulative PnL ($)")
    ax.set_title("Cumulative Taker PnL — Top 4 Combos")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "paper_cum_pnl.png", dpi=150)
    plt.close(fig)
    print("  Saved output/paper_cum_pnl.png")

    # Chart 2: Entry price — win rate and PnL
    fig2, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    pb_data = []
    for lo, hi in [(20, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 80)]:
        b = s.filter((pl.col("entry_c") >= lo) & (pl.col("entry_c") < hi))
        if b.height >= 3:
            n = b.height
            wins = b.filter(pl.col("result") == "WIN").height
            pb_data.append(("{}-{}".format(lo, hi), wins / n * 100, b["pnl_taker"].sum(), n))
    if pb_data:
        labels, wrs, pnls, counts = zip(*pb_data)
        colors_wr = ["#2ecc71" if w >= 65 else "#f39c12" if w >= 55 else "#e74c3c" for w in wrs]
        colors_pnl = ["#2ecc71" if p >= 0 else "#e74c3c" for p in pnls]
        ax1.bar(labels, wrs, color=colors_wr)
        ax1.axhline(50, color="red", linewidth=0.5, linestyle="--")
        ax1.set_ylabel("Win Rate (%)")
        ax1.set_title("Win Rate by Entry Price")
        ax1.set_xlabel("Entry Price (cents)")
        ax2.bar(labels, pnls, color=colors_pnl)
        ax2.axhline(0, color="gray", linewidth=0.5)
        ax2.set_ylabel("Total PnL ($)")
        ax2.set_title("PnL by Entry Price")
        ax2.set_xlabel("Entry Price (cents)")
    plt.tight_layout()
    fig2.savefig(OUTPUT_DIR / "paper_entry_analysis.png", dpi=150)
    plt.close(fig2)
    print("  Saved output/paper_entry_analysis.png")

    # Chart 3: Time remaining — win rate and PnL
    fig3, (ax3, ax4) = plt.subplots(1, 2, figsize=(14, 5))
    tb_data = []
    for lo, hi in [(240, 300), (180, 240), (120, 180), (60, 120), (0, 60)]:
        b = s.filter((pl.col("time_remaining") >= lo) & (pl.col("time_remaining") < hi))
        if b.height >= 3:
            n = b.height
            wins = b.filter(pl.col("result") == "WIN").height
            tb_data.append(("{}-{}".format(lo, hi), wins / n * 100, b["pnl_taker"].sum(), n))
    if tb_data:
        labels, wrs, pnls, counts = zip(*tb_data)
        colors_wr = ["#2ecc71" if w >= 65 else "#f39c12" if w >= 55 else "#e74c3c" for w in wrs]
        colors_pnl = ["#2ecc71" if p >= 0 else "#e74c3c" for p in pnls]
        ax3.bar(labels, wrs, color=colors_wr)
        ax3.axhline(50, color="red", linewidth=0.5, linestyle="--")
        ax3.set_ylabel("Win Rate (%)")
        ax3.set_title("Win Rate by Time Remaining")
        ax4.bar(labels, pnls, color=colors_pnl)
        ax4.axhline(0, color="gray", linewidth=0.5)
        ax4.set_ylabel("Total PnL ($)")
        ax4.set_title("PnL by Time Remaining")
    plt.tight_layout()
    fig3.savefig(OUTPUT_DIR / "paper_time_analysis.png", dpi=150)
    plt.close(fig3)
    print("  Saved output/paper_time_analysis.png")

    # Chart 4: Per-window PnL bar chart
    fig4, ax5 = plt.subplots(figsize=(16, 5))
    wpnls = [r[4] for r in win_results]
    colors_w = ["#2ecc71" if p >= 0 else "#e74c3c" for p in wpnls]
    ax5.bar(range(len(wpnls)), wpnls, color=colors_w, width=0.8)
    ax5.axhline(0, color="gray", linewidth=0.5)
    ax5.set_xlabel("Window #")
    ax5.set_ylabel("PnL ($)")
    ax5.set_title("PnL per Window (all combos combined)")
    ax5.grid(True, alpha=0.2, axis="y")
    plt.tight_layout()
    fig4.savefig(OUTPUT_DIR / "paper_window_pnl.png", dpi=150)
    plt.close(fig4)
    print("  Saved output/paper_window_pnl.png")

    # Chart 5: Rolling win rate (all trades combined)
    all_wins = (s.sort("timestamp")["result"] == "WIN").to_list()
    if len(all_wins) >= 50:
        wins_arr = np.array([1.0 if w else 0.0 for w in all_wins])
        window_size = min(50, len(wins_arr) // 3)
        if window_size >= 10:
            rolling = np.convolve(wins_arr, np.ones(window_size) / window_size, mode="valid")
            fig5, ax6 = plt.subplots(figsize=(14, 5))
            ax6.plot(rolling * 100, linewidth=1, color="steelblue")
            ax6.axhline(50, color="red", linewidth=0.5, linestyle="--", label="50%")
            ax6.axhline(65, color="green", linewidth=0.5, linestyle="--", label="65%")
            ax6.set_xlabel("Trade #")
            ax6.set_ylabel("Win Rate (%)")
            ax6.set_title("Rolling {}-Trade Win Rate (All Combos)".format(window_size))
            ax6.legend()
            ax6.grid(True, alpha=0.3)
            ax6.set_ylim(20, 100)
            plt.tight_layout()
            fig5.savefig(OUTPUT_DIR / "paper_rolling_wr.png", dpi=150)
            plt.close(fig5)
            print("  Saved output/paper_rolling_wr.png")

    print("\n{}{}{}".format(M, "=" * 70, RST))
    print("  Run: {}python analyze_paper.py{} after each session".format(BOLD, RST))
    print("{}{}{}".format(M, "=" * 70, RST))


if __name__ == "__main__":
    main()
