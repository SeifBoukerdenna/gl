"""
Polymarket Bot Dashboard — Real-time WebSocket-powered UI
localhost:5555

- WebSocket streams live stats from bot's stats.json
- REST API for CRUD operations on sessions/configs
- Single-page app with live charts, trade feed, window timer
"""

import asyncio
import csv
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template_string, jsonify, request, redirect, flash

app = Flask(__name__)
app.secret_key = "pm-bot-dash"

CONFIGS_DIR = Path("configs")
DATA_DIR = Path("data")

# ══════════════════════════════════════════════════════════════
# CONFIG KNOBS
# ══════════════════════════════════════════════════════════════
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


def get_all_sessions():
    sessions = []
    seen = set()
    # Local
    try:
        result = subprocess.run(["pgrep", "-af", "python.*paper_trade_v2.py"], capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            if not line.strip() or "pgrep" in line or "bash" in line:
                continue
            parts = line.split(None, 1)
            cmd = parts[1] if len(parts) > 1 else ""
            if "paper_trade_v2.py" not in cmd:
                continue
            name = cmd.split("--instance")[-1].strip().split()[0] if "--instance" in cmd else "default"
            if name in seen:
                continue
            seen.add(name)
            d = get_session_data(name)
            sessions.append({"name": name, "where": "local", "status": "running",
                             "trades": d.get("trades", 0), "wins": d.get("wins", 0),
                             "wr": d.get("wr", 0), "pnl_taker": d.get("pnl_taker", 0)})
    except Exception:
        pass
    # VPS
    raw = ssh_cmd("""
        for svc in $(systemctl list-units 'polymarket-bot@*' --no-pager --no-legend 2>/dev/null | awk '{print $1}'); do
            INST=$(echo $svc | sed 's/polymarket-bot@//;s/\\.service//'); ST=$(systemctl is-active $svc 2>/dev/null)
            STATS="/opt/polymarket-bot/data/${INST}/stats.json"
            if [ -f "$STATS" ]; then
                python3 -c "import json;d=json.load(open('$STATS'));print('${INST}|${ST}|'+str(d.get('trades',0))+'|'+str(d.get('wins',0))+'|'+str(int(d.get('pnl_taker',0))))" 2>/dev/null || echo "${INST}|${ST}|0|0|0"
            else
                echo "${INST}|${ST}|0|0|0"
            fi
        done
    """)
    for line in raw.strip().split("\n"):
        if not line.strip() or "|" not in line:
            continue
        p = line.split("|")
        if len(p) >= 5:
            name = p[0]
            if name in seen:
                continue
            seen.add(name)
            n = int(p[2]) if p[2].isdigit() else 0
            w = int(p[3]) if p[3].isdigit() else 0
            sessions.append({
                "name": name, "where": "vps", "status": p[1],
                "trades": n, "wins": w, "wr": round(w / n * 100, 1) if n > 0 else 0,
                "pnl_taker": int(p[4]) if p[4].lstrip("-").isdigit() else 0,
            })
    return sessions


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
# THE ENTIRE FRONTEND — single HTML file with embedded JS
# ══════════════════════════════════════════════════════════════
FRONTEND = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PM Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0e14;--card:#12171f;--border:#1c2333;--text:#c5cdd9;--dim:#4a5568;
--blue:#5b9df9;--green:#34d399;--red:#f87171;--yellow:#fbbf24;--purple:#a78bfa}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px}
a{color:var(--blue);text-decoration:none;cursor:pointer}
.app{display:grid;grid-template-columns:220px 1fr;height:100vh}
/* Sidebar */
.sb{background:var(--card);border-right:1px solid var(--border);padding:16px;overflow-y:auto}
.sb h1{font-size:1.1em;background:linear-gradient(135deg,var(--blue),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:16px}
.sb-section{margin-bottom:16px}
.sb-label{font-size:.68em;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.sb-item{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;border-radius:6px;cursor:pointer;margin-bottom:2px;transition:background .15s}
.sb-item:hover{background:rgba(91,157,249,.08)}
.sb-item.active{background:rgba(91,157,249,.12);color:var(--blue)}
.sb-item .name{font-weight:500;font-size:.88em}
.sb-item .meta{font-size:.72em;color:var(--dim)}
.sb-item .dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.sb-item .dot-on{background:var(--green);box-shadow:0 0 6px var(--green)}
.sb-item .dot-off{background:var(--dim)}
.sb-btn{display:block;width:100%;padding:8px;text-align:center;border-radius:6px;font-size:.82em;font-weight:500;border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer;margin-top:8px;transition:all .15s}
.sb-btn:hover{background:#1e2536}
.sb-btn-g{border-color:#059669;color:var(--green)}.sb-btn-g:hover{background:#059669;color:#fff}
/* Main */
.main{overflow-y:auto;padding:20px 24px}
/* Cards */
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:12px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 16px;min-width:90px}
.stat-v{font-size:1.5em;font-weight:800;font-variant-numeric:tabular-nums}
.stat-l{font-size:.66em;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-top:1px}
/* Live bar */
.live{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;padding:12px;background:linear-gradient(135deg,var(--card),#1a1f2e);border:1px solid #2a3040;border-radius:8px;margin-bottom:14px}
.live-l{font-size:.66em;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.live-v{font-size:.95em;font-weight:600;margin-top:1px}
/* Window timer */
.timer{font-size:2em;font-weight:800;color:var(--blue);font-variant-numeric:tabular-nums}
.timer-label{font-size:.7em;color:var(--dim)}
.progress-bar{height:4px;background:var(--border);border-radius:2px;margin-top:6px;overflow:hidden}
.progress-fill{height:100%;background:var(--blue);border-radius:2px;transition:width 1s linear}
/* Window pills */
.wp{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.7em;font-weight:600;margin:1px}
.wu{background:rgba(52,211,153,.12);color:var(--green)}.wd{background:rgba(248,113,113,.12);color:var(--red)}
/* Table */
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:6px 10px;color:var(--dim);font-size:.7em;font-weight:600;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid var(--border)}
td{padding:6px 10px;border-bottom:1px solid var(--border);font-size:.84em}
tr:hover{background:rgba(91,157,249,.03)}
/* Tags */
.tag{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.68em;font-weight:600}
.tag-w{background:rgba(52,211,153,.12);color:var(--green)}.tag-l{background:rgba(248,113,113,.12);color:var(--red)}
/* Colors */
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.d{color:var(--dim)}.b{color:var(--blue)}.p{color:var(--purple)}
.pg{color:var(--green);text-shadow:0 0 8px rgba(52,211,153,.2)}
.pr{color:var(--red);text-shadow:0 0 8px rgba(248,113,113,.2)}
/* Win bar */
.wb{height:3px;border-radius:1.5px;background:rgba(248,113,113,.15);margin-top:3px}
.wbf{height:100%;border-radius:1.5px;transition:width .5s}
/* Sections */
h2{font-size:.75em;color:var(--dim);text-transform:uppercase;letter-spacing:1.5px;margin:16px 0 8px;font-weight:600}
/* Empty */
.empty{text-align:center;padding:40px;color:var(--dim)}
.empty-icon{font-size:2em;margin-bottom:6px}
/* Pulse */
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.pulse{animation:pulse 2s infinite}
/* Scrollbar */
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
/* Forms */
input,textarea,select{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:5px;font-family:inherit;font-size:.86em;width:100%}
input:focus,textarea:focus{border-color:var(--blue);outline:none;box-shadow:0 0 0 2px rgba(91,157,249,.15)}
.btn{display:inline-flex;align-items:center;gap:4px;padding:6px 14px;border-radius:6px;font-size:.8em;font-family:inherit;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text);transition:all .15s;font-weight:500}
.btn:hover{background:#1e2536;transform:translateY(-1px)}
.btn-g{border-color:#059669;color:var(--green)}.btn-g:hover{background:#059669;color:#fff}
.btn-r{border-color:#dc2626;color:var(--red)}.btn-r:hover{background:#dc2626;color:#fff}
.btn-b{border-color:#2563eb;color:var(--blue)}.btn-b:hover{background:#2563eb;color:#fff}
</style>
</head>
<body>
<div class="app">
<!-- Sidebar -->
<div class="sb" id="sidebar">
    <h1>PM Bot</h1>
    <div class="sb-section">
        <div class="sb-label">Sessions</div>
        <div id="session-list"></div>
        <button class="sb-btn sb-btn-g" onclick="showNewSession()">+ New Session</button>
    </div>
    <div class="sb-section">
        <div class="sb-label">Configs</div>
        <div id="config-list"></div>
    </div>
</div>

<!-- Main content -->
<div class="main" id="main">
    <div class="empty"><div class="empty-icon">📡</div>Select a session or create one</div>
</div>
</div>

<script>
// ── State ──────────────────────────────────────────────
let currentSession = null;
let pollTimer = null;

// ── API calls ──────────────────────────────────────────
async function api(url, opts) {
    const r = await fetch(url, opts);
    return r.json();
}

// ── Time formatting (local timezone) ────────────────────
function ft(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function ftShort(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit'});
}
function ago(ts) {
    if (!ts) return '—';
    const d = Math.floor(Date.now()/1000 - ts);
    if (d < 60) return d + 's ago';
    if (d < 3600) return Math.floor(d/60) + 'm ago';
    return Math.floor(d/3600) + 'h' + Math.floor((d%3600)/60) + 'm ago';
}
function windowTimeRange(ws) {
    if (!ws) return '—';
    const s = new Date(ws * 1000);
    const e = new Date((ws + 300) * 1000);
    return s.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'}) + ' - ' +
           e.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});
}

// ── Sidebar ────────────────────────────────────────────
async function refreshSidebar() {
    const sessions = await api('/api/sessions');
    const configs = await api('/api/configs');

    let sh = '';
    sessions.forEach(s => {
        const active = s.status === 'running' || s.status === 'active';
        const isCurrent = currentSession === s.name;
        sh += `<div class="sb-item ${isCurrent?'active':''}" onclick="selectSession('${s.name}')">
            <div><div class="name">${s.name}</div><div class="meta">${s.trades} trades · ${s.where}</div></div>
            <div class="dot ${active?'dot-on pulse':'dot-off'}"></div>
        </div>`;
    });
    if (!sessions.length) sh = '<div class="d" style="padding:8px;font-size:.82em">No sessions running</div>';
    document.getElementById('session-list').innerHTML = sh;

    let ch = '';
    configs.forEach(c => {
        ch += `<div class="sb-item" onclick="editConfig('${c.name}')">
            <div class="name">${c.name}</div>
            <div class="meta" style="font-size:.68em">${c.desc ? c.desc.substring(0,25) : ''}</div>
        </div>`;
    });
    document.getElementById('config-list').innerHTML = ch;
}

// ── Session Detail ─────────────────────────────────────
async function selectSession(name) {
    currentSession = name;
    refreshSidebar();
    await refreshSession();
    // Poll every 5 seconds
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refreshSession, 5000);
}

async function refreshSession() {
    if (!currentSession) return;
    const d = await api('/api/session/' + currentSession);
    renderSession(d);
}

function renderSession(d) {
    const ws = d.current_window || 0;
    const we = d.window_end || 0;
    const now = Date.now() / 1000;
    const trem = Math.max(0, Math.floor(we - now));
    const elapsed = Math.max(0, 300 - trem);
    const pct = Math.min(100, (elapsed / 300) * 100);

    // Combo table
    let comboRows = '';
    const combos = d.combos || {};
    const sorted = Object.entries(combos).sort((a,b) => (b[1].pnl_taker||0) - (a[1].pnl_taker||0));
    sorted.forEach(([cn, cv]) => {
        const wr = cv.wr || 0;
        const pnl = Math.round(cv.pnl_taker || 0);
        const wrClass = wr >= 65 ? 'g' : wr >= 55 ? 'y' : 'r';
        const pnlClass = pnl > 0 ? 'pg' : pnl < 0 ? 'pr' : 'd';
        const barColor = wr >= 60 ? 'var(--green)' : wr >= 50 ? 'var(--yellow)' : 'var(--red)';
        comboRows += `<tr>
            <td><strong>${cn}</strong></td><td>${cv.trades||0}</td><td>${cv.wins||0}</td>
            <td class="${wrClass}">${wr}%</td>
            <td class="${pnlClass}" style="font-weight:600">$${pnl}</td>
            <td>$${Math.round(cv.bankroll||1000)}</td>
            <td><div class="wb"><div class="wbf" style="width:${wr}%;background:${barColor}"></div></div></td>
        </tr>`;
    });

    // Trade log
    let tradeRows = '';
    const trades = (d.recent_trades || []).slice().reverse();
    trades.forEach(t => {
        const pnl = t.pnl_taker;
        const pnlClass = pnl && pnl > 0 ? 'pg' : pnl && pnl < 0 ? 'pr' : 'd';
        const resTag = t.result === 'WIN' ? '<span class="tag tag-w">WIN</span>' :
                       t.result === 'LOSS' ? '<span class="tag tag-l">LOSS</span>' : '—';
        const entry = t.fill_price ? (parseFloat(t.fill_price)*100).toFixed(1)+'¢' : '?';
        tradeRows += `<tr>
            <td class="d">${ft(t.timestamp)}</td><td>${t.combo||'?'}</td><td>${t.direction||'?'}</td>
            <td>${entry}</td><td>${t.filled_size||'?'}</td><td>${t.impulse_bps||'?'}bp</td>
            <td>${t.time_remaining||'?'}s</td><td>${resTag}</td>
            <td class="${pnlClass}" style="font-weight:600">${pnl ? '$'+pnl : '—'}</td>
        </tr>`;
    });

    // Window pills
    let pills = '';
    (d.recent_windows || []).forEach(w => {
        const cls = w.outcome === 'Up' ? 'wu' : 'wd';
        pills += `<span class="wp ${cls}">${w.outcome}</span>`;
    });

    const btc = d.btc_price ? '$' + d.btc_price.toLocaleString('en-US',{minimumFractionDigits:2}) : '—';
    const ptb = d.window_open ? '$' + d.window_open.toLocaleString('en-US',{minimumFractionDigits:2}) : '—';
    const bid = d.book_bid ? Math.round(d.book_bid*100)+'¢' : '—';
    const ask = d.book_ask ? Math.round(d.book_ask*100)+'¢' : '—';
    const spr = d.book_spread ? Math.round(d.book_spread*100)+'¢' : '—';
    const pnlTaker = Math.round(d.pnl_taker || 0);
    const pnlMaker = Math.round(d.pnl_maker || 0);
    const totalWr = d.wr || 0;

    document.getElementById('main').innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
            <h1 style="font-size:1.3em;font-weight:700">${currentSession}
                ${d.window_active ? '<span class="tag tag-w" style="margin-left:6px;font-size:.6em">LIVE</span>' : ''}
                ${d.cooldown_active ? '<span class="tag tag-l" style="margin-left:6px;font-size:.6em">COOLDOWN</span>' : ''}
            </h1>
            <div style="display:flex;gap:6px">
                <a class="btn" href="/logs/${currentSession}" target="_blank">Logs</a>
                <a class="btn" href="/edit/${currentSession}">Config</a>
                <button class="btn btn-r" onclick="stopSession('${currentSession}','${d.where||'vps'}')">Stop</button>
            </div>
        </div>

        <!-- Live Status + Timer -->
        <div class="live">
            <div><div class="live-l">Window</div><div class="live-v">${windowTimeRange(ws)}</div></div>
            <div><div class="live-l">Time Left</div>
                <div class="timer">${Math.floor(trem/60)}:${String(trem%60).padStart(2,'0')}</div>
                <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
            </div>
            <div><div class="live-l">BTC</div><div class="live-v">${btc}</div></div>
            <div><div class="live-l">Price To Beat</div><div class="live-v">${ptb}</div></div>
            <div><div class="live-l">PM Book</div><div class="live-v"><span class="g">${bid}</span> / <span class="r">${ask}</span> <span class="d">spr ${spr}</span></div></div>
            <div><div class="live-l">Updated</div><div class="live-v d">${ago(d.updated)}</div></div>
        </div>

        <!-- Stats -->
        <div class="stats">
            <div class="stat"><div class="stat-v b">${d.trades||0}</div><div class="stat-l">Trades</div></div>
            <div class="stat"><div class="stat-v ${totalWr>=60?'g':totalWr>=50?'y':'r'}">${totalWr}%</div><div class="stat-l">Win Rate</div></div>
            <div class="stat"><div class="stat-v ${pnlTaker>=0?'pg':'pr'}">$${pnlTaker}</div><div class="stat-l">PnL Taker</div></div>
            <div class="stat"><div class="stat-v ${pnlMaker>=0?'pg':'pr'}">$${pnlMaker}</div><div class="stat-l">PnL Maker</div></div>
            <div class="stat"><div class="stat-v b">${d.windows_settled||0}</div><div class="stat-l">Windows</div></div>
        </div>

        <!-- Windows -->
        ${pills ? '<h2>Recent Windows</h2><div style="margin-bottom:12px">'+pills+'</div>' : ''}

        <!-- Combos -->
        ${comboRows ? `<h2>Combos</h2>
        <table><tr><th>Combo</th><th>Trades</th><th>Wins</th><th>Win%</th><th>PnL</th><th>Bank</th><th style="width:80px"></th></tr>
        ${comboRows}</table>` : ''}

        <!-- Trade Log -->
        ${tradeRows ? `<h2>Trade Log</h2>
        <table><tr><th>Time</th><th>Combo</th><th>Dir</th><th>Entry</th><th>Size</th><th>Impulse</th><th>T-rem</th><th>Result</th><th>PnL</th></tr>
        ${tradeRows}</table>` : '<div class="empty"><div class="empty-icon">⏳</div>Waiting for trades...</div>'}

        ${d.errors ? '<div style="margin-top:16px;font-size:.78em;color:var(--dim)">Errors: '+JSON.stringify(d.errors)+'</div>' : ''}
    `;
}

// ── New Session ────────────────────────────────────────
function showNewSession() {
    window.location.href = '/new';
}

// ── Edit Config ────────────────────────────────────────
function editConfig(name) {
    window.location.href = '/edit/' + name;
}

// ── Stop ───────────────────────────────────────────────
async function stopSession(name, where) {
    if (!confirm('Stop ' + name + '?')) return;
    await fetch('/stop/' + where + '/' + name);
    setTimeout(refreshSidebar, 2000);
}

// ── Init ───────────────────────────────────────────────
refreshSidebar();
setInterval(refreshSidebar, 15000);

// Auto-select first session
setTimeout(async () => {
    const sessions = await api('/api/sessions');
    if (sessions.length > 0 && !currentSession) {
        selectSession(sessions[0].name);
    }
}, 1000);
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════
# EDIT/NEW pages (kept as server-rendered for simplicity)
# ══════════════════════════════════════════════════════════════
EDIT_CSS = """<style>
:root{--bg:#0a0e14;--card:#12171f;--border:#1c2333;--text:#c5cdd9;--dim:#4a5568;--blue:#5b9df9;--green:#34d399;--red:#f87171;--purple:#a78bfa}
*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px;padding:24px}
.c{max-width:600px;margin:0 auto}
h1{font-size:1.2em;margin-bottom:16px;color:var(--blue)}
label{display:block;font-size:.82em;color:var(--dim);margin-bottom:3px;margin-top:12px}
input,textarea{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:5px;font-family:inherit;font-size:.88em;width:100%}
input:focus{border-color:var(--blue);outline:none}textarea{min-height:60px;resize:vertical}
.btn{display:inline-flex;padding:8px 16px;border-radius:6px;font-size:.84em;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text);font-family:inherit;margin-top:16px;margin-right:6px}
.btn-g{border-color:#059669;color:var(--green)}.btn-g:hover{background:#059669;color:#fff}
.btn-b{border-color:#2563eb;color:var(--blue)}.btn-b:hover{background:#2563eb;color:#fff}
hr{border:none;border-top:1px solid var(--border);margin:16px 0}
.kr{display:grid;grid-template-columns:1fr 80px;gap:8px;align-items:center;margin-bottom:4px}
.kr label{margin:0;font-size:.82em}.kr input{text-align:right;margin:0}
.flash{padding:10px;border-radius:6px;margin-bottom:12px;font-size:.86em;background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.2)}
.flash-w{background:rgba(251,191,36,.1);color:var(--yellow);border-color:rgba(251,191,36,.3)}
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


@app.route("/api/session/<name>")
def api_session(name):
    # Pull stats.json from VPS if not local
    try:
        local_dir = DATA_DIR / name
        local_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "scp", "-q", "-o", "ConnectTimeout=2",
            "root@167.172.50.38:/opt/polymarket-bot/data/{}/stats.json".format(name),
            str(local_dir / "stats.json")
        ], capture_output=True, timeout=5)
    except Exception:
        pass
    d = get_session_data(name)
    return jsonify(d)


@app.route("/new")
def new_session():
    config = json.loads(Path("configs/default.json").read_text()) if Path("configs/default.json").exists() else {}
    return render_template_string(EDIT_CSS + """
<div class="c"><h1>New Session</h1>
{% for m in get_flashed_messages() %}<div class="flash {{'flash-w' if m.startswith('⚠') else ''}}">{{m}}</div>{% endfor %}
<form method="POST" action="/create">
<label>Name</label><input name="name" required pattern="[a-z0-9-]+" placeholder="my-session">
<label>Description</label><input name="_description" value="{{config.get('_description','')}}">
<hr>
{% for key,meta in knobs.items() %}
<div class="kr"><label>{{meta.desc}}</label><input type="number" name="{{key}}" value="{{config.get(key,meta.default)}}" step="{{meta.step}}" min="{{meta.min}}" max="{{meta.max}}"></div>
{% endfor %}
<hr><label>Raw JSON (optional)</label><textarea name="raw_json"></textarea>
<button type="submit" class="btn btn-g">Create</button>
<button type="submit" formaction="/create?start=local" class="btn btn-b">Create & Run Local</button>
<button type="submit" formaction="/create?start=vps" class="btn btn-b">Create & Run VPS</button>
<a href="/" class="btn">Cancel</a>
</form></div>""", config=config, knobs=KNOB_DEFS)


@app.route("/edit/<name>")
def edit_config(name):
    config = json.loads(Path("configs/{}.json".format(name)).read_text()) if Path("configs/{}.json".format(name)).exists() else {}
    return render_template_string(EDIT_CSS + """
<div class="c"><h1>Edit: {{name}}</h1>
{% for m in get_flashed_messages() %}<div class="flash {{'flash-w' if m.startswith('⚠') else ''}}">{{m}}</div>{% endfor %}
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
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name required"); return redirect("/new")
    try:
        config = parse_form_config(request.form)
    except json.JSONDecodeError:
        flash("⚠ Invalid JSON"); return redirect("/new")
    errors = validate_config(config)
    if errors:
        for e in errors: flash("⚠ " + e)
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
        flash("⚠ Invalid JSON"); return redirect("/edit/" + name)
    errors = validate_config(config)
    if errors:
        for e in errors: flash("⚠ " + e)
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
    time.sleep(1)
    return redirect("/")


@app.route("/stop/<where>/<name>")
def stop_session(where, name):
    if where == "local":
        subprocess.run(["pkill", "-INT", "-f", "paper_trade_v2.py.*--instance {}".format(name)])
    elif where == "vps":
        ssh_cmd("systemctl stop polymarket-bot@{}".format(name))
    return jsonify({"ok": True})


@app.route("/logs/<name>")
def view_logs(name):
    lines = ssh_cmd("tail -100 /var/log/polymarket-bot/{}.log 2>/dev/null".format(name))
    if not lines.strip():
        for p in [Path("data/{}.log".format(name)), Path("data/{}/bot.log".format(name))]:
            if p.exists():
                lines = p.read_text()[-10000:]
                break
    if not lines.strip():
        lines = "No logs found for '{}'".format(name)
    return render_template_string(EDIT_CSS + """
<div class="c" style="max-width:900px"><h1><a href="/">←</a> {{name}} / Logs</h1>
<a href="/logs/{{name}}" class="btn" style="margin-bottom:12px">↻ Refresh</a>
<pre style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:.76em;overflow-x:auto;white-space:pre-wrap;word-break:break-all;max-height:80vh;overflow-y:auto;line-height:1.5">{{logs}}</pre>
</div>""", name=name, logs=lines)


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:5555\n")
    app.run(host="0.0.0.0", port=5555, debug=False)
