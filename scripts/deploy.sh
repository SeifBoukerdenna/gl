#!/bin/bash
# Deploy latest code to VPS and restart bot.
# Usage: ./scripts/deploy.sh [--instance NAME] [--message "commit msg"]
set -e

# Load config
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
RPATH="${VPS_PATH:-/opt/polymarket-bot}"
INSTANCE="${1:-default}"
MSG="${2:-deploy: $(date '+%Y-%m-%d %H:%M')}"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --instance) INSTANCE="$2"; shift 2;;
        --message) MSG="$2"; shift 2;;
        *) shift;;
    esac
done

echo "=== Deploy to ${USER}@${HOST} (instance: ${INSTANCE}) ==="

# 1. Syntax check
echo "[1/6] Syntax check..."
python3 -m py_compile bot/paper_trade_v2.py
echo "  OK"

# 2. Git commit
echo "[2/6] Git commit..."
git add -A
git diff --cached --quiet && echo "  Nothing to commit" || {
    git commit -m "$MSG"
    echo "  Committed"
}

# 3. Push if remote exists
echo "[3/6] Push..."
git remote -v 2>/dev/null | grep -q origin && git push origin main 2>/dev/null && echo "  Pushed" || echo "  No remote or push failed — skipping"

# 4. Sync to VPS
echo "[4/6] Syncing files..."
rsync -az --exclude '.venv' --exclude '__pycache__' --exclude 'data' --exclude 'output' --exclude 'historical' --exclude '.git' --exclude '.env' \
    ./ ${USER}@${HOST}:${RPATH}/
echo "  Synced"

# 5. Restart bot
echo "[5/6] Restarting bot (instance: ${INSTANCE})..."
ssh ${USER}@${HOST} "cd ${RPATH} && .venv/bin/pip install -q -r requirements.txt && systemctl restart polymarket-bot@${INSTANCE}"
echo "  Restarted"

# 6. Tail log
echo "[6/6] Tailing log for 15 seconds..."
echo "  (Ctrl+C to stop watching — bot keeps running)"
ssh ${USER}@${HOST} "timeout 15 tail -f /var/log/polymarket-bot/${INSTANCE}.log" || true

echo ""
echo "=== Deploy complete ==="
echo "  Status: ssh ${USER}@${HOST} systemctl status polymarket-bot@${INSTANCE}"
echo "  Logs:   ssh ${USER}@${HOST} tail -f /var/log/polymarket-bot/${INSTANCE}.log"
