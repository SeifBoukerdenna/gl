#!/bin/bash
# Gracefully stop the bot (SIGINT → analysis → exit)
# Usage: ./scripts/stop.sh [INSTANCE]
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
INSTANCE="${1:-default}"

echo "Stopping polymarket-bot@${INSTANCE}..."
ssh ${USER}@${HOST} "systemctl stop polymarket-bot@${INSTANCE}"
echo "Stopped. Last 30 lines of log:"
ssh ${USER}@${HOST} "tail -30 /var/log/polymarket-bot/${INSTANCE}.log"
