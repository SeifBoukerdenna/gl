"""
Polymarket Bot Dashboard — localhost:5555
"""

import csv
import json
import subprocess
import time
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, redirect, flash

app = Flask(__name__)
app.secret_key = "pm-bot-dash"
CONFIGS_DIR = Path("configs")
DATA_DIR = Path("data")

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
    ds = config.get("DEAD_ZONE_START", 90)
    de = config.get("DEAD_ZONE_END", 210)
    if ds >= de:
        errors.append("DEAD_ZONE_START must be < END")
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
    """Read stats.json or fall back to CSV. Returns dict with all session info."""
    stats_path = DATA_DIR / name / "stats.json"
    if stats_path.exists():
        try:
            return json.loads(stats_path.read_text())
        except Exception:
            pass

    # CSV fallback
    for csv_path in [DATA_DIR / name / "trades.csv", DATA_DIR / name / "paper_trades_v2.csv",
                      DATA_DIR / "paper_trades_v2.csv" if name == "default" else None]:
        if csv_path and csv_path.exists():
            try:
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    if reader.fieldnames:
                        reader.fieldnames = [h.strip() for h in reader.fieldnames]
                    trades = [{k.strip(): v.strip() if isinstance(v, str) else v for k, v in row.items()} for row in reader]
                n = len(trades)
                wins = sum(1 for t in trades if t.get("result") == "WIN")
                combos = {}
                for t in trades:
                    c = t.get("combo", "?")
                    if c not in combos:
                        combos[c] = {"trades": 0, "wins": 0, "pnl_taker": 0}
                    combos[c]["trades"] += 1
                    if t.get("result") == "WIN":
                        combos[c]["wins"] += 1
                    combos[c]["pnl_taker"] += float(t.get("pnl_taker", 0) or 0)
                for c in combos.values():
                    c["wr"] = round(c["wins"] / c["trades"] * 100, 1) if c["trades"] > 0 else 0
                    c["pnl_taker"] = round(c["pnl_taker"], 2)
                recent = []
                for t in trades[-20:]:
                    recent.append({
                        "combo": t.get("combo"), "direction": t.get("direction"),
                        "fill_price": t.get("fill_price"), "filled_size": t.get("filled_size"),
                        "impulse_bps": t.get("impulse_bps"), "time_remaining": t.get("time_remaining"),
                        "result": t.get("result"), "pnl_taker": t.get("pnl_taker"),
                        "timestamp": t.get("timestamp"), "outcome": t.get("outcome"),
                    })
                return {
                    "trades": n, "wins": wins,
                    "wr": round(wins / n * 100, 1) if n > 0 else 0,
                    "pnl_taker": round(sum(float(t.get("pnl_taker", 0) or 0) for t in trades), 2),
                    "combos": combos, "recent_trades": recent,
                }
            except Exception:
                pass
            break
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
            data = get_session_data(name)
            sessions.append({"name": name, "where": "local", "status": "running", **data})
    except Exception:
        pass

    # VPS
    raw = ssh_cmd("""
        for svc in $(systemctl list-units 'polymarket-bot@*' --no-pager --no-legend 2>/dev/null | awk '{print $1}'); do
            INST=$(echo $svc | sed 's/polymarket-bot@//;s/\\.service//'); ST=$(systemctl is-active $svc 2>/dev/null)
            CSV="/opt/polymarket-bot/data/${INST}/trades.csv"; [ ! -f "$CSV" ] && CSV="/opt/polymarket-bot/data/${INST}/paper_trades_v2.csv"
            [ ! -f "$CSV" ] && CSV="/opt/polymarket-bot/data/paper_trades_v2.csv"
            N=0; W=0; P=0
            if [ -f "$CSV" ]; then
                HEADER=$(head -1 "$CSV"|tr -d ' ')
                PNLCOL=$(echo "$HEADER"|tr ',' '\n'|grep -n 'pnl_taker'|cut -d: -f1)
                RESCOL=$(echo "$HEADER"|tr ',' '\n'|grep -n 'result'|cut -d: -f1)
                N=$(tail -n +2 "$CSV" 2>/dev/null|wc -l|tr -d ' ')
                [ -n "$RESCOL" ] && W=$(tail -n +2 "$CSV" 2>/dev/null|cut -d, -f$RESCOL|tr -d ' '|grep -c WIN||echo 0)
                [ -n "$PNLCOL" ] && P=$(tail -n +2 "$CSV" 2>/dev/null|cut -d, -f$PNLCOL|tr -d ' '|awk '{s+=$1}END{printf "%.0f",s}'||echo 0)
            fi
            echo "${INST}|${ST}|${N}|${W}|${P}"
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
                "combos": {}, "recent_trades": [],
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
    for key, meta in KNOB_DEFS.items():
        val = form.get(key, "").strip()
        if val:
            config[key] = float(val) if "." in val else int(val)
    return config


# ══════════════════════════════════════════════════════════════════
# TEMPLATES
# ══════════════════════════════════════════════════════════════════

CSS = """
<style>
:root{--bg:#0a0e14;--card:#12171f;--border:#1c2333;--text:#c5cdd9;--dim:#4a5568;
--blue:#5b9df9;--green:#34d399;--red:#f87171;--yellow:#fbbf24;--purple:#a78bfa}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:14px}
a{color:var(--blue);text-decoration:none}a:hover{color:#818cf8}
.c{max-width:1200px;margin:0 auto;padding:16px 20px}
h1{font-size:1.3em;font-weight:700;background:linear-gradient(135deg,var(--blue),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
h2{font-size:0.78em;color:var(--dim);text-transform:uppercase;letter-spacing:1.5px;margin:20px 0 10px;font-weight:600}
.top{display:flex;justify-content:space-between;align-items:center;padding:12px 0 16px;border-bottom:1px solid var(--border);margin-bottom:16px}
.top .r{display:flex;gap:6px;align-items:center}
.btn{display:inline-flex;align-items:center;gap:4px;padding:6px 14px;border-radius:6px;font-size:0.8em;font-family:inherit;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text);transition:all .15s;font-weight:500}
.btn:hover{background:#1e2536;transform:translateY(-1px);box-shadow:0 2px 8px rgba(0,0,0,.3);text-decoration:none}
.btn-s{padding:4px 10px;font-size:.75em;border-radius:5px}
.btn-g{border-color:#059669;color:var(--green)}.btn-g:hover{background:#059669;color:#fff}
.btn-r{border-color:#dc2626;color:var(--red)}.btn-r:hover{background:#dc2626;color:#fff}
.btn-b{border-color:#2563eb;color:var(--blue)}.btn-b:hover{background:#2563eb;color:#fff}
.btn-p{border-color:#7c3aed;color:var(--purple)}.btn-p:hover{background:#7c3aed;color:#fff}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:8px 12px;color:var(--dim);font-size:.72em;font-weight:600;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid var(--border)}
td{padding:8px 12px;border-bottom:1px solid var(--border);font-size:.86em}
tr:hover{background:rgba(91,157,249,.04)}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;transition:border-color .2s}
.card:hover{border-color:var(--dim)}
.cg{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}
.sr{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px}
.st{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 18px;min-width:100px}
.sv{font-size:1.6em;font-weight:800;font-variant-numeric:tabular-nums}
.sl{font-size:.68em;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-top:1px}
.tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:.7em;font-weight:600}
.tag-a{background:rgba(52,211,153,.12);color:var(--green)}.tag-d{background:rgba(248,113,113,.12);color:var(--red)}
.tag-l{background:rgba(91,157,249,.12);color:var(--blue)}.tag-v{background:rgba(167,139,250,.12);color:var(--purple)}
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.d{color:var(--dim)}
.pg{color:var(--green);text-shadow:0 0 10px rgba(52,211,153,.25)}
.pr{color:var(--red);text-shadow:0 0 10px rgba(248,113,113,.25)}
.flash{padding:10px 16px;border-radius:6px;margin-bottom:12px;font-size:.86em;background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.2);animation:fi .3s}
.flash-w{background:rgba(251,191,36,.1);color:var(--yellow);border-color:rgba(251,191,36,.3)}
@keyframes fi{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.pulse{animation:pulse 2s infinite}
input,textarea{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:5px;font-family:inherit;font-size:.86em;width:100%}
input:focus,textarea:focus{border-color:var(--blue);outline:none;box-shadow:0 0 0 2px rgba(91,157,249,.15)}
textarea{min-height:100px;resize:vertical}
.kr{display:grid;grid-template-columns:1fr 80px;gap:8px;align-items:center;margin-bottom:6px;padding:4px 0}
.kr label{font-size:.82em}.kr input{text-align:right}
.live{background:linear-gradient(135deg,var(--card),#1a1f2e);border-color:#2a3040}
.ll{font-size:.68em;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.lv{font-size:1em;font-weight:600;margin-top:1px}
.wp{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.72em;font-weight:600;margin:1px}
.wu{background:rgba(52,211,153,.12);color:var(--green)}.wd{background:rgba(248,113,113,.12);color:var(--red)}
hr{border:none;border-top:1px solid var(--border);margin:14px 0}
.ar{position:fixed;bottom:12px;right:12px;font-size:.7em;color:var(--dim);background:var(--card);padding:5px 10px;border-radius:16px;border:1px solid var(--border)}
.ar .dot{display:inline-block;width:5px;height:5px;background:var(--green);border-radius:50%;margin-right:4px;animation:pulse 2s infinite}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script>
function ta(ts){if(!ts)return'—';var d=Math.floor(Date.now()/1000-ts);if(d<0)d=0;if(d<60)return d+'s ago';if(d<3600)return Math.floor(d/60)+'m ago';return Math.floor(d/3600)+'h'+Math.floor((d%3600)/60)+'m ago'}
function ft(ts){if(!ts)return'—';var d=new Date(ts*1000);return d.toLocaleTimeString()}
</script>
"""

HOME = CSS + """
<meta http-equiv="refresh" content="20">
<div class="c">
<div class="top"><h1>Polymarket Bot</h1><div class="r"><span class="d" style="font-size:.72em">refreshes 20s</span><a href="/" class="btn">↻</a><a href="/import" class="btn btn-p">Import</a><a href="/new" class="btn btn-g">+ New</a></div></div>
{% for m in get_flashed_messages() %}<div class="flash {{ 'flash-w' if m.startswith('⚠') else '' }}">{{ m }}</div>{% endfor %}
<div class="sr">
<div class="st"><div class="sv" style="color:var(--blue)">{{ sessions|length }}</div><div class="sl">Sessions</div></div>
<div class="st"><div class="sv">{{ tt }}</div><div class="sl">Trades</div></div>
<div class="st"><div class="sv {{ 'pg' if tp>=0 else 'pr' }}">${{ tp }}</div><div class="sl">PnL (Taker)</div></div>
<div class="st"><div class="sv {{ 'g' if tw>=60 else 'y' if tw>=50 else 'r' }}">{{ tw }}%</div><div class="sl">Win Rate</div></div>
</div>
{% if sessions %}
<h2>Sessions</h2>
<table>
<tr><th>Name</th><th></th><th>Status</th><th>Trades</th><th>Win%</th><th>PnL</th><th></th></tr>
{% for s in sessions %}
<tr onclick="location='/session/{{s.name}}'" style="cursor:pointer">
<td><strong>{{s.name}}</strong></td>
<td><span class="tag tag-{{s.where[0]}}">{{s.where}}</span></td>
<td><span class="tag tag-{{'a' if s.status in ['running','active'] else 'd'}}">{% if s.status in ['running','active'] %}<span class="dot pulse" style="display:inline-block;width:5px;height:5px;background:var(--green);border-radius:50%;margin-right:3px"></span>{% endif %}{{s.status}}</span></td>
<td>{{s.trades}}</td>
<td class="{{'g' if s.wr>=65 else 'y' if s.wr>=55 else 'r' if s.trades>0 else 'd'}}">{{s.wr}}%</td>
<td class="{{'pg' if s.pnl_taker>0 else 'pr' if s.pnl_taker<0 else 'd'}}" style="font-weight:600">${{s.pnl_taker}}</td>
<td style="text-align:right">
{% if s.status in ['running','active'] %}<a href="/stop/{{s.where}}/{{s.name}}" class="btn btn-s btn-r" onclick="event.stopPropagation();return confirm('Stop?')">Stop</a>
{% else %}<a href="/start/local/{{s.name}}" class="btn btn-s btn-g" onclick="event.stopPropagation()">▶</a>{% endif %}
</td></tr>
{% endfor %}
</table>
{% else %}
<div style="text-align:center;padding:40px;color:var(--dim)"><div style="font-size:2.5em;margin-bottom:8px">📡</div>No sessions<br><a href="/new" class="btn btn-g" style="margin-top:10px">Create one</a></div>
{% endif %}
<h2>Configs</h2>
<div class="cg">
{% for c in configs %}
<div class="card">
<strong>{{c.name}}</strong>{% if c.name=='default' %} <span class="tag tag-a" style="font-size:.6em">DEFAULT</span>{% endif %}
<div class="d" style="font-size:.8em;margin:3px 0 8px">{{c.desc}}</div>
<div style="font-size:.72em;color:var(--dim);display:flex;gap:8px;flex-wrap:wrap">
<span style="background:var(--bg);padding:1px 6px;border-radius:3px">{{(c.data.get('MIN_ENTRY_PRICE',0.2)*100)|int}}-{{(c.data.get('MAX_ENTRY_PRICE',0.8)*100)|int}}¢</span>
<span style="background:var(--bg);padding:1px 6px;border-radius:3px">&lt;{{c.data.get('MAX_IMPULSE_BP','?')}}bp</span>
<span style="background:var(--bg);padding:1px 6px;border-radius:3px">DZ {{c.data.get('DEAD_ZONE_START','?')}}-{{c.data.get('DEAD_ZONE_END','?')}}s</span>
</div>
<div style="display:flex;gap:4px;margin-top:8px">
<a href="/edit/{{c.name}}" class="btn btn-s">Edit</a>
<a href="/clone/{{c.name}}" class="btn btn-s btn-p">Clone</a>
<a href="/start/local/{{c.name}}" class="btn btn-s btn-g">▶ Local</a>
<a href="/start/vps/{{c.name}}" class="btn btn-s btn-b">▶ VPS</a>
{% if c.name!='default' %}<a href="/delete-config/{{c.name}}" class="btn btn-s btn-r" onclick="return confirm('Delete?')">✕</a>{% endif %}
</div></div>
{% endfor %}
</div>
<div class="ar"><span class="dot"></span>20s</div>
</div>
"""

SESSION = CSS + """
<meta http-equiv="refresh" content="15">
<div class="c">
<div class="top">
<h1><a href="/" style="color:var(--dim);-webkit-text-fill-color:var(--dim)">←</a> {{name}} {% if d.get('window_active') %}<span class="tag tag-a">LIVE</span>{% endif %}</h1>
<div class="r"><a href="/logs/{{name}}" class="btn">Logs</a><a href="/edit/{{name}}" class="btn">Config</a><a href="/session/{{name}}" class="btn">↻</a></div>
</div>
{% if d.get('btc_price') %}
<div class="card live" style="margin-bottom:16px">
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px">
<div><div class="ll">BTC</div><div class="lv">${{"${:,.2f}".format(d.btc_price)}}</div></div>
<div><div class="ll">Price To Beat</div><div class="lv">{{("${:,.2f}".format(d.window_open)) if d.window_open else '—'}}</div></div>
<div><div class="ll">PM Book</div><div class="lv"><span class="g">{{"{:.0f}".format((d.book_bid or 0)*100)}}¢</span>/<span class="r">{{"{:.0f}".format((d.book_ask or 0)*100)}}¢</span></div></div>
<div><div class="ll">Window</div><div class="lv">{{d.windows_settled or 0}} settled</div></div>
<div><div class="ll">Updated</div><div class="lv" id="ua">—</div><script>document.getElementById('ua').textContent=ta({{d.updated or 0}})</script></div>
{% if d.get('cooldown_active') %}<div><div class="ll">Status</div><div class="lv y">⏸ COOLDOWN</div></div>{% endif %}
</div></div>
{% endif %}
<div class="sr">
<div class="st"><div class="sv" style="color:var(--blue)">{{d.trades or 0}}</div><div class="sl">Trades</div></div>
<div class="st"><div class="sv {{'g' if (d.wr or 0)>=60 else 'y' if (d.wr or 0)>=50 else 'r'}}">{{d.wr or 0}}%</div><div class="sl">Win Rate</div></div>
<div class="st"><div class="sv {{'pg' if (d.pnl_taker or 0)>=0 else 'pr'}}">${{d.pnl_taker or 0}}</div><div class="sl">PnL (Taker)</div></div>
<div class="st"><div class="sv" style="color:var(--purple)">${{d.pnl_maker or 0}}</div><div class="sl">PnL (Maker)</div></div>
</div>
{% if d.get('recent_windows') %}
<h2>Recent Windows</h2>
<div style="display:flex;flex-wrap:wrap;gap:3px;margin-bottom:12px">
{% for w in d.recent_windows %}<span class="wp w{{w.outcome[0]|lower}}">{{w.outcome}}</span>{% endfor %}
</div>
{% endif %}
{% if d.get('combos') %}
<h2>Combos</h2>
<table>
<tr><th>Combo</th><th>Trades</th><th>Win%</th><th>PnL</th><th>Bank</th><th style="width:100px"></th></tr>
{% for cn,cv in combos %}
<tr><td><strong>{{cn}}</strong></td><td>{{cv.trades}}</td>
<td class="{{'g' if cv.wr>=65 else 'y' if cv.wr>=55 else 'r'}}">{{cv.wr}}%</td>
<td class="{{'pg' if cv.pnl_taker>0 else 'pr'}}" style="font-weight:600">${{cv.pnl_taker|round|int}}</td>
<td class="d">${{cv.bankroll|round|int if cv.bankroll else '—'}}</td>
<td><div style="height:4px;border-radius:2px;background:rgba(248,113,113,.15)"><div style="height:100%;border-radius:2px;width:{{cv.wr}}%;background:{{'var(--green)' if cv.wr>=60 else 'var(--yellow)' if cv.wr>=50 else 'var(--red)'}}"></div></div></td>
</tr>{% endfor %}
</table>
{% endif %}
{% if trades %}
<h2>Trade Log</h2>
<table>
<tr><th>Time</th><th>Combo</th><th>Dir</th><th>Entry</th><th>Size</th><th>Impulse</th><th>T-rem</th><th>Result</th><th>PnL</th></tr>
{% for t in trades %}
<tr>
<td class="d"><script>document.write(ft({{t.ts or 0}}))</script></td>
<td>{{t.combo}}</td>
<td>{{t.dir}}</td>
<td>{{t.entry}}¢</td>
<td>{{t.size}}</td>
<td>{{t.imp}}bp</td>
<td>{{t.tr}}s</td>
<td><span class="tag tag-{{'a' if t.res=='WIN' else 'd'}}">{{t.res or '—'}}</span></td>
<td class="{{'pg' if t.pnl and t.pnl>0 else 'pr' if t.pnl and t.pnl<0 else 'd'}}" style="font-weight:600">{{('$'+t.pnl_s) if t.pnl_s else '—'}}</td>
</tr>{% endfor %}
</table>
{% endif %}
{% if not d.get('trades') and not trades %}
<div style="text-align:center;padding:40px;color:var(--dim)"><div style="font-size:2em;margin-bottom:6px">📊</div>No data yet</div>
{% endif %}
<div class="ar"><span class="dot"></span>15s</div>
</div>
"""

EDIT = CSS + """
<div class="c" style="max-width:600px">
<div class="top"><h1>{{'New Session' if is_new else 'Edit: '+name}}</h1><a href="/" class="btn">Cancel</a></div>
{% for m in get_flashed_messages() %}<div class="flash {{'flash-w' if m.startswith('⚠') else ''}}">{{m}}</div>{% endfor %}
{% if stats and stats.trades > 0 %}
<div class="card" style="margin-bottom:14px"><div style="display:flex;gap:20px;flex-wrap:wrap">
<div><span style="font-size:1.3em;font-weight:700">{{stats.trades}}</span><br><span class="d" style="font-size:.7em">TRADES</span></div>
<div><span style="font-size:1.3em;font-weight:700" class="{{'g' if stats.wr>=60 else 'y' if stats.wr>=50 else 'r'}}">{{stats.wr}}%</span><br><span class="d" style="font-size:.7em">WIN</span></div>
<div><span style="font-size:1.3em;font-weight:700" class="{{'pg' if stats.pnl>=0 else 'pr'}}">${{stats.pnl}}</span><br><span class="d" style="font-size:.7em">PNL</span></div>
</div></div>
{% endif %}
<form method="POST" action="{{'/save/'+name if not is_new else '/create'}}">
{% if is_new %}<div style="margin-bottom:12px"><label class="d" style="font-size:.82em">Name</label><input name="name" required pattern="[a-z0-9-]+" placeholder="my-session"></div>{% endif %}
<div style="margin-bottom:12px"><label class="d" style="font-size:.82em">Description</label><input name="_description" value="{{config.get('_description','')}}"></div>
<hr>
{% for key,meta in knobs.items() %}
<div class="kr"><label>{{meta.desc}} <span class="d" style="font-size:.7em">({{key}})</span></label>
<input type="number" name="{{key}}" value="{{config.get(key,meta.default)}}" step="{{meta.step}}" min="{{meta.min}}" max="{{meta.max}}"></div>
{% endfor %}
<hr>
<div style="margin-bottom:12px"><label class="d" style="font-size:.82em">Raw JSON override (optional)</label><textarea name="raw_json" style="min-height:60px"></textarea></div>
<div style="display:flex;gap:6px;margin-top:12px">
<button type="submit" class="btn btn-g">Save</button>
{% if is_new %}<button type="submit" formaction="/create?start=local" class="btn btn-b">Save & Run</button>{% endif %}
<a href="/" class="btn">Cancel</a>
</div></form></div>
"""


# ══════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    sessions = get_all_sessions()
    configs = []
    for f in sorted(CONFIGS_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            configs.append({"name": f.stem, "desc": d.get("_description", ""), "data": d})
        except Exception:
            configs.append({"name": f.stem, "desc": "error", "data": {}})

    tt = sum(s.get("trades", 0) for s in sessions)
    tw_n = sum(s.get("wins", 0) for s in sessions)
    tp = sum(s.get("pnl_taker", 0) for s in sessions)
    tw = round(tw_n / tt * 100, 1) if tt > 0 else 0
    return render_template_string(HOME, sessions=sessions, configs=configs, tt=tt, tp=round(tp), tw=tw)


@app.route("/session/<name>")
def session_detail(name):
    d = get_session_data(name)
    if d.get("trades", 0) == 0:
        try:
            subprocess.run(["./scripts/pull-data.sh", name], capture_output=True, timeout=15)
            d = get_session_data(name)
        except Exception:
            pass

    combos = sorted(d.get("combos", {}).items(), key=lambda x: x[1].get("pnl_taker", 0), reverse=True)

    # Format trades — ALL timestamps converted client-side via JS
    trades = []
    for t in reversed(d.get("recent_trades", [])[-20:]):
        fp = t.get("fill_price")
        pnl = t.get("pnl_taker")
        trades.append({
            "ts": t.get("timestamp", 0),
            "combo": t.get("combo", "?"),
            "dir": t.get("direction", "?"),
            "entry": "{:.1f}".format(float(fp) * 100) if fp else "?",
            "size": t.get("filled_size", "?"),
            "imp": t.get("impulse_bps", "?"),
            "tr": t.get("time_remaining", "?"),
            "res": t.get("result"),
            "pnl": float(pnl) if pnl else None,
            "pnl_s": str(pnl) if pnl else None,
        })

    return render_template_string(SESSION, name=name, d=d, combos=combos, trades=trades)


@app.route("/new")
def new_session():
    config = json.loads(Path("configs/default.json").read_text()) if Path("configs/default.json").exists() else {}
    return render_template_string(EDIT, name="", config=config, knobs=KNOB_DEFS, is_new=True, stats={})


@app.route("/edit/<name>")
def edit_config(name):
    path = Path("configs/{}.json".format(name))
    config = json.loads(path.read_text()) if path.exists() else {}
    d = get_session_data(name)
    stats = {"trades": d.get("trades", 0), "wr": d.get("wr", 0), "pnl": round(d.get("pnl_taker", 0))}
    return render_template_string(EDIT, name=name, config=config, knobs=KNOB_DEFS, is_new=False, stats=stats)


@app.route("/clone/<name>")
def clone_config(name):
    path = Path("configs/{}.json".format(name))
    config = json.loads(path.read_text()) if path.exists() else {}
    config["_description"] = "Clone of " + name
    return render_template_string(EDIT, name="", config=config, knobs=KNOB_DEFS, is_new=True, stats={})


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
    flash("✓ Created: " + name)
    if request.args.get("start") == "local":
        Path("data/{}".format(name)).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["python3", "-u", "bot/paper_trade_v2.py", "--instance", name],
                         stdout=open("data/{}.log".format(name), "a"), stderr=subprocess.STDOUT, start_new_session=True)
        flash("✓ Started: " + name)
        time.sleep(1)
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
    flash("✓ Saved: " + name)
    return redirect("/")


@app.route("/delete-config/<name>")
def delete_config(name):
    p = Path("configs/{}.json".format(name))
    if p.exists() and name != "default":
        p.unlink(); flash("✓ Deleted: " + name)
    return redirect("/")


@app.route("/import", methods=["GET"])
def import_page():
    return render_template_string(CSS + """
<div class="c" style="max-width:600px">
<div class="top"><h1>Import Config</h1><a href="/" class="btn">Cancel</a></div>
<form method="POST" action="/import" enctype="multipart/form-data">
<div style="margin-bottom:12px"><label class="d">Name</label><input name="name" required pattern="[a-z0-9-]+"></div>
<div style="margin-bottom:12px"><label class="d">Upload JSON</label><input type="file" name="file" accept=".json"></div>
<div style="margin-bottom:12px"><label class="d">Or paste JSON</label><textarea name="json_text"></textarea></div>
<button type="submit" class="btn btn-g">Import</button></form></div>""")


@app.route("/import", methods=["POST"])
def import_config():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name required"); return redirect("/import")
    f = request.files.get("file")
    if f and f.filename:
        config = json.loads(f.read())
    else:
        raw = request.form.get("json_text", "").strip()
        if raw:
            config = json.loads(raw)
        else:
            flash("⚠ No config"); return redirect("/import")
    errors = validate_config(config)
    if errors:
        for e in errors: flash("⚠ " + e)
        return redirect("/import")
    Path("configs/{}.json".format(name)).write_text(json.dumps(config, indent=4))
    flash("✓ Imported: " + name)
    return redirect("/")


@app.route("/start/<where>/<name>")
def start_session(where, name):
    if where == "local":
        Path("data/{}".format(name)).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["python3", "-u", "bot/paper_trade_v2.py", "--instance", name],
                         stdout=open("data/{}.log".format(name), "a"), stderr=subprocess.STDOUT, start_new_session=True)
        flash("✓ Started local: " + name)
    elif where == "vps":
        subprocess.Popen(["./scripts/new-session.sh", name])
        flash("✓ Starting VPS: " + name)
    time.sleep(1)
    return redirect("/")


@app.route("/stop/<where>/<name>")
def stop_session(where, name):
    if where == "local":
        subprocess.run(["pkill", "-INT", "-f", "paper_trade_v2.py.*--instance {}".format(name)])
        flash("✓ Stopping: " + name)
    elif where == "vps":
        subprocess.Popen(["./scripts/stop.sh", name])
        flash("✓ Stopping VPS: " + name)
    time.sleep(2)
    return redirect("/")


@app.route("/logs/<name>")
def view_logs(name):
    # Try VPS logs
    lines = ssh_cmd("tail -80 /var/log/polymarket-bot/{}.log 2>/dev/null || tail -80 /opt/polymarket-bot/data/{}.log 2>/dev/null".format(name, name))
    if not lines.strip():
        # Try local
        for p in [Path("data/{}.log".format(name)), Path("data/{}/bot.log".format(name))]:
            if p.exists():
                lines = p.read_text()[-8000:]  # last ~8KB
                break
    if not lines.strip():
        lines = "No logs found for '{}'".format(name)
    return render_template_string(CSS + """
<div class="c">
<div class="top"><h1><a href="/session/{{name}}" style="color:var(--dim);-webkit-text-fill-color:var(--dim)">← {{name}}</a> / Logs</h1>
<div class="r"><a href="/logs/{{name}}" class="btn">↻ Refresh</a></div></div>
<pre style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;font-size:.78em;overflow-x:auto;white-space:pre-wrap;word-break:break-all;max-height:80vh;overflow-y:auto;line-height:1.5">{{logs}}</pre>
</div>""", name=name, logs=lines)


@app.route("/api/sessions")
def api_sessions():
    return jsonify(get_all_sessions())


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:5555\n")
    app.run(host="0.0.0.0", port=5555, debug=False)
