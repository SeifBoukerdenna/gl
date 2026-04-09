"""
Polymarket Bot Dashboard — Real-time WebSocket-powered UI
localhost:5555

VPS data streams in via WebSocket relay (port 8765).
Frontend receives Server-Sent Events (SSE) for real-time updates.
No polling, no SSH, no scp for live data.
"""

import asyncio
import csv
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime

from flask import Flask, Response, render_template_string, jsonify, request, redirect, flash, send_file

app = Flask(__name__)
app.secret_key = "pm-bot-dash"

CONFIGS_DIR = Path("configs")
DATA_DIR = Path("data")
VPS_HOST = "167.172.50.38"
VPS_WS_PORT = 8765

# ══════════════════════════════════════════════════════════════
# Real-time VPS cache — updated via WebSocket relay
# ══════════════════════════════════════════════════════════════
_vps_cache = {}  # name -> stats dict
_vps_cache_lock = threading.Lock()
_vps_connected = [False]
_sse_subscribers = []  # list of queue.Queue for SSE clients


def _push_sse(data):
    """Push data to all SSE subscribers."""
    msg = json.dumps(data)
    dead = []
    for q in _sse_subscribers:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    for q in dead:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass


def _bg_vps_ws():
    """Background thread: connect to VPS WebSocket relay for real-time stats."""
    import websockets.sync.client as ws_client

    while True:
        try:
            url = "ws://{}:{}".format(VPS_HOST, VPS_WS_PORT)
            with ws_client.connect(url, open_timeout=5, close_timeout=3) as ws:
                _vps_connected[0] = True
                print("[ws] Connected to VPS relay at {}".format(url))

                while True:
                    try:
                        raw = ws.recv(timeout=10)
                    except TimeoutError:
                        # Send ping to keep alive
                        ws.send(json.dumps({"type": "ping"}))
                        continue

                    data = json.loads(raw)
                    msg_type = data.get("type")

                    if msg_type == "full_state":
                        with _vps_cache_lock:
                            _vps_cache.clear()
                            _vps_cache.update(data.get("sessions", {}))
                        _push_sse({"type": "full_state", "sessions": data.get("sessions", {})})

                    elif msg_type == "update":
                        changed = data.get("changed", {})
                        removed = data.get("removed", [])
                        with _vps_cache_lock:
                            _vps_cache.update(changed)
                            for name in removed:
                                _vps_cache.pop(name, None)
                        if changed or removed:
                            _push_sse({"type": "update", "changed": changed, "removed": removed})

                    elif msg_type == "pong":
                        pass

                    elif msg_type == "trades":
                        _push_sse(data)

        except Exception as e:
            _vps_connected[0] = False
            print("[ws] VPS relay disconnected: {}. Retrying in 3s...".format(e))
        time.sleep(3)


# Start WebSocket client thread
_ws_thread = threading.Thread(target=_bg_vps_ws, daemon=True)
_ws_thread.start()

KNOB_DEFS = {
    "BASE_TRADE_DOLLARS": {"default": 100, "desc": "$ per trade", "min": 1, "max": 10000, "step": 1},
    "MAX_SHARES": {"default": 500, "desc": "Max shares", "min": 5, "max": 10000, "step": 1},
    "MAX_RISK_PCT": {"default": 0.10, "desc": "Max risk %", "min": 0.01, "max": 1.0, "step": 0.01},
    "STARTING_BANKROLL": {"default": 1000, "desc": "Starting $", "min": 10, "max": 1000000, "step": 1},
    "MIN_ENTRY_PRICE": {"default": 0.20, "desc": "Min entry", "min": 0.01, "max": 0.99, "step": 0.01},
    "MAX_ENTRY_PRICE": {"default": 0.80, "desc": "Max entry", "min": 0.01, "max": 0.99, "step": 0.01},
    "DOWN_MIN_ENTRY": {"default": 0.25, "desc": "DOWN floor", "min": 0.01, "max": 0.99, "step": 0.01},
    "MAX_IMPULSE_BP": {"default": 25, "desc": "Max impulse bp", "min": 1, "max": 200, "step": 1},
    "DEAD_ZONE_START": {"default": 90, "desc": "Dead zone start (s)", "min": 0, "max": 300, "step": 1},
    "DEAD_ZONE_END": {"default": 210, "desc": "Dead zone end (s)", "min": 0, "max": 300, "step": 1},
    "COOLDOWN_RANGE_BP": {"default": 50, "desc": "Cooldown range bp", "min": 5, "max": 500, "step": 1},
    "COOLDOWN_DURATION": {"default": 120, "desc": "Cooldown sec", "min": 0, "max": 3600, "step": 1},
}


def validate_config(config):
    errors = []
    for key, meta in KNOB_DEFS.items():
        if key not in config:
            continue
        val = config[key]
        if not isinstance(val, (int, float)):
            errors.append("{}: must be a number".format(key))
            continue
        if val < meta["min"] or val > meta["max"]:
            errors.append("{}: {} outside {}-{}".format(key, val, meta["min"], meta["max"]))
    me = config.get("MIN_ENTRY_PRICE", 0.2)
    mx = config.get("MAX_ENTRY_PRICE", 0.8)
    if me >= mx:
        errors.append("MIN_ENTRY must be < MAX_ENTRY")
    return errors


def ssh_cmd(cmd):
    try:
        r = subprocess.run(["ssh", "-q", "-o", "ConnectTimeout=3",
                            "root@167.172.50.38", cmd],
                           capture_output=True, text=True, timeout=10)
        return r.stdout
    except Exception:
        return ""


def get_session_data(name):
    stats_path = DATA_DIR / name / "stats.json"
    if stats_path.exists():
        try:
            return json.loads(stats_path.read_text())
        except Exception:
            pass
    return {"trades": 0, "wins": 0, "wr": 0, "pnl_taker": 0, "combos": {}, "recent_trades": []}


def get_csv_stats(name):
    """Read trade stats from the CSV file (source of truth for historical data)."""
    csv_path = None
    for fname in ["trades.csv", "paper_trades_v2.csv"]:
        p = DATA_DIR / name / fname
        if p.exists():
            csv_path = p
            break
    if not csv_path:
        return None
    try:
        trades = 0
        wins = 0
        pnl = 0.0
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                result = (row.get("result") or "").strip()
                pnl_val = row.get("pnl_taker", "")
                if result in ("WIN", "LOSS"):
                    trades += 1
                    if result == "WIN":
                        wins += 1
                    try:
                        pnl += float(pnl_val)
                    except (ValueError, TypeError):
                        pass
        wr = round(wins / trades * 100, 1) if trades > 0 else 0
        return {"trades": trades, "wins": wins, "wr": wr, "pnl_taker": round(pnl)}
    except Exception:
        return None


def _get_session_architecture(name):
    """Get architecture for a session from cache, stats.json, or config file."""
    # 1. VPS cache (fastest)
    with _vps_cache_lock:
        cached = _vps_cache.get(name)
    if cached and cached.get("architecture"):
        return cached["architecture"]
    # 2. Local stats.json
    stats_path = DATA_DIR / name / "stats.json"
    if stats_path.exists():
        try:
            d = json.loads(stats_path.read_text())
            if d.get("architecture"):
                return d["architecture"]
        except Exception:
            pass
    # 3. Config file
    cfg_path = CONFIGS_DIR / "{}.json".format(name)
    if cfg_path.exists():
        try:
            d = json.loads(cfg_path.read_text())
            return d.get("ARCHITECTURE", "impulse_lag")
        except Exception:
            pass
    return "impulse_lag"


def get_all_sessions():
    """Get sessions with status from VPS + stats from CSV (source of truth)."""
    sessions = []
    seen = set()

    # Get VPS running status
    vps_status = {}
    raw = ssh_cmd("""
        for svc in $(systemctl list-units 'polymarket-bot@*' 'polymarket-mr@*' --no-pager --no-legend 2>/dev/null | awk '{print $1}'); do
            INST=$(echo $svc | sed 's/polymarket-bot@//;s/polymarket-mr@//;s/\\.service//')
            ST=$(systemctl is-active $svc 2>/dev/null)
            echo "${INST}|${ST}"
        done
    """)
    for line in raw.strip().split("\n"):
        if "|" in line:
            p = line.split("|")
            if len(p) >= 2:
                vps_status[p[0]] = p[1]

    # Check local running
    local_running = set()
    try:
        result = subprocess.run(["pgrep", "-lf", "paper_trade_v2.py"], capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            if not line.strip() or "pgrep" in line or "bash" in line:
                continue
            parts = line.split(None, 1)
            cmd = parts[1] if len(parts) > 1 else ""
            if "paper_trade_v2.py" not in cmd:
                continue
            name = cmd.split("--instance")[-1].strip().split()[0] if "--instance" in cmd else "default"
            local_running.add(name)
    except Exception:
        pass

    # Build session list from CSV data (source of truth)
    skip = {"_archive_20260403"}
    if DATA_DIR.exists():
        for d in sorted(DATA_DIR.iterdir()):
            if not d.is_dir() or d.name in skip or d.name.startswith("_"):
                continue
            csv_stats = get_csv_stats(d.name)
            if csv_stats is None:
                continue

            name = d.name
            if name in local_running:
                status = "running"
                where = "local"
            elif name in vps_status:
                status = vps_status[name]
                where = "vps"
            else:
                status = "stopped"
                where = "vps"

            # Get architecture from stats.json cache, local stats.json, or config
            arch = _get_session_architecture(name)

            sessions.append({
                "name": name, "where": where, "status": status,
                "architecture": arch,
                "trades": csv_stats["trades"], "wins": csv_stats["wins"],
                "wr": csv_stats["wr"], "pnl_taker": csv_stats["pnl_taker"],
            })
            seen.add(name)

    # Add VPS sessions that don't have local CSV data yet
    for name, st in vps_status.items():
        if name not in seen:
            arch = _get_session_architecture(name)
            sessions.append({
                "name": name, "where": "vps", "status": st,
                "architecture": arch,
                "trades": 0, "wins": 0, "wr": 0, "pnl_taker": 0,
            })
            seen.add(name)

    # Add local running sessions that don't have CSV yet (just started)
    for name in local_running:
        if name not in seen:
            sessions.append({
                "name": name, "where": "local", "status": "running",
                "architecture": _get_session_architecture(name),
                "trades": 0, "wins": 0, "wr": 0, "pnl_taker": 0,
            })

    return sessions


def get_all_trades():
    """Load all trades from all session CSVs."""
    all_trades = []
    skip = {"_archive_20260403"}
    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or d.name in skip or d.name.startswith("_"):
            continue
        for fname in ["trades.csv", "paper_trades_v2.csv"]:
            csv_path = d / fname
            if csv_path.exists():
                try:
                    with open(csv_path) as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            row["_session"] = d.name
                            all_trades.append(row)
                except Exception:
                    pass
                break
    all_trades.sort(key=lambda x: float(x.get("timestamp", 0) or 0), reverse=True)
    return all_trades


def _compute_risk_metrics(pnls):
    """Compute Sharpe, Sortino, MaxDD from a list of per-trade PnL values."""
    import math
    n = len(pnls)
    if n < 2:
        return {"sharpe": 0, "sortino": 0, "max_dd": 0, "best": 0, "worst": 0,
                "expectancy": 0, "rr": 0, "avg_win": 0, "avg_loss": 0}
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.001
    sharpe = round(mean / std, 2)
    downside = [p for p in pnls if p < 0]
    if downside:
        dd_std = math.sqrt(sum(p ** 2 for p in downside) / len(downside))
        sortino = round(mean / dd_std, 2) if dd_std > 0 else 0
    else:
        sortino = 99.99 if mean > 0 else 0
    max_dd = 0
    cum = 0
    peak = 0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    wr = len(wins) / n
    expectancy = round(wr * avg_win - (1 - wr) * avg_loss, 2)
    rr = round(avg_win / avg_loss, 2) if avg_loss > 0 else 99.99
    return {
        "sharpe": sharpe, "sortino": sortino,
        "max_dd": round(max_dd), "best": round(max(pnls), 1), "worst": round(min(pnls), 1),
        "expectancy": expectancy, "rr": rr,
        "avg_win": round(avg_win, 1), "avg_loss": round(avg_loss, 1),
    }


def get_analysis_data():
    """Build analysis summary from CSV data for the analysis page."""
    skip = {"_archive_20260403"}
    session_summaries = []
    all_combos = []

    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or d.name in skip or d.name.startswith("_"):
            continue
        csv_path = None
        for fname in ["trades.csv", "paper_trades_v2.csv"]:
            p = d / fname
            if p.exists():
                csv_path = p
                break
        if not csv_path:
            continue

        trades = []
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get("result") or "").strip() in ("WIN", "LOSS"):
                        trades.append(row)
        except Exception:
            continue

        if not trades:
            continue

        n = len(trades)
        wins = sum(1 for t in trades if t["result"].strip() == "WIN")
        pnl = sum(float(t.get("pnl_taker", 0) or 0) for t in trades)
        wr = round(wins / n * 100, 1) if n > 0 else 0
        pnl_list = [float(t.get("pnl_taker", 0) or 0) for t in trades]
        risk = _compute_risk_metrics(pnl_list)
        arch = _get_session_architecture(d.name)

        # Combo breakdown
        combos = {}
        for t in trades:
            c = t.get("combo", "?").strip()
            if c not in combos:
                combos[c] = {"n": 0, "wins": 0, "pnl": 0.0}
            combos[c]["n"] += 1
            if t["result"].strip() == "WIN":
                combos[c]["wins"] += 1
            combos[c]["pnl"] += float(t.get("pnl_taker", 0) or 0)

        combo_list = []
        for cn, cv in sorted(combos.items(), key=lambda x: x[1]["pnl"], reverse=True):
            combo_list.append({
                "combo": cn, "n": cv["n"],
                "wr": round(cv["wins"] / cv["n"] * 100, 1) if cv["n"] > 0 else 0,
                "pnl": round(cv["pnl"]),
                "avg_pnl": round(cv["pnl"] / cv["n"], 1) if cv["n"] > 0 else 0,
            })
            all_combos.append({
                "session": d.name, "combo": cn, "n": cv["n"],
                "wr": round(cv["wins"] / cv["n"] * 100, 1) if cv["n"] > 0 else 0,
                "pnl": round(cv["pnl"]),
                "avg_pnl": round(cv["pnl"] / cv["n"], 1) if cv["n"] > 0 else 0,
            })

        # Check chart existence
        has_charts = (Path("output") / d.name / "cum_pnl.png").exists()

        session_summaries.append({
            "name": d.name, "architecture": arch,
            "trades": n, "wins": wins, "wr": wr,
            "pnl": round(pnl), "avg_pnl": round(pnl / n, 1) if n > 0 else 0,
            "sharpe": risk["sharpe"], "sortino": risk["sortino"],
            "max_dd": risk["max_dd"], "best": risk["best"], "worst": risk["worst"],
            "expectancy": risk["expectancy"], "rr": risk["rr"],
            "avg_win": risk["avg_win"], "avg_loss": risk["avg_loss"],
            "combos": combo_list, "has_charts": has_charts,
        })

    session_summaries.sort(key=lambda x: x["pnl"], reverse=True)
    all_combos.sort(key=lambda x: x["pnl"], reverse=True)
    has_comparison = Path("output/comparison/all_sessions_cum_pnl.png").exists()

    # Per-architecture aggregation
    arch_map = {}
    for s in session_summaries:
        a = s["architecture"]
        if a not in arch_map:
            arch_map[a] = {"trades": 0, "wins": 0, "pnl": 0.0, "pnl_list": []}
        arch_map[a]["trades"] += s["trades"]
        arch_map[a]["wins"] += s["wins"]
        arch_map[a]["pnl"] += s["pnl"]
    # Re-read per-trade PnLs for architecture-level risk metrics
    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or d.name in skip or d.name.startswith("_"):
            continue
        arch = _get_session_architecture(d.name)
        if arch not in arch_map:
            continue
        csv_path = None
        for fname in ["trades.csv", "paper_trades_v2.csv"]:
            p = d / fname
            if p.exists():
                csv_path = p
                break
        if not csv_path:
            continue
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get("result") or "").strip() in ("WIN", "LOSS"):
                        arch_map[arch]["pnl_list"].append(float(row.get("pnl_taker", 0) or 0))
        except Exception:
            pass

    arch_summaries = []
    for a, v in arch_map.items():
        n = v["trades"]
        risk = _compute_risk_metrics(v["pnl_list"]) if len(v["pnl_list"]) >= 2 else {"sharpe": 0, "sortino": 0, "max_dd": 0, "best": 0, "worst": 0, "expectancy": 0, "rr": 0, "avg_win": 0, "avg_loss": 0}
        arch_summaries.append({
            "architecture": a, "trades": n,
            "wins": v["wins"],
            "wr": round(v["wins"] / n * 100, 1) if n > 0 else 0,
            "pnl": round(v["pnl"]),
            "avg_pnl": round(v["pnl"] / n, 1) if n > 0 else 0,
            "sharpe": risk["sharpe"], "sortino": risk["sortino"],
            "max_dd": risk["max_dd"],
            "expectancy": risk["expectancy"], "rr": risk["rr"],
            "avg_win": risk["avg_win"], "avg_loss": risk["avg_loss"],
        })
    arch_summaries.sort(key=lambda x: x["sharpe"], reverse=True)

    # A/B test comparisons: group sessions by architecture
    ab_groups = {}
    for s in session_summaries:
        a = s["architecture"]
        if a not in ab_groups:
            ab_groups[a] = []
        ab_groups[a].append(s)

    ab_tests = []
    for arch, group_sessions in ab_groups.items():
        if len(group_sessions) < 2:
            continue
        entries = []
        for s in group_sessions:
            entries.append({
                "name": s["name"], "trades": s["trades"], "wins": s["wins"],
                "wr": s["wr"], "pnl": s["pnl"], "avg_pnl": s["avg_pnl"],
                "sharpe": s["sharpe"], "sortino": s["sortino"], "max_dd": s["max_dd"],
                "combos": s["combos"],
            })
        # Determine winner: highest $/trade among sessions with 30+ trades
        qualified = [e for e in entries if e["trades"] >= 30]
        if len(qualified) >= 2:
            best = max(qualified, key=lambda e: e["avg_pnl"])
            second = sorted(qualified, key=lambda e: e["avg_pnl"], reverse=True)[1]
            wr_diff = abs(best["wr"] - second["wr"])
            if wr_diff < 2.0:
                winner = "inconclusive"
            else:
                winner = best["name"]
        else:
            winner = "insufficient data"
        ab_tests.append({
            "architecture": arch,
            "sessions": entries,
            "winner": winner,
        })
    ab_tests.sort(key=lambda x: len(x["sessions"]), reverse=True)

    return {
        "sessions": session_summaries,
        "architectures": arch_summaries,
        "best_combos": all_combos[:20],
        "has_comparison": has_comparison,
        "ab_tests": ab_tests,
    }


def parse_form_config(form):
    raw = form.get("raw_json", "").strip()
    if raw:
        return json.loads(raw)
    config = {}
    desc = form.get("_description", "").strip()
    if desc:
        config["_description"] = desc
    for key in KNOB_DEFS:
        val = form.get(key, "").strip()
        if val:
            config[key] = float(val) if "." in val else int(val)
    return config


# ══════════════════════════════════════════════════════════════
# THE FRONTEND
# ══════════════════════════════════════════════════════════════
FRONTEND = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Bot</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#08090d;--bg2:#0d1017;--card:#111622;--card2:#161d2a;--border:#1a2236;--border2:#243049;
  --text:#d1d5e0;--dim:#5a6478;--dim2:#3d4555;
  --blue:#4d8ef7;--green:#10b981;--red:#ef4444;--yellow:#f59e0b;--purple:#8b5cf6;--cyan:#06b6d4;
  --green-bg:rgba(16,185,129,.08);--red-bg:rgba(239,68,68,.08);--blue-bg:rgba(77,142,247,.08);
  --green-border:rgba(16,185,129,.2);--red-border:rgba(239,68,68,.2);--blue-border:rgba(77,142,247,.2);
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px;-webkit-font-smoothing:antialiased}
a{color:var(--blue);text-decoration:none}
.mono{font-family:'JetBrains Mono',monospace}

