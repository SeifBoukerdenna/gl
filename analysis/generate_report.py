"""
Generate a self-contained HTML analysis report.
Includes all charts as embedded base64, tables, and key metrics.

Usage: ANALYZE_ALL=1 python analysis/generate_report.py
       Opens output/report.html in browser.
"""

import csv
import base64
import json
import os
from pathlib import Path
from datetime import datetime


DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")
SKIP = {"_archive_20260403"}


def load_session(name, csv_path):
    """Load a session's trades and compute all stats."""
    trades = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k.strip(): v.strip() if isinstance(v, str) else v for k, v in row.items()}
            if row.get("result") in ("WIN", "LOSS"):
                trades.append(row)

    if not trades:
        return None

    trades.sort(key=lambda x: float(x.get("timestamp", 0) or 0))
    n = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    pnl = sum(float(t.get("pnl_taker", 0) or 0) for t in trades)
    wr = round(wins / n * 100, 1) if n > 0 else 0

    # Cumulative PnL
    cum = []
    running = 0.0
    for t in trades:
        running += float(t.get("pnl_taker", 0) or 0)
        cum.append(round(running, 1))

    # Per-combo
    combos = {}
    for t in trades:
        c = t.get("combo", "?")
        if c not in combos:
            combos[c] = {"n": 0, "wins": 0, "pnl": 0.0, "cum": [], "running": 0.0}
        combos[c]["n"] += 1
        if t["result"] == "WIN":
            combos[c]["wins"] += 1
        combos[c]["pnl"] += float(t.get("pnl_taker", 0) or 0)
        combos[c]["running"] += float(t.get("pnl_taker", 0) or 0)
        combos[c]["cum"].append(round(combos[c]["running"], 1))

    for c in combos:
        cv = combos[c]
        cv["wr"] = round(cv["wins"] / cv["n"] * 100, 1) if cv["n"] > 0 else 0
        cv["avg_pnl"] = round(cv["pnl"] / cv["n"], 1) if cv["n"] > 0 else 0
        cv["pnl"] = round(cv["pnl"])
        del cv["running"]

    # Entry price analysis
    entry_buckets = {}
    for lo, hi in [(20, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 80)]:
        label = "{}-{}c".format(lo, hi)
        bt = [t for t in trades if lo <= float(t.get("fill_price", 0) or 0) * 100 < hi]
        if len(bt) >= 2:
            bw = sum(1 for t in bt if t["result"] == "WIN")
            bp = sum(float(t.get("pnl_taker", 0) or 0) for t in bt)
            entry_buckets[label] = {"n": len(bt), "wr": round(bw / len(bt) * 100, 1), "pnl": round(bp)}

    # Time remaining analysis
    time_buckets = {}
    for lo, hi in [(240, 300), (180, 240), (120, 180), (60, 120), (0, 60)]:
        label = "{}-{}s".format(lo, hi)
        bt = [t for t in trades if lo <= float(t.get("time_remaining", 0) or 0) < hi]
        if len(bt) >= 2:
            bw = sum(1 for t in bt if t["result"] == "WIN")
            bp = sum(float(t.get("pnl_taker", 0) or 0) for t in bt)
            time_buckets[label] = {"n": len(bt), "wr": round(bw / len(bt) * 100, 1), "pnl": round(bp)}

    # Direction analysis
    directions = {}
    for d_name in ["YES", "NO"]:
        dt = [t for t in trades if t.get("direction") == d_name]
        if dt:
            dw = sum(1 for t in dt if t["result"] == "WIN")
            dp = sum(float(t.get("pnl_taker", 0) or 0) for t in dt)
            directions[d_name] = {"n": len(dt), "wr": round(dw / len(dt) * 100, 1), "pnl": round(dp)}

    # Worst/best trades
    sorted_trades = sorted(trades, key=lambda t: float(t.get("pnl_taker", 0) or 0))
    worst = sorted_trades[:10]
    best = sorted_trades[-10:][::-1]

    # Hours since start
    ts = [float(t.get("timestamp", 0) or 0) for t in trades]
    hours = (max(ts) - min(ts)) / 3600 if ts else 0

    return {
        "name": name, "trades": n, "wins": wins, "wr": wr,
        "pnl": round(pnl), "avg_pnl": round(pnl / n, 1) if n > 0 else 0,
        "hours": round(hours, 1), "cum_pnl": cum,
        "combos": combos, "entry_buckets": entry_buckets,
        "time_buckets": time_buckets, "directions": directions,
        "worst": worst, "best": best,
    }


