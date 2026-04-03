#!/bin/bash
# Pull trade data and outputs from VPS for local analysis
# Usage: ./scripts/pull-data.sh [INSTANCE]
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
RPATH="${VPS_PATH:-/opt/polymarket-bot}"
INSTANCE="${1:-default}"

echo "Pulling data from ${USER}@${HOST}..."

# Try new path first, fall back to old ~/bot/ path
rsync -avz ${USER}@${HOST}:${RPATH}/data/ ./data/ 2>/dev/null || \
rsync -avz ${USER}@${HOST}:/root/bot/data/ ./data/ 2>/dev/null || \
echo "No data found at either path"

echo ""
echo "Files pulled:"
ls -lh data/*.csv 2>/dev/null || echo "No CSVs"