.shell{display:flex;flex-direction:column;height:100vh}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:48px;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0}
.topbar-left{display:flex;align-items:center;gap:20px}
.logo{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:14px;background:linear-gradient(135deg,var(--blue),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.5px;cursor:pointer}
.nav{display:flex;gap:2px}
.nav-item{padding:6px 14px;border-radius:6px;font-size:12px;font-weight:500;color:var(--dim);cursor:pointer;transition:all .15s;letter-spacing:.2px}
.nav-item:hover{color:var(--text);background:var(--card)}
.nav-item.active{color:var(--blue);background:var(--blue-bg)}
.topbar-right{display:flex;align-items:center;gap:10px}
.status-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 8px rgba(16,185,129,.4)}
.status-label{font-size:11px;color:var(--dim);font-family:'JetBrains Mono',monospace}

.content{flex:1;overflow-y:auto;padding:20px 24px}

/* Loading */
.loading{display:flex;align-items:center;justify-content:center;padding:60px;color:var(--dim);gap:10px}
.spinner{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--blue);border-radius:50%;animation:spin .8s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes fadeIn{from{opacity:.7}to{opacity:1}}
.fade-in{animation:fadeIn .2s ease}
@keyframes deltaFade{0%{opacity:1;transform:translateY(0)}92%{opacity:1;transform:translateY(0)}100%{opacity:0;transform:translateY(-6px)}}
.ov-delta{display:inline-block;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;margin-left:6px;animation:deltaFade 240s ease-out forwards}

/* Stat boxes */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:16px}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 18px}
.stat-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);margin-bottom:4px}
.stat-value{font-size:22px;font-weight:800;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums;letter-spacing:-.5px;transition:color .3s}
.stat-sub{font-size:10px;color:var(--dim);margin-top:2px;font-family:'JetBrains Mono',monospace}

/* Session cards */
.sessions-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;margin-bottom:20px}
.session-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 18px;cursor:pointer;transition:all .2s;position:relative;overflow:hidden}
.session-card:hover{border-color:var(--border2);transform:translateY(-1px);box-shadow:0 4px 24px rgba(0,0,0,.3)}
.session-card.positive{border-left:3px solid var(--green)}
.session-card.negative{border-left:3px solid var(--red)}
.session-card.zero{border-left:3px solid var(--dim2)}
.sc-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.sc-name{font-size:14px;font-weight:700;letter-spacing:-.2px}
.sc-dot{width:6px;height:6px;border-radius:50%;display:inline-block}
.sc-dot.on{background:var(--green);box-shadow:0 0 8px rgba(16,185,129,.5)}
.sc-dot.off{background:var(--dim2)}
.sc-where{font-size:10px;color:var(--dim);font-family:'JetBrains Mono',monospace;text-transform:uppercase}
.sc-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.sc-stat-label{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.sc-stat-value{font-size:16px;font-weight:700;font-family:'JetBrains Mono',monospace;margin-top:1px;transition:color .3s}
.sc-bar{height:3px;background:var(--border);border-radius:2px;margin-top:12px;overflow:hidden}
.sc-bar-fill{height:100%;border-radius:2px;transition:width .5s}

/* Tables */
table{width:100%;border-collapse:separate;border-spacing:0}
thead{position:sticky;top:0;z-index:1}
th{text-align:left;padding:8px 12px;color:var(--dim);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;background:var(--card);border-bottom:1px solid var(--border)}
td{padding:8px 12px;border-bottom:1px solid var(--border);font-size:12px;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}
tr:hover td{background:rgba(77,142,247,.02)}
th:first-child{border-radius:8px 0 0 0}th:last-child{border-radius:0 8px 0 0}

/* Tags */
.tag{display:inline-flex;align-items:center;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;font-family:'JetBrains Mono',monospace;letter-spacing:.3px}
.tag-win{background:var(--green-bg);color:var(--green);border:1px solid var(--green-border)}
.tag-loss{background:var(--red-bg);color:var(--red);border:1px solid var(--red-border)}
.tag-live{background:var(--green-bg);color:var(--green);border:1px solid var(--green-border)}
.tag-dead{background:rgba(90,100,120,.1);color:var(--dim);border:1px solid rgba(90,100,120,.2)}
.tag-session{background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-border)}

.pill{display:inline-block;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:600;margin:1px;font-family:'JetBrains Mono',monospace}
.pill-up{background:var(--green-bg);color:var(--green)}.pill-dn{background:var(--red-bg);color:var(--red)}

.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.d{color:var(--dim)}.b{color:var(--blue)}.p{color:var(--purple)}.cy{color:var(--cyan)}

/* Live bar */
.live-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;padding:16px;background:var(--bg2);border:1px solid var(--border);border-radius:10px;margin-bottom:16px}
.live-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--dim)}
.live-value{font-size:14px;font-weight:600;margin-top:3px;font-family:'JetBrains Mono',monospace;transition:color .3s}
.timer-value{font-size:28px;font-weight:800;color:var(--blue);font-family:'JetBrains Mono',monospace;letter-spacing:-1px}
.progress{height:4px;background:var(--border);border-radius:2px;margin-top:6px;overflow:hidden}
.progress-fill{height:100%;border-radius:2px;transition:width 1s linear}

.winbar{height:3px;border-radius:2px;background:var(--red-bg);margin-top:4px}
.winbar-fill{height:100%;border-radius:2px;transition:width .5s}

/* Buttons */
.btn{display:inline-flex;align-items:center;gap:5px;padding:7px 14px;border-radius:6px;font-size:11px;font-family:'Inter',sans-serif;font-weight:600;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text);transition:all .15s;letter-spacing:.2px}
.btn:hover{background:var(--card2);border-color:var(--border2)}
.btn-green{border-color:var(--green-border);color:var(--green)}.btn-green:hover{background:var(--green);color:#fff;border-color:var(--green)}
.btn-red{border-color:var(--red-border);color:var(--red)}.btn-red:hover{background:var(--red);color:#fff;border-color:var(--red)}
.btn-blue{border-color:var(--blue-border);color:var(--blue)}.btn-blue:hover{background:var(--blue);color:#fff;border-color:var(--blue)}
.btn-sm{padding:4px 10px;font-size:10px}

/* Filters */
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.filter-select{background:var(--card);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:11px;font-family:'Inter',sans-serif;cursor:pointer}
.filter-select:focus{border-color:var(--blue);outline:none}

.section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.section-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--dim)}

/* Card */
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:14px}
.card-header{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.card-header h3{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--dim)}

/* Charts */
.chart-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:12px;margin-bottom:20px}
.chart-img{width:100%;border-radius:8px;border:1px solid var(--border);background:var(--card)}

