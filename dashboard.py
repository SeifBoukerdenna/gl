"""
Polymarket Bot Dashboard — lightweight web UI on localhost:5555
Usage: python dashboard.py
"""

import csv
import json
import os
import subprocess
import signal
import time
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request, redirect

app = Flask(__name__)
CONFIGS_DIR = Path("configs")
DATA_DIR = Path("data")

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
            configs.append({
                "name": f.stem,
                "description": data.get("_description", ""),
                "data": data,
            })
        except Exception:
            configs.append({"name": f.stem, "description": "error", "data": {}})
    return configs


def get_local_sessions():
    sessions = []
    try:
        result = subprocess.run(["pgrep", "-af", "paper_trade_v2.py"], capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(None, 1)
            pid = parts[0]
            cmd = parts[1] if len(parts) > 1 else ""
            if "--instance" in cmd:
                name = cmd.split("--instance")[-1].strip().split()[0]
            else:
                name = "default"
            sessions.append({"name": name, "pid": pid, "where": "local"})
    except Exception:
        pass

    for s in sessions:
        s.update(get_session_stats(s["name"], "local"))
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
                "name": parts[0],
                "where": "vps",
                "status": parts[1],
                "trades": n,
                "wins": w,
                "wr": round(w / n * 100, 1) if n > 0 else 0,
                "pnl": int(parts[4]) if parts[4].lstrip("-").isdigit() else 0,
            })
    return sessions


def get_session_stats(name, where="local"):
    if where == "local":
        csv_path = DATA_DIR / name / "paper_trades_v2.csv" if name != "default" else DATA_DIR / "paper_trades_v2.csv"
    else:
        return {}

    if not csv_path.exists():
        return {"trades": 0, "wins": 0, "wr": 0, "pnl": 0, "status": "no data"}

    trades = []
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            # Strip whitespace from headers
            if reader.fieldnames:
                reader.fieldnames = [h.strip() for h in reader.fieldnames]
            for row in reader:
                row = {k.strip(): v.strip() if isinstance(v, str) else v for k, v in row.items()}
                trades.append(row)
    except Exception:
        return {"trades": 0, "wins": 0, "wr": 0, "pnl": 0, "status": "error"}

    n = len(trades)
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    pnl = sum(float(t.get("pnl_taker", 0) or 0) for t in trades)
    return {
        "trades": n,
        "wins": wins,
        "wr": round(wins / n * 100, 1) if n > 0 else 0,
        "pnl": round(pnl),
        "status": "running",
    }


