#!/bin/bash
# Check bot status and show recent output
# Usage: ./scripts/status.sh [INSTANCE]
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
INSTANCE="${1:-default}"

echo "=== Bot Status (${INSTANCE}) ==="
ssh ${USER}@${HOST} "systemctl status polymarket-bot@${INSTANCE} --no-pager 2>/dev/null || echo 'Not running as systemd service'"
echo ""
echo "=== Last 20 lines ==="
ssh ${USER}@${HOST} "tail -20 /var/log/polymarket-bot/${INSTANCE}.log 2>/dev/null || tail -20 /root/bot/bot_v2.log 2>/dev/null || echo 'No log found'"
echo ""
echo "=== Trade count ==="
ssh ${USER}@${HOST} "wc -l /opt/polymarket-bot/data/paper_trades_v2.csv 2>/dev/null || wc -l /root/bot/data/paper_trades_v2.csv 2>/dev/null || echo 'No trades file'"