/* Analysis section */
.analysis-section{margin-bottom:24px}
.analysis-section-title{font-size:13px;font-weight:700;color:var(--text);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
.analysis-section-title .badge{background:var(--purple);color:white;font-size:9px;padding:2px 6px;border-radius:3px;font-weight:700}

/* Collapsible */
.collapsible{cursor:pointer;user-select:none}
.collapsible::before{content:'\25B6';display:inline-block;margin-right:6px;font-size:9px;transition:transform .2s}
.collapsible.open::before{transform:rotate(90deg)}
.collapse-body{display:none;margin-top:8px}
.collapse-body.show{display:block}

/* Empty */
.empty{text-align:center;padding:48px;color:var(--dim)}
.empty-title{font-size:14px;font-weight:600;margin-top:8px}

/* Scrollbar */
::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
@media(max-width:768px){.sessions-grid{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(2,1fr)}.chart-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="shell">

<!-- Global Window Banner -->
<div style="background:linear-gradient(135deg,var(--bg2),#0f1420);border-bottom:1px solid var(--border);padding:6px 24px;display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:16px;flex-shrink:0">
  <!-- Left: Timer + Window -->
  <div style="display:flex;align-items:center;gap:12px">
    <div class="logo" onclick="navigate('overview')" style="font-size:14px;cursor:pointer">PM</div>
    <span class="mono" style="font-size:26px;font-weight:800;letter-spacing:-1px" id="g-timer">0:00</span>
    <div style="display:flex;flex-direction:column;gap:2px">
      <div style="height:3px;width:100px;background:var(--border);border-radius:2px;overflow:hidden">
        <div id="g-progress" style="height:100%;background:var(--blue);border-radius:2px;width:0%;transition:width 1s linear"></div>
      </div>
      <span class="mono d" style="font-size:10px" id="g-window">\u2014</span>
    </div>
  </div>
  <!-- Center: BTC Price + PTB -->
  <div style="display:flex;align-items:center;justify-content:center;gap:24px">
    <div style="text-align:center">
      <div class="d mono" style="font-size:8px;letter-spacing:1px">BTC PRICE</div>
      <div style="display:flex;align-items:baseline;gap:6px">
        <span class="mono" style="font-size:20px;font-weight:800" id="g-btc">\u2014</span>
        <span class="mono" style="font-size:12px;font-weight:700" id="g-btc-delta"></span>
      </div>
    </div>
    <div style="width:1px;height:28px;background:var(--border)"></div>
    <div style="text-align:center">
      <div class="d mono" style="font-size:8px;letter-spacing:1px">PRICE TO BEAT</div>
      <span class="mono" style="font-size:16px;font-weight:700;color:var(--yellow)" id="g-ptb">\u2014</span>
    </div>
    <div style="width:1px;height:28px;background:var(--border)"></div>
    <div style="text-align:center">
      <div class="d mono" style="font-size:8px;letter-spacing:1px">DELTA</div>
      <span class="mono" style="font-size:16px;font-weight:700" id="g-delta">\u2014</span>
    </div>
  </div>
  <!-- Right: PM Book YES/NO -->
  <div style="display:flex;align-items:center;gap:14px">
    <div style="text-align:center">
      <div class="d mono" style="font-size:8px;letter-spacing:1px">YES (UP)</div>
      <span class="mono g" style="font-size:16px;font-weight:700" id="g-yes">\u2014</span>
    </div>
    <div style="text-align:center">
      <div class="d mono" style="font-size:8px;letter-spacing:1px">NO (DOWN)</div>
      <span class="mono r" style="font-size:16px;font-weight:700" id="g-no">\u2014</span>
    </div>
  </div>
</div>

<!-- Nav bar -->
<div class="topbar">
  <div class="topbar-left">
    <div class="nav">
      <div class="nav-item active" data-page="overview" onclick="navigate('overview')">Overview</div>
      <div class="nav-item" data-page="trades" onclick="navigate('trades')">All Trades</div>
      <div class="nav-item" data-page="analysis" onclick="navigate('analysis')">Analysis</div>
      <div class="nav-item" data-page="status" onclick="navigate('status')">Status</div>
    </div>
  </div>
  <div class="topbar-right">
    <div id="refresh-info" class="mono" style="font-size:11px"></div>
    <div class="status-dot" id="status-dot"></div>
    <div class="status-label" id="status-label">connecting...</div>
    <button class="btn btn-green btn-sm" onclick="location.href='/new'">+ New</button>
  </div>
</div>
<div class="content" id="content">
  <div class="loading"><div class="spinner"></div> Loading sessions...</div>
</div>
</div>

<script>
const S = {
    page: 'overview', sessions: [], selectedSession: null,
    allTrades: null, analysisData: null,
    sessionData: {}, lastRenderedPage: null, lastDataJSON: '',
};

// ═══ Helpers ═══
async function api(url) { return (await fetch(url)).json(); }
function ft(ts) { return ts ? new Date(ts*1000).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '\u2014'; }
function ftDate(ts) { if(!ts) return '\u2014'; const d=new Date(ts*1000); return d.toLocaleDateString('en-US',{month:'short',day:'numeric'})+' '+d.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
function ago(ts) { if(!ts) return '\u2014'; const d=Math.floor(Date.now()/1000-ts); return d<60?d+'s ago':d<3600?Math.floor(d/60)+'m ago':Math.floor(d/3600)+'h'+Math.floor((d%3600)/60)+'m ago'; }
function winRange(ws) { if(!ws) return '\u2014'; const s=new Date(ws*1000),e=new Date((ws+300)*1000); return s.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'})+' \u2013 '+e.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'}); }
function pc(v){return v>0?'g':v<0?'r':'d'}
function wc(v){return v>=65?'g':v>=55?'y':'r'}
function fp(v){if(v==null)return'\u2014';const n=Math.round(v);return(n>=0?'+':'')+('$'+n.toLocaleString())}
function fps(v){return v==null?'\u2014':(v>=0?'+':'')+('$'+v.toFixed(1))}

// ═══ Navigation ═══
function navigate(page, sess) {
    S.page = page; S.selectedSession = sess || null;
    S.lastRenderedPage = null; // Force full render on navigation
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
    render();
    if (page === 'session' && sess) loadSession(sess);
    if (page === 'trades' && !S.allTrades) loadAllTrades();
    if (page === 'analysis') renderAnalysis(document.getElementById('content'));
    if (page === 'status') renderStatus(document.getElementById('content'));
}

// ═══ Data Loading ═══
async function loadSessions() {
    try {
        S.sessions = await api('/api/sessions');
        const active = S.sessions.filter(s => s.status==='running'||s.status==='active').length;
        document.getElementById('status-dot').style.background = active>0?'var(--green)':'var(--dim2)';
        document.getElementById('status-dot').style.boxShadow = active>0?'0 0 8px rgba(16,185,129,.4)':'none';
        document.getElementById('status-label').textContent = active+' active';
        if (S.page === 'overview') render();
    } catch(e) {
        document.getElementById('status-label').textContent = 'offline';
        document.getElementById('status-dot').style.background = 'var(--red)';
    }
}

async function loadSession(name) {
    const el = document.getElementById('content');
    // INSTANT: render from SSE cache first (no network call)
    if(_sseVpsCache[name]) {
        S.sessionData[name] = Object.assign(S.sessionData[name] || {}, _sseVpsCache[name]);
        render();
    } else {
        el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading '+name+'...</div>';
    }
    // BACKGROUND: fetch full CSV trade log (slower, non-blocking)
    try {
        const full = await api('/api/session/'+name);
        S.sessionData[name] = full;
        render();
    } catch(e) {}
}

async function loadAllTrades() {
    document.getElementById('content').innerHTML = '<div class="loading"><div class="spinner"></div> Loading all trades...</div>';
    try { S.allTrades = await api('/api/all-trades'); render(); } catch(e) { render(); }
}

async function loadAnalysis() {
    document.getElementById('content').innerHTML = '<div class="loading"><div class="spinner"></div> Loading analysis data...</div>';
    try { S.analysisData = await api('/api/analysis'); render(); } catch(e) { render(); }
}

// ═══ Render Router ═══
function render() {
    const el = document.getElementById('content');
    switch(S.page) {
        case 'overview': renderOverview(el); break;
        case 'session': renderSession(el); break;
        case 'trades': renderTrades(el); break;
        case 'analysis': renderAnalysis(el); break;
        case 'status': renderStatus(el); break;
    }
    S.lastRenderedPage = S.page;
}

// ═══ Smart update: only re-render if data changed or page switched ═══
function updateOverview() {
    if (S.page !== 'overview' || S.lastRenderedPage !== 'overview') return;
    const newJSON = JSON.stringify(S.sessions);
    if (newJSON === S.lastDataJSON) return; // Nothing changed
    S.lastDataJSON = newJSON;
    renderOverview(document.getElementById('content'));
}

// ═══ Overview ═══
S._ovSort='pnl'; S._ovArchSet=new Set(); S._ovDir='desc'; S._ovChartData=null; S._ovChartDirty=true; S._ovTimeAxis=false; S._ovTimePeriod=null;
S._ovPrev={pnl:null,trades:null,wr:null};
S._ovDeltaCache={};  // {key: {color, text, ts}}
S._ovDeltaPrev={};   // {key: lastValue} — auto-tracks previous values per key
function ovDeltaBadge(key,cur,prev,fmt){
    const now=Date.now();
    // If we have a cached delta still within display window, show it
    const cached=S._ovDeltaCache[key];
    if(cached && now-cached.ts < 240000) {
        const remaining=Math.max(0.1,240-(now-cached.ts)/1000);
        return '<span class="ov-delta" style="color:'+cached.color+';animation-duration:'+remaining.toFixed(1)+'s">'+cached.text+'</span>';
    }
    // Use auto-tracked prev if explicit prev is null
    if(prev===null) prev=S._ovDeltaPrev[key]!=null?S._ovDeltaPrev[key]:null;
    S._ovDeltaPrev[key]=cur;
    // New delta
    if(prev===null||prev===cur) return '';
    const d=cur-prev;
    if(Math.abs(d)<0.01) return '';
    const color=d>0?'var(--green)':'var(--red)';
    const text=(d>0?'+':'')+fmt(d);
    S._ovDeltaCache[key]={color,text,ts:now};
    return '<span class="ov-delta" style="color:'+color+'">'+text+'</span>';
}
function _ovSortSessions(ss){
    const key=S._ovSort, dir=S._ovDir==='desc'?-1:1;
    return [...ss].sort((a,b)=>{
        let va,vb;
        if(key==='pnl'){va=a.pnl_taker||0;vb=b.pnl_taker||0}
        else if(key==='wr'){va=a.wr||0;vb=b.wr||0}
        else if(key==='trades'){va=a.trades||0;vb=b.trades||0}
        else if(key==='avg'){va=a.trades?(a.pnl_taker||0)/a.trades:0;vb=b.trades?(b.pnl_taker||0)/b.trades:0}
        else if(key==='name'){return dir*a.name.localeCompare(b.name)}
        else if(key==='arch'){return dir*(a.architecture||'').localeCompare(b.architecture||'')}
        else{va=a.pnl_taker||0;vb=b.pnl_taker||0}
        return dir*(va-vb);
    });
}
function ovSetSort(key){
    if(S._ovSort===key) S._ovDir=S._ovDir==='desc'?'asc':'desc';
    else{S._ovSort=key;S._ovDir='desc'}
    renderOverview(document.getElementById('content'));
}
async function ovEnsureChart(){
    if(S._ovChartData){ovBuildCumChart();return}
    if(S._ovChartLoading) return;
    S._ovChartLoading=true;
    try{S._ovChartData=await api('/api/chart-data');ovBuildCumChart()}catch(e){}
    finally{S._ovChartLoading=false}
}
function ovToggleTimeAxis(){
    S._ovTimeAxis=!S._ovTimeAxis;
    ovEnsureChart();
    const btn=document.getElementById('ov-time-btn');
    if(btn) btn.textContent=S._ovTimeAxis?'Show by Trade #':'Show by Time';
}
function ovSetTimePeriod(p){
    S._ovTimePeriod=(p==='null'||p===null)?null:p;
    ovEnsureChart();
    // Update button styles
    const cont=document.getElementById('ov-period-btns');
    if(cont) cont.querySelectorAll('button').forEach(btn=>{
        const label=btn.textContent.trim();
        const val=label==='All'?null:label.toLowerCase();
        const active=(val===S._ovTimePeriod)||(val===null&&S._ovTimePeriod===null);
        btn.className='btn btn-sm'+(active?' btn-blue':'');
    });
}
function ovSelectPositive(){
    const ss=S.sessions;
    const archPnl={};
    ss.forEach(s=>{const a=s.architecture||'impulse_lag';archPnl[a]=(archPnl[a]||0)+(s.pnl_taker||0)});
    S._ovArchSet=new Set(Object.entries(archPnl).filter(([a,p])=>p>0).map(([a])=>a));
    renderOverview(document.getElementById('content'));
    setTimeout(()=>ovEnsureChart(),50);
}
function ovToggleArch(arch){
    if(arch===''){S._ovArchSet.clear()}
    else if(S._ovArchSet.has(arch)){S._ovArchSet.delete(arch)}
    else{S._ovArchSet.add(arch)}
    renderOverview(document.getElementById('content'));
    // Rebuild charts from cached data after DOM update (auto-fetches if needed)
    setTimeout(()=>ovEnsureChart(),50);
}
async function ovSyncChart(){
    // Pull fresh data from VPS, then rebuild charts
    const btn=document.querySelector('#ov-charts-zone .btn-blue');
    if(btn){btn.textContent='Syncing...';btn.disabled=true}
    S._ovChartData=null;
    try{S._ovChartData=await(await fetch('/api/pull-analyze',{method:'POST'})).json()}catch(e){
        try{S._ovChartData=await api('/api/chart-data')}catch(e2){}}
    ovBuildCumChart();
    if(btn){btn.textContent='Sync Chart';btn.disabled=false}
}
async function ovLoadChart(){
    // Only auto-load on first render or if dirty, otherwise keep existing chart
    if(!S._ovChartData && S._ovChartDirty){
        try{S._ovChartData=await api('/api/chart-data')}catch(e){return}
        ovBuildCumChart();
        S._ovChartDirty=false;
    } else if(S._ovChartData && !chartInstances['ov-cum-chart']){
        // Chart canvas was recreated by render but chart not rebuilt — rebuild from cache
        ovBuildCumChart();
    }
}
function ovBuildCumChart(){
    if(!S._ovChartData) return;
    ['ov-cum-chart','ov-wr-chart'].forEach(id=>{if(chartInstances[id]){try{chartInstances[id].destroy()}catch(e){}}delete chartInstances[id]});
    // Inject time-period buttons if not already present
    if(!document.getElementById('ov-period-btns')){
        const hdr=document.querySelector('#ov-charts-zone .card div[style*="flex"]');
        if(hdr){
            const wrap=document.createElement('div');
            wrap.id='ov-period-btns';
            wrap.style.cssText='display:flex;gap:3px;margin-right:8px';
            ['1h','4h','12h','1d','All'].forEach(p=>{
                const b=document.createElement('button');
                b.className='btn btn-sm'+((p==='All'&&!S._ovTimePeriod)||(S._ovTimePeriod===p.toLowerCase())?' btn-blue':'');
                b.style.cssText='font-size:9px;padding:1px 7px;min-width:28px';
                b.textContent=p;
                b.onclick=()=>ovSetTimePeriod(p==='All'?null:p.toLowerCase());
                wrap.appendChild(b);
            });
            const btnGroup=hdr.querySelector('div[style*="gap"]');
            if(btnGroup) btnGroup.prepend(wrap);
            else hdr.appendChild(wrap);
        }
    }
    const cd=S._ovChartData.sessions||{};
    const selected=S._ovArchSet.size>0?S._ovArchSet:new Set(Object.values(cd).map(s=>s.architecture));

    // Collect all trades across selected sessions with timestamps
    const allTradesRaw=[];  // {pnl, arch, ts}
    Object.entries(cd).forEach(([name,s])=>{
        const a=s.architecture||'impulse_lag';
        if(!selected.has(a)) return;
        const cum=s.cum_pnl||[];
        const ts=s.cum_ts||[];
        let prev=0;
        cum.forEach((v,i)=>{const diff=v-prev; allTradesRaw.push({pnl:diff,arch:a,ts:ts[i]||0}); prev=v});
    });
    // Time period filter
    let allTrades=allTradesRaw;
    if(S._ovTimePeriod){
        const hrs={'1h':1,'4h':4,'12h':12,'1d':24}[S._ovTimePeriod]||null;
        if(hrs){
            const cutoff=Date.now()/1000 - hrs*3600;
            allTrades=allTradesRaw.filter(t=>t.ts>=cutoff);
        }
    }
    // Sort by timestamp for time-axis mode
    if(S._ovTimeAxis) allTrades.sort((a,b)=>a.ts-b.ts);
    // Group by architecture
    const archTrades={};
    allTrades.forEach(t=>{if(!archTrades[t.arch]) archTrades[t.arch]=[];archTrades[t.arch].push(t)});

    // Portfolio cumulative PnL
    const portCum=[]; const portTs=[]; let portRunning=0;
    allTrades.forEach(t=>{portRunning+=t.pnl; portCum.push(Math.round(portRunning*10)/10); portTs.push(t.ts)});

    // Per-architecture cumulative PnL
    const archCum={};
    for(const a in archTrades){
        archCum[a]=[]; let r=0;
        archTrades[a].forEach(t=>{r+=t.pnl; archCum[a].push(Math.round(r*10)/10)});
    }
    const archNames=Object.keys(archCum).sort((a,b)=>(archCum[b][archCum[b].length-1]||0)-(archCum[a][archCum[a].length-1]||0));

    // Build x-axis labels
    const xLabels=S._ovTimeAxis
        ? portTs.map(ts=>{if(!ts)return'';const d=new Date(ts*1000);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false})})
        : portCum.map((_,i)=>i+1);

    // Portfolio rolling WR
    const wrWindow=20;
    const portWr=[];
    let portWinCount=0;
    allTrades.forEach((t,j)=>{
        portWinCount+=(t.pnl>0?1:0);
        if(j>=wrWindow) portWinCount-=(allTrades[j-wrWindow].pnl>0?1:0);
        if(j>=wrWindow-1) portWr.push(Math.round(portWinCount/wrWindow*1000)/10);
    });

    // Per-architecture rolling WR
    const archWr={};
    archNames.forEach(a=>{
        const trades=archTrades[a];
        if(trades.length<wrWindow) return;
        archWr[a]=[];
        let wc=0;
        trades.forEach((p,j)=>{
            wc+=(p>0?1:0);
            if(j>=wrWindow) wc-=(trades[j-wrWindow]>0?1:0);
            if(j>=wrWindow-1) archWr[a].push(Math.round(wc/wrWindow*1000)/10);
        });
    });

    const crosshairPlugin={id:'ov-cross',afterDraw(c){if(c._crosshairX==null)return;const ctx=c.ctx,y=c.chartArea;ctx.save();ctx.beginPath();ctx.moveTo(c._crosshairX,y.top);ctx.lineTo(c._crosshairX,y.bottom);ctx.lineWidth=1;ctx.strokeStyle='rgba(255,255,255,.15)';ctx.setLineDash([4,4]);ctx.stroke();ctx.restore()},afterEvent(c,args){const e=args.event;if(e.type==='mousemove'&&c.chartArea){c._crosshairX=(e.x>=c.chartArea.left&&e.x<=c.chartArea.right)?e.x:null}else if(e.type==='mouseout'){c._crosshairX=null}c.draw()}};
    const ttStyle={backgroundColor:'rgba(17,22,34,.95)',titleColor:'#d1d5e0',bodyColor:'#d1d5e0',borderColor:'#243049',borderWidth:1,
        bodyFont:{family:'JetBrains Mono',size:10},cornerRadius:6,displayColors:true,boxWidth:6,boxHeight:6};
    const legendCfg={position:'bottom',labels:{color:'#d1d5e0',font:{family:'JetBrains Mono',size:10},boxWidth:10,padding:6,usePointStyle:true},
        onClick(e,item,legend){const ci=legend.chart;const idx=item.datasetIndex;ci.isDatasetVisible(idx)?ci.hide(idx):ci.show(idx)}};

    // ── Cumulative PnL chart ──
    const cumDs=[];
    // Portfolio total line (always visible, white, thick)
    const portFinal=portCum.length?portCum[portCum.length-1]:0;
    cumDs.push({label:'Portfolio ($'+(portFinal>=0?'+':'')+portFinal.toFixed(0)+')',
        data:portCum,borderColor:'rgba(255,255,255,.9)',borderWidth:3,
        pointRadius:0,pointHoverRadius:5,tension:.15,fill:false});
    // Per-architecture lines (hidden by default, click legend to show)
    archNames.forEach((a,i)=>{
        const cum=archCum[a];
        const final=cum[cum.length-1]||0;
        cumDs.push({label:a.replace(/_/g,' ')+' ($'+(final>=0?'+':'')+final.toFixed(0)+')',
            data:cum,borderColor:COLORS[i%COLORS.length],borderWidth:2,
            pointRadius:0,pointHoverRadius:4,tension:.15,fill:false,hidden:true});
    });
    const cumMaxLen=Math.max(...cumDs.map(d=>d.data.length),1);
    makeChart('ov-cum-chart',{type:'line',data:{labels:xLabels.length?xLabels:Array.from({length:cumMaxLen},(_,i)=>i+1),datasets:cumDs},
        options:{responsive:true,maintainAspectRatio:false,
            interaction:{mode:'index',intersect:false},hover:{mode:'index',intersect:false},
            plugins:{legend:legendCfg,
                tooltip:{...ttStyle,mode:'index',intersect:false,itemSort:(a,b)=>b.parsed.y-a.parsed.y,
                    callbacks:{title(items){return S._ovTimeAxis?items[0].label:'Trade #'+items[0].label},
                        label(ctx){const v=ctx.parsed.y;return ' '+ctx.dataset.label.split(' (')[0]+': $'+(v>=0?'+':'')+v.toFixed(0)}}},
                title:{display:true,text:'Cumulative PnL (click legend to show per-architecture)',color:'#d1d5e0',font:{size:12}}},
            scales:{x:{ticks:{color:'#5a6478',font:{size:9},maxTicksLimit:20},grid:{color:'rgba(26,34,54,.5)'}},
                y:{ticks:{color:'#5a6478',font:{size:10},callback:v=>'$'+v.toLocaleString()},grid:{color:'rgba(26,34,54,.5)'}}}
        },plugins:[crosshairPlugin]});

    // ── Rolling Win Rate chart ──
    const wrDs=[];
    // Portfolio WR line (always visible)
    if(portWr.length>0){
        wrDs.push({label:'Portfolio ('+portWr[portWr.length-1].toFixed(1)+'%)',
            data:portWr,borderColor:'rgba(255,255,255,.9)',borderWidth:3,
            pointRadius:0,pointHoverRadius:4,tension:.2,fill:false});
    }
    // Per-architecture WR lines (hidden by default)
    archNames.forEach((a,i)=>{
        if(!archWr[a]||archWr[a].length===0) return;
        wrDs.push({label:a.replace(/_/g,' '),data:archWr[a],
            borderColor:COLORS[i%COLORS.length],borderWidth:2,
            pointRadius:0,pointHoverRadius:4,tension:.2,fill:false,hidden:true});
    });
    if(wrDs.length>0){
        const wrMaxLen=Math.max(...wrDs.map(d=>d.data.length));
        const wrLabels=S._ovTimeAxis
            ? portTs.slice(wrWindow-1).map(ts=>{if(!ts)return'';const d=new Date(ts*1000);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false})})
            : Array.from({length:wrMaxLen},(_,i)=>i+wrWindow);
        makeChart('ov-wr-chart',{type:'line',data:{labels:wrLabels.length>=wrMaxLen?wrLabels.slice(0,wrMaxLen):Array.from({length:wrMaxLen},(_,i)=>i+wrWindow),datasets:wrDs},
            options:{responsive:true,maintainAspectRatio:false,
                interaction:{mode:'index',intersect:false},hover:{mode:'index',intersect:false},
                plugins:{legend:legendCfg,
                    tooltip:{...ttStyle,mode:'index',intersect:false,itemSort:(a,b)=>b.parsed.y-a.parsed.y,
                        callbacks:{title(items){return S._ovTimeAxis?items[0].label:'Trade #'+items[0].label},
                            label(ctx){return ' '+ctx.dataset.label.split(' (')[0]+': '+ctx.parsed.y.toFixed(1)+'%'}}},
                    title:{display:true,text:'Rolling Win Rate ('+wrWindow+'-trade window, click legend for per-arch)',color:'#d1d5e0',font:{size:12}}},
                scales:{x:{ticks:{color:'#5a6478',font:{size:9},maxTicksLimit:20},grid:{color:'rgba(26,34,54,.5)'}},
                    y:{ticks:{color:'#5a6478',font:{size:10},callback:v=>v+'%'},grid:{color:'rgba(26,34,54,.5)'},min:0,max:100}}
            },plugins:[crosshairPlugin,{id:'wr-refs',afterDraw(chart){
                const ctx=chart.ctx,y=chart.chartArea,yScale=chart.scales.y;
                [50,65].forEach(v=>{const yPos=yScale.getPixelForValue(v);
                    ctx.save();ctx.beginPath();ctx.moveTo(y.left,yPos);ctx.lineTo(y.right,yPos);
                    ctx.lineWidth=1;ctx.strokeStyle=v===50?'rgba(239,68,68,.3)':'rgba(16,185,129,.3)';
                    ctx.setLineDash([6,4]);ctx.stroke();ctx.restore()});
            }}]});
    }
}
function renderOverview(el) {
    let ss = S.sessions;
    if (!ss.length) { el.innerHTML = '<div class="empty"><div style="font-size:28px;opacity:.3">&#x1f4e1;</div><div class="empty-title">No sessions with data</div><div class="d" style="margin-top:4px;font-size:11px">Run <code>pm pull</code> to sync VPS data, or start a new session</div></div>'; return; }

    // Collect unique architectures
    const allArchs=[...new Set(ss.map(s=>s.architecture||'impulse_lag'))].sort();

    // Filter by selected architectures (multi-select)
    const filtered=S._ovArchSet.size>0?ss.filter(s=>S._ovArchSet.has(s.architecture||'impulse_lag')):ss;

    const tot = filtered.reduce((a,s) => a+(s.trades||0), 0);
    const pnl = filtered.reduce((a,s) => a+(s.pnl_taker||0), 0);
    const wins = filtered.reduce((a,s) => a+(s.wins||0), 0);
    const wr = tot > 0 ? (wins/tot*100).toFixed(1) : 0;
    const act = filtered.filter(s => s.status==='running'||s.status==='active').length;
    const avg = tot > 0 ? pnl/tot : 0;
    const sorted = _ovSortSessions(filtered);

    // Architecture summary pills
    const archStats={};
    ss.forEach(s=>{const a=s.architecture||'impulse_lag';if(!archStats[a])archStats[a]={pnl:0,trades:0,wins:0,sessions:0};archStats[a].pnl+=(s.pnl_taker||0);archStats[a].trades+=(s.trades||0);archStats[a].wins+=(s.wins||0);archStats[a].sessions++});
    const positiveArchs=Object.entries(archStats).filter(([a,v])=>v.pnl>0).map(([a])=>a);
    const isPosActive=positiveArchs.length>0&&positiveArchs.length===S._ovArchSet.size&&positiveArchs.every(a=>S._ovArchSet.has(a));
    let archPills='<button class="btn btn-sm'+(S._ovArchSet.size===0?' btn-blue':'')+'" style="font-size:10px;padding:3px 10px" onclick="ovToggleArch(\'\')">All</button>';
    archPills+='<button class="btn btn-sm'+(isPosActive?' btn-blue':'')+'" style="font-size:10px;padding:3px 10px;border-color:var(--green-border);color:var(--green)" onclick="ovSelectPositive()">Profitable Only</button>';
    Object.entries(archStats).sort((a,b)=>b[1].pnl-a[1].pnl).forEach(([a,v])=>{
        const active=S._ovArchSet.has(a);
        const awr=v.trades>0?(v.wins/v.trades*100).toFixed(0):'0';
        const apnlD=ovDeltaBadge('arch_pnl_'+a,Math.round(v.pnl),null,d=>'$'+d.toLocaleString());
        const atrdD=ovDeltaBadge('arch_trd_'+a,v.trades,null,d=>d.toString());
        archPills+=`<button class="btn btn-sm${active?' btn-blue':''}" style="font-size:10px;padding:3px 10px" onclick="ovToggleArch('${a}')">
            ${a.replace(/_/g,' ')} <span class="${pc(v.pnl)}" style="font-weight:700">${fp(v.pnl)}</span>${apnlD}
            <span class="d">${v.trades}t ${awr}%</span>${atrdD}</button>`;
    });

    // Sort indicator
    const sortKeys=[{k:'pnl',l:'PnL'},{k:'wr',l:'WR'},{k:'trades',l:'Trades'},{k:'avg',l:'$/Trade'},{k:'name',l:'Name'},{k:'arch',l:'Arch'}];
    let sortBtns='';
    sortKeys.forEach(({k,l})=>{
        const active=S._ovSort===k;
        const arrow=active?(S._ovDir==='desc'?'\u25BC':'\u25B2'):'';
        sortBtns+=`<button class="btn btn-sm${active?' btn-blue':''}" style="font-size:10px;padding:2px 8px" onclick="ovSetSort('${k}')">${l} ${arrow}</button>`;
    });

    let cards = '';
    sorted.forEach((s,i) => {
        const p=s.pnl_taker||0, w=s.wr||0, active=s.status==='running'||s.status==='active';
        const cls=p>0?'positive':p<0?'negative':'zero';
        const bc=w>=60?'var(--green)':w>=50?'var(--yellow)':'var(--red)';
        const avgP=s.trades>0?p/s.trades:0;
        const dpnl=ovDeltaBadge('sc_pnl_'+s.name,p,null,d=>'$'+Math.round(d).toLocaleString());
        const dwr=ovDeltaBadge('sc_wr_'+s.name,w,null,d=>d.toFixed(1)+'pp');
        const dtrades=ovDeltaBadge('sc_trades_'+s.name,s.trades||0,null,d=>d.toString());
        cards += `<div class="session-card ${cls}" onclick="navigate('session','${s.name}')">
          <div class="sc-top"><div><span class="d mono" style="font-size:10px;font-weight:700">#${i+1}</span> <span class="sc-name">${s.name}</span></div>
          <div style="display:flex;align-items:center;gap:5px"><span class="sc-where">${s.architecture||'impulse_lag'}</span><span class="sc-where">${s.where}</span><div class="sc-dot ${active?'on':'off'}"></div></div></div>
          <div class="sc-stats">
            <div><div class="sc-stat-label">PnL</div><div class="sc-stat-value ${pc(p)}">${fp(p)}${dpnl}</div></div>
            <div><div class="sc-stat-label">Win Rate</div><div class="sc-stat-value ${wc(w)}">${w}%${dwr}</div></div>
            <div><div class="sc-stat-label">Trades</div><div class="sc-stat-value">${s.trades||0}${dtrades}</div></div>
            <div><div class="sc-stat-label">$/Trade</div><div class="sc-stat-value ${pc(avgP)}">${s.trades>0?fps(avgP):'\u2014'}</div></div>
          </div>
          <div class="sc-bar"><div class="sc-bar-fill" style="width:${Math.min(100,w)}%;background:${bc}"></div></div>
        </div>`;
    });
    if (!filtered.length) cards = '<div class="empty"><div class="empty-title">No sessions match filter</div></div>';

    // Compute runtime from session data
    const totalWindows=filtered.reduce((a,s)=>a+(s.windows_settled||0),0);
    const startTimes=filtered.map(s=>s.started||0).filter(t=>t>0);
    let runtimeStr='\u2014';
    if(startTimes.length>0){
        const earliest=Math.min(...startTimes);
        const nowSec=Date.now()/1000;
        const runSec=Math.max(0,nowSec-earliest);
        const rh=Math.floor(runSec/3600);
        const rm=Math.floor((runSec%3600)/60);
        runtimeStr=rh>0?rh+'h '+rm+'m':rm+'m';
    }

    // Split rendering: charts zone is preserved, only stats/cards are updated
    const chartsExist=el.querySelector('#ov-charts-zone');
    if(!chartsExist){
        // First render — build full page including chart containers
        el.innerHTML = `
        <div id="ov-dynamic-zone"></div>
        <div id="ov-charts-zone">
          <div class="card" style="padding:16px;margin:12px 0">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
              <span class="d" style="font-size:11px">Cumulative PnL by Architecture</span>
              <div style="display:flex;gap:6px;align-items:center">
                <button class="btn btn-sm" id="ov-time-btn" style="font-size:10px;padding:2px 10px" onclick="ovToggleTimeAxis()">${S._ovTimeAxis?'Show by Trade #':'Show by Time'}</button>
                <button class="btn btn-sm btn-blue" style="font-size:10px;padding:2px 10px" onclick="ovSyncChart()">Sync Chart</button>
              </div>
            </div>
            <div style="height:300px"><canvas id="ov-cum-chart"></canvas></div>
          </div>
          <div class="card" style="padding:16px;margin:0 0 12px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
              <span class="d" style="font-size:11px">Rolling Win Rate by Architecture</span>
            </div>
            <div style="height:250px"><canvas id="ov-wr-chart"></canvas></div>
          </div>
        </div>
        <div id="ov-sessions-zone"></div>`;
        // Initial chart load
        if(!S._ovChartLoading){
            S._ovChartLoading=true;
            api('/api/chart-data').then(d=>{S._ovChartData=d;S._ovChartLoading=false;ovBuildCumChart()}).catch(()=>{S._ovChartLoading=false});
        }
    }

    // Compute deltas from previous values
    const pnlNum=Math.round(pnl);
    const wrNum=parseFloat(wr);
    const dpnl=ovDeltaBadge('pnl',pnlNum,S._ovPrev.pnl,d=>'$'+d.toLocaleString());
    const dtrades=ovDeltaBadge('trades',tot,S._ovPrev.trades,d=>d.toString());
    const dwr=ovDeltaBadge('wr',wrNum,S._ovPrev.wr,d=>d.toFixed(1)+'pp');
    S._ovPrev={pnl:pnlNum,trades:tot,wr:wrNum};

    // Update dynamic zone (stats + filters) — no chart destruction
    document.getElementById('ov-dynamic-zone').innerHTML = `
    <div class="section-header"><div class="section-title">Overview</div><div class="d mono" style="font-size:10px">Updated ${new Date().toLocaleTimeString()}</div></div>
    <div class="stats-grid">
      <div class="stat-box"><div class="stat-label">Total PnL${S._ovArchSet.size>0?' ('+S._ovArchSet.size+' arch)':''}</div><div class="stat-value ${pc(pnl)}">${fp(pnl)}${dpnl}</div><div class="stat-sub">${fps(avg)} / trade</div></div>
      <div class="stat-box"><div class="stat-label">Total Trades</div><div class="stat-value b">${tot.toLocaleString()}${dtrades}</div><div class="stat-sub">${filtered.length}${S._ovArchSet.size>0?' / '+ss.length:''} sessions</div></div>
      <div class="stat-box"><div class="stat-label">Win Rate</div><div class="stat-value ${wc(wr)}">${wr}%${dwr}</div><div class="stat-sub">${wins}W / ${tot-wins}L</div></div>
      <div class="stat-box"><div class="stat-label">Runtime</div><div class="stat-value cy">${runtimeStr}</div><div class="stat-sub">${totalWindows} windows settled</div></div>
      <div class="stat-box"><div class="stat-label">Active</div><div class="stat-value ${act>0?'g':'d'}">${act}</div><div class="stat-sub">sessions running</div></div>
      ${(()=>{
        const allWins=[];
        Object.values(_sseVpsCache).forEach(d=>{(d.recent_windows||[]).forEach(w=>allWins.push(w))});
        allWins.sort((a,b)=>(a.window_start||0)-(b.window_start||0));
        const unique=[];const seen=new Set();
        allWins.forEach(w=>{const k=w.window_start;if(!seen.has(k)){seen.add(k);unique.push(w.outcome)}});
        const last10=unique.slice(-10);
        if(last10.length<4) return '<div class="stat-box"><div class="stat-label">Regime</div><div class="stat-value d">...</div><div class="stat-sub">waiting for data</div></div>';
        const alts=last10.slice(1).reduce((n,o,i)=>n+(o!==last10[i]?1:0),0);
        const altRate=alts/(last10.length-1);
        const regime=altRate>=0.70?'CHOP':altRate<=0.35?'TREND':'MIX';
        const rc=regime==='TREND'?'g':regime==='CHOP'?'r':'cy';
        const dots=last10.map(o=>o==='Up'?'<span style="color:#10b981">U</span>':'<span style="color:#ef4444">D</span>').join(' ');
        return '<div class="stat-box"><div class="stat-label">Regime</div><div class="stat-value '+rc+'">'+regime+'</div><div class="stat-sub" style="font-size:10px">'+dots+' <span class="d">('+Math.round(altRate*100)+'% alt)</span></div></div>';
      })()}
    </div>
    <div style="margin:14px 0 6px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
      <span class="d" style="font-size:10px;margin-right:4px">FILTER</span>${archPills}
    </div>`;

    // Update sessions zone
    document.getElementById('ov-sessions-zone').innerHTML = `
    <div class="section-header" style="margin-top:10px">
      <div class="section-title">Sessions</div>
      <div style="display:flex;gap:4px;align-items:center"><span class="d" style="font-size:10px;margin-right:4px">SORT</span>${sortBtns}</div>
    </div>
    <div class="sessions-grid">${cards}</div>`;
    S.lastDataJSON = JSON.stringify(ss);
}

