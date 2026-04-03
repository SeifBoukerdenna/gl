"""
Polymarket Bot Dashboard — web UI on localhost:5555
Usage: python dashboard.py
"""

import csv
import json
import subprocess
import time
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request, redirect, flash

app = Flask(__name__)
app.secret_key = "pm-bot-dash"
CONFIGS_DIR = Path("configs")
DATA_DIR = Path("data")

# All tunable knobs with descriptions and defaults
KNOB_DEFS = {
    "BASE_TRADE_DOLLARS": {"default": 100, "desc": "Dollar amount per trade", "type": "number", "step": 10, "min": 1, "max": 10000},
    "MAX_SHARES": {"default": 500, "desc": "Maximum shares per trade", "type": "number", "step": 50, "min": 5, "max": 10000},
    "MAX_RISK_PCT": {"default": 0.10, "desc": "Max % of bankroll per trade", "type": "number", "step": 0.01, "min": 0.01, "max": 1.0},
    "MIN_SHARES": {"default": 10, "desc": "Minimum shares per trade", "type": "number", "step": 5, "min": 1, "max": 1000},
    "STARTING_BANKROLL": {"default": 1000, "desc": "Starting $ per combo", "type": "number", "step": 100, "min": 10, "max": 1000000},
    "MIN_ENTRY_PRICE": {"default": 0.20, "desc": "Don't buy below this (0-1)", "type": "number", "step": 0.05, "min": 0.01, "max": 0.99},
    "MAX_ENTRY_PRICE": {"default": 0.80, "desc": "Don't buy above this (0-1)", "type": "number", "step": 0.05, "min": 0.01, "max": 0.99},
    "DOWN_MIN_ENTRY": {"default": 0.25, "desc": "Extra floor for BUY DOWN trades", "type": "number", "step": 0.05, "min": 0.01, "max": 0.99},
    "MAX_IMPULSE_BP": {"default": 25, "desc": "Skip if BTC moved > this many bp", "type": "number", "step": 5, "min": 1, "max": 200},
    "MAX_SPREAD": {"default": 0.03, "desc": "Skip if PM spread > this", "type": "number", "step": 0.01, "min": 0.005, "max": 0.50},
    "MIN_BOOK_LEVELS": {"default": 3, "desc": "Min order book depth", "type": "number", "step": 1, "min": 1, "max": 50},
    "DEAD_ZONE_START": {"default": 90, "desc": "Dead zone start (seconds remaining)", "type": "number", "step": 10, "min": 0, "max": 300},
    "DEAD_ZONE_END": {"default": 210, "desc": "Dead zone end (seconds remaining)", "type": "number", "step": 10, "min": 0, "max": 300},
    "WINDOW_BUFFER_START": {"default": 10, "desc": "Skip first N seconds of window", "type": "number", "step": 5, "min": 0, "max": 120},
    "WINDOW_BUFFER_END": {"default": 10, "desc": "Skip last N seconds of window", "type": "number", "step": 5, "min": 0, "max": 120},
    "COOLDOWN_RANGE_BP": {"default": 50, "desc": "Pause if window range > this bp", "type": "number", "step": 10, "min": 5, "max": 500},
    "COOLDOWN_DURATION": {"default": 120, "desc": "Cooldown duration in seconds", "type": "number", "step": 30, "min": 0, "max": 3600},
    "PRINT_STATUS_INTERVAL": {"default": 15, "desc": "Status line every N seconds", "type": "number", "step": 5, "min": 5, "max": 300},
}


def validate_config(config):
    """Validate config values. Returns list of error strings (empty = valid)."""
    errors = []

    # Check each knob against its range
    for key, meta in KNOB_DEFS.items():
        if key not in config:
            continue
        val = config[key]
        if not isinstance(val, (int, float)):
            errors.append("{}: must be a number, got {}".format(key, type(val).__name__))
            continue
        if val < meta["min"]:
            errors.append("{}: {} is below minimum {}".format(key, val, meta["min"]))
        if val > meta["max"]:
            errors.append("{}: {} is above maximum {}".format(key, val, meta["max"]))

    # Cross-field validations
    min_entry = config.get("MIN_ENTRY_PRICE", KNOB_DEFS["MIN_ENTRY_PRICE"]["default"])
    max_entry = config.get("MAX_ENTRY_PRICE", KNOB_DEFS["MAX_ENTRY_PRICE"]["default"])
    down_min = config.get("DOWN_MIN_ENTRY", KNOB_DEFS["DOWN_MIN_ENTRY"]["default"])
    dz_start = config.get("DEAD_ZONE_START", KNOB_DEFS["DEAD_ZONE_START"]["default"])
    dz_end = config.get("DEAD_ZONE_END", KNOB_DEFS["DEAD_ZONE_END"]["default"])
    min_shares = config.get("MIN_SHARES", KNOB_DEFS["MIN_SHARES"]["default"])
    max_shares = config.get("MAX_SHARES", KNOB_DEFS["MAX_SHARES"]["default"])
    buf_start = config.get("WINDOW_BUFFER_START", KNOB_DEFS["WINDOW_BUFFER_START"]["default"])
    buf_end = config.get("WINDOW_BUFFER_END", KNOB_DEFS["WINDOW_BUFFER_END"]["default"])

    if min_entry >= max_entry:
        errors.append("MIN_ENTRY_PRICE ({}) must be less than MAX_ENTRY_PRICE ({})".format(min_entry, max_entry))
    if down_min < min_entry:
        errors.append("DOWN_MIN_ENTRY ({}) should be >= MIN_ENTRY_PRICE ({})".format(down_min, min_entry))
    if dz_start >= dz_end:
        errors.append("DEAD_ZONE_START ({}) must be less than DEAD_ZONE_END ({})".format(dz_start, dz_end))
    if min_shares > max_shares:
        errors.append("MIN_SHARES ({}) must be <= MAX_SHARES ({})".format(min_shares, max_shares))
    if buf_start + buf_end >= 280:
        errors.append("WINDOW_BUFFER_START + END ({}) leaves less than 20s of trading".format(buf_start + buf_end))

    # Check for unknown keys
    known = set(KNOB_DEFS.keys()) | {"_description"}
    for key in config:
        if key not in known:
            errors.append("Unknown key: '{}' — bot will ignore it".format(key))

    return errors


