#!/bin/bash
# Pull trade data from VPS
# Usage: ./scripts/pull-data.sh              (pull all sessions)
#        ./scripts/pull-data.sh SESSION_NAME  (pull one session)
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
RPATH="${VPS_PATH:-/opt/polymarket-bot}"

echo "Pulling data from ${HOST}..."

if [ -n "$1" ]; then
    # Single session
    mkdir -p data/$1
    rsync -avz ${USER}@${HOST}:${RPATH}/data/$1/ ./data/$1/
else
    # All data — exclude archives, sync CSVs + stats.json, delete local dirs not on VPS
    rsync -avz --include='*/' --include='*.csv' --include='*.json' --exclude='_archive*' --exclude='*.parquet' --exclude='*.log' --exclude='.gitkeep' ${USER}@${HOST}:${RPATH}/data/ ./data/ --delete
fi

echo ""
echo "=== Local data ==="
find data -name "*.csv" -exec sh -c 'echo "  $(wc -l < "$1") lines  $1"' _ {} \;