// ═══ Session Detail ═══
function renderSession(el) {
    const name = S.selectedSession;
    if (!name) { el.innerHTML = '<div class="loading">No session selected</div>'; return; }
    const d = S.sessionData[name];
    if (!d) { el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading '+name+'...</div>'; return; }

    const ws=d.current_window||0, we=d.window_end||0, now=Date.now()/1000;
    const trem=Math.max(0,Math.floor(we-now)), pct=Math.min(100,((300-trem)/300)*100);
    const csvPnl=d.csv_pnl!=null?d.csv_pnl:Math.round(d.pnl_taker||0);
    const csvWr=d.csv_wr!=null?d.csv_wr:(d.wr||0);
    const csvTrades=d.csv_trades!=null?d.csv_trades:(d.trades||0);
    const csvWins=d.csv_wins!=null?d.csv_wins:(d.wins||0);
    const btc=d.btc_price?'$'+Number(d.btc_price).toLocaleString('en-US',{minimumFractionDigits:2}):'\u2014';
    const ptb=d.window_open?'$'+Number(d.window_open).toLocaleString('en-US',{minimumFractionDigits:2}):'\u2014';
    const bid=d.book_bid?(d.book_bid*100).toFixed(1)+'\u00a2':'\u2014';
    const ask=d.book_ask?(d.book_ask*100).toFixed(1)+'\u00a2':'\u2014';
    const spr=d.book_spread?(d.book_spread*100).toFixed(1)+'\u00a2':'\u2014';
    // NO book = inverted YES book
    const noBid=d.book_ask?((1-d.book_ask)*100).toFixed(1)+'\u00a2':'\u2014';
    const noAsk=d.book_bid?((1-d.book_bid)*100).toFixed(1)+'\u00a2':'\u2014';

    // Health indicators
    const h=d.health||{};
    const hHtml=h.running?`<span class="tag tag-live" style="font-size:9px">Bot OK</span>
        <span class="tag ${h.binance_ok?'tag-live':'tag-loss'}" style="font-size:9px">Binance ${h.binance_ok?'OK':'ERR'}</span>
        <span class="tag ${h.pm_ok?'tag-live':'tag-loss'}" style="font-size:9px">PM ${h.pm_ok?'OK':'ERR'}</span>`
        :'<span class="tag tag-dead" style="font-size:9px">Bot not responding</span>';

    // Use CSV combos (source of truth) — same data source as stats
    const combos = d.csv_combos || {};
    let comboRows='';
    Object.entries(combos).sort((a,b)=>(b[1].pnl_taker||0)-(a[1].pnl_taker||0)).forEach(([cn,cv])=>{
        const w=cv.wr||0, p=cv.pnl_taker||0, avg=cv.trades>0?(p/cv.trades):0;
        const bc=w>=60?'var(--green)':w>=50?'var(--yellow)':'var(--red)';
        comboRows+=`<tr><td style="font-weight:600;color:var(--text)">${cn}</td><td>${cv.trades}</td><td>${cv.wins}</td><td class="${wc(w)}">${w}%</td><td class="${pc(p)}" style="font-weight:700">${fp(p)}</td><td class="${pc(avg)}">${fps(avg)}</td><td style="width:80px"><div class="winbar"><div class="winbar-fill" style="width:${w}%;background:${bc}"></div></div></td></tr>`;
    });

    // CSV trade log with combo filter
    const csvLog = d.csv_trade_log || [];
    const combosInLog = [...new Set(csvLog.map(t=>(t.combo||'').trim()).filter(Boolean))].sort();
    // Detect if this is a mean_revert session (has exit_price column)
    const isMR = csvLog.length > 0 && csvLog[0].exit_price;

    let tradeRows='';
    csvLog.forEach(t=>{
        const p=parseFloat(t.pnl_taker||0);
        const result=(t.result||'').trim();
        const tag=result==='WIN'?'<span class="tag tag-win">WIN</span>':result==='LOSS'?'<span class="tag tag-loss">LOSS</span>':'<span class="tag tag-dead">PENDING</span>';
        const entry=t.fill_price?(parseFloat(t.fill_price)*100).toFixed(1)+'\u00a2':'?';
        const cost=t.total_cost?'$'+parseFloat(t.total_cost).toFixed(0):'\u2014';
        const ts=t.timestamp?ftDate(parseFloat(t.timestamp)):'\u2014';
        if(isMR) {
            const exitP=t.exit_price?(parseFloat(t.exit_price)*100).toFixed(1)+'\u00a2':'\u2014';
            const holdS=t.hold_seconds?parseFloat(t.hold_seconds).toFixed(0)+'s':'\u2014';
            const reason=t.exit_reason||'\u2014';
            const reasonTag=reason==='target_profit'?'<span class="tag tag-win" style="font-size:8px">TARGET</span>':
                           reason==='stop_loss'?'<span class="tag tag-loss" style="font-size:8px">STOP</span>':
                           reason==='trailing_stop'?'<span class="tag" style="font-size:8px;background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue-border)">TRAIL</span>':
                           reason==='time_max_hold'?'<span class="tag tag-dead" style="font-size:8px">TIME</span>':
                           reason==='window_end'?'<span class="tag tag-dead" style="font-size:8px">EXPIRY</span>':
                           '<span class="d" style="font-size:9px">'+reason+'</span>';
            tradeRows+=`<tr data-combo="${(t.combo||'').trim()}" data-result="${result}"><td class="d">${ts}</td><td>${(t.combo||'?').trim()}</td><td>${t.direction||'?'}</td><td>${entry}</td><td class="b">${exitP}</td><td>${holdS}</td><td>${t.filled_size||'?'}</td><td>${cost}</td><td>${reasonTag}</td><td>${tag}</td><td class="${pc(p)}" style="font-weight:700">${result?fp(p):'\u2014'}</td></tr>`;
        } else {
            const imp=t.impulse_bps?parseFloat(t.impulse_bps).toFixed(1)+'bp':'?';
            const trem2=t.time_remaining?Math.round(parseFloat(t.time_remaining))+'s':'?';
            tradeRows+=`<tr data-combo="${(t.combo||'').trim()}" data-result="${result}"><td class="d">${ts}</td><td>${(t.combo||'?').trim()}</td><td>${t.direction||'?'}</td><td>${entry}</td><td>${t.filled_size||'?'}</td><td>${cost}</td><td>${imp}</td><td>${trem2}</td><td>${tag}</td><td class="${pc(p)}" style="font-weight:700">${result?fp(p):'\u2014'}</td></tr>`;
        }
    });

    let pills='';
    (d.recent_windows||[]).forEach(w=>{pills+=`<span class="pill ${w.outcome==='Up'?'pill-up':'pill-dn'}">${w.outcome}</span>`;});

    const meta=S.sessions.find(s=>s.name===name)||{};
    const active=meta.status==='running'||meta.status==='active';
    const avgPnl=csvTrades>0?(csvPnl/csvTrades):0;

    el.innerHTML=`
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div style="display:flex;align-items:center;gap:12px">
        <a onclick="navigate('overview')" style="cursor:pointer;font-size:16px;color:var(--dim)">\u2190</a>
        <h1 style="font-size:18px;font-weight:700">${name}</h1>
        ${active?'<span class="tag tag-live">LIVE</span>':'<span class="tag tag-dead">STOPPED</span>'}
        <span class="tag" style="background:${d.is_local?'var(--blue-bg)':'rgba(139,92,246,.1)'};color:${d.is_local?'var(--blue)':'var(--purple)'};border:1px solid ${d.is_local?'var(--blue-border)':'rgba(139,92,246,.2)'}">${d.is_local?'LOCAL':'VPS'}</span>
        <span class="tag" style="background:rgba(6,182,212,.1);color:var(--cyan);border:1px solid rgba(6,182,212,.2)">${d.architecture||meta.architecture||'impulse_lag'}</span>
        ${hHtml}
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-blue btn-sm" id="sync-btn" onclick="syncData()">Sync</button>
        <a class="btn btn-sm" href="/logs/${name}" target="_blank">Logs</a>
        <a class="btn btn-sm" href="/edit/${name}">Config</a>
        ${active?`<button class="btn btn-red btn-sm" onclick="stopSess('${name}','${meta.where||'vps'}')">Stop</button>`
               :`<button class="btn btn-green btn-sm" onclick="startSess('${name}','vps')">Start VPS</button>
                 <button class="btn btn-green btn-sm" onclick="startSess('${name}','local')">Start Local</button>`}
        <button class="btn btn-sm" style="border-color:var(--red-border);color:var(--red);font-size:9px" onclick="deleteSess('${name}')">Del</button>
      </div>
    </div>
    <div class="live-grid">
      <div style="text-align:center"><div class="live-label">Time Left</div><div class="timer-value" id="live-timer" style="color:${trem<60?'var(--red)':trem<120?'var(--yellow)':'var(--blue)'}">${Math.floor(trem/60)}:${String(trem%60).padStart(2,'0')}</div><div class="progress"><div class="progress-fill" id="live-progress" style="width:${pct}%;background:${trem<60?'var(--red)':trem<120?'var(--yellow)':'var(--blue)'};transition:width 1s linear"></div></div></div>
      <div><div class="live-label">Window</div><div class="live-value" id="live-window">${winRange(ws)}</div></div>
      <div><div class="live-label">BTC</div><div class="live-value" id="live-btc">${btc}</div></div>
      <div><div class="live-label">Price To Beat</div><div class="live-value" id="live-ptb">${ptb}</div></div>
      <div><div class="live-label">YES Book</div><div class="live-value">Bid <span class="g" id="live-bid">${bid}</span> / Ask <span class="r" id="live-ask">${ask}</span> <span class="d">(<span id="live-spr">${spr}</span>)</span></div></div>
      <div><div class="live-label">NO Book</div><div class="live-value">Bid <span class="g" id="live-nobid">${noBid}</span> / Ask <span class="r" id="live-noask">${noAsk}</span></div></div>
      <div><div class="live-label">Updated</div><div class="live-value d" id="live-updated">${ago(d.updated)}</div></div>
    </div>
    <div class="stats-grid">
      <div class="stat-box"><div class="stat-label">PnL (Taker)</div><div class="stat-value ${pc(csvPnl)}">${fp(csvPnl)}</div><div class="stat-sub">${fps(avgPnl)} / trade</div></div>
      <div class="stat-box"><div class="stat-label">Win Rate</div><div class="stat-value ${wc(csvWr)}">${csvWr}%</div><div class="stat-sub">${csvWins}W / ${csvTrades-csvWins}L</div></div>
      <div class="stat-box"><div class="stat-label">Trades</div><div class="stat-value b">${csvTrades}</div></div>
      <div class="stat-box"><div class="stat-label">Windows</div><div class="stat-value cy">${d.windows_settled||0}</div></div>
    </div>
    ${pills?'<div class="section-header"><div class="section-title">Recent Windows</div></div><div style="margin-bottom:16px">'+pills+'</div>':''}
    ${comboRows?`<div class="card"><div class="card-header"><h3>Combo Performance</h3><span class="d mono" style="font-size:10px">${csvTrades} trades from CSV</span></div><div style="overflow-x:auto"><table><thead><tr><th>Combo</th><th>Trades</th><th>Wins</th><th>Win%</th><th>PnL</th><th>$/Trade</th><th style="width:80px">WR</th></tr></thead><tbody>${comboRows}</tbody></table></div></div>`:''}
    ${(()=>{
        // Rolling 12-window PnL progression
        if(!csvLog.length) return '';
        // Group trades by window_start
        const winMap={};
        csvLog.forEach(t=>{
            const ws=t.window_start||'';
            if(!ws) return;
            if(!winMap[ws]) winMap[ws]={trades:[],combos:{}};
            winMap[ws].trades.push(t);
            const combo=t.combo||'combined';
            if(!winMap[ws].combos[combo]) winMap[ws].combos[combo]={pnl:0,w:0,n:0};
            winMap[ws].combos[combo].n++;
            winMap[ws].combos[combo].pnl+=parseFloat(t.pnl_taker||0);
            if(t.result==='WIN') winMap[ws].combos[combo].w++;
        });
        const sortedWins=Object.keys(winMap).sort((a,b)=>parseFloat(a)-parseFloat(b));
        if(sortedWins.length<2) return '';
        // Get all combo names
        const allCombos=[...new Set(csvLog.map(t=>t.combo||'').filter(Boolean))].sort();
        // Build rolling 12-window blocks
        const blockSize=12;
        const blocks=[];
        for(let i=0;i<sortedWins.length;i+=blockSize){
            const slice=sortedWins.slice(i,i+blockSize);
            const firstTs=parseFloat(slice[0]);
            const lastTs=parseFloat(slice[slice.length-1]);
            const fmtEst=ts=>{const d=new Date((ts-4*3600)*1000);const m=d.getUTCMonth()+1;const dy=d.getUTCDate();const h=d.getUTCHours();const mn=d.getUTCMinutes();const ampm=h>=12?'PM':'AM';const h12=h%12||12;return m+'/'+dy+' '+h12+':'+String(mn).padStart(2,'0')+ampm};
            const block={label:fmtEst(firstTs)+' — '+fmtEst(lastTs+300),trades:0,wins:0,pnl:0,combos:{}};
            allCombos.forEach(c=>{block.combos[c]={pnl:0,w:0,n:0}});
            slice.forEach(ws=>{
                const w=winMap[ws];
                w.trades.forEach(t=>{
                    block.trades++;
                    block.pnl+=parseFloat(t.pnl_taker||0);
                    if(t.result==='WIN') block.wins++;
                    const c=t.combo||'';
                    if(c&&block.combos[c]){
                        block.combos[c].n++;
                        block.combos[c].pnl+=parseFloat(t.pnl_taker||0);
                        if(t.result==='WIN') block.combos[c].w++;
                    }
                });
            });
            blocks.push(block);
        }
        if(blocks.length<2) return '';
        // Build table
        let hdr='<th>Block</th><th>Trades</th><th>WR</th><th>PnL</th><th>$/t</th>';
        allCombos.forEach(c=>{hdr+='<th>'+c+'</th>'});
        let rows='';
        let cumPnl=0;
        blocks.forEach(b=>{
            cumPnl+=b.pnl;
            const wr=b.trades>0?(b.wins/b.trades*100):0;
            const avg=b.trades>0?b.pnl/b.trades:0;
            const pnlCls=b.pnl>=0?'g':'r';
            rows+='<tr><td class="b">'+b.label+'</td><td>'+b.trades+'</td><td class="'+((wr>=55)?'g':(wr>=50)?'cy':'r')+'">'+wr.toFixed(0)+'%</td><td class="'+pnlCls+'" style="font-weight:700">$'+(b.pnl>=0?'+':'')+Math.round(b.pnl).toLocaleString()+'</td><td class="'+pnlCls+'">$'+(avg>=0?'+':'')+avg.toFixed(0)+'</td>';
            allCombos.forEach(c=>{
                const cc=b.combos[c];
                if(cc.n>0){
                    const cpnl=cc.pnl;
                    rows+='<td class="'+(cpnl>=0?'g':'r')+'" style="font-size:10px">$'+(cpnl>=0?'+':'')+Math.round(cpnl)+'<span class="d"> ('+cc.n+'t)</span></td>';
                }else{
                    rows+='<td class="d" style="font-size:10px">\u2014</td>';
                }
            });
            rows+='</tr>';
        });
        return '<div class="card"><div class="card-header"><h3>Rolling 12-Window PnL</h3><span class="d mono" style="font-size:10px">'+sortedWins.length+' windows, '+blocks.length+' blocks</span></div><div style="overflow-x:auto;max-height:400px;overflow-y:auto"><table><thead><tr>'+hdr+'</tr></thead><tbody>'+rows+'</tbody></table></div></div>';
    })()}
    <div class="card" style="padding:16px;margin-bottom:14px"><div style="height:350px"><canvas id="session-combo-chart"></canvas></div></div>
    <div class="card"><div class="card-header"><h3>Trade Log</h3>
      <div style="display:flex;gap:6px;align-items:center">
        <select class="filter-select" id="sf-combo" onchange="filterST()"><option value="">All Combos</option>${combosInLog.map(c=>'<option value="'+c+'">'+c+'</option>').join('')}</select>
        <select class="filter-select" id="sf-result" onchange="filterST()"><option value="">All</option><option value="WIN">WIN</option><option value="LOSS">LOSS</option></select>
        <span class="d mono" style="font-size:10px" id="stc">${csvLog.length} trades</span>
      </div>
    </div>
    ${tradeRows?`<div style="overflow-x:auto;max-height:600px;overflow-y:auto"><table id="st"><thead><tr>${isMR?'<th>Time</th><th>Combo</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Hold</th><th>Size</th><th>Cost</th><th>Exit Reason</th><th>Result</th><th>PnL</th>':'<th>Time</th><th>Combo</th><th>Dir</th><th>Entry</th><th>Size</th><th>Cost</th><th>Impulse</th><th>T-rem</th><th>Result</th><th>PnL</th>'}</tr></thead><tbody>${tradeRows}</tbody></table></div>`
        :'<div class="empty"><div class="empty-title">No trades yet. Waiting for signals...</div></div>'}
    </div>
    <div style="margin-top:8px;padding:8px 12px;background:var(--card);border:1px solid var(--border);border-radius:8px;font-size:11px;color:var(--dim);font-family:'JetBrains Mono',monospace">
      Tail logs: <code style="background:var(--card2);padding:2px 6px;border-radius:4px;color:var(--blue)">${d.is_local?'tail -f data/'+name+'.log':'pm logs '+name}</code>
    </div>`;

    // Build combo PnL chart
    buildSessionComboChart(d);
}
function buildSessionComboChart(d){
    destroyCharts();
    const cumData=d.combo_cum_pnl||{};
    const totalCum=d.total_cum_pnl||[];
    const comboNames=Object.keys(cumData).sort((a,b)=>{
        const pa=cumData[a],pb=cumData[b];
        return (pb.length?pb[pb.length-1]:0)-(pa.length?pa[pa.length-1]:0);
    });
    if(!comboNames.length&&!totalCum.length) return;

    const crosshairPlugin={id:'crosshair-session',afterDraw(chart){if(chart._crosshairX==null)return;const ctx=chart.ctx,y=chart.chartArea;ctx.save();ctx.beginPath();ctx.moveTo(chart._crosshairX,y.top);ctx.lineTo(chart._crosshairX,y.bottom);ctx.lineWidth=1;ctx.strokeStyle='rgba(255,255,255,.15)';ctx.setLineDash([4,4]);ctx.stroke();ctx.restore()},afterEvent(chart,args){const e=args.event;if(e.type==='mousemove'&&chart.chartArea){chart._crosshairX=(e.x>=chart.chartArea.left&&e.x<=chart.chartArea.right)?e.x:null}else if(e.type==='mouseout'){chart._crosshairX=null}chart.draw()}};

    const datasets=[];
    // Total line (dashed, white)
    if(totalCum.length){
        datasets.push({label:'TOTAL ($'+(totalCum[totalCum.length-1]>=0?'+':'')+totalCum[totalCum.length-1]+')',
            data:totalCum,borderColor:'rgba(255,255,255,.5)',borderWidth:2,borderDash:[6,3],
            pointRadius:0,pointHoverRadius:4,tension:.15,fill:false});
    }
    // Per-combo lines
    comboNames.forEach((cn,i)=>{
        const cum=cumData[cn];
        const final=cum.length?cum[cum.length-1]:0;
        datasets.push({label:cn+' ($'+(final>=0?'+':'')+final+')',
            data:cum,borderColor:COLORS[i%COLORS.length],borderWidth:1.5,
            pointRadius:0,pointHoverRadius:4,tension:.15,fill:false});
    });
    const maxLen=Math.max(totalCum.length,...comboNames.map(cn=>cumData[cn].length));

    makeChart('session-combo-chart',{type:'line',
        data:{labels:Array.from({length:maxLen},(_,i)=>i+1),datasets},
        options:{responsive:true,maintainAspectRatio:false,
            interaction:{mode:'index',intersect:false},hover:{mode:'index',intersect:false},
            plugins:{
                legend:{position:'bottom',labels:{color:'#d1d5e0',font:{family:'JetBrains Mono',size:10},boxWidth:10,padding:6,usePointStyle:true},
                    onClick(e,item,legend){const ci=legend.chart;const i=item.datasetIndex;ci.isDatasetVisible(i)?ci.hide(i):ci.show(i)}},
                tooltip:{backgroundColor:'rgba(17,22,34,.95)',titleColor:'#d1d5e0',bodyColor:'#d1d5e0',
                    borderColor:'#243049',borderWidth:1,bodyFont:{family:'JetBrains Mono',size:10},
                    cornerRadius:6,displayColors:true,boxWidth:6,boxHeight:6,
                    mode:'index',intersect:false,itemSort:(a,b)=>b.parsed.y-a.parsed.y,
                    callbacks:{
                        title(items){return 'Trade #'+items[0].label},
                        label(ctx){const v=ctx.parsed.y;return ' '+ctx.dataset.label.split(' (')[0]+': $'+(v>=0?'+':'')+v.toFixed(0)}
                    }},
                title:{display:true,text:'Cumulative PnL by Combo (click legend to toggle)',color:'#d1d5e0',font:{size:12}}
            },
            scales:{
                x:{ticks:{color:'#5a6478',font:{size:9},maxTicksLimit:20},grid:{color:'rgba(26,34,54,.5)'}},
                y:{ticks:{color:'#5a6478',font:{size:10},callback:v=>'$'+v.toLocaleString()},grid:{color:'rgba(26,34,54,.5)'}}
            }
        },plugins:[crosshairPlugin]});
}
function filterST(){const c=document.getElementById('sf-combo').value,r=document.getElementById('sf-result').value;let n=0;document.querySelectorAll('#st tbody tr').forEach(row=>{const m=(!c||row.dataset.combo===c)&&(!r||row.dataset.result===r);row.style.display=m?'':'none';if(m)n++});document.getElementById('stc').textContent=n+' shown'}

// ═══ Status Page ═══
async function renderStatus(el) {
    el.innerHTML='<div class="loading"><div class="spinner"></div> Checking VPS health...</div>';
    try {
        const h = await api('/api/vps-health');
        let sessRows='';
        Object.entries(h.sessions||{}).sort((a,b)=>a[0].localeCompare(b[0])).forEach(([name,s])=>{
            const sysOk=s.systemd==='active';
            const fresh=!s.stale;
            sessRows+=`<tr onclick="navigate('session','${name}')" style="cursor:pointer">
                <td style="font-weight:600">${name}</td>
                <td><span class="tag ${sysOk?'tag-live':'tag-loss'}">${s.systemd}</span></td>
                <td><span class="tag ${fresh?'tag-live':'tag-loss'}">${s.last_update_age>=0?s.last_update_age+'s ago':'N/A'}</span></td>
                <td><span class="tag ${s.binance_ok?'tag-live':'tag-loss'}">Binance ${s.binance_ok?'OK':'ERR ('+((s.errors||{}).binance_ws||0)+')'}</span></td>
                <td><span class="tag ${s.pm_ok?'tag-live':'tag-loss'}">PM ${s.pm_ok?'OK':'ERR ('+((s.errors||{}).pm_ws||0)+')'}</span></td>
                <td class="d">${JSON.stringify(s.errors||{})}</td>
            </tr>`;
        });
        el.innerHTML=`
        <div class="section-header"><div class="section-title">VPS Health</div><button class="btn btn-blue btn-sm" onclick="navigate('status')">Refresh</button></div>
        <div class="stats-grid" style="margin-bottom:16px">
            <div class="stat-box"><div class="stat-label">VPS</div><div class="stat-value ${h.vps_reachable?'g':'r'}">${h.vps_reachable?'Online':'Offline'}</div></div>
            <div class="stat-box"><div class="stat-label">Memory</div><div class="stat-value b">${h.memory||'\u2014'}</div></div>
            <div class="stat-box"><div class="stat-label">Uptime</div><div class="stat-value d">${h.uptime||'\u2014'}</div></div>
            <div class="stat-box"><div class="stat-label">Sessions</div><div class="stat-value cy">${Object.keys(h.sessions||{}).length}</div></div>
        </div>
        <div class="card"><div class="card-header"><h3>Session Health</h3></div>
            <div style="overflow-x:auto"><table><thead><tr><th>Session</th><th>systemd</th><th>Last Update</th><th>Binance WS</th><th>PM WS</th><th>Errors</th></tr></thead>
            <tbody>${sessRows||'<tr><td colspan="6" class="d">No sessions found</td></tr>'}</tbody></table></div>
        </div>`;
    } catch(e) {
        el.innerHTML='<div class="empty"><div class="empty-title r">Failed to reach VPS</div><div class="d" style="margin-top:4px">'+e.message+'</div></div>';
    }
}

// ═══ All Trades ═══
function renderTrades(el) {
    if (!S.allTrades) return;
    const trades=S.allTrades;
    const sessions=[...new Set(trades.map(t=>t._session))].sort();
    const combos=[...new Set(trades.map(t=>t.combo).filter(Boolean))].sort();
    const settled=trades.filter(t=>t.result==='WIN'||t.result==='LOSS');
    const wins=settled.filter(t=>t.result==='WIN').length;
    const totalPnl=settled.reduce((a,t)=>a+parseFloat(t.pnl_taker||0),0);
    const wr=settled.length>0?(wins/settled.length*100):0;

    let rows='';
    trades.slice(0,500).forEach(t=>{
        const p=parseFloat(t.pnl_taker||0);
        const tag=t.result==='WIN'?'<span class="tag tag-win">WIN</span>':t.result==='LOSS'?'<span class="tag tag-loss">LOSS</span>':'<span class="tag tag-dead">PENDING</span>';
        const entry=t.fill_price?(parseFloat(t.fill_price)*100).toFixed(1)+'\u00a2':'?';
        const cost=t.total_cost?'$'+parseFloat(t.total_cost).toFixed(0):'\u2014';
        rows+=`<tr data-session="${t._session}" data-combo="${t.combo||''}" data-result="${t.result||''}"><td class="d">${ftDate(parseFloat(t.timestamp||0))}</td><td><span class="tag tag-session">${t._session}</span></td><td style="font-weight:600">${t.combo||'?'}</td><td>${t.direction||'?'}</td><td>${entry}</td><td>${t.filled_size||'?'}</td><td>${cost}</td><td>${t.impulse_bps?parseFloat(t.impulse_bps).toFixed(1)+'bp':'?'}</td><td>${t.time_remaining?Math.round(parseFloat(t.time_remaining))+'s':'?'}</td><td>${tag}</td><td class="${pc(p)}" style="font-weight:700">${t.pnl_taker?fp(p):'\u2014'}</td></tr>`;
    });

    el.innerHTML=`
    <div class="section-header"><div class="section-title">All Trades</div><button class="btn btn-blue btn-sm" onclick="syncData()" id="sync-btn">Sync from VPS</button></div>
    <div class="stats-grid" style="margin-bottom:14px">
      <div class="stat-box"><div class="stat-label">Total PnL</div><div class="stat-value ${pc(totalPnl)}">${fp(totalPnl)}</div></div>
      <div class="stat-box"><div class="stat-label">Win Rate</div><div class="stat-value ${wc(wr)}">${wr.toFixed(1)}%</div></div>
      <div class="stat-box"><div class="stat-label">Settled</div><div class="stat-value b">${settled.length}</div></div>
      <div class="stat-box"><div class="stat-label">Sessions</div><div class="stat-value cy">${sessions.length}</div></div>
    </div>
    <div class="filters">
      <select class="filter-select" id="f-sess" onchange="filterT()"><option value="">All Sessions (${sessions.length})</option>${sessions.map(s=>`<option value="${s}">${s}</option>`).join('')}</select>
      <select class="filter-select" id="f-combo" onchange="filterT()"><option value="">All Combos (${combos.length})</option>${combos.map(c=>`<option value="${c}">${c}</option>`).join('')}</select>
      <select class="filter-select" id="f-res" onchange="filterT()"><option value="">All Results</option><option value="WIN">WIN</option><option value="LOSS">LOSS</option></select>
      <span class="d mono" style="font-size:10px" id="tc">${settled.length} settled / ${trades.length} total</span>
    </div>
    <div class="card"><div style="overflow-x:auto;max-height:70vh;overflow-y:auto">
      <table id="tt"><thead><tr><th>Time</th><th>Session</th><th>Combo</th><th>Dir</th><th>Entry</th><th>Size</th><th>Cost</th><th>Impulse</th><th>T-rem</th><th>Result</th><th>PnL</th></tr></thead><tbody>${rows}</tbody></table>
    </div></div>
    ${trades.length>500?'<div class="d" style="text-align:center;padding:12px;font-size:11px">Showing first 500 of '+trades.length+' trades</div>':''}`;
}
function filterT(){const s=document.getElementById('f-sess').value,c=document.getElementById('f-combo').value,r=document.getElementById('f-res').value;let n=0;document.querySelectorAll('#tt tbody tr').forEach(row=>{const m=(!s||row.dataset.session===s)&&(!c||row.dataset.combo===c)&&(!r||row.dataset.result===r);row.style.display=m?'':'none';if(m)n++});document.getElementById('tc').textContent=n+' shown'}

// ═══ Analysis ═══
const COLORS=['#10b981','#4d8ef7','#ef4444','#f59e0b','#8b5cf6','#06b6d4','#f97316','#64748b','#ec4899','#14b8a6'];
let chartInstances={};
function destroyCharts(){Object.values(chartInstances).forEach(c=>{try{c.destroy()}catch(e){}});chartInstances={}}
function makeChart(id,cfg){const ctx=document.getElementById(id);if(!ctx)return;chartInstances[id]=new Chart(ctx,cfg)}

async function pullAndAnalyze(){
    const btn=document.getElementById('pa-btn');
    if(btn){btn.textContent='Pulling & analyzing...';btn.disabled=true}
    try{
        S.chartData=await(await fetch('/api/pull-analyze',{method:'POST'})).json();
        S.analysisData=null; S.allTrades=null;
        await loadSessions();
        renderAnalysis(document.getElementById('content'));
    }catch(e){console.error(e)}
    if(btn){btn.textContent='Sync & Analyze';btn.disabled=false}
}

async function loadChartData(){
    if(S.chartData) return;
    S.chartData = await api('/api/chart-data');
}

async function renderAnalysis(el) {
    if (!S.analysisData && !S.chartData) {
        el.innerHTML='<div class="loading"><div class="spinner"></div> Loading analysis...</div>';
        try{
            const [ad,cd]=await Promise.all([api('/api/analysis'),api('/api/chart-data')]);
            S.analysisData=ad; S.chartData=cd;
        }catch(e){el.innerHTML='<div class="empty r">Failed to load</div>';return}
    }
    if(!S.analysisData) S.analysisData=await api('/api/analysis');
    if(!S.chartData) S.chartData=await api('/api/chart-data');
    destroyCharts();

    const d=S.analysisData, cd=S.chartData;
    const ss=d.sessions||[], bc=d.best_combos||[], archs=d.architectures||[];
    const sessions=cd.sessions||{};
    const sortedNames=Object.keys(sessions).sort((a,b)=>(sessions[b].pnl||0)-(sessions[a].pnl||0));

    // Session-level risk metrics lookup from analysis data
    const sessRisk={};
    ss.forEach(s=>{sessRisk[s.name]=s});

    // Architecture summary cards
    let archCards='';
    archs.forEach(a=>{
        const sortStr=a.sortino>=99?'INF':a.sortino.toFixed(2);
        archCards+=`<div class="card" style="padding:14px;min-width:180px">
            <div style="font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">${a.architecture.replace(/_/g,' ')}</div>
            <div class="${pc(a.pnl)}" style="font-size:20px;font-weight:700">${fp(a.pnl)}</div>
            <div style="display:flex;gap:12px;margin-top:6px;font-size:11px">
                <span>${a.trades} trades</span>
                <span class="${wc(a.wr)}">${a.wr}%</span>
            </div>
            <div style="display:flex;gap:10px;margin-top:4px;font-size:10px;color:var(--dim)">
                <span>E[X] <b style="color:${(a.expectancy||0)>0?'var(--green)':(a.expectancy||0)<0?'var(--red)':'var(--dim)'}">${fps(a.expectancy||0)}</b></span>
                <span>R:R <b style="color:${(a.rr||0)>=0.8?'var(--green)':(a.rr||0)>=0.5?'var(--yellow)':'var(--red)'}">${(a.rr||0).toFixed(2)}</b></span>
                <span>MaxDD <b style="color:${a.max_dd===0?'var(--green)':'var(--red)'}">$${a.max_dd}</b></span>
            </div>
            <div style="display:flex;gap:10px;margin-top:2px;font-size:9px;color:var(--dim)">
                <span>AvgW <b class="g">$${(a.avg_win||0).toFixed(0)}</b></span>
                <span>AvgL <b class="r">$${(a.avg_loss||0).toFixed(0)}</b></span>
                <span>Sharpe <b>${a.sharpe.toFixed(2)}</b></span>
            </div>
        </div>`;
    });

    // Leaderboard
    let lbRows='';
    sortedNames.forEach((name,i)=>{
        const s=sessions[name];
        const r=sessRisk[name]||{};
        const exp=r.expectancy||0;
        const rr=r.rr||0;
        const aw=r.avg_win||0;
        const al=r.avg_loss||0;
        const maxdd=r.max_dd||0;
        lbRows+=`<tr style="cursor:pointer" onclick="navigate('session','${name}')">
            <td style="font-weight:700;color:${i<3?'var(--yellow)':'var(--dim)'}">#${i+1}</td>
            <td style="font-weight:700;color:var(--text)">${name}</td>
            <td><span class="tag" style="background:rgba(6,182,212,.1);color:var(--cyan);border:1px solid rgba(6,182,212,.2);font-size:9px">${s.architecture||'?'}</span></td>
            <td>${s.trades}</td>
            <td class="${wc(s.wr)}">${s.wr}%</td>
            <td class="${pc(s.pnl)}" style="font-weight:700">${fp(s.pnl)}</td>
            <td class="${pc(s.avg_pnl)}">${fps(s.avg_pnl)}</td>
            <td style="font-weight:700;color:${exp>0?'var(--green)':exp<0?'var(--red)':'var(--dim)'}">${fps(exp)}</td>
            <td style="color:${rr>=0.8?'var(--green)':rr>=0.5?'var(--yellow)':'var(--red)'}">${rr.toFixed(2)}</td>
            <td class="g" style="font-size:10px">$${aw.toFixed(0)}</td>
            <td class="r" style="font-size:10px">$${al.toFixed(0)}</td>
            <td style="color:${maxdd===0?'var(--green)':'var(--red)'}">$${maxdd}</td></tr>`;
    });

    // Best combos
    let bcRows='';
    bc.forEach(c=>{bcRows+=`<tr><td><span class="tag tag-session">${c.session}</span></td><td style="font-weight:600">${c.combo}</td><td>${c.n}</td><td class="${wc(c.wr)}">${c.wr}%</td><td class="${pc(c.pnl)}" style="font-weight:700">${fp(c.pnl)}</td><td class="${pc(c.avg_pnl)}">${fps(c.avg_pnl)}</td></tr>`});

    // Per-session sections
    let sessionSections='';
    sortedNames.forEach((name,i)=>{
        const s=sessions[name];
        let cRows='';
        Object.entries(s.combos||{}).sort((a,b)=>b[1].pnl-a[1].pnl).forEach(([cn,cv])=>{
            cRows+=`<tr><td style="font-weight:600">${cn}</td><td>${cv.trades}</td><td class="${wc(cv.wr)}">${cv.wr}%</td><td class="${pc(cv.pnl)}" style="font-weight:700">${fp(cv.pnl)}</td><td class="${pc(cv.avg_pnl)}">${fps(cv.avg_pnl)}</td></tr>`;
        });
        sessionSections+=`
        <div class="analysis-section">
            <div class="analysis-section-title collapsible" onclick="this.classList.toggle('open');this.nextElementSibling.classList.toggle('show');setTimeout(()=>buildSessionCharts('${name}'),100)">
                ${name} <span class="tag" style="background:rgba(6,182,212,.1);color:var(--cyan);border:1px solid rgba(6,182,212,.2);font-size:9px;margin-left:6px">${s.architecture||'?'}</span>
                <span class="badge">${s.trades} trades</span>
                <span class="${pc(s.pnl)}" style="font-weight:700;margin-left:auto">${fp(s.pnl)}</span>
                <span class="${wc(s.wr)}" style="margin-left:8px">${s.wr}%</span>
            </div>
            <div class="collapse-body" id="cb-${name}">
                <div style="overflow-x:auto;margin-bottom:12px"><table><thead><tr><th>Combo</th><th>Trades</th><th>Win%</th><th>PnL</th><th>$/Trade</th></tr></thead><tbody>${cRows}</tbody></table></div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
                    <div class="card" style="padding:12px"><div style="height:280px"><canvas id="sc-pnl-${name}"></canvas></div></div>
                    <div class="card" style="padding:12px"><div style="height:280px"><canvas id="sc-entry-${name}"></canvas></div></div>
                </div>
            </div>
        </div>`;
    });

    // A/B Test Comparisons
    const abTests=d.ab_tests||[];
    let abSection='';
    if(abTests.length>0){
        let abCards='';
        abTests.forEach(ab=>{
            const sNames=ab.sessions.map(s=>s.name);
            const metrics=[
                {key:'trades',label:'Trades',fmt:v=>v,higher:true},
                {key:'wr',label:'WR%',fmt:v=>v+'%',higher:true},
                {key:'pnl',label:'PnL',fmt:v=>fp(v),higher:true},
                {key:'avg_pnl',label:'$/Trade',fmt:v=>fps(v),higher:true},
                {key:'sharpe',label:'Sharpe',fmt:v=>v.toFixed(2),higher:true},
                {key:'max_dd',label:'MaxDD',fmt:v=>'$'+v,higher:false},
            ];
            let headerCols=sNames.map(n=>`<th style="text-align:center;min-width:120px">${n}</th>`).join('');
            let metricRows='';
            metrics.forEach(m=>{
                const vals=ab.sessions.map(s=>s[m.key]);
                const bestVal=m.higher?Math.max(...vals):Math.min(...vals);
                let cells=ab.sessions.map(s=>{
                    const v=s[m.key];
                    const isBest=v===bestVal&&vals.filter(x=>x===bestVal).length===1;
                    const bg=isBest?'background:rgba(16,185,129,.12);':'';
                    const clr=isBest?'color:var(--green);font-weight:700':'';
                    return `<td style="text-align:center;${bg}${clr}">${m.fmt(v)}</td>`;
                }).join('');
                metricRows+=`<tr><td style="font-weight:600;color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.5px">${m.label}</td>${cells}</tr>`;
            });
            // Verdict badge
            let verdict='';
            if(ab.winner==='insufficient data'){
                verdict=`<span class="tag" style="background:rgba(245,158,11,.1);color:var(--yellow);border:1px solid rgba(245,158,11,.2);font-size:10px;padding:3px 10px">INCONCLUSIVE &mdash; insufficient data (&lt;30 trades)</span>`;
            }else if(ab.winner==='inconclusive'){
                verdict=`<span class="tag" style="background:rgba(245,158,11,.1);color:var(--yellow);border:1px solid rgba(245,158,11,.2);font-size:10px;padding:3px 10px">INCONCLUSIVE &mdash; WR diff &lt;2pp</span>`;
            }else{
                verdict=`<span class="tag" style="background:rgba(16,185,129,.1);color:var(--green);border:1px solid rgba(16,185,129,.2);font-size:10px;padding:3px 10px">WINNER: ${ab.winner}</span>`;
            }
            // Collapsible per-combo breakdown
            const abId='ab-'+ab.architecture.replace(/[^a-zA-Z0-9]/g,'');
            let comboBreakdown='';
            // Gather all unique combo names across sessions
            const allComboNames=new Set();
            ab.sessions.forEach(s=>(s.combos||[]).forEach(c=>allComboNames.add(c.combo)));
            if(allComboNames.size>0){
                const comboArr=[...allComboNames].sort();
                let comboHeaderCols=sNames.map(n=>`<th colspan="3" style="text-align:center;border-bottom:1px solid var(--border)">${n}</th>`).join('');
                let comboSubHeader=sNames.map(()=>`<th style="text-align:center;font-size:9px">Trades</th><th style="text-align:center;font-size:9px">WR%</th><th style="text-align:center;font-size:9px">PnL</th>`).join('');
                let comboRows='';
                comboArr.forEach(cn=>{
                    let cells=ab.sessions.map(s=>{
                        const cm=(s.combos||[]).find(c=>c.combo===cn);
                        if(!cm) return `<td style="text-align:center;color:var(--dim)">-</td><td style="text-align:center;color:var(--dim)">-</td><td style="text-align:center;color:var(--dim)">-</td>`;
                        return `<td style="text-align:center">${cm.n}</td><td style="text-align:center" class="${wc(cm.wr)}">${cm.wr}%</td><td style="text-align:center;font-weight:600" class="${pc(cm.pnl)}">${fp(cm.pnl)}</td>`;
                    }).join('');
                    comboRows+=`<tr><td style="font-weight:600">${cn}</td>${cells}</tr>`;
                });
                comboBreakdown=`
                <div style="margin-top:10px">
                    <div class="collapsible" style="font-size:11px;color:var(--dim);padding:4px 0" onclick="this.classList.toggle('open');this.nextElementSibling.classList.toggle('show')">Per-Combo Breakdown</div>
                    <div class="collapse-body">
                        <div style="overflow-x:auto;margin-top:6px"><table><thead><tr><th>Combo</th>${comboHeaderCols}</tr><tr><th></th>${comboSubHeader}</tr></thead><tbody>${comboRows}</tbody></table></div>
                    </div>
                </div>`;
            }
            abCards+=`<div class="card" style="margin-bottom:12px">
                <div class="card-header"><h3>${ab.architecture.replace(/_/g,' ')}</h3><div>${verdict}</div></div>
                <div style="padding:14px 18px">
                    <div style="overflow-x:auto"><table><thead><tr><th>Metric</th>${headerCols}</tr></thead><tbody>${metricRows}</tbody></table></div>
                    ${comboBreakdown}
                </div>
            </div>`;
        });
        abSection=`
        <div class="analysis-section">
            <div class="analysis-section-title"><span style="color:var(--purple)">&#9878;</span> A/B Test Comparisons</div>
            ${abCards}
        </div>`;
    }

    el.innerHTML=`
    <div class="section-header"><div class="section-title">Analysis</div>
        <button class="btn btn-blue btn-sm" id="pa-btn" onclick="pullAndAnalyze()">Sync & Analyze</button>
    </div>

    <div class="analysis-section">
        <div class="analysis-section-title"><span style="color:var(--cyan)">&#9670;</span> Architecture Performance</div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">${archCards}</div>
    </div>

    ${abSection}

    <div class="analysis-section">
        <div class="analysis-section-title"><span style="color:var(--yellow)">&#9733;</span> Session Leaderboard</div>
        <div class="card"><div style="overflow-x:auto"><table><thead><tr><th>#</th><th>Session</th><th>Arch</th><th>Trades</th><th>Win%</th><th>PnL</th><th>$/Trade</th><th>E[X]</th><th>R:R</th><th>AvgW</th><th>AvgL</th><th>MaxDD</th></tr></thead><tbody>${lbRows}</tbody></table></div></div>
    </div>

    <!-- Interactive Charts -->
    <div class="card" style="padding:16px;margin-bottom:14px"><div style="height:400px"><canvas id="chart-cum-pnl"></canvas></div></div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px">
        <div class="card" style="padding:16px"><div style="height:280px"><canvas id="chart-pnl-bar"></canvas></div></div>
        <div class="card" style="padding:16px"><div style="height:280px"><canvas id="chart-wr-bar"></canvas></div></div>
        <div class="card" style="padding:16px"><div style="height:280px"><canvas id="chart-avg-pnl"></canvas></div></div>
    </div>

    <div class="analysis-section">
        <div class="analysis-section-title"><span style="color:var(--green)">&#9650;</span> Best Combos <span class="badge">Top 20</span></div>
        <div class="card"><div style="overflow-x:auto;max-height:400px;overflow-y:auto"><table><thead><tr><th>Session</th><th>Combo</th><th>Trades</th><th>Win%</th><th>PnL</th><th>$/Trade</th></tr></thead><tbody>${bcRows}</tbody></table></div></div>
    </div>

    <div class="section-header" style="margin-top:20px"><div class="section-title">Per-Session Breakdown</div><div class="d" style="font-size:10px">Click to expand</div></div>
    ${sessionSections}`;

    // Build main charts — crosshair plugin for vertical line on hover
    const crosshairPlugin={id:'crosshair',afterDraw(chart){if(chart._crosshairX==null)return;const ctx=chart.ctx,y=chart.chartArea;ctx.save();ctx.beginPath();ctx.moveTo(chart._crosshairX,y.top);ctx.lineTo(chart._crosshairX,y.bottom);ctx.lineWidth=1;ctx.strokeStyle='rgba(255,255,255,.2)';ctx.setLineDash([4,4]);ctx.stroke();ctx.restore()},afterEvent(chart,args){const e=args.event;if(e.type==='mousemove'&&chart.chartArea){const x=e.x;chart._crosshairX=(x>=chart.chartArea.left&&x<=chart.chartArea.right)?x:null}else if(e.type==='mouseout'){chart._crosshairX=null}chart.draw()}};

    const ttStyle={backgroundColor:'rgba(17,22,34,.95)',titleColor:'#d1d5e0',bodyColor:'#d1d5e0',borderColor:'#243049',borderWidth:1,padding:10,bodyFont:{family:'JetBrains Mono',size:11},titleFont:{family:'Inter',size:12,weight:'600'},cornerRadius:6,displayColors:true,boxWidth:8,boxHeight:8,boxPadding:4};
    const legendCfg={labels:{color:'#d1d5e0',font:{family:'Inter',size:10},boxWidth:12,padding:8,usePointStyle:true},onClick(e,item,legend){const ci=legend.chart;const i=item.datasetIndex;ci.isDatasetVisible(i)?ci.hide(i):ci.show(i)}};
    const gridCfg={color:'rgba(26,34,54,.5)'};
    const tickCfg={color:'#5a6478',font:{size:10}};

    const barOpts={responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:ttStyle},scales:{x:{ticks:tickCfg,grid:gridCfg},y:{ticks:tickCfg,grid:gridCfg}}};

    // 1. Cumulative PnL — crosshair mode, rich tooltip
    const cumDs=sortedNames.map((name,i)=>({
        label:name,
        data:sessions[name].cum_pnl,
        borderColor:COLORS[i%COLORS.length],backgroundColor:COLORS[i%COLORS.length]+'20',
        borderWidth:2,pointRadius:0,pointHoverRadius:5,pointHoverBorderWidth:2,
        pointHoverBackgroundColor:COLORS[i%COLORS.length],tension:.15,fill:false
    }));
    const maxLen=Math.max(...sortedNames.map(n=>sessions[n].cum_pnl.length));
    makeChart('chart-cum-pnl',{type:'line',data:{labels:Array.from({length:maxLen},(_,i)=>i+1),datasets:cumDs},
        options:{responsive:true,maintainAspectRatio:false,
            interaction:{mode:'index',intersect:false},
            hover:{mode:'index',intersect:false},
            plugins:{legend:legendCfg,crosshair:true,
                tooltip:{...ttStyle,mode:'index',intersect:false,
                    callbacks:{
                        title(items){return 'Trade #'+items[0].label},
                        label(ctx){const v=ctx.parsed.y;const s=sessions[sortedNames[ctx.datasetIndex]];
                            const wr=s?s.wr+'%':'';const tr=s?s.trades+' trades':'';
                            return ' '+ctx.dataset.label+': $'+(v>=0?'+':'')+v.toFixed(0)},
                        afterBody(items){const vals=items.filter(i=>i.parsed.y!=null).map(i=>({name:i.dataset.label,val:i.parsed.y}));
                            vals.sort((a,b)=>b.val-a.val);
                            const best=vals[0],worst=vals[vals.length-1];
                            return best?['\nBest: '+best.name+' $'+(best.val>=0?'+':'')+best.val.toFixed(0),
                                         'Worst: '+worst.name+' $'+(worst.val>=0?'+':'')+worst.val.toFixed(0)]:[]}
                    },itemSort:(a,b)=>b.parsed.y-a.parsed.y},
                title:{display:true,text:'Cumulative PnL — All Sessions (hover to compare, click legend to toggle)',color:'#d1d5e0',font:{size:12}}},
            scales:{x:{ticks:{...tickCfg,maxTicksLimit:20},grid:gridCfg},y:{ticks:{...tickCfg,callback:v=>'$'+v.toLocaleString()},grid:gridCfg}}
        },plugins:[crosshairPlugin]});

    // 2. PnL bar
    makeChart('chart-pnl-bar',{type:'bar',data:{labels:sortedNames,datasets:[{label:'PnL ($)',data:sortedNames.map(n=>sessions[n].pnl),
        backgroundColor:sortedNames.map(n=>sessions[n].pnl>=0?'#10b981':'#ef4444')}]},
        options:{...barOpts,indexAxis:'y',plugins:{...barOpts.plugins,
            tooltip:{...ttStyle,callbacks:{label(ctx){const n=ctx.label;const s=sessions[n];return[' PnL: $'+(s.pnl>=0?'+':'')+s.pnl,' Win Rate: '+s.wr+'%',' Trades: '+s.trades,' $/Trade: $'+(s.avg_pnl>=0?'+':'')+s.avg_pnl]}}},
            title:{display:true,text:'Total PnL by Session',color:'#d1d5e0',font:{size:12}}}}});

    // 3. Win Rate bar
    makeChart('chart-wr-bar',{type:'bar',data:{labels:sortedNames,datasets:[{label:'Win Rate (%)',data:sortedNames.map(n=>sessions[n].wr),
        backgroundColor:sortedNames.map(n=>sessions[n].wr>=65?'#10b981':sessions[n].wr>=55?'#f59e0b':'#ef4444')}]},
        options:{...barOpts,indexAxis:'y',plugins:{...barOpts.plugins,
            tooltip:{...ttStyle,callbacks:{label(ctx){const n=ctx.label;const s=sessions[n];return[' Win Rate: '+s.wr+'%',' '+s.wins+'W / '+(s.trades-s.wins)+'L',' PnL: $'+(s.pnl>=0?'+':'')+s.pnl]}}},
            title:{display:true,text:'Win Rate by Session',color:'#d1d5e0',font:{size:12}}},scales:{...barOpts.scales,x:{...barOpts.scales.x,min:0,max:100}}}});

    // 4. Avg PnL/trade bar
    makeChart('chart-avg-pnl',{type:'bar',data:{labels:sortedNames,datasets:[{label:'$/Trade',data:sortedNames.map(n=>sessions[n].avg_pnl),
        backgroundColor:sortedNames.map(n=>sessions[n].avg_pnl>=0?'#10b981':'#ef4444')}]},
        options:{...barOpts,indexAxis:'y',plugins:{...barOpts.plugins,
            tooltip:{...ttStyle,callbacks:{label(ctx){const n=ctx.label;const s=sessions[n];return[' $/Trade: $'+(s.avg_pnl>=0?'+':'')+s.avg_pnl,' Total PnL: $'+(s.pnl>=0?'+':'')+s.pnl,' '+s.trades+' trades']}}},
            title:{display:true,text:'Avg PnL per Trade',color:'#d1d5e0',font:{size:12}}}}});
}