# ── Helpers ───────────────────────────────────────────────────────
def load_env():
    env = {}
    if Path(".env").exists():
        for line in open(".env"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def ssh_cmd(cmd):
    env = load_env()
    host = env.get("VPS_HOST", "167.172.50.38")
    user = env.get("VPS_USER", "root")
    try:
        r = subprocess.run(
            ["ssh", "-q", "-o", "ConnectTimeout=3", "{}@{}".format(user, host), cmd],
            capture_output=True, text=True, timeout=10
        )
        return r.stdout
    except Exception:
        return ""


def get_configs():
    configs = []
    for f in sorted(CONFIGS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            configs.append({"name": f.stem, "description": data.get("_description", ""), "data": data})
        except Exception:
            configs.append({"name": f.stem, "description": "parse error", "data": {}})
    return configs


def get_session_stats(name):
    """Read stats.json (fast, written by bot) or fall back to parsing CSV."""
    # Try stats.json first — written atomically by the bot, no lock issues
    stats_path = DATA_DIR / name / "stats.json"
    if stats_path.exists():
        try:
            data = json.loads(stats_path.read_text())
            combos = {}
            for cname, c in data.get("combos", {}).items():
                combos[cname] = {"n": c["trades"], "wins": c["wins"], "wr": c["wr"], "pnl": round(c["pnl_taker"])}
            return {
                "trades": data.get("trades", 0),
                "wins": data.get("wins", 0),
                "wr": data.get("wr", 0),
                "pnl": round(data.get("pnl_taker", 0)),
                "combos": combos,
                "windows": data.get("windows", 0),
                "updated": data.get("updated", 0),
            }
        except Exception:
            pass

    # Fallback: parse CSV (slower, for sessions that don't have stats.json yet)
    csv_path = DATA_DIR / name / "trades.csv"
    # Also check old file naming
    if not csv_path.exists():
        csv_path = DATA_DIR / name / "paper_trades_v2.csv"
    if not csv_path.exists():
        csv_path = DATA_DIR / "paper_trades_v2.csv" if name == "default" else csv_path
    if not csv_path.exists():
        return {"trades": 0, "wins": 0, "wr": 0, "pnl": 0, "combos": {}}

    trades = []
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                reader.fieldnames = [h.strip() for h in reader.fieldnames]
            for row in reader:
                trades.append({k.strip(): v.strip() if isinstance(v, str) else v for k, v in row.items()})
    except Exception:
        return {"trades": 0, "wins": 0, "wr": 0, "pnl": 0, "combos": {}}

    n = len(trades)
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    pnl = sum(float(t.get("pnl_taker", 0) or 0) for t in trades)

    combos = {}
    for t in trades:
        c = t.get("combo", "?")
        if c not in combos:
            combos[c] = {"n": 0, "wins": 0, "pnl": 0}
        combos[c]["n"] += 1
        if t.get("result") == "WIN":
            combos[c]["wins"] += 1
        combos[c]["pnl"] += float(t.get("pnl_taker", 0) or 0)

    for c in combos.values():
        c["wr"] = round(c["wins"] / c["n"] * 100, 1) if c["n"] > 0 else 0
        c["pnl"] = round(c["pnl"])

    return {"trades": n, "wins": wins, "wr": round(wins / n * 100, 1) if n > 0 else 0, "pnl": round(pnl), "combos": combos}


def get_local_sessions():
    sessions = []
    seen = set()
    try:
        result = subprocess.run(["pgrep", "-af", "python.*paper_trade_v2.py"], capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            # Only match actual python processes, not bash wrappers or grep itself
            if "pgrep" in line or "bash" in line or "grep" in line:
                continue
            parts = line.split(None, 1)
            pid = parts[0]
            cmd = parts[1] if len(parts) > 1 else ""
            if "paper_trade_v2.py" not in cmd:
                continue
            name = cmd.split("--instance")[-1].strip().split()[0] if "--instance" in cmd else "default"
            # Deduplicate by instance name
            if name in seen:
                continue
            seen.add(name)
            stats = get_session_stats(name)
            sessions.append({"name": name, "pid": pid, "where": "local", "status": "running", **stats})
    except Exception:
        pass
    return sessions


def get_vps_sessions():
    raw = ssh_cmd("""
        for svc in $(systemctl list-units 'polymarket-bot@*' --no-pager --no-legend 2>/dev/null | awk '{print $1}'); do
            INST=$(echo $svc | sed 's/polymarket-bot@//;s/\\.service//')
            ST=$(systemctl is-active $svc 2>/dev/null)
            CSV="/opt/polymarket-bot/data/${INST}/paper_trades_v2.csv"
            [ ! -f "$CSV" ] && CSV="/opt/polymarket-bot/data/paper_trades_v2.csv"
            N=0; W=0; PNL=0
            if [ -f "$CSV" ]; then
                N=$(tail -n +2 "$CSV" 2>/dev/null | wc -l | tr -d ' ')
                W=$(tail -n +2 "$CSV" 2>/dev/null | awk -F',' '{print $NF}' | grep -c WIN 2>/dev/null || echo 0)
                PNL=$(tail -n +2 "$CSV" 2>/dev/null | awk -F',' '{s+=$22} END {printf "%.0f", s}' 2>/dev/null || echo 0)
            fi
            echo "${INST}|${ST}|${N}|${W}|${PNL}"
        done
    """)
    sessions = []
    for line in raw.strip().split("\n"):
        if not line.strip() or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) >= 5:
            n = int(parts[2]) if parts[2].isdigit() else 0
            w = int(parts[3]) if parts[3].isdigit() else 0
            sessions.append({
                "name": parts[0], "where": "vps", "status": parts[1],
                "trades": n, "wins": w, "wr": round(w / n * 100, 1) if n > 0 else 0,
                "pnl": int(parts[4]) if parts[4].lstrip("-").isdigit() else 0, "combos": {},
            })
    return sessions


# ── Templates ─────────────────────────────────────────────────────
BASE_CSS = """
:root { --bg: #0a0e14; --card: #12171f; --border: #1c2333; --text: #c5cdd9;
        --dim: #4a5568; --blue: #5b9df9; --green: #34d399; --red: #f87171;
        --yellow: #fbbf24; --purple: #a78bfa; --accent: #818cf8; }
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: 'Inter', -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--text); font-size:14px; }
a { color: var(--blue); text-decoration: none; transition: color 0.15s; }
a:hover { color: var(--accent); }

.container { max-width: 1140px; margin: 0 auto; padding: 24px; }

/* Topbar */
.topbar { display:flex; justify-content:space-between; align-items:center; padding:16px 0 20px; margin-bottom:24px; }
.topbar h1 { font-size:1.4em; font-weight:700; background: linear-gradient(135deg, var(--blue), var(--purple)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
.topbar .actions { display:flex; gap:8px; align-items:center; }
.topbar .refresh-hint { font-size:0.72em; color:var(--dim); }

.section { margin-bottom: 32px; }
.section h2 { font-size:0.78em; color:var(--dim); text-transform:uppercase; letter-spacing:1.5px; margin-bottom:14px; font-weight:600; }

/* Buttons */
.btn { display:inline-flex; align-items:center; gap:5px; padding:7px 16px; border-radius:8px; font-size:0.82em;
       font-family:inherit; cursor:pointer; border:1px solid var(--border); background:var(--card); color:var(--text);
       transition:all 0.2s; font-weight:500; }
.btn:hover { background:#1e2536; transform:translateY(-1px); box-shadow:0 2px 8px rgba(0,0,0,0.3); text-decoration:none; }
.btn:active { transform:translateY(0); }
.btn-sm { padding:4px 10px; font-size:0.76em; border-radius:6px; }
.btn-green { border-color:#059669; color:var(--green); }
.btn-green:hover { background:#059669; color:#fff; }
.btn-red { border-color:#dc2626; color:var(--red); }
.btn-red:hover { background:#dc2626; color:#fff; }
.btn-blue { border-color:#2563eb; color:var(--blue); }
.btn-blue:hover { background:#2563eb; color:#fff; }
.btn-purple { border-color:#7c3aed; color:var(--purple); }
.btn-purple:hover { background:#7c3aed; color:#fff; }

/* Table */
table { width:100%; border-collapse:collapse; }
th { text-align:left; padding:10px 14px; color:var(--dim); font-size:0.72em; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; border-bottom:2px solid var(--border); }
td { padding:10px 14px; border-bottom:1px solid var(--border); font-size:0.88em; }
tr { transition: background 0.15s; }
tr:hover { background:rgba(91,157,249,0.04); }

/* Cards */
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:18px;
        transition:border-color 0.2s, box-shadow 0.2s; }
.card:hover { border-color:var(--dim); box-shadow:0 4px 16px rgba(0,0,0,0.2); }
.card-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); gap:14px; }
.card h3 { font-size:1em; margin-bottom:3px; font-weight:600; }
.card .desc { color:var(--dim); font-size:0.82em; margin-bottom:10px; }
.card .meta { font-size:0.76em; color:var(--dim); padding:8px 0; border-top:1px solid var(--border); margin-top:8px;
              display:flex; gap:12px; flex-wrap:wrap; }
.card .meta span { background:var(--bg); padding:2px 8px; border-radius:4px; }
.card .actions { display:flex; gap:6px; margin-top:12px; flex-wrap:wrap; }

/* Stats */
.stats-row { display:flex; gap:24px; margin-bottom:8px; flex-wrap:wrap; }
.stat { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px 24px; min-width:120px; }
.stat-val { font-size:1.8em; font-weight:800; font-variant-numeric:tabular-nums; }
.stat-label { font-size:0.68em; color:var(--dim); text-transform:uppercase; letter-spacing:0.5px; margin-top:2px; }

/* Tags */
.tag { display:inline-block; padding:3px 10px; border-radius:20px; font-size:0.7em; font-weight:600; letter-spacing:0.3px; }
.tag-active { background:rgba(52,211,153,0.12); color:var(--green); }
.tag-dead { background:rgba(248,113,113,0.12); color:var(--red); }
.tag-local { background:rgba(91,157,249,0.12); color:var(--blue); }
.tag-vps { background:rgba(167,139,250,0.12); color:var(--purple); }

.green { color:var(--green); } .red { color:var(--red); } .yellow { color:var(--yellow); } .dim { color:var(--dim); }

/* Pnl with glow */
.pnl-positive { color:var(--green); text-shadow:0 0 12px rgba(52,211,153,0.3); }
.pnl-negative { color:var(--red); text-shadow:0 0 12px rgba(248,113,113,0.3); }

/* Form */
.form-group { margin-bottom:14px; }
.form-group label { display:block; font-size:0.82em; color:var(--dim); margin-bottom:4px; font-weight:500; }
.form-group input, .form-group textarea, .form-group select {
    width:100%; padding:9px 12px; background:var(--bg); border:1px solid var(--border);
    color:var(--text); border-radius:6px; font-family:inherit; font-size:0.88em; transition:border-color 0.2s; }
.form-group input:focus, .form-group textarea:focus { border-color:var(--blue); outline:none; box-shadow:0 0 0 3px rgba(91,157,249,0.15); }
textarea { min-height:120px; resize:vertical; }

/* Flash */
.flash { padding:12px 18px; border-radius:8px; margin-bottom:16px; font-size:0.88em;
         background:rgba(52,211,153,0.1); color:var(--green); border:1px solid rgba(52,211,153,0.2);
         animation: flashIn 0.3s ease; }
.flash-warn { background:rgba(251,191,36,0.1); color:var(--yellow); border-color:rgba(251,191,36,0.3); }
@keyframes flashIn { from { opacity:0; transform:translateY(-8px); } to { opacity:1; transform:translateY(0); } }

/* Knob editor */
.knob-row { display:grid; grid-template-columns:1fr 90px; gap:10px; align-items:center; margin-bottom:6px;
            padding:6px 0; border-bottom:1px solid rgba(28,35,51,0.5); }
.knob-row label { font-size:0.82em; }
.knob-row .knob-key { font-size:0.68em; color:var(--dim); font-family:'SF Mono',monospace; }
.knob-row input { text-align:right; padding:6px 8px; }

/* Pulse animation for active sessions */
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.5; } }
.pulse { animation: pulse 2s ease-in-out infinite; }

/* Empty state */
.empty { text-align:center; padding:40px; color:var(--dim); }
.empty-icon { font-size:2.5em; margin-bottom:10px; }

/* Scrollbar */
::-webkit-scrollbar { width:6px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--dim); }

/* Auto-refresh indicator */
.auto-refresh { position:fixed; bottom:16px; right:16px; font-size:0.72em; color:var(--dim); background:var(--card);
                padding:6px 12px; border-radius:20px; border:1px solid var(--border); }
.auto-refresh .dot { display:inline-block; width:6px; height:6px; background:var(--green); border-radius:50%; margin-right:5px;
                     animation:pulse 2s infinite; }

hr { border:none; border-top:1px solid var(--border); margin:18px 0; }
"""

INDEX_HTML = """
<!DOCTYPE html><html><head><title>PM Dashboard</title><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>""" + BASE_CSS + """</style></head><body>
<div class="container">

<div class="topbar">
    <h1>Polymarket Bot</h1>
    <div class="actions">
        <span class="refresh-hint">auto-refreshes every 30s</span>
        <a href="/" class="btn">Refresh</a>
        <a href="/import" class="btn btn-purple">Import</a>
        <a href="/new" class="btn btn-green">+ New Session</a>
    </div>
</div>

{% for msg in get_flashed_messages() %}
<div class="flash {{ 'flash-warn' if msg.startswith('⚠') else '' }}">{{ msg }}</div>
{% endfor %}

<!-- Overview Stats -->
<div class="section">
    <div class="stats-row">
        <div class="stat">
            <div class="stat-val" style="color:var(--blue)">{{ sessions|length }}</div>
            <div class="stat-label">Active Sessions</div>
        </div>
        <div class="stat">
            <div class="stat-val">{{ total_trades }}</div>
            <div class="stat-label">Total Trades</div>
        </div>
        <div class="stat">
            <div class="stat-val {{ 'pnl-positive' if total_pnl >= 0 else 'pnl-negative' }}">${{ total_pnl }}</div>
            <div class="stat-label">Total PnL (Taker)</div>
        </div>
        <div class="stat">
            <div class="stat-val {{ 'green' if total_wr >= 60 else 'yellow' if total_wr >= 50 else 'red' }}">{{ total_wr }}%</div>
            <div class="stat-label">Overall Win Rate</div>
        </div>
    </div>
</div>

<!-- Sessions Table -->
<div class="section">
    <h2>Sessions</h2>
    {% if sessions %}
    <table>
    <tr><th>Name</th><th>Where</th><th>Status</th><th>Trades</th><th>Win%</th><th>PnL</th><th>Best Combo</th><th></th></tr>
    {% for s in sessions %}
    <tr>
        <td><strong><a href="/session/{{ s.name }}">{{ s.name }}</a></strong></td>
        <td><span class="tag tag-{{ s.where }}">{{ s.where }}</span></td>
        <td><span class="tag tag-{{ 'active' if s.status in ['running','active'] else 'dead' }}">
            {% if s.status in ['running','active'] %}<span class="dot pulse" style="display:inline-block;width:6px;height:6px;background:var(--green);border-radius:50%;margin-right:4px"></span>{% endif %}
            {{ s.status }}</span></td>
        <td>{{ s.trades }}</td>
        <td class="{{ 'green' if s.wr >= 65 else 'yellow' if s.wr >= 55 else 'red' if s.trades > 0 else 'dim' }}">
            {{ s.wr }}%</td>
        <td class="{{ 'pnl-positive' if s.pnl > 0 else 'pnl-negative' if s.pnl < 0 else 'dim' }}" style="font-weight:600">
            ${{ s.pnl }}</td>
        <td class="dim" style="font-size:0.8em">{{ s.top_combo or '--' }}</td>
        <td style="text-align:right">
            {% if s.status in ['running','active'] %}
            <a href="/stop/{{ s.where }}/{{ s.name }}" class="btn btn-sm btn-red" onclick="return confirm('Stop {{ s.name }}?')">Stop</a>
            {% else %}
            <a href="/start/local/{{ s.name }}" class="btn btn-sm btn-green">Local</a>
            <a href="/start/vps/{{ s.name }}" class="btn btn-sm btn-blue">VPS</a>
            {% endif %}
        </td>
    </tr>
    {% endfor %}
    </table>
    {% else %}
    <div class="empty">
        <div class="empty-icon">📡</div>
        <div>No sessions running</div>
        <div style="margin-top:8px"><a href="/new" class="btn btn-green">Create your first session</a></div>
    </div>
    {% endif %}
</div>

<!-- Configs -->
<div class="section">
    <h2>Configs</h2>
    <div class="card-grid">
    {% for c in configs %}
    <div class="card">
        <h3>{{ c.name }}{% if c.name == 'default' %} <span class="tag tag-active" style="font-size:0.65em;vertical-align:middle">DEFAULT</span>{% endif %}</h3>
        <div class="desc">{{ c.description or 'No description' }}</div>
        <div class="meta">
            <span>Entry: {{ (c.data.get('MIN_ENTRY_PRICE', 0.2) * 100)|int }}-{{ (c.data.get('MAX_ENTRY_PRICE', 0.8) * 100)|int }}¢</span>
            <span>Impulse: &lt;{{ c.data.get('MAX_IMPULSE_BP', '?') }}bp</span>
            <span>Dead: {{ c.data.get('DEAD_ZONE_START', '?') }}-{{ c.data.get('DEAD_ZONE_END', '?') }}s</span>
            <span>${{ c.data.get('BASE_TRADE_DOLLARS', 100) }}/trade</span>
        </div>
        <div class="actions">
            <a href="/edit/{{ c.name }}" class="btn btn-sm">Edit</a>
            <a href="/clone/{{ c.name }}" class="btn btn-sm btn-purple">Clone</a>
            <a href="/start/local/{{ c.name }}" class="btn btn-sm btn-green">▶ Local</a>
            <a href="/start/vps/{{ c.name }}" class="btn btn-sm btn-blue">▶ VPS</a>
            {% if c.name != 'default' %}
            <a href="/delete-config/{{ c.name }}" class="btn btn-sm btn-red" onclick="return confirm('Delete {{ c.name }}?')">✕</a>
            {% endif %}
        </div>
    </div>
    {% endfor %}
    </div>
</div>

<div class="auto-refresh"><span class="dot"></span>Auto-refresh 30s</div>

</div></body></html>
"""

SESSION_HTML = """
<!DOCTYPE html><html><head><title>{{ name }} — PM Dashboard</title><meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>""" + BASE_CSS + """
.combo-bar { height:4px; border-radius:2px; margin-top:4px; }
.combo-bar-fill { height:100%; border-radius:2px; transition:width 0.5s; }
.live-card { background:linear-gradient(135deg, var(--card), #1a1f2e); border-color:#2a3040; }
.live-label { font-size:0.68em; color:var(--dim); text-transform:uppercase; letter-spacing:0.5px; }
.live-val { font-size:1.1em; font-weight:600; margin-top:2px; }
.window-pill { display:inline-block; padding:3px 10px; border-radius:12px; font-size:0.75em; font-weight:600; margin:2px; }
.window-up { background:rgba(52,211,153,0.12); color:var(--green); }
.window-down { background:rgba(248,113,113,0.12); color:var(--red); }
</style>
<script>
function timeAgo(ts) {
    if (!ts) return '--';
    var d = Math.floor(Date.now()/1000 - ts);
    if (d < 60) return d + 's ago';
    if (d < 3600) return Math.floor(d/60) + 'm ago';
    return Math.floor(d/3600) + 'h ' + Math.floor((d%3600)/60) + 'm ago';
}
</script>
</head><body>
<div class="container">
<div class="topbar">
    <h1><a href="/" style="color:var(--dim);-webkit-text-fill-color:var(--dim)">Dashboard</a>
        <span style="color:var(--dim);-webkit-text-fill-color:var(--dim)">/</span> {{ name }}
        {% if live.get('window_active') %}<span class="tag tag-active" style="margin-left:8px">LIVE</span>{% endif %}
    </h1>
    <div class="actions">
        <a href="/" class="btn">← Back</a>
        <a href="/edit/{{ name }}" class="btn">Edit Config</a>
        <a href="/session/{{ name }}" class="btn">Refresh</a>
    </div>
</div>

<!-- Live Status Bar -->
{% if live.get('btc_price') %}
<div class="card live-card" style="margin-bottom:20px">
    <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:16px;">
        <div>
            <div class="live-label">BTC Price</div>
            <div class="live-val">${{ "{:,.2f}".format(live.btc_price or 0) }}</div>
        </div>
        <div>
            <div class="live-label">Price To Beat</div>
            <div class="live-val">{{ "${:,.2f}".format(live.window_open) if live.window_open else '—' }}
                <span class="dim" style="font-size:0.7em">{{ live.window_open_source or '' }}</span></div>
        </div>
        <div>
            <div class="live-label">PM Book</div>
            <div class="live-val">
                <span class="green">{{ "{:.0f}".format((live.book_bid or 0)*100) }}¢</span> /
                <span class="red">{{ "{:.0f}".format((live.book_ask or 0)*100) }}¢</span>
                <span class="dim" style="font-size:0.7em">spr {{ "{:.0f}".format((live.book_spread or 0)*100) }}¢</span>
            </div>
        </div>
        <div>
            <div class="live-label">Current Window</div>
            <div class="live-val">{{ live.current_window or '—' }}
                {% if live.window_active %}<span class="tag tag-active" style="font-size:0.65em">active</span>{% endif %}
            </div>
        </div>
        <div>
            <div class="live-label">Last Updated</div>
            <div class="live-val dim" id="updated-ago">—</div>
            <script>document.getElementById('updated-ago').textContent = timeAgo({{ live.updated or 0 }});</script>
        </div>
        {% if live.cooldown_active %}
        <div>
            <div class="live-label">Status</div>
            <div class="live-val yellow">⏸ COOLDOWN</div>
        </div>
        {% endif %}
    </div>
</div>
{% endif %}

<!-- Stats Row -->
<div class="stats-row" style="margin-bottom:24px">
    <div class="stat">
        <div class="stat-val" style="color:var(--blue)">{{ stats.trades }}</div>
        <div class="stat-label">Trades</div>
    </div>
    <div class="stat">
        <div class="stat-val {{ 'green' if stats.wr >= 60 else 'yellow' if stats.wr >= 50 else 'red' }}">{{ stats.wr }}%</div>
        <div class="stat-label">Win Rate</div>
    </div>
    <div class="stat">
        <div class="stat-val {{ 'pnl-positive' if stats.pnl >= 0 else 'pnl-negative' }}">${{ stats.pnl }}</div>
        <div class="stat-label">PnL (Taker)</div>
    </div>
    <div class="stat">
        <div class="stat-val" style="color:var(--blue)">{{ live.get('windows_settled', stats.get('windows', 0)) }}</div>
        <div class="stat-label">Windows</div>
    </div>
</div>

<!-- Recent Windows -->
{% if live.get('recent_windows') %}
<div class="section">
    <h2>Recent Windows</h2>
    <div style="display:flex; flex-wrap:wrap; gap:4px;">
    {% for w in live.recent_windows %}
        <span class="window-pill window-{{ w.outcome|lower }}">{{ w.outcome }}</span>
    {% endfor %}
    </div>
</div>
{% endif %}

<!-- Per-Combo Breakdown -->
{% if stats.combos %}
<div class="section">
    <h2>Per-Combo Breakdown</h2>
    <table>
    <tr><th>Combo</th><th>Trades</th><th>Wins</th><th>Win%</th><th>PnL</th><th style="width:120px">Performance</th></tr>
    {% for cname, c in combo_sorted %}
    <tr>
        <td><strong>{{ cname }}</strong></td>
        <td>{{ c.n }}</td>
        <td>{{ c.wins }}</td>
        <td class="{{ 'green' if c.wr >= 65 else 'yellow' if c.wr >= 55 else 'red' }}">{{ c.wr }}%</td>
        <td class="{{ 'pnl-positive' if c.pnl > 0 else 'pnl-negative' }}" style="font-weight:600">${{ c.pnl }}</td>
        <td>
            <div class="combo-bar" style="background:rgba(248,113,113,0.15)">
                <div class="combo-bar-fill" style="width:{{ c.wr }}%; background:{{ 'var(--green)' if c.wr >= 60 else 'var(--yellow)' if c.wr >= 50 else 'var(--red)' }}"></div>
            </div>
        </td>
    </tr>
    {% endfor %}
    </table>
</div>
{% endif %}

<!-- Trade Log -->
{% if trades %}
<div class="section">
    <h2>Trade Log (last {{ trades|length }})</h2>
    <table>
    <tr><th>Time</th><th>Combo</th><th>Dir</th><th>Entry</th><th>Size</th><th>Impulse</th><th>T-Rem</th><th>Outcome</th><th>Result</th><th>PnL</th></tr>
    {% for t in trades %}
    <tr>
        <td class="dim">{{ t._time }}</td>
        <td>{{ t.combo }}</td>
        <td>{{ t.direction }}</td>
        <td>{{ t._entry_c }}¢</td>
        <td>{{ t.filled_size }}</td>
        <td>{{ t.impulse_bps }}bp</td>
        <td>{{ t.time_remaining }}s</td>
        <td>{{ t.outcome or '—' }}</td>
        <td><span class="tag {{ 'tag-active' if t.result=='WIN' else 'tag-dead' if t.result=='LOSS' else '' }}">{{ t.result or '—' }}</span></td>
        <td class="{{ 'pnl-positive' if (t._pnl or 0) > 0 else 'pnl-negative' if (t._pnl or 0) < 0 else 'dim' }}" style="font-weight:600">
            {{ '${}'.format(t.pnl_taker) if t.pnl_taker else '—' }}</td>
    </tr>
    {% endfor %}
    </table>
</div>
{% else %}
<div class="empty">
    <div class="empty-icon">📊</div>
    <div>No trade data yet</div>
</div>
{% endif %}

{% if live.get('errors') %}
<div class="section">
    <h2>Errors</h2>
    <div class="dim" style="font-size:0.82em">
    {% for k, v in live.errors.items() %}
        {{ k }}: {{ v }}{% if not loop.last %} | {% endif %}
    {% endfor %}
    </div>
</div>
{% endif %}

<div class="auto-refresh"><span class="dot"></span>Auto-refresh 15s</div>
</div></body></html>
"""

EDIT_HTML = """
<!DOCTYPE html><html><head><title>Edit {{ name }}</title><meta charset="utf-8">
<style>""" + BASE_CSS + """</style></head><body>
<div class="container" style="max-width:650px">
<div class="topbar">
    <h1>{{ 'Edit' if not is_new else 'New Session' }}: {{ name }}</h1>
    <a href="/" class="btn">Cancel</a>
</div>
{% for msg in get_flashed_messages() %}
<div class="flash {{ 'flash-warn' if msg.startswith('⚠') else '' }}">{{ msg }}</div>
{% endfor %}
{% if not is_new and stats and stats.trades > 0 %}
<div class="card" style="margin-bottom:20px">
    <div style="display:flex; gap:24px; flex-wrap:wrap">
        <div><span style="font-size:1.4em;font-weight:700">{{ stats.trades }}</span><br><span class="dim" style="font-size:0.72em">TRADES</span></div>
        <div><span style="font-size:1.4em;font-weight:700" class="{{ 'green' if stats.wr >= 60 else 'yellow' if stats.wr >= 50 else 'red' }}">{{ stats.wr }}%</span><br><span class="dim" style="font-size:0.72em">WIN RATE</span></div>
        <div><span style="font-size:1.4em;font-weight:700" class="{{ 'pnl-positive' if stats.pnl >= 0 else 'pnl-negative' }}">${{ stats.pnl }}</span><br><span class="dim" style="font-size:0.72em">PNL (TAKER)</span></div>
    </div>
</div>
{% endif %}
<form method="POST" action="{{ '/save/' + name if not is_new else '/create' }}">
    {% if is_new %}
    <div class="form-group">
        <label>Session Name</label>
        <input name="name" placeholder="my-session" required pattern="[a-z0-9-]+" value="">
    </div>
    {% endif %}
    <div class="form-group">
        <label>Description</label>
        <input name="_description" value="{{ config.get('_description', '') }}" placeholder="What this session tests">
    </div>
    <hr style="border-color:var(--border); margin:16px 0">
    <h2 style="font-size:0.85em; color:var(--dim); margin-bottom:12px">FILTERS</h2>
    {% for key, meta in knobs.items() %}
    <div class="knob-row">
        <label>{{ meta.desc }} <span class="dim">({{ key }})</span></label>
        <input type="{{ meta.type }}" name="{{ key }}" value="{{ config.get(key, meta.default) }}"
               step="{{ meta.step }}" min="{{ meta.min }}" max="{{ meta.max }}"
               title="{{ key }}: {{ meta.min }} – {{ meta.max }}">
    </div>
    {% endfor %}
    <hr style="border-color:var(--border); margin:16px 0">
    <div class="form-group">
        <label>Raw JSON (advanced — overrides above)</label>
        <textarea name="raw_json" style="min-height:80px" placeholder="Leave empty to use form values above"></textarea>
    </div>
    <div style="display:flex; gap:8px; margin-top:16px">
        <button type="submit" class="btn btn-green">Save{% if is_new %} & Create{% endif %}</button>
        <a href="/" class="btn">Cancel</a>
        {% if is_new %}
        <button type="submit" formaction="/create?start=local" class="btn btn-blue">Save & Start Local</button>
        {% endif %}
    </div>
</form>
</div></body></html>
"""

IMPORT_HTML = """
<!DOCTYPE html><html><head><title>Import Config</title><meta charset="utf-8">
<style>""" + BASE_CSS + """</style></head><body>
<div class="container" style="max-width:650px">
<div class="topbar"><h1>Import Config</h1><a href="/" class="btn">Cancel</a></div>
<form method="POST" action="/import" enctype="multipart/form-data">
    <div class="form-group">
        <label>Session Name</label>
        <input name="name" required pattern="[a-z0-9-]+" placeholder="my-session">
    </div>
    <div class="form-group">
        <label>Upload JSON file</label>
        <input type="file" name="file" accept=".json">
    </div>
    <div class="form-group">
        <label>Or paste JSON</label>
        <textarea name="json_text" placeholder='{"MIN_ENTRY_PRICE": 0.30, ...}'></textarea>
    </div>
    <button type="submit" class="btn btn-green">Import</button>
</form>
</div></body></html>
"""


# ── Routes ────────────────────────────────────────────────────────
@app.route("/")
def index():
    local = get_local_sessions()
    vps = get_vps_sessions()
    sessions = local + vps

    # Add top combo for each session
    for s in sessions:
        combos = s.get("combos", {})
        if combos:
            top = max(combos.items(), key=lambda x: x[1]["pnl"])
            s["top_combo"] = "{} (${})".format(top[0], top[1]["pnl"])
        else:
            s["top_combo"] = None

    configs = get_configs()
    total_trades = sum(s["trades"] for s in sessions)
    total_pnl = sum(s["pnl"] for s in sessions)

    total_wins = sum(s.get("wins", 0) for s in sessions)
    total_wr = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0

    return render_template_string(INDEX_HTML, sessions=sessions, configs=configs,
                                  total_trades=total_trades, total_pnl=total_pnl, total_wr=total_wr)


@app.route("/session/<name>")
def session_detail(name):
    from datetime import datetime

    stats = get_session_stats(name)

    # If no local data, try pulling from VPS
    if stats["trades"] == 0:
        try:
            subprocess.run(["./scripts/pull-data.sh", name], capture_output=True, timeout=15)
            stats = get_session_stats(name)
        except Exception:
            pass

    # Read live stats.json for real-time info
    live = {}
    stats_json = DATA_DIR / name / "stats.json"
    if stats_json.exists():
        try:
            live = json.loads(stats_json.read_text())
        except Exception:
            pass

    # Get trades from stats.json recent_trades (preferred) or CSV fallback
    trades = []
    if live.get("recent_trades"):
        for t in live["recent_trades"]:
            ts = t.get("timestamp", 0)
            fp = t.get("fill_price", 0) or 0
            pnl = t.get("pnl_taker")
            t["_time"] = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"
            t["_entry_c"] = "{:.1f}".format(float(fp) * 100) if fp else "?"
            t["_pnl"] = float(pnl) if pnl else 0
            trades.append(t)
        trades.reverse()  # newest first
    else:
        # CSV fallback
        for p in [DATA_DIR / name / "trades.csv", DATA_DIR / name / "paper_trades_v2.csv",
                   DATA_DIR / "paper_trades_v2.csv" if name == "default" else None]:
            if p and p.exists():
                try:
                    with open(p) as f:
                        reader = csv.DictReader(f)
                        if reader.fieldnames:
                            reader.fieldnames = [h.strip() for h in reader.fieldnames]
                        rows = [{k.strip(): v.strip() if isinstance(v, str) else v for k, v in row.items()} for row in reader]
                    for t in rows[-20:]:
                        ts = float(t.get("timestamp", 0) or 0)
                        fp = float(t.get("fill_price", 0) or 0)
                        pnl = t.get("pnl_taker")
                        t["_time"] = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"
                        t["_entry_c"] = "{:.1f}".format(fp * 100) if fp else "?"
                        t["_pnl"] = float(pnl) if pnl else 0
                        trades.append(t)
                    trades.reverse()
                except Exception:
                    pass
                break

    combo_sorted = sorted(stats.get("combos", {}).items(), key=lambda x: x[1]["pnl"], reverse=True)
    return render_template_string(SESSION_HTML, name=name, stats=stats, combo_sorted=combo_sorted,
                                  trades=trades, live=live)


@app.route("/new")
def new_session():
    config = json.loads(Path("configs/default.json").read_text()) if Path("configs/default.json").exists() else {}
    return render_template_string(EDIT_HTML, name="", config=config, knobs=KNOB_DEFS, is_new=True)


@app.route("/edit/<name>")
def edit_config(name):
    path = Path("configs/{}.json".format(name))
    config = json.loads(path.read_text()) if path.exists() else {}
    stats = get_session_stats(name)
    return render_template_string(EDIT_HTML, name=name, config=config, knobs=KNOB_DEFS, is_new=False, stats=stats)


@app.route("/clone/<name>")
def clone_config(name):
    path = Path("configs/{}.json".format(name))
    config = json.loads(path.read_text()) if path.exists() else {}
    config["_description"] = "Clone of " + name
    return render_template_string(EDIT_HTML, name="", config=config, knobs=KNOB_DEFS, is_new=True)


def parse_form_config(form):
    """Build config dict from form data."""
    # Check for raw JSON override
    raw = form.get("raw_json", "").strip()
    if raw:
        return json.loads(raw)

    config = {}
    desc = form.get("_description", "").strip()
    if desc:
        config["_description"] = desc

    for key, meta in KNOB_DEFS.items():
        val = form.get(key, "").strip()
        if val:
            if meta["type"] == "number":
                config[key] = float(val) if "." in val else int(val)
            else:
                config[key] = val
    return config


@app.route("/create", methods=["POST"])
def create_session():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name is required")
        return redirect("/new")
    try:
        config = parse_form_config(request.form)
    except json.JSONDecodeError:
        flash("Invalid JSON")
        return redirect("/new")

    # Validate
    errors = validate_config(config)
    if errors:
        for e in errors:
            flash("⚠ " + e)
        return redirect("/new")

    Path("configs/{}.json".format(name)).write_text(json.dumps(config, indent=4))
    flash("✓ Created config: {}".format(name))

    if request.args.get("start") == "local":
        Path("data/{}".format(name)).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["python3", "-u", "bot/paper_trade_v2.py", "--instance", name],
            stdout=open("data/{}.log".format(name), "a"),
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        flash("Started local: {}".format(name))
        time.sleep(1)

    return redirect("/")


@app.route("/save/<name>", methods=["POST"])
def save_config(name):
    try:
        config = parse_form_config(request.form)
    except json.JSONDecodeError:
        flash("Invalid JSON")
        return redirect("/edit/" + name)

    errors = validate_config(config)
    if errors:
        for e in errors:
            flash("⚠ " + e)
        return redirect("/edit/" + name)

    Path("configs/{}.json".format(name)).write_text(json.dumps(config, indent=4))
    flash("✓ Saved: {}".format(name))
    return redirect("/")


@app.route("/delete-config/<name>")
def delete_config(name):
    path = Path("configs/{}.json".format(name))
    if path.exists() and name != "default":
        path.unlink()
        flash("Deleted config: {}".format(name))
    return redirect("/")


@app.route("/import", methods=["GET"])
def import_page():
    return render_template_string(IMPORT_HTML)


@app.route("/import", methods=["POST"])
def import_config():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name required")
        return redirect("/import")

    # Try file upload first
    f = request.files.get("file")
    if f and f.filename:
        config = json.loads(f.read())
    else:
        raw = request.form.get("json_text", "").strip()
        if raw:
            config = json.loads(raw)
        else:
            flash("No config provided")
            return redirect("/import")

    errors = validate_config(config)
    if errors:
        for e in errors:
            flash("⚠ " + e)
        return redirect("/import")

    Path("configs/{}.json".format(name)).write_text(json.dumps(config, indent=4))
    flash("✓ Imported: {}".format(name))
    return redirect("/")


@app.route("/start/<where>/<name>")
def start_session(where, name):
    if where == "local":
        Path("data/{}".format(name)).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["python3", "-u", "bot/paper_trade_v2.py", "--instance", name],
            stdout=open("data/{}.log".format(name), "a"),
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        flash("Started local: {}".format(name))
    elif where == "vps":
        subprocess.Popen(["./scripts/new-session.sh", name])
        flash("Starting on VPS: {}".format(name))
    time.sleep(1)
    return redirect("/")


@app.route("/stop/<where>/<name>")
def stop_session(where, name):
    if where == "local":
        subprocess.run(["pkill", "-INT", "-f", "paper_trade_v2.py.*--instance {}".format(name)])
        flash("Stopping local: {}".format(name))
    elif where == "vps":
        subprocess.Popen(["./scripts/stop.sh", name])
        flash("Stopping VPS: {}".format(name))
    time.sleep(2)
    return redirect("/")


@app.route("/api/sessions")
def api_sessions():
    return jsonify({"local": get_local_sessions(), "vps": get_vps_sessions()})


@app.route("/api/configs")
def api_configs():
    return jsonify(get_configs())


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:5555\n")
    app.run(host="0.0.0.0", port=5555, debug=False)
