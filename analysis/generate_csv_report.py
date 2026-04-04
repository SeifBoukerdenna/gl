"""
Generate CSV/Excel analysis report for LLM consumption.
Outputs multiple CSV files in output/report/ that any LLM can parse.

Usage: python analysis/generate_csv_report.py
"""

import csv
import os
from pathlib import Path
from datetime import datetime

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output/report")
SKIP = {"_archive_20260403"}


def load_trades(name, csv_path):
    trades = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            if row.get("result") in ("WIN", "LOSS"):
                row["_session"] = name
                trades.append(row)
    trades.sort(key=lambda x: float(x.get("timestamp", 0) or 0))
    return trades


def generate():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades = []
    sessions = []

    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or d.name in SKIP or d.name.startswith("_"):
            continue
        csv_path = None
        for fname in ["trades.csv", "paper_trades_v2.csv"]:
            p = d / fname
            if p.exists():
                csv_path = p
                break
        if not csv_path:
            continue
        trades = load_trades(d.name, csv_path)
        if not trades:
            continue
        all_trades.extend(trades)

        n = len(trades)
        wins = sum(1 for t in trades if t["result"] == "WIN")
        pnl = sum(float(t.get("pnl_taker", 0) or 0) for t in trades)
        ts = [float(t.get("timestamp", 0) or 0) for t in trades]
        hours = (max(ts) - min(ts)) / 3600 if ts else 0

        # Detect architecture from first trade
        arch = trades[0].get("architecture", "impulse_lag") if trades else "impulse_lag"
        sessions.append({
            "session": d.name, "architecture": arch,
            "trades": n, "wins": wins, "losses": n - wins,
            "win_rate": round(wins / n * 100, 1), "pnl_taker": round(pnl, 2),
            "avg_pnl_per_trade": round(pnl / n, 2), "hours": round(hours, 1),
        })

    sessions.sort(key=lambda x: x["pnl_taker"], reverse=True)

    # 1. Session leaderboard
    with open(OUTPUT_DIR / "01_session_leaderboard.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["rank", "session", "architecture", "trades", "wins", "losses",
                                          "win_rate", "pnl_taker", "avg_pnl_per_trade", "hours"])
        w.writeheader()
        for i, s in enumerate(sessions):
            w.writerow({"rank": i + 1, **s})

    # 2. Per-session combo breakdown
    combo_rows = []
    for s_name in [s["session"] for s in sessions]:
        s_trades = [t for t in all_trades if t["_session"] == s_name]
        combos = {}
        for t in s_trades:
            c = t.get("combo", "?")
            if c not in combos:
                combos[c] = {"n": 0, "wins": 0, "pnl": 0.0}
            combos[c]["n"] += 1
            if t["result"] == "WIN":
                combos[c]["wins"] += 1
            combos[c]["pnl"] += float(t.get("pnl_taker", 0) or 0)
        for cn, cv in sorted(combos.items(), key=lambda x: x[1]["pnl"], reverse=True):
            combo_rows.append({
                "session": s_name, "combo": cn, "trades": cv["n"], "wins": cv["wins"],
                "losses": cv["n"] - cv["wins"],
                "win_rate": round(cv["wins"] / cv["n"] * 100, 1) if cv["n"] > 0 else 0,
                "pnl_taker": round(cv["pnl"], 2),
                "avg_pnl_per_trade": round(cv["pnl"] / cv["n"], 2) if cv["n"] > 0 else 0,
            })

    with open(OUTPUT_DIR / "02_combo_breakdown.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["session", "combo", "trades", "wins", "losses",
                                          "win_rate", "pnl_taker", "avg_pnl_per_trade"])
        w.writeheader()
        w.writerows(combo_rows)

    # 3. Best combos across all sessions (sorted by avg pnl)
    best = sorted(combo_rows, key=lambda x: x["avg_pnl_per_trade"], reverse=True)
    with open(OUTPUT_DIR / "03_best_combos.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["session", "combo", "trades", "wins", "losses",
                                          "win_rate", "pnl_taker", "avg_pnl_per_trade"])
        w.writeheader()
        w.writerows(best[:30])

    # 4. Entry price analysis (all sessions combined)
    entry_rows = []
    for lo, hi in [(20, 25), (25, 30), (30, 35), (35, 40), (40, 45),
                    (45, 50), (50, 55), (55, 60), (60, 65), (65, 70), (70, 80)]:
        bt = [t for t in all_trades if lo <= float(t.get("fill_price", 0) or 0) * 100 < hi]
        if len(bt) >= 3:
            bw = sum(1 for t in bt if t["result"] == "WIN")
            bp = sum(float(t.get("pnl_taker", 0) or 0) for t in bt)
            entry_rows.append({
                "entry_range": "{}-{}c".format(lo, hi), "trades": len(bt),
                "wins": bw, "losses": len(bt) - bw,
                "win_rate": round(bw / len(bt) * 100, 1),
                "total_pnl": round(bp, 2), "avg_pnl": round(bp / len(bt), 2),
            })

    with open(OUTPUT_DIR / "04_entry_price_analysis.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["entry_range", "trades", "wins", "losses",
                                          "win_rate", "total_pnl", "avg_pnl"])
        w.writeheader()
        w.writerows(entry_rows)

    # 5. Time remaining analysis
    time_rows = []
    for lo, hi in [(270, 300), (240, 270), (210, 240), (180, 210), (150, 180),
                    (120, 150), (90, 120), (60, 90), (30, 60), (0, 30)]:
        bt = [t for t in all_trades if lo <= float(t.get("time_remaining", 0) or 0) < hi]
        if len(bt) >= 3:
            bw = sum(1 for t in bt if t["result"] == "WIN")
            bp = sum(float(t.get("pnl_taker", 0) or 0) for t in bt)
            time_rows.append({
                "time_range": "T-{}-{}s".format(lo, hi), "trades": len(bt),
                "wins": bw, "losses": len(bt) - bw,
                "win_rate": round(bw / len(bt) * 100, 1),
                "total_pnl": round(bp, 2), "avg_pnl": round(bp / len(bt), 2),
            })

    with open(OUTPUT_DIR / "05_time_remaining_analysis.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time_range", "trades", "wins", "losses",
                                          "win_rate", "total_pnl", "avg_pnl"])
        w.writeheader()
        w.writerows(time_rows)

    # 6. Direction analysis per session
    dir_rows = []
    for s_name in [s["session"] for s in sessions]:
        s_trades = [t for t in all_trades if t["_session"] == s_name]
        for d_name, label in [("YES", "BUY_UP"), ("NO", "BUY_DOWN")]:
            dt = [t for t in s_trades if t.get("direction") == d_name]
            if dt:
                dw = sum(1 for t in dt if t["result"] == "WIN")
                dp = sum(float(t.get("pnl_taker", 0) or 0) for t in dt)
                dir_rows.append({
                    "session": s_name, "direction": label, "trades": len(dt),
                    "wins": dw, "win_rate": round(dw / len(dt) * 100, 1),
                    "pnl_taker": round(dp, 2),
                })

    with open(OUTPUT_DIR / "06_direction_analysis.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["session", "direction", "trades", "wins",
                                          "win_rate", "pnl_taker"])
        w.writeheader()
        w.writerows(dir_rows)

    # 7. Impulse analysis
    imp_rows = []
    for lo, hi in [(0, 3), (3, 5), (5, 7), (7, 10), (10, 15), (15, 20), (20, 30), (30, 200)]:
        bt = [t for t in all_trades if lo <= abs(float(t.get("impulse_bps", 0) or 0)) < hi]
        if len(bt) >= 3:
            bw = sum(1 for t in bt if t["result"] == "WIN")
            bp = sum(float(t.get("pnl_taker", 0) or 0) for t in bt)
            label = "{}+bp".format(lo) if hi >= 200 else "{}-{}bp".format(lo, hi)
            imp_rows.append({
                "impulse_range": label, "trades": len(bt),
                "wins": bw, "losses": len(bt) - bw,
                "win_rate": round(bw / len(bt) * 100, 1),
                "total_pnl": round(bp, 2), "avg_pnl": round(bp / len(bt), 2),
            })

    with open(OUTPUT_DIR / "07_impulse_analysis.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["impulse_range", "trades", "wins", "losses",
                                          "win_rate", "total_pnl", "avg_pnl"])
        w.writeheader()
        w.writerows(imp_rows)

    # 8. All trades raw (for LLM deep analysis)
    trade_fields = ["_session", "architecture", "timestamp", "window_start", "combo", "direction",
                    "impulse_bps", "time_remaining", "fill_price", "filled_size",
                    "slippage", "fee", "effective_entry_taker", "notional",
                    "total_fee", "total_slippage", "total_cost",
                    "btc_price", "delta_bps", "outcome", "pnl_taker", "result"]
    with open(OUTPUT_DIR / "08_all_trades_raw.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=trade_fields, extrasaction="ignore")
        w.writeheader()
        for t in sorted(all_trades, key=lambda x: float(x.get("timestamp", 0) or 0), reverse=True):
            w.writerow(t)

    # 9. Summary text for LLM context
    with open(OUTPUT_DIR / "00_summary.txt", "w") as f:
        f.write("POLYMARKET BOT PAPER TRADING ANALYSIS\n")
        f.write("Generated: {}\n\n".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
        f.write("STRATEGY: Exploit price lag between Binance BTC/USDT and Polymarket 5-min BTC Up/Down markets.\n")
        f.write("When Binance BTC moves sharply (impulse), buy the direction on PM before PM reprices.\n")
        f.write("Settlement: PM book mid > 0.50 at window close = Up, else Down.\n")
        f.write("Each combo = different impulse threshold + lookback window (e.g. A_5bp_30s = 5bp impulse in 30s).\n\n")
        f.write("SESSIONS: {} configs tested with different entry price ranges, impulse caps, dead zones.\n".format(len(sessions)))
        f.write("TOTAL: {} trades, {:.1f}% win rate, ${:+,.0f} PnL\n\n".format(
            sum(s["trades"] for s in sessions),
            sum(s["wins"] for s in sessions) / max(1, sum(s["trades"] for s in sessions)) * 100,
            sum(s["pnl_taker"] for s in sessions)))
        f.write("FILES IN THIS REPORT:\n")
        f.write("  00_summary.txt              - This file (context for LLM)\n")
        f.write("  01_session_leaderboard.csv  - All sessions ranked by PnL\n")
        f.write("  02_combo_breakdown.csv      - Per-session combo performance\n")
        f.write("  03_best_combos.csv          - Top 30 combos by avg $/trade\n")
        f.write("  04_entry_price_analysis.csv - Win rate and PnL by entry price bucket\n")
        f.write("  05_time_remaining_analysis.csv - Win rate and PnL by time remaining\n")
        f.write("  06_direction_analysis.csv   - BUY_UP vs BUY_DOWN per session\n")
        f.write("  07_impulse_analysis.csv     - Win rate and PnL by impulse size\n")
        f.write("  08_all_trades_raw.csv       - Every trade with full details\n\n")
        f.write("KEY QUESTIONS FOR ANALYSIS:\n")
        f.write("  1. Which session config performs best risk-adjusted?\n")
        f.write("  2. Which combos are consistently profitable across sessions?\n")
        f.write("  3. What entry price range gives the best edge?\n")
        f.write("  4. Is there a time-of-window sweet spot?\n")
        f.write("  5. Are NO (DOWN) trades more profitable than YES (UP)?\n")
        f.write("  6. What impulse threshold maximizes expected value?\n")
        f.write("  7. What would the optimal config be based on all data?\n")

    # Print summary
    print("\n  CSV report generated in output/report/")
    print("  Files:")
    for f in sorted(OUTPUT_DIR.glob("*")):
        lines = sum(1 for _ in open(f)) if f.suffix == ".csv" else 0
        size = f.stat().st_size
        print("    {:40s} {:>6} {:>8}".format(
            f.name, "{} rows".format(lines - 1) if lines else "", "{:.0f}KB".format(size / 1024)))
    return OUTPUT_DIR


if __name__ == "__main__":
    generate()