function buildSessionCharts(name){
    if(!S.chartData||chartInstances['sc-pnl-'+name])return;
    const s=S.chartData.sessions[name];if(!s)return;
    const combos=s.combos||{};
    const cNames=Object.keys(combos).sort((a,b)=>(combos[b].pnl||0)-(combos[a].pnl||0));

    const crosshairPlugin={id:'crosshair-'+name,afterDraw(chart){if(chart._crosshairX==null)return;const ctx=chart.ctx,y=chart.chartArea;ctx.save();ctx.beginPath();ctx.moveTo(chart._crosshairX,y.top);ctx.lineTo(chart._crosshairX,y.bottom);ctx.lineWidth=1;ctx.strokeStyle='rgba(255,255,255,.2)';ctx.setLineDash([4,4]);ctx.stroke();ctx.restore()},afterEvent(chart,args){const e=args.event;if(e.type==='mousemove'&&chart.chartArea){chart._crosshairX=(e.x>=chart.chartArea.left&&e.x<=chart.chartArea.right)?e.x:null}else if(e.type==='mouseout'){chart._crosshairX=null}chart.draw()}};
    const ttStyle={backgroundColor:'rgba(17,22,34,.95)',titleColor:'#d1d5e0',bodyColor:'#d1d5e0',borderColor:'#243049',borderWidth:1,bodyFont:{family:'JetBrains Mono',size:10},cornerRadius:6,displayColors:true,boxWidth:6,boxHeight:6};
    const legendCfg={labels:{color:'#d1d5e0',font:{size:9},boxWidth:10,usePointStyle:true},onClick(e,item,legend){const ci=legend.chart;const i=item.datasetIndex;ci.isDatasetVisible(i)?ci.hide(i):ci.show(i)}};
    const tickCfg={color:'#5a6478',font:{size:9}};const gridCfg={color:'rgba(26,34,54,.5)'};

    const ds=cNames.slice(0,8).map((cn,i)=>({label:cn+' ($'+(combos[cn].pnl>=0?'+':'')+combos[cn].pnl+')',
        data:combos[cn].cum_pnl,borderColor:COLORS[i%COLORS.length],borderWidth:1.5,pointRadius:0,pointHoverRadius:4,tension:.15,fill:false}));
    const maxL=Math.max(...cNames.slice(0,8).map(cn=>combos[cn].cum_pnl.length));
    makeChart('sc-pnl-'+name,{type:'line',data:{labels:Array.from({length:maxL},(_,i)=>i+1),datasets:ds},
        options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},hover:{mode:'index',intersect:false},
            plugins:{legend:legendCfg,tooltip:{...ttStyle,mode:'index',intersect:false,itemSort:(a,b)=>b.parsed.y-a.parsed.y,
                callbacks:{title(items){return 'Trade #'+items[0].label},label(ctx){return ' '+ctx.dataset.label.split(' (')[0]+': $'+(ctx.parsed.y>=0?'+':'')+ctx.parsed.y.toFixed(0)}}},
                title:{display:true,text:name+' \u2014 Combo PnL (hover to compare)',color:'#d1d5e0',font:{size:11}}},
            scales:{x:{ticks:{...tickCfg,maxTicksLimit:15},grid:gridCfg},y:{ticks:{...tickCfg,callback:v=>'$'+v},grid:gridCfg}}},
        plugins:[crosshairPlugin]});

    // Entry price analysis
    const eb=s.entry_buckets||{};
    const eLabels=Object.keys(eb);
    if(eLabels.length>0){
        makeChart('sc-entry-'+name,{type:'bar',data:{labels:eLabels,datasets:[
            {label:'Win Rate %',data:eLabels.map(l=>eb[l].wr),backgroundColor:eLabels.map(l=>eb[l].wr>=65?'#10b981':eb[l].wr>=55?'#f59e0b':'#ef4444'),yAxisID:'y'},
            {label:'PnL $',data:eLabels.map(l=>eb[l].pnl),backgroundColor:eLabels.map(l=>eb[l].pnl>=0?'rgba(77,142,247,.6)':'rgba(239,68,68,.4)'),yAxisID:'y1'}
        ]},options:{...chartOpts,plugins:{...chartOpts.plugins,title:{display:true,text:name+' — Entry Price Analysis',color:'#d1d5e0',font:{size:11}}},scales:{...chartOpts.scales,y:{position:'left',ticks:{color:'#5a6478',font:{size:9}},grid:{color:'rgba(26,34,54,.5)'}},y1:{position:'right',ticks:{color:'#5a6478',font:{size:9}},grid:{display:false}}}}});
    }
}

