---
name: Operational Playbook
description: How to actually do things in this project — sync data, deploy, nuke sessions, run analysis, check status. Concrete commands for common operations.
type: reference
---

## Environment

- **VPS:** `root@167.172.50.38:/opt/polymarket-bot`
- **Local dev:** `/Users/sboukerdenna/perso/gl`
- **VPS timezone:** UTC (confirmed via `timedatectl` — this is the source of the overnight skip bug that was fixed)
- **Dashboard URL:** `https://learned-restricted-manufacturer-accommodation.trycloudflare.com` (auth: admin / changeme123)
- **Python env on VPS:** `/opt/polymarket-bot/.venv/bin/python`

## Common operations

### Sync data from VPS to local
```bash
rsync -az -e 'ssh -o ConnectTimeout=30' root@167.172.50.38:/opt/polymarket-bot/data/ data/
```
For clean sync (remove local files deleted on VPS):
```bash
rsync -az --delete -e 'ssh -o ConnectTimeout=30' root@167.172.50.38:/opt/polymarket-bot/data/<session>/ data/<session>/
```

### Check session status
```bash
ssh root@167.172.50.38 'systemctl list-units "polymarket-bot@*" --type=service --no-legend --no-pager'
```

### Start / stop / restart a session
```bash
ssh root@167.172.50.38 'systemctl start polymarket-bot@<session>'
ssh root@167.172.50.38 'systemctl stop polymarket-bot@<session>'
ssh root@167.172.50.38 'systemctl restart polymarket-bot@<session>'
```

### Nuke a session (delete trade history, keep running)
```bash
ssh root@167.172.50.38 '
  systemctl stop polymarket-bot@<session>
  rm -f /opt/polymarket-bot/data/<session>/trades.csv /opt/polymarket-bot/data/<session>/skipped.csv /opt/polymarket-bot/data/<session>/windows.csv /opt/polymarket-bot/data/<session>/stats.json
  systemctl start polymarket-bot@<session>
'
```
Also nuke local:
```bash
rm -rf data/<session>
mkdir data/<session>
```

### Kill a session entirely (archive to _killed_*)
```bash
ssh root@167.172.50.38 '
  systemctl stop polymarket-bot@<session>
  systemctl disable polymarket-bot@<session> 2>/dev/null
  mv /opt/polymarket-bot/data/<session> /opt/polymarket-bot/data/_killed_<session>_$(date +%s)
  mv /opt/polymarket-bot/output/<session> /opt/polymarket-bot/output/_killed_<session>_$(date +%s)
'
# Also archive locally
mv data/<session> "data/_killed_<session>_$(date +%s)"
```
The dashboard auto-skips directories starting with `_`, so no UI change needed.

### Deploy a new/modified architecture
```bash
# 1. Edit bot/architectures/<name>.py locally
# 2. Smoke test the import
python -c "from bot.architectures import load_architecture; s = load_architecture('<name>'); print('OK')"

# 3. Create/update configs/<session>.json with the architecture name
# 4. Sync to VPS
rsync -az bot/architectures/<name>.py root@167.172.50.38:/opt/polymarket-bot/bot/architectures/
rsync -az configs/<session>.json root@167.172.50.38:/opt/polymarket-bot/configs/

# 5. Create data and output dirs, start the service
ssh root@167.172.50.38 '
  mkdir -p /opt/polymarket-bot/data/<session> /opt/polymarket-bot/output/<session>
  systemctl start polymarket-bot@<session>
'

# 6. Verify (wait a few seconds for stats.json to populate)
ssh root@167.172.50.38 'sleep 10 && cat /opt/polymarket-bot/data/<session>/stats.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(\"active\" if d[\"window_active\"] else \"idle\")"'
```

### Deploy engine changes (paper_trade_v2.py)
This affects ALL sessions. Always smoke-test architecture imports afterwards and consider nuking + restarting affected sessions for clean data.
```bash
python -c "import bot.paper_trade_v2; print('engine imports OK')"
rsync -az bot/paper_trade_v2.py root@167.172.50.38:/opt/polymarket-bot/bot/
# Restart affected sessions
ssh root@167.172.50.38 'systemctl restart polymarket-bot@<session1> polymarket-bot@<session2>'
```

### Deploy dashboard changes
```bash
./scripts/deploy-dashboard.sh
```
This updates dashboard.py on VPS, restarts the `polymarket-dashboard` systemd unit, and sets up the Cloudflare tunnel. Bot sessions are NOT touched.

### Foreground smoke test (CRITICAL after any architecture change)
This is the ONLY way to catch silent failures (like the halt dict bug or the overnight UTC bug). `systemctl is-active` only tells you the process didn't crash — it doesn't tell you if `check_signals` is silently erroring.
```bash
ssh root@167.172.50.38 '
  systemctl stop polymarket-bot@<session>
  sleep 1
  cd /opt/polymarket-bot
  timeout 90 /opt/polymarket-bot/.venv/bin/python -u bot/paper_trade_v2.py --instance <session> 2>&1 | tail -40
  systemctl start polymarket-bot@<session>
'
```
Look for:
- Startup banner prints (edge table loaded, filters summary, etc.)
- Status prints every 12s (if nothing prints, check_signals is silently erroring)
- Actual FIRE or QUEUE messages if conditions are met

### Extend kline data
```bash
python analysis/extend_klines.py
```
Fetches from the last ts in the parquet to now. Incremental. ~30 sec per hour of data to fetch.

## Diagnostic commands

### Check VPS load and memory
```bash
ssh root@167.172.50.38 'uptime; free -m | head -2'
```
Healthy: load avg ~4-6 with ~12 sessions. Alarm: load > 7 or mem > 90%.

### See recent trades for a session
```bash
python -c "
import csv
trades = list(csv.DictReader(open('data/<session>/trades.csv')))[-10:]
for t in trades:
    print(t['timestamp'], t['direction'], t['fill_price'], t['pnl_taker'], t['result'])
"
```

### Journalctl for a session
```bash
ssh root@167.172.50.38 'journalctl -u polymarket-bot@<session> -n 50 --no-pager'
```
**Warning:** journalctl only captures STDOUT if the process prints. For architecture sessions, most output goes to `bot/paper_trade_v2.py`'s stdout which may or may not be captured. If journalctl is empty, do a foreground smoke test instead.

### Count trades per session
```bash
for s in data/*/; do
  name=$(basename "$s")
  if [[ ! "$name" == _* ]] && [ -f "$s/trades.csv" ]; then
    n=$(($(wc -l < "$s/trades.csv") - 1))
    echo "$name: $n"
  fi
done
```

## Current tunnel URL
`https://learned-restricted-manufacturer-accommodation.trycloudflare.com`
Auth: `admin / changeme123`

If the URL changes (tunnel rotates), find the new one:
```bash
ssh root@167.172.50.38 'grep trycloudflare /var/log/polymarket-bot/tunnel.log | tail -1'
```

## Safety rules

1. **Never nuke sessions mid-analysis** — sync data first if you need historical trades
2. **Always smoke-test architecture imports locally** before syncing to VPS (`load_architecture('<name>')`)
3. **Don't deploy engine changes during live-traded market hours** (if we had them) — affects all 14 sessions simultaneously
4. **Check VPS load before adding new sessions** — 14 is approaching the ceiling on 2 cores
5. **Don't use destructive git operations without asking** — user has been burned by this before
6. **Kill sessions by archiving (mv to `_killed_*`), not deleting** — preserves forensic data for later analysis