def embed_chart(path):
    """Convert a PNG chart to base64 img tag."""
    if not path.exists():
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode()
    return '<img src="data:image/png;base64,{}" style="width:100%;border-radius:8px;margin:8px 0">'.format(b64)


def wr_color(wr):
    if wr >= 65:
        return "#10b981"
    if wr >= 55:
        return "#f59e0b"
    return "#ef4444"


def pnl_color(v):
    return "#10b981" if v >= 0 else "#ef4444"


def generate_report():
    """Generate full HTML report."""
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
        result = load_session(d.name, csv_path)
        if result:
            sessions.append(result)

    sessions.sort(key=lambda x: x["pnl"], reverse=True)

    if not sessions:
        print("No session data found.")
        return

    total_trades = sum(s["trades"] for s in sessions)
    total_pnl = sum(s["pnl"] for s in sessions)
    total_wins = sum(s["wins"] for s in sessions)
    total_wr = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    # Best combos across all sessions
    all_combos = []
    for s in sessions:
        for cn, cv in s["combos"].items():
            all_combos.append({"session": s["name"], "combo": cn, **cv})
    all_combos.sort(key=lambda x: x["pnl"], reverse=True)

    # Build HTML
    html = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Polymarket Bot — Analysis Report</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#08090d;--card:#111622;--border:#1a2236;--text:#d1d5e0;--dim:#5a6478;--blue:#4d8ef7;--green:#10b981;--red:#ef4444;--yellow:#f59e0b;--purple:#8b5cf6}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);font-size:13px;-webkit-font-smoothing:antialiased;padding:0}