// ═══ Actions ═══
async function syncData(){
    const btn=document.getElementById('sync-btn');
    if(btn){btn.textContent='Syncing...';btn.disabled=true}
    try{
        await fetch('/api/sync',{method:'POST'});
        S.allTrades=null; S.analysisData=null; // Invalidate caches
        await loadSessions();
        if(S.page==='trades') loadAllTrades();
        else if(S.page==='session'&&S.selectedSession) loadSession(S.selectedSession);
        else if(S.page==='analysis') loadAnalysis();
        else render();
    }catch(e){}
    if(btn){btn.textContent='Sync from VPS';btn.disabled=false}
}
async function stopSess(n,w){if(!confirm('Stop '+n+'?'))return;await fetch('/stop/'+w+'/'+n);setTimeout(loadSessions,2000)}
async function startSess(n,w){await fetch('/start/'+w+'/'+n);setTimeout(loadSessions,2000)}
async function deleteSess(n){if(!confirm('DELETE session '+n+'? This removes config and data.')){return}
    S.sessions=S.sessions.filter(s=>s.name!==n);delete S.sessionData[n];S.allTrades=null;S.analysisData=null;S.chartData=null;
    navigate('overview');
    await fetch('/delete/'+n,{method:'POST'});
    loadSessions()}

// ═══ Refresh Timer ═══
let refreshInterval=3, refreshCountdown=3, refreshPaused=false, refreshTimer=null;
function tickRefresh(){
    if(refreshPaused)return;
    refreshCountdown--;
    updateRefreshUI();
    if(refreshCountdown<=0){
        refreshCountdown=refreshInterval;
        loadSessions();
        if(S.page==='session'&&S.selectedSession) refreshSessionLive(S.selectedSession);
    }
}
async function refreshSessionLive(name){
    // Use fast /api/live endpoint (reads cache, no scp, ~1ms)
    try{
        const fresh=await api('/api/live/'+name);
        // Merge live data into cached session data
        const old=S.sessionData[name]||{};
        Object.assign(old,{
            btc_price:fresh.btc_price, window_open:fresh.window_open,
            book_bid:fresh.book_bid, book_ask:fresh.book_ask, book_spread:fresh.book_spread,
            current_window:fresh.current_window, window_end:fresh.window_end,
            window_active:fresh.window_active, updated:fresh.updated,
            health:fresh.health, errors:fresh.errors,
            combos:fresh.combos, recent_windows:fresh.recent_windows,
            trades:fresh.trades, wins:fresh.wins, wr:fresh.wr,
            windows_settled:fresh.windows_settled,
        });
        S.sessionData[name]=old;
        // Update live values in-place (no DOM rebuild)
        const u=id=>document.getElementById(id);
        if(u('live-btc')) u('live-btc').textContent=fresh.btc_price?'$'+Number(fresh.btc_price).toLocaleString('en-US',{minimumFractionDigits:2}):'\u2014';
        if(u('live-ptb')) u('live-ptb').textContent=fresh.window_open?'$'+Number(fresh.window_open).toLocaleString('en-US',{minimumFractionDigits:2}):'\u2014';
        if(u('live-bid')) u('live-bid').textContent=fresh.book_bid?(fresh.book_bid*100).toFixed(1)+'\u00a2':'\u2014';
        if(u('live-ask')) u('live-ask').textContent=fresh.book_ask?(fresh.book_ask*100).toFixed(1)+'\u00a2':'\u2014';
        if(u('live-spr')) u('live-spr').textContent=fresh.book_spread?(fresh.book_spread*100).toFixed(1)+'\u00a2':'\u2014';
        if(u('live-nobid')) u('live-nobid').textContent=fresh.book_ask?((1-fresh.book_ask)*100).toFixed(1)+'\u00a2':'\u2014';
        if(u('live-noask')) u('live-noask').textContent=fresh.book_bid?((1-fresh.book_bid)*100).toFixed(1)+'\u00a2':'\u2014';
        if(u('live-updated')) u('live-updated').textContent=ago(fresh.updated);
        if(u('live-window')&&fresh.current_window) u('live-window').textContent=winRange(fresh.current_window);
        // Don't auto-reload on new trades — user clicks Sync manually
        // This prevents table/chart flickering every 3 seconds
    }catch(e){}
}
function togglePause(){
    refreshPaused=!refreshPaused;
    updateRefreshUI();
}
function updateRefreshUI(){
    const el=document.getElementById('refresh-info');
    if(!el)return;
    const connected = evtSource && evtSource.readyState === 1;
    if(connected){
        el.innerHTML='<span class="g mono" style="font-size:10px">LIVE</span> <span class="d" style="font-size:9px">WebSocket</span>';
    } else {
        el.innerHTML='<span class="y mono" style="font-size:10px">connecting...</span>';
    }
}