# ── HTML Template ─────────────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html>
<head>
<title>PM Bot Dashboard</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'SF Mono', 'Fira Code', monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { color: #58a6ff; margin-bottom: 5px; font-size: 1.4em; }
h2 { color: #8b949e; font-size: 1em; margin: 25px 0 10px; text-transform: uppercase; letter-spacing: 1px; }
.subtitle { color: #8b949e; font-size: 0.85em; margin-bottom: 20px; }
table { width: 100%; border-collapse: collapse; margin-bottom: 15px; }
th { text-align: left; padding: 8px 12px; color: #8b949e; font-size: 0.8em; border-bottom: 1px solid #21262d; }
td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 0.9em; }
tr:hover { background: #161b22; }
.green { color: #3fb950; }
.red { color: #f85149; }
.yellow { color: #d29922; }
.dim { color: #484f58; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.75em; }
.badge-active { background: #0d4429; color: #3fb950; }
.badge-dead { background: #3d1e20; color: #f85149; }
.badge-local { background: #1c2a3d; color: #58a6ff; }
.badge-vps { background: #2d1f3d; color: #bc8cff; }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 15px; margin-bottom: 15px; }
.btn { display: inline-block; padding: 6px 14px; border-radius: 6px; text-decoration: none;
       font-size: 0.85em; font-family: inherit; cursor: pointer; border: 1px solid #30363d;
       background: #21262d; color: #c9d1d9; margin: 2px; }
.btn:hover { background: #30363d; }
.btn-green { border-color: #238636; color: #3fb950; }
.btn-green:hover { background: #238636; color: white; }
.btn-red { border-color: #da3633; color: #f85149; }
.btn-red:hover { background: #da3633; color: white; }
.config-json { background: #0d1117; border: 1px solid #21262d; padding: 10px; border-radius: 4px;
               font-size: 0.8em; white-space: pre; overflow-x: auto; max-height: 200px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.pnl-big { font-size: 1.8em; font-weight: bold; }
.stat-label { font-size: 0.75em; color: #8b949e; }
input, textarea { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 8px;
                  border-radius: 4px; font-family: inherit; font-size: 0.9em; width: 100%; }
textarea { min-height: 200px; }
.topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.refresh { color: #484f58; font-size: 0.8em; }
</style>
</head>
<body>

<div class="topbar">
    <div>
        <h1>Polymarket Bot</h1>
        <div class="subtitle">Dashboard</div>
    </div>
    <div>
        <a href="/" class="btn">Refresh</a>
        <a href="/new" class="btn btn-green">+ New Session</a>
    </div>
</div>

<!-- Sessions -->
<h2>Sessions</h2>
<table>
<tr>
    <th>Name</th><th>Where</th><th>Status</th><th>Trades</th><th>Win%</th><th>PnL</th><th>Actions</th>
</tr>
{% for s in sessions %}
<tr>
    <td><strong>{{ s.name }}</strong></td>
    <td><span class="badge badge-{{ s.where }}">{{ s.where }}</span></td>
    <td><span class="badge badge-{{ 'active' if s.status in ['running','active'] else 'dead' }}">{{ s.status }}</span></td>
    <td>{{ s.trades }}</td>
    <td class="{{ 'green' if s.wr >= 65 else 'yellow' if s.wr >= 55 else 'red' if s.trades > 0 else 'dim' }}">{{ s.wr }}%</td>
    <td class="{{ 'green' if s.pnl > 0 else 'red' if s.pnl < 0 else 'dim' }}">${{ s.pnl }}</td>
    <td>
        {% if s.status in ['running','active'] %}
        <a href="/stop/{{ s.where }}/{{ s.name }}" class="btn btn-red" onclick="return confirm('Stop {{ s.name }}?')">Stop</a>
        {% else %}
        <a href="/start/local/{{ s.name }}" class="btn btn-green">Start Local</a>
        <a href="/start/vps/{{ s.name }}" class="btn btn-green">Start VPS</a>
        {% endif %}
    </td>
</tr>
{% endfor %}
{% if not sessions %}
<tr><td colspan="7" class="dim">No sessions. <a href="/new" class="btn btn-green">Create one</a></td></tr>
{% endif %}
</table>

<!-- Configs -->
<h2>Configs</h2>
<div class="grid">
{% for c in configs %}
<div class="card">
    <strong>{{ c.name }}</strong>
    <div class="dim" style="margin:4px 0 8px">{{ c.description }}</div>
    <div class="config-json">{{ c.data | tojson(indent=2) }}</div>
    <div style="margin-top:8px">
        <a href="/edit/{{ c.name }}" class="btn">Edit</a>
        <a href="/start/local/{{ c.name }}" class="btn btn-green">Run Local</a>
        <a href="/start/vps/{{ c.name }}" class="btn btn-green">Run VPS</a>
    </div>
</div>
{% endfor %}
</div>

<div style="margin-top:30px; color:#484f58; font-size:0.8em">
    Auto-refresh: <a href="/" style="color:#58a6ff">manual</a> |
    <a href="/api/sessions" style="color:#58a6ff">API: /api/sessions</a>
</div>

</body>
</html>
"""

NEW_HTML = """
<!DOCTYPE html>
<html>
<head><title>New Session</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; padding: 40px; max-width: 600px; margin: 0 auto; }
h1 { color: #58a6ff; margin-bottom: 20px; }
label { display: block; margin: 15px 0 5px; color: #8b949e; font-size: 0.85em; }
input, textarea { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 10px;
                  border-radius: 4px; font-family: inherit; font-size: 0.9em; width: 100%; }
textarea { min-height: 300px; }
.btn { display: inline-block; padding: 10px 20px; border-radius: 6px; text-decoration: none;
       font-family: inherit; cursor: pointer; border: none; margin-top: 15px; font-size: 0.9em; }
.btn-green { background: #238636; color: white; }
.btn-back { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
</style>
</head>
<body>
<h1>New Session</h1>
<form method="POST" action="/create">
    <label>Session Name</label>
    <input name="name" placeholder="my-session" required pattern="[a-z0-9-]+">
    <label>Config (JSON)</label>
    <textarea name="config">{{ default_config }}</textarea>
    <button type="submit" class="btn btn-green">Create & Start</button>
    <a href="/" class="btn btn-back">Cancel</a>
</form>
</body>
</html>
"""

EDIT_HTML = """
<!DOCTYPE html>
<html>
<head><title>Edit {{ name }}</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'SF Mono', monospace; background: #0d1117; color: #c9d1d9; padding: 40px; max-width: 600px; margin: 0 auto; }
h1 { color: #58a6ff; margin-bottom: 20px; }
label { display: block; margin: 15px 0 5px; color: #8b949e; }
textarea { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 10px;
           border-radius: 4px; font-family: inherit; font-size: 0.9em; width: 100%; min-height: 350px; }
.btn { display: inline-block; padding: 10px 20px; border-radius: 6px; text-decoration: none;
       font-family: inherit; cursor: pointer; border: none; margin-top: 15px; font-size: 0.9em; }
.btn-green { background: #238636; color: white; }
.btn-back { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
</style>
</head>
<body>
<h1>Edit: {{ name }}</h1>
<form method="POST" action="/save/{{ name }}">
    <textarea name="config">{{ config }}</textarea>
    <button type="submit" class="btn btn-green">Save</button>
    <a href="/" class="btn btn-back">Cancel</a>
</form>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────────
@app.route("/")
def index():
    local = get_local_sessions()
    vps = get_vps_sessions()
    sessions = local + vps
    configs = get_configs()
    return render_template_string(HTML, sessions=sessions, configs=configs)


@app.route("/api/sessions")
def api_sessions():
    local = get_local_sessions()
    vps = get_vps_sessions()
    return jsonify({"local": local, "vps": vps})


@app.route("/new")
def new_session():
    default = json.dumps(json.loads(Path("configs/default.json").read_text()), indent=2)
    return render_template_string(NEW_HTML, default_config=default)


@app.route("/create", methods=["POST"])
def create_session():
    name = request.form.get("name", "").strip()
    config = request.form.get("config", "{}")
    if not name:
        return redirect("/new")
    # Save config
    try:
        parsed = json.loads(config)
        Path("configs/{}.json".format(name)).write_text(json.dumps(parsed, indent=4))
    except json.JSONDecodeError:
        return "Invalid JSON", 400
    # Start locally
    subprocess.Popen(
        ["python3", "-u", "bot/paper_trade_v2.py", "--instance", name],
        stdout=open("data/{}.log".format(name), "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(1)
    return redirect("/")


@app.route("/edit/<name>")
def edit_config(name):
    path = Path("configs/{}.json".format(name))
    if not path.exists():
        return "Config not found", 404
    config = json.dumps(json.loads(path.read_text()), indent=4)
    return render_template_string(EDIT_HTML, name=name, config=config)


@app.route("/save/<name>", methods=["POST"])
def save_config(name):
    config = request.form.get("config", "{}")
    try:
        parsed = json.loads(config)
        Path("configs/{}.json".format(name)).write_text(json.dumps(parsed, indent=4))
    except json.JSONDecodeError:
        return "Invalid JSON", 400
    return redirect("/")


@app.route("/start/<where>/<name>")
def start_session(where, name):
    if where == "local":
        Path("data/{}".format(name)).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["python3", "-u", "bot/paper_trade_v2.py", "--instance", name],
            stdout=open("data/{}.log".format(name), "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    elif where == "vps":
        subprocess.Popen(["./scripts/new-session.sh", name])
    time.sleep(1)
    return redirect("/")


@app.route("/stop/<where>/<name>")
def stop_session(where, name):
    if where == "local":
        subprocess.run(["pkill", "-INT", "-f", "paper_trade_v2.py.*--instance {}".format(name)])
    elif where == "vps":
        subprocess.Popen(["./scripts/stop.sh", name])
    time.sleep(2)
    return redirect("/")


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:5555\n")
    app.run(host="0.0.0.0", port=5555, debug=False)