.container{max-width:1200px;margin:0 auto;padding:32px 24px}
.mono{font-family:'JetBrains Mono',monospace}
h1{font-size:24px;font-weight:800;margin-bottom:4px}
h2{font-size:16px;font-weight:700;margin:32px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
h3{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);margin:20px 0 8px}
.subtitle{color:var(--dim);font-size:12px;margin-bottom:24px}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin:16px 0}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
.stat-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);margin-bottom:4px}
.stat-value{font-size:24px;font-weight:800;font-family:'JetBrains Mono',monospace}
.stat-sub{font-size:10px;color:var(--dim);margin-top:2px;font-family:'JetBrains Mono',monospace}
table{width:100%;border-collapse:separate;border-spacing:0;margin:8px 0 16px;background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
th{text-align:left;padding:10px 14px;color:var(--dim);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--border);background:var(--card)}
td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:12px;font-family:'JetBrains Mono',monospace}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(77,142,247,.02)}
.g{color:#10b981}.r{color:#ef4444}.y{color:#f59e0b}.d{color:#5a6478}.b{color:#4d8ef7}
.tag{display:inline-flex;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;font-family:'JetBrains Mono',monospace}
.tag-w{background:rgba(16,185,129,.1);color:#10b981}.tag-l{background:rgba(239,68,68,.1);color:#ef4444}
.tag-s{background:rgba(77,142,247,.1);color:#4d8ef7}
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0}
.chart-full{margin:12px 0}
.session-section{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin:16px 0}
.session-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.rank{font-size:12px;font-weight:700;color:var(--yellow);margin-right:8px}
.badge{background:var(--purple);color:white;font-size:9px;padding:2px 6px;border-radius:3px;font-weight:700;margin-left:6px}
.divider{height:1px;background:var(--border);margin:24px 0}
@media print{body{background:#fff;color:#000}table,th,td,.stat,.session-section{border-color:#ddd}
.g{color:#059669}.r{color:#dc2626}.y{color:#d97706}}
@media(max-width:768px){.chart-grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}
</style>
</head><body>
<div class="container">
"""

    # Header
    html += '<h1>Polymarket Bot Analysis Report</h1>\n'
    html += '<div class="subtitle">Generated {now} &mdash; {n} sessions, {t} trades</div>\n'.format(
        now=now, n=len(sessions), t=total_trades)

    # Summary stats
    html += '<div class="stats">\n'
    html += '  <div class="stat"><div class="stat-label">Total PnL</div><div class="stat-value" style="color:{c}">{v}</div><div class="stat-sub">${avg}/trade</div></div>\n'.format(
        c=pnl_color(total_pnl), v="${:+,}".format(total_pnl), avg=round(total_pnl / total_trades, 1) if total_trades else 0)
    html += '  <div class="stat"><div class="stat-label">Total Trades</div><div class="stat-value" style="color:var(--blue)">{}</div><div class="stat-sub">{} sessions</div></div>\n'.format(
        total_trades, len(sessions))
    html += '  <div class="stat"><div class="stat-label">Win Rate</div><div class="stat-value" style="color:{c}">{wr}%</div><div class="stat-sub">{w}W / {l}L</div></div>\n'.format(
        c=wr_color(total_wr), wr=total_wr, w=total_wins, l=total_trades - total_wins)
    html += '</div>\n'

    # Leaderboard
    html += '<h2>Session Leaderboard</h2>\n'
    html += '<table><tr><th>#</th><th>Session</th><th>Trades</th><th>Win%</th><th>PnL</th><th>$/Trade</th><th>Hours</th></tr>\n'
    for i, s in enumerate(sessions):
        html += '<tr><td style="font-weight:700;color:{rc}">#{r}</td><td style="font-weight:700">{name}</td><td>{t}</td><td style="color:{wc}">{wr}%</td><td style="color:{pc};font-weight:700">${pnl:+,}</td><td style="color:{ac}">${avg}</td><td class="d">{h}h</td></tr>\n'.format(
            r=i + 1, rc="#f59e0b" if i < 3 else "#5a6478", name=s["name"], t=s["trades"],
            wc=wr_color(s["wr"]), wr=s["wr"], pc=pnl_color(s["pnl"]), pnl=s["pnl"],
            ac=pnl_color(s["avg_pnl"]), avg=s["avg_pnl"], h=s["hours"])
    html += '</table>\n'

    # Comparison charts
    html += '<h2>Cross-Session Comparison</h2>\n'
    comp_dir = OUTPUT_DIR / "comparison"
    for chart_name in ["all_sessions_cum_pnl.png", "session_leaderboard.png",
                       "avg_pnl_per_trade.png", "combo_scatter.png", "time_aligned_pnl.png"]:
        chart = embed_chart(comp_dir / chart_name)
        if chart:
            html += '<div class="chart-full">{}</div>\n'.format(chart)

    # Best combos
    html += '<h2>Best Combos Across All Sessions</h2>\n'
    html += '<table><tr><th>Session</th><th>Combo</th><th>Trades</th><th>Win%</th><th>PnL</th><th>$/Trade</th></tr>\n'
    for c in all_combos[:25]:
        html += '<tr><td><span class="tag tag-s">{sess}</span></td><td style="font-weight:600">{combo}</td><td>{n}</td><td style="color:{wc}">{wr}%</td><td style="color:{pc};font-weight:700">${pnl:+,}</td><td style="color:{ac}">${avg}</td></tr>\n'.format(
            sess=c["session"], combo=c["combo"], n=c["n"],
            wc=wr_color(c["wr"]), wr=c["wr"], pc=pnl_color(c["pnl"]), pnl=c["pnl"],
            ac=pnl_color(c["avg_pnl"]), avg=c["avg_pnl"])
    html += '</table>\n'

    # Per-session details
    html += '<h2>Per-Session Breakdown</h2>\n'
    for i, s in enumerate(sessions):
        html += '<div class="session-section">\n'
        html += '  <div class="session-header"><div><span class="rank">#{}</span><strong style="font-size:16px">{}</strong><span class="badge">{} trades</span></div><div style="font-size:18px;font-weight:800;color:{}">${:+,}</div></div>\n'.format(
            i + 1, s["name"], s["trades"], pnl_color(s["pnl"]), s["pnl"])

        # Stats row
        html += '  <div class="stats">\n'
        html += '    <div class="stat"><div class="stat-label">Win Rate</div><div class="stat-value" style="color:{c}">{wr}%</div></div>\n'.format(
            c=wr_color(s["wr"]), wr=s["wr"])
        html += '    <div class="stat"><div class="stat-label">$/Trade</div><div class="stat-value" style="color:{c}">${v}</div></div>\n'.format(
            c=pnl_color(s["avg_pnl"]), v=s["avg_pnl"])
        html += '    <div class="stat"><div class="stat-label">Duration</div><div class="stat-value" style="color:var(--dim)">{h}h</div></div>\n'.format(
            h=s["hours"])
        html += '  </div>\n'

        # Combo table
        html += '  <h3>Combo Performance</h3>\n'
        html += '  <table><tr><th>Combo</th><th>Trades</th><th>Wins</th><th>Win%</th><th>PnL</th><th>$/Trade</th></tr>\n'
        for cn, cv in sorted(s["combos"].items(), key=lambda x: x[1]["pnl"], reverse=True):
            html += '  <tr><td style="font-weight:600">{}</td><td>{}</td><td>{}</td><td style="color:{}">{wr}%</td><td style="color:{};font-weight:700">${pnl:+,}</td><td style="color:{}">${avg}</td></tr>\n'.format(
                cn, cv["n"], cv["wins"], wr_color(cv["wr"]), pnl_color(cv["pnl"]), pnl_color(cv["avg_pnl"]),
                wr=cv["wr"], pnl=cv["pnl"], avg=cv["avg_pnl"])
        html += '  </table>\n'

        # Entry price + Time remaining
        if s["entry_buckets"]:
            html += '  <h3>Entry Price Analysis</h3>\n'
            html += '  <table><tr><th>Entry</th><th>Trades</th><th>Win%</th><th>PnL</th></tr>\n'
            for label, b in s["entry_buckets"].items():
                html += '  <tr><td>{}</td><td>{}</td><td style="color:{}">{wr}%</td><td style="color:{};font-weight:700">${pnl:+,}</td></tr>\n'.format(
                    label, b["n"], wr_color(b["wr"]), pnl_color(b["pnl"]), wr=b["wr"], pnl=b["pnl"])
            html += '  </table>\n'

        if s["time_buckets"]:
            html += '  <h3>Time Remaining Analysis</h3>\n'
            html += '  <table><tr><th>Time</th><th>Trades</th><th>Win%</th><th>PnL</th></tr>\n'
            for label, b in s["time_buckets"].items():
                html += '  <tr><td>T-{}</td><td>{}</td><td style="color:{}">{wr}%</td><td style="color:{};font-weight:700">${pnl:+,}</td></tr>\n'.format(
                    label, b["n"], wr_color(b["wr"]), pnl_color(b["pnl"]), wr=b["wr"], pnl=b["pnl"])
            html += '  </table>\n'

        # Direction
        if s["directions"]:
            html += '  <h3>Direction Analysis</h3>\n'
            html += '  <table><tr><th>Direction</th><th>Trades</th><th>Win%</th><th>PnL</th></tr>\n'
            for dn, dv in s["directions"].items():
                label = "BUY UP (YES)" if dn == "YES" else "BUY DOWN (NO)"
                html += '  <tr><td>{}</td><td>{}</td><td style="color:{}">{wr}%</td><td style="color:{};font-weight:700">${pnl:+,}</td></tr>\n'.format(
                    label, dv["n"], wr_color(dv["wr"]), pnl_color(dv["pnl"]), wr=dv["wr"], pnl=dv["pnl"])
            html += '  </table>\n'

        # Worst/Best trades
        for label, trades_list, cls in [("Worst Trades", s["worst"], "r"), ("Best Trades", s["best"], "g")]:
            if trades_list:
                html += '  <h3>{}</h3>\n'.format(label)
                html += '  <table><tr><th>Combo</th><th>Dir</th><th>Entry</th><th>Size</th><th>PnL</th><th>Impulse</th><th>T-rem</th><th>Result</th></tr>\n'
                for t in trades_list:
                    p = float(t.get("pnl_taker", 0) or 0)
                    entry = "{:.0f}c".format(float(t.get("fill_price", 0) or 0) * 100)
                    imp = "{:+.0f}bp".format(float(t.get("impulse_bps", 0) or 0))
                    trem = "{:.0f}s".format(float(t.get("time_remaining", 0) or 0))
                    res = t.get("result", "")
                    res_tag = '<span class="tag tag-w">WIN</span>' if res == "WIN" else '<span class="tag tag-l">LOSS</span>'
                    html += '  <tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td style="color:{};font-weight:700">${:+,.0f}</td><td>{}</td><td>{}</td><td>{}</td></tr>\n'.format(
                        t.get("combo", "?"), t.get("direction", "?"), entry,
                        t.get("filled_size", "?"), pnl_color(p), p, imp, trem, res_tag)
                html += '  </table>\n'

        # Charts
        session_chart_dir = OUTPUT_DIR / s["name"]
        charts = ["cum_pnl.png", "entry_analysis.png", "time_analysis.png", "window_pnl.png", "rolling_wr.png"]
        rendered = [embed_chart(session_chart_dir / c) for c in charts if (session_chart_dir / c).exists()]
        if rendered:
            html += '  <h3>Charts</h3>\n'
            html += '  <div class="chart-grid">\n'
            for c in rendered:
                html += '    {}\n'.format(c)
            html += '  </div>\n'

        html += '</div>\n'

    # Footer
    html += """
<div class="divider"></div>
<div class="d" style="text-align:center;padding:16px;font-size:11px">
    Generated by Polymarket Bot Analysis &mdash; {now}
</div>
</div></body></html>""".format(now=now)

    # Write report
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / "report.html"
    report_path.write_text(html)
    print("\n  Report saved: {}".format(report_path))
    return report_path


if __name__ == "__main__":
    path = generate_report()
    if path:
        import subprocess
        subprocess.run(["open", str(path)], capture_output=True)