// ═══ Smooth Timer — ticks locally every second ═══
let _lastBtcPrice = 0;

function tickTimer(){
    // Update GLOBAL banner timer from any VPS session data
    const anySession = Object.values(_sseVpsCache)[0];
    if(anySession && anySession.window_end) {
        const now=Date.now()/1000;
        let we=anySession.window_end;
        // If window_end is in the past, predict next window (300s intervals)
        while(we<=now) we+=300;
        const trem=Math.max(0,Math.floor(we-now));
        const pct=Math.min(100,((300-trem)/300)*100);
        const gt=document.getElementById('g-timer');
        const gp=document.getElementById('g-progress');
        if(gt){
            gt.textContent=Math.floor(trem/60)+':'+String(trem%60).padStart(2,'0');
            gt.style.color=trem<60?'var(--red)':trem<120?'var(--yellow)':'var(--blue)';
        }
        if(gp){gp.style.width=pct+'%';gp.style.background=trem<60?'var(--red)':trem<120?'var(--yellow)':'var(--blue)'}
    }

    // Update session detail timer too
    const timerEl=document.getElementById('live-timer');
    const progEl=document.getElementById('live-progress');
    if(timerEl&&S.page==='session') {
        const d=S.selectedSession?S.sessionData[S.selectedSession]:null;
        if(d&&d.window_end) {
            const now=Date.now()/1000;
            let we2=d.window_end;
            while(we2<=now) we2+=300;
            const trem=Math.max(0,Math.floor(we2-now));
            const pct=Math.min(100,((300-trem)/300)*100);
            timerEl.textContent=Math.floor(trem/60)+':'+String(trem%60).padStart(2,'0');
            timerEl.style.color=trem<60?'var(--red)':trem<120?'var(--yellow)':'var(--blue)';
            if(progEl){progEl.style.width=pct+'%';progEl.style.background=trem<60?'var(--red)':trem<120?'var(--yellow)':'var(--blue)'}
        }
    }
}

function updateGlobalBanner(d){
    // BTC price with tick delta
    if(d.btc_price){
        const gb=document.getElementById('g-btc');
        const gd=document.getElementById('g-btc-delta');
        if(gb) gb.textContent='$'+Number(d.btc_price).toLocaleString('en-US',{minimumFractionDigits:2});
        if(gd && _lastBtcPrice > 0){
            const diff = d.btc_price - _lastBtcPrice;
            if(Math.abs(diff) > 0.005) {
                gd.textContent=(diff>=0?'\u25B2':'\u25BC')+Math.abs(diff).toFixed(2);
                gd.style.color=diff>=0?'var(--green)':'var(--red)';
            }
        }
        _lastBtcPrice = d.btc_price;
    }
    // Price to beat
    if(d.window_open){
        const gp=document.getElementById('g-ptb');
        if(gp) gp.textContent='$'+Number(d.window_open).toLocaleString('en-US',{minimumFractionDigits:2});
    }
    // Delta from PTB
    if(d.btc_price && d.window_open && d.window_open > 0){
        const corrected = d.btc_price - (d.btc_price - d.window_open); // approximate
        const delta_bps = ((d.btc_price - d.window_open) / d.window_open * 10000);
        const gdelta=document.getElementById('g-delta');
        if(gdelta){
            gdelta.textContent=(delta_bps>=0?'+':'')+delta_bps.toFixed(1)+'bp';
            gdelta.style.color=delta_bps>=0?'var(--green)':'var(--red)';
        }
    }
    // Window range
    if(d.current_window){
        const gw=document.getElementById('g-window');
        if(gw) gw.textContent=winRange(d.current_window);
    }
    // YES/NO prices
    if(d.book_ask){
        const gy=document.getElementById('g-yes');
        if(gy) gy.textContent=(d.book_ask*100).toFixed(0)+'\u00a2';
    }
    if(d.book_bid){
        const gn=document.getElementById('g-no');
        if(gn) gn.textContent=((1-d.book_bid)*100).toFixed(0)+'\u00a2';
    }
}

// ═══ SSE Real-time Stream — PRIMARY data source ═══
let evtSource = null;
let _sseVpsCache = {};  // session name -> latest VPS stats
let _lastOverviewRender = 0;

function connectSSE(){
    if(evtSource) evtSource.close();
    evtSource = new EventSource('/api/stream');
    evtSource.onopen = () => {
        document.getElementById('status-dot').style.background='var(--green)';
        document.getElementById('status-dot').style.boxShadow='0 0 8px rgba(16,185,129,.4)';
        document.getElementById('status-label').textContent='live';
        updateRefreshUI();
    };
    evtSource.onmessage = (e) => {
        try {
            const data = JSON.parse(e.data);
            if(data.type === 'full_state' || data.type === 'update') {
                const sessions = data.sessions || data.changed || {};
                const removed = data.removed || [];

                // Update VPS cache
                Object.entries(sessions).forEach(([name, stats]) => {
                    _sseVpsCache[name] = stats;
                    // Merge into session data for session detail
                    if(!S.sessionData[name]) S.sessionData[name] = {};
                    Object.assign(S.sessionData[name], stats);
                });
                removed.forEach(n => { delete _sseVpsCache[n]; delete S.sessionData[n]; });

                // Update global banner from any session's data
                const anyD = Object.values(sessions)[0] || Object.values(_sseVpsCache)[0];
                if(anyD) updateGlobalBanner(anyD);

                // Update session detail page in real-time
                if(S.page === 'session' && S.selectedSession) {
                    const d = _sseVpsCache[S.selectedSession];
                    if(d) _updateSessionLive(d);
                }

                // Update overview — but throttle to avoid constant re-renders
                if(S.page === 'overview') {
                    const now = Date.now();
                    if(now - _lastOverviewRender > 2000) { // max 1 render per 2s
                        _lastOverviewRender = now;
                        _updateOverviewFromSSE();
                    }
                }
            }
        } catch(err) { console.error('SSE parse error:', err); }
    };
    evtSource.onerror = () => {
        document.getElementById('status-dot').style.background='var(--yellow)';
        document.getElementById('status-label').textContent='reconnecting...';
    };
}

function _updateSessionLive(d){
    const u=id=>document.getElementById(id);
    // BTC price (this is the Binance price — closest real-time proxy)
    if(u('live-btc')&&d.btc_price) u('live-btc').textContent='$'+Number(d.btc_price).toLocaleString('en-US',{minimumFractionDigits:2});
    // Price to beat (Chainlink-based settlement reference)
    if(u('live-ptb')&&d.window_open) u('live-ptb').textContent='$'+Number(d.window_open).toLocaleString('en-US',{minimumFractionDigits:2});
    // YES book
    if(u('live-bid')&&d.book_bid) u('live-bid').textContent=(d.book_bid*100).toFixed(1)+'\u00a2';
    if(u('live-ask')&&d.book_ask) u('live-ask').textContent=(d.book_ask*100).toFixed(1)+'\u00a2';
    if(u('live-spr')&&d.book_spread!=null) u('live-spr').textContent=(d.book_spread*100).toFixed(1)+'\u00a2';
    // NO book (derived)
    if(u('live-nobid')&&d.book_ask) u('live-nobid').textContent=((1-d.book_ask)*100).toFixed(1)+'\u00a2';
    if(u('live-noask')&&d.book_bid) u('live-noask').textContent=((1-d.book_bid)*100).toFixed(1)+'\u00a2';
    // Updated ago
    if(u('live-updated')&&d.updated) u('live-updated').textContent=ago(d.updated);
    // Window range
    if(u('live-window')&&d.current_window) u('live-window').textContent=winRange(d.current_window);
    // Detect window transition — if window_end changed, timer resets smoothly
    if(d.window_end && S.sessionData[S.selectedSession]) {
        S.sessionData[S.selectedSession].window_end = d.window_end;
        S.sessionData[S.selectedSession].current_window = d.current_window;
    }
    // Don't auto-reload on new trades — user clicks Sync manually
    // This prevents table/chart flickering on SSE updates
}

function _updateOverviewFromSSE(){
    // Build session list purely from SSE VPS cache
    const sessions = Object.entries(_sseVpsCache).map(([name, d]) => ({
        name, where: d._name ? 'vps' : 'local',
        status: d.updated && (Date.now()/1000 - d.updated) < 120 ? 'active' : 'stopped',
        architecture: d.architecture || 'impulse_lag',
        trades: d.trades || 0, wins: d.wins || 0,
        wr: d.wr ? Number(d.wr) : 0,
        pnl_taker: Math.round(d.pnl_taker || 0),
        windows_settled: d.windows_settled || 0,
        updated: d.updated || 0,
        started: d.started || 0,
    }));
    S.sessions = sessions;

    // Re-render overview (updates stats, filters, session cards — preserves chart zone)
    const el = document.getElementById('content');
    if(!el || S.lastRenderedPage !== 'overview') {
        renderOverview(el);
        return;
    }
    renderOverview(el);
    return;

    // Dead code below — kept for reference but bypassed by the return above
    const tot = sessions.reduce((a,s) => a+(s.trades||0), 0);
    const pnl = sessions.reduce((a,s) => a+(s.pnl_taker||0), 0);
    const wins = sessions.reduce((a,s) => a+(s.wins||0), 0);
    const wr = tot > 0 ? (wins/tot*100).toFixed(1) : 0;
    const act = sessions.filter(s => s.status==='active').length;

    const statVals = el.querySelectorAll('.stat-value');
    const statSubs = el.querySelectorAll('.stat-sub');
    if(statVals.length >= 4) {
        statVals[0].textContent = fp(pnl); statVals[0].className = 'stat-value ' + pc(pnl);
        statVals[1].textContent = tot.toLocaleString();
        statVals[2].textContent = wr + '%'; statVals[2].className = 'stat-value ' + wc(wr);
        statVals[3].textContent = act;
    }
    if(statSubs.length >= 3) {
        statSubs[0].textContent = fps(tot > 0 ? pnl/tot : 0) + ' / trade';
        statSubs[1].textContent = sessions.length + ' sessions';
        statSubs[2].textContent = wins + 'W / ' + (tot-wins) + 'L';
    }

    // Update session cards — find by name and update PnL/WR/trades
    sessions.sort((a,b) => (b.pnl_taker||0)-(a.pnl_taker||0));
    const cards = el.querySelectorAll('.session-card');
    // If card count doesn't match, do full re-render
    if(cards.length !== sessions.length) {
        renderOverview(el);
        return;
    }
    cards.forEach((card, i) => {
        const s = sessions[i];
        if(!s) return;
        const vals = card.querySelectorAll('.sc-stat-value');
        if(vals.length >= 4) {
            vals[0].textContent = fp(s.pnl_taker); vals[0].className = 'sc-stat-value ' + pc(s.pnl_taker);
            vals[1].textContent = s.wr + '%'; vals[1].className = 'sc-stat-value ' + wc(s.wr);
            vals[2].textContent = s.trades || 0;
            vals[3].textContent = s.trades > 0 ? fps(s.pnl_taker/s.trades) : '\u2014';
            vals[3].className = 'sc-stat-value ' + pc(s.pnl_taker);
        }
    });
}

// ═══ Init ═══
connectSSE();      // real-time stream from VPS (primary data source)
// Delayed initial REST load — only if SSE hasn't provided data yet
setTimeout(() => { if(S.sessions.length === 0) loadSessions(); }, 3000);
setInterval(tickTimer, 1000);  // local timer tick

// NO REST polling for overview — SSE is the sole data source for live stats.
// Only poll REST for trade CSV data when explicitly requested (session detail, all trades).
setInterval(updateRefreshUI, 3000);
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════
# EDIT/NEW pages
# ══════════════════════════════════════════════════════════════
EDIT_CSS = """<style>
:root{--bg:#08090d;--bg2:#0d1017;--card:#111622;--border:#1a2236;--text:#d1d5e0;--dim:#5a6478;--blue:#4d8ef7;--green:#10b981;--red:#ef4444;--yellow:#f59e0b;--purple:#8b5cf6}
*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px;padding:24px;-webkit-font-smoothing:antialiased}
.c{max-width:600px;margin:0 auto}
h1{font-size:1.2em;margin-bottom:16px;color:var(--blue)}
label{display:block;font-size:.82em;color:var(--dim);margin-bottom:3px;margin-top:12px}
input,textarea{background:var(--bg2);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:6px;font-family:inherit;font-size:.88em;width:100%}
input:focus{border-color:var(--blue);outline:none;box-shadow:0 0 0 2px rgba(77,142,247,.15)}textarea{min-height:60px;resize:vertical}
.btn{display:inline-flex;padding:8px 16px;border-radius:6px;font-size:.84em;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text);font-family:inherit;margin-top:16px;margin-right:6px;transition:all .15s}
.btn-g{border-color:rgba(16,185,129,.3);color:var(--green)}.btn-g:hover{background:var(--green);color:#fff}
.btn-b{border-color:rgba(77,142,247,.3);color:var(--blue)}.btn-b:hover{background:var(--blue);color:#fff}
hr{border:none;border-top:1px solid var(--border);margin:16px 0}
.kr{display:grid;grid-template-columns:1fr 100px;gap:8px;align-items:center;margin-bottom:4px}
.kr label{margin:0;font-size:.82em}.kr input{text-align:right;margin:0}
.flash{padding:10px;border-radius:6px;margin-bottom:12px;font-size:.86em;background:rgba(16,185,129,.08);color:var(--green);border:1px solid rgba(16,185,129,.2)}
.flash-w{background:rgba(245,158,11,.08);color:var(--yellow);border-color:rgba(245,158,11,.2)}
</style><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">"""


# ══════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template_string(FRONTEND)


@app.route("/api/sessions")
def api_sessions():
    return jsonify(get_all_sessions())


