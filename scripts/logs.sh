#!/bin/bash
# Tail bot logs
# Usage: ./scripts/logs.sh [INSTANCE]
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
INSTANCE="${1:-default}"

ssh ${USER}@${HOST} "tail -f /var/log/polymarket-bot/${INSTANCE}.log 2>/dev/null || tail -f /root/bot/bot_v2.log"
