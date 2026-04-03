#!/bin/bash
# Stop and remove a session
# Usage: ./scripts/kill-session.sh SESSION_NAME
set -e
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"

if [ -z "$1" ]; then
    echo "Usage: ./scripts/kill-session.sh SESSION_NAME"
    exit 1
fi

NAME="$1"

echo "Stopping ${NAME}..."
ssh ${USER}@${HOST} "systemctl stop polymarket-bot@${NAME} 2>/dev/null; systemctl disable polymarket-bot@${NAME} 2>/dev/null; true"
echo "Stopped. Data preserved at /opt/polymarket-bot/data/${NAME}/"
echo ""
echo "Last 10 lines:"
ssh ${USER}@${HOST} "tail -10 /var/log/polymarket-bot/${NAME}.log 2>/dev/null" || echo "(no log)"