@app.route("/api/configs")
def api_configs():
    configs = []
    for f in sorted(CONFIGS_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            configs.append({"name": f.stem, "desc": d.get("_description", "")})
        except Exception:
            configs.append({"name": f.stem, "desc": "error"})
    return jsonify(configs)


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events endpoint — real-time push to frontend."""
    def generate():
        q = queue.Queue(maxsize=100)
        _sse_subscribers.append(q)
        try:
            # Send initial full state
            with _vps_cache_lock:
                initial = dict(_vps_cache)
            yield "data: {}\n\n".format(json.dumps({
                "type": "full_state", "sessions": initial,
                "connected": _vps_connected[0],
            }))
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield "data: {}\n\n".format(msg)
                except queue.Empty:
                    # Keepalive
                    yield ": keepalive\n\n"
        finally:
            try:
                _sse_subscribers.remove(q)
            except ValueError:
                pass
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/ws-status")
def api_ws_status():
    """Check if VPS WebSocket relay is connected."""
    return jsonify({"connected": _vps_connected[0], "cached_sessions": len(_vps_cache)})


@app.route("/api/live/<name>")
def api_live(name):
    """Fast endpoint — reads from cache, no scp. ~1ms response."""
    # For local sessions, read stats.json directly
    stats_path = DATA_DIR / name / "stats.json"
    if stats_path.exists():
        try:
            d = json.loads(stats_path.read_text())
            # Add health
            errors = d.get("errors", {})
            updated = d.get("updated")
            d["health"] = {
                "running": bool(updated and (time.time() - updated) < 120),
                "binance_ok": errors.get("binance_ws", 0) == 0,
                "pm_ok": errors.get("pm_ws", 0) < 10,
            }
            return jsonify(d)
        except Exception:
            pass
    # For VPS sessions, read from background cache
    with _vps_cache_lock:
        d = _vps_cache.get(name)
    if d:
        errors = d.get("errors", {})
        updated = d.get("updated")
        d["health"] = {
            "running": bool(updated and (time.time() - updated) < 120),
            "binance_ok": errors.get("binance_ws", 0) == 0,
            "pm_ok": errors.get("pm_ws", 0) < 10,
        }
        return jsonify(d)
    return jsonify({"trades": 0, "wins": 0, "wr": 0, "pnl_taker": 0, "combos": {}, "recent_trades": []})


@app.route("/api/session/<name>")
def api_session(name):
    local_dir = DATA_DIR / name
    local_dir.mkdir(parents=True, exist_ok=True)

    # Check if this is a local session
    is_local = False
    try:
        result = subprocess.run(["pgrep", "-lf", "paper_trade_v2.py.*--instance {}".format(name)],
                                capture_output=True, text=True)
        is_local = name in result.stdout
    except Exception:
        pass

    # Only pull from VPS if not running locally
    if not is_local:
        for remote_file, local_file in [
            ("stats.json", "stats.json"),
            ("trades.csv", "trades.csv"),
        ]:
            try:
                subprocess.run([
                    "scp", "-q", "-o", "ConnectTimeout=2",
                    "root@167.172.50.38:/opt/polymarket-bot/data/{}/{}".format(name, remote_file),
                    str(local_dir / local_file)
                ], capture_output=True, timeout=5)
            except Exception:
                pass

    d = get_session_data(name)  # live status from stats.json
    d["is_local"] = is_local

    # Load full trade log from CSV (source of truth)
    csv_trade_log = []
    for fname in ["trades.csv", "paper_trades_v2.csv"]:
        csv_path = local_dir / fname
        if csv_path.exists():
            try:
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        csv_trade_log.append(row)
            except Exception:
                pass
            break
    csv_trade_log.sort(key=lambda x: float(x.get("timestamp", 0) or 0), reverse=True)
    d["csv_trade_log"] = csv_trade_log[:500]

    # Compute stats and combos from CSV (consistent source)
    settled = [t for t in csv_trade_log if (t.get("result") or "").strip() in ("WIN", "LOSS")]
    wins = [t for t in settled if t["result"].strip() == "WIN"]
    total_pnl = sum(float(t.get("pnl_taker", 0) or 0) for t in settled)
    d["csv_trades"] = len(settled)
    d["csv_wins"] = len(wins)
    d["csv_wr"] = round(len(wins) / len(settled) * 100, 1) if settled else 0
    d["csv_pnl"] = round(total_pnl)

    # Combo stats from CSV
    csv_combos = {}
    for t in settled:
        c = (t.get("combo") or "?").strip()
        if c not in csv_combos:
            csv_combos[c] = {"trades": 0, "wins": 0, "pnl_taker": 0.0}
        csv_combos[c]["trades"] += 1
        if t["result"].strip() == "WIN":
            csv_combos[c]["wins"] += 1
        csv_combos[c]["pnl_taker"] += float(t.get("pnl_taker", 0) or 0)
    for c in csv_combos:
        cv = csv_combos[c]
        cv["wr"] = round(cv["wins"] / cv["trades"] * 100, 1) if cv["trades"] > 0 else 0
        cv["pnl_taker"] = round(cv["pnl_taker"])
    d["csv_combos"] = csv_combos

    # Cumulative PnL per combo (for chart) — use chronological order
    settled_chrono = sorted(settled, key=lambda x: float(x.get("timestamp", 0) or 0))
    combo_cum = {}
    for t in settled_chrono:
        c = (t.get("combo") or "?").strip()
        if c not in combo_cum:
            combo_cum[c] = {"cum": [], "running": 0.0}
        combo_cum[c]["running"] += float(t.get("pnl_taker", 0) or 0)
        combo_cum[c]["cum"].append(round(combo_cum[c]["running"], 1))
    # Also total cumulative
    total_cum = []
    running_total = 0.0
    for t in settled_chrono:
        running_total += float(t.get("pnl_taker", 0) or 0)
        total_cum.append(round(running_total, 1))
    d["combo_cum_pnl"] = {c: v["cum"] for c, v in combo_cum.items()}
    d["total_cum_pnl"] = total_cum

    # Health check — works for both local and VPS
    health = {"binance_ws": False, "pm_ws": False, "running": False}
    errors = d.get("errors", {})
    updated = d.get("updated")
    if updated and (time.time() - updated) < 120:
        health["running"] = True
        health["binance_ok"] = errors.get("binance_ws", 0) == 0
        health["pm_ok"] = errors.get("pm_ws", 0) < 10
    d["health"] = health

    return jsonify(d)


@app.route("/api/all-trades")
def api_all_trades():
    return jsonify(get_all_trades())


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Pull latest data from VPS."""
    try:
        result = subprocess.run(
            ["./scripts/pull-data.sh"],
            capture_output=True, text=True, timeout=30
        )
        return jsonify({"ok": True, "output": result.stdout[-500:]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/vps-health")
def api_vps_health():
    """Get VPS system health and per-session status."""
    info = {"vps_reachable": False, "memory": "", "uptime": "", "sessions": {}}
    # System info
    raw = ssh_cmd("uptime -p 2>/dev/null; echo '|||'; free -m | awk 'NR==2{printf \"%d/%dMB\", $3, $2}'")
    if raw.strip():
        info["vps_reachable"] = True
        parts = raw.split("|||")
        info["uptime"] = parts[0].strip() if parts else ""
        info["memory"] = parts[1].strip() if len(parts) > 1 else ""
    # Per-session status
    raw2 = ssh_cmd("""
        for svc in $(systemctl list-units 'polymarket-bot@*' 'polymarket-mr@*' --no-pager --no-legend 2>/dev/null | awk '{print $1}'); do
            INST=$(echo $svc | sed 's/polymarket-bot@//;s/polymarket-mr@//;s/\\.service//')
            ST=$(systemctl is-active $svc 2>/dev/null)
            STATS="/opt/polymarket-bot/data/${INST}/stats.json"
            UP=$(systemctl show $svc --property=ActiveEnterTimestamp --value 2>/dev/null)
            if [ -f "$STATS" ]; then
                python3 -c "
import json,time
d=json.load(open('$STATS'))
u=d.get('updated',0)
age=int(time.time()-u) if u else -1
e=d.get('errors',{})
print('${INST}|${ST}|'+str(age)+'|'+json.dumps(e))
" 2>/dev/null || echo "${INST}|${ST}|-1|{}"
            else
                echo "${INST}|${ST}|-1|{}"
            fi
        done
    """)
    for line in raw2.strip().split("\n"):
        if "|" not in line:
            continue
        p = line.split("|", 3)
        if len(p) >= 4:
            name = p[0]
            try:
                errors = json.loads(p[3])
            except Exception:
                errors = {}
            age = int(p[2]) if p[2].lstrip("-").isdigit() else -1
            info["sessions"][name] = {
                "systemd": p[1],
                "last_update_age": age,
                "stale": age > 120 or age < 0,
                "errors": errors,
                "binance_ok": errors.get("binance_ws", 0) == 0,
                "pm_ok": errors.get("pm_ws", 0) < 10,
            }
    return jsonify(info)


@app.route("/api/analysis")
def api_analysis():
    return jsonify(get_analysis_data())


@app.route("/api/pull-analyze", methods=["POST"])
def api_pull_analyze():
    """Pull data from VPS, return fresh chart data."""
    try:
        subprocess.run(["./scripts/pull-data.sh"], capture_output=True, text=True, timeout=30)
    except Exception:
        pass
    return jsonify(get_chart_data())


@app.route("/api/chart-data")
def api_chart_data_route():
    return jsonify(get_chart_data())


def get_chart_data():
    """Build interactive chart data from CSVs."""
    skip = {"_archive_20260403"}
    sessions = {}

    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or d.name in skip or d.name.startswith("_"):
            continue
        csv_path = None
        for fname in ["trades.csv", "paper_trades_v2.csv"]:
            p = d / fname
            if p.exists():
                csv_path = p
                break
        if not csv_path:
            continue

        trades = []
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get("result") or "").strip() in ("WIN", "LOSS"):
                        trades.append(row)
        except Exception:
            continue
        if not trades:
            continue

        trades.sort(key=lambda x: float(x.get("timestamp", 0) or 0))
        n = len(trades)
        wins = sum(1 for t in trades if t["result"].strip() == "WIN")
        total_pnl = sum(float(t.get("pnl_taker", 0) or 0) for t in trades)

        # Cumulative PnL series (with timestamps for time-axis)
        cum = []
        cum_ts = []
        running = 0.0
        for t in trades:
            running += float(t.get("pnl_taker", 0) or 0)
            cum.append(round(running, 1))
            cum_ts.append(round(float(t.get("timestamp", 0) or 0)))

        # Per-combo breakdown with cum PnL
        combos = {}
        for t in trades:
            c = (t.get("combo") or "?").strip()
            if c not in combos:
                combos[c] = {"trades": 0, "wins": 0, "pnl": 0.0, "cum_pnl": []}
            combos[c]["trades"] += 1
            if t["result"].strip() == "WIN":
                combos[c]["wins"] += 1
            combos[c]["pnl"] += float(t.get("pnl_taker", 0) or 0)
            combos[c]["cum_pnl"].append(round(combos[c]["pnl"], 1))

        for c in combos:
            cv = combos[c]
            cv["wr"] = round(cv["wins"] / cv["trades"] * 100, 1) if cv["trades"] > 0 else 0
            cv["pnl"] = round(cv["pnl"])
            cv["avg_pnl"] = round(cv["pnl"] / cv["trades"], 1) if cv["trades"] > 0 else 0

        # Entry price buckets (wider range for all architectures)
        entry_buckets = {}
        for lo, hi in [(1,10),(10,20),(20,30),(30,40),(40,50),(50,60),(60,70),(70,80),(80,90),(90,99)]:
            label = "{}-{}c".format(lo, hi)
            bucket_trades = [t for t in trades if lo <= float(t.get("fill_price",0) or 0)*100 < hi]
            if len(bucket_trades) >= 2:
                bw = sum(1 for t in bucket_trades if t["result"].strip() == "WIN")
                bp = sum(float(t.get("pnl_taker",0) or 0) for t in bucket_trades)
                entry_buckets[label] = {"n": len(bucket_trades), "wr": round(bw/len(bucket_trades)*100,1), "pnl": round(bp)}

        # Time remaining buckets
        time_buckets = {}
        for lo, hi in [(240,300),(180,240),(120,180),(60,120),(0,60)]:
            label = "{}-{}s".format(lo, hi)
            bucket_trades = [t for t in trades if lo <= float(t.get("time_remaining",0) or 0) < hi]
            if len(bucket_trades) >= 2:
                bw = sum(1 for t in bucket_trades if t["result"].strip() == "WIN")
                bp = sum(float(t.get("pnl_taker",0) or 0) for t in bucket_trades)
                time_buckets[label] = {"n": len(bucket_trades), "wr": round(bw/len(bucket_trades)*100,1), "pnl": round(bp)}

        # Detect architecture from trades or config
        arch = _get_session_architecture(d.name)

        sessions[d.name] = {
            "architecture": arch,
            "trades": n, "wins": wins, "pnl": round(total_pnl),
            "wr": round(wins / n * 100, 1), "avg_pnl": round(total_pnl / n, 1),
            "cum_pnl": cum, "cum_ts": cum_ts, "combos": combos,
            "entry_buckets": entry_buckets, "time_buckets": time_buckets,
        }

    return {"sessions": sessions}


@app.route("/chart/<path:filepath>")
def serve_chart(filepath):
    chart_path = Path("output") / filepath
    if chart_path.exists() and chart_path.suffix == ".png":
        return send_file(str(chart_path), mimetype="image/png")
    return "", 404


@app.route("/new")
def new_session():
    from_template = request.args.get("from", "default")
    cfg_path = Path("configs/{}.json".format(from_template))
    if not cfg_path.exists():
        cfg_path = Path("configs/default.json")
    config = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    templates = []
    for f in sorted(CONFIGS_DIR.glob("*.json")):
        templates.append(f.stem)
    return render_template_string(EDIT_CSS + """
<div class="c"><h1><a href="/" style="color:var(--dim);margin-right:8px">&larr;</a> New Session</h1>
{% for m in get_flashed_messages() %}<div class="flash {{'flash-w' if m.startswith('\u26a0') else ''}}">{{m}}</div>{% endfor %}
<form method="POST" action="/create">
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
  <div><label>Name</label><input name="name" required pattern="[a-z0-9_-]+" placeholder="my-session"></div>
  <div><label>Copy from template</label><select onchange="if(this.value)location.href='/new?from='+this.value" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:6px;font-size:.88em;width:100%">
    <option value="">default</option>
    {% for t in templates %}<option value="{{t}}" {{'selected' if request.args.get('from')==t}}>{{t}}</option>{% endfor %}
  </select></div>
</div>
<label>Description</label><input name="_description" value="{{config.get('_description','')}}">
<hr>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
{% for key,meta in knobs.items() %}
<div class="kr"><label>{{meta.desc}}</label><input type="number" name="{{key}}" value="{{config.get(key,meta.default)}}" step="{{meta.step}}" min="{{meta.min}}" max="{{meta.max}}"></div>
{% endfor %}
</div>
<hr>
<div style="display:flex;gap:8px;margin-top:12px">
<button type="submit" class="btn btn-g">Create Config</button>
<button type="submit" formaction="/create?start=local" class="btn btn-b">Create & Run Local</button>
<button type="submit" formaction="/create?start=vps" class="btn btn-b">Create & Run VPS</button>
<a href="/" class="btn">Cancel</a>
</div>
</form></div>""", config=config, knobs=KNOB_DEFS, templates=templates)


@app.route("/edit/<name>")
def edit_config(name):
    config = json.loads(Path("configs/{}.json".format(name)).read_text()) if Path("configs/{}.json".format(name)).exists() else {}
    return render_template_string(EDIT_CSS + """
<div class="c"><h1><a href="/" style="color:var(--dim);margin-right:8px">&larr;</a> Edit: {{name}}</h1>
{% for m in get_flashed_messages() %}<div class="flash {{'flash-w' if m.startswith('\u26a0') else ''}}">{{m}}</div>{% endfor %}
<form method="POST" action="/save/{{name}}">
<label>Description</label><input name="_description" value="{{config.get('_description','')}}">
<hr>
{% for key,meta in knobs.items() %}
<div class="kr"><label>{{meta.desc}}</label><input type="number" name="{{key}}" value="{{config.get(key,meta.default)}}" step="{{meta.step}}" min="{{meta.min}}" max="{{meta.max}}"></div>
{% endfor %}
<hr><label>Raw JSON (optional)</label><textarea name="raw_json"></textarea>
<button type="submit" class="btn btn-g">Save</button>
<a href="/" class="btn">Cancel</a>
</form></div>""", name=name, config=config, knobs=KNOB_DEFS)


@app.route("/create", methods=["POST"])
def create_session():
    import re
    name = request.form.get("name", "").strip().lower()
    name = re.sub(r'[^a-z0-9_-]', '-', name).strip('-')
    if not name:
        flash("Name required"); return redirect("/new")
    try:
        config = parse_form_config(request.form)
    except json.JSONDecodeError:
        flash("Invalid JSON"); return redirect("/new")
    errors = validate_config(config)
    if errors:
        for e in errors: flash(e)
        return redirect("/new")
    Path("configs/{}.json".format(name)).write_text(json.dumps(config, indent=4))
    start = request.args.get("start")
    if start == "local":
        Path("data/{}".format(name)).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["python3", "-u", "bot/paper_trade_v2.py", "--instance", name],
                         stdout=open("data/{}.log".format(name), "a"), stderr=subprocess.STDOUT, start_new_session=True)
    elif start == "vps":
        subprocess.Popen(["./scripts/new-session.sh", name])
    return redirect("/")


@app.route("/save/<name>", methods=["POST"])
def save_config(name):
    try:
        config = parse_form_config(request.form)
    except json.JSONDecodeError:
        flash("Invalid JSON"); return redirect("/edit/" + name)
    errors = validate_config(config)
    if errors:
        for e in errors: flash(e)
        return redirect("/edit/" + name)
    Path("configs/{}.json".format(name)).write_text(json.dumps(config, indent=4))
    return redirect("/")


@app.route("/start/<where>/<name>")
def start_session(where, name):
    if where == "local":
        Path("data/{}".format(name)).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["python3", "-u", "bot/paper_trade_v2.py", "--instance", name],
                         stdout=open("data/{}.log".format(name), "a"), stderr=subprocess.STDOUT, start_new_session=True)
    elif where == "vps":
        subprocess.Popen(["./scripts/new-session.sh", name])
    # Return JSON for API calls, redirect for form submissions
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("api"):
        return jsonify({"ok": True})
    time.sleep(1)
    return redirect("/")


@app.route("/stop/<where>/<name>")
def stop_session(where, name):
    if where == "local":
        subprocess.run(["pkill", "-INT", "-f", "paper_trade_v2.py.*--instance {}".format(name)])
    elif where == "vps":
        ssh_cmd("systemctl stop polymarket-bot@{}".format(name))
    return jsonify({"ok": True})


@app.route("/delete/<name>", methods=["POST"])
def delete_session(name):
    """Stop session, remove config and data."""
    import shutil
    # Stop if running
    subprocess.run(["pkill", "-INT", "-f", "paper_trade_v2.py.*--instance {}".format(name)],
                   capture_output=True)
    ssh_cmd("systemctl stop polymarket-bot@{} 2>/dev/null".format(name))
    # Remove config
    cfg = Path("configs/{}.json".format(name))
    if cfg.exists():
        cfg.unlink()
    # Remove local data
    data_dir = DATA_DIR / name
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    # Remove log
    log = Path("data/{}.log".format(name))
    if log.exists():
        log.unlink()
    # Clear from VPS cache
    with _vps_cache_lock:
        _vps_cache.pop(name, None)
    return jsonify({"ok": True})


@app.route("/logs/<name>")
def view_logs(name):
    lines = ssh_cmd("tail -200 /var/log/polymarket-bot/{}.log 2>/dev/null".format(name))
    if not lines.strip():
        for p in [Path("data/{}.log".format(name)), Path("data/{}/bot.log".format(name))]:
            if p.exists():
                lines = p.read_text()[-15000:]
                break
    if not lines.strip():
        lines = "No logs found for '{}'".format(name)
    return render_template_string(EDIT_CSS + """
<div class="c" style="max-width:960px"><h1><a href="/" style="color:var(--dim);margin-right:8px">&larr;</a> {{name}} / Logs</h1>
<a href="/logs/{{name}}" class="btn" style="margin-bottom:12px">Refresh</a>
<pre style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;font-size:11px;font-family:'JetBrains Mono',monospace;overflow-x:auto;white-space:pre-wrap;word-break:break-all;max-height:80vh;overflow-y:auto;line-height:1.6">{{logs}}</pre>
</div>""", name=name, logs=lines)


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:5555\n")
    app.run(host="0.0.0.0", port=5555, debug=False)
