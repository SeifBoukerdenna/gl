#!/bin/bash
# Create and start a new bot session on VPS
# Usage: ./scripts/new-session.sh SESSION_NAME [--dry-run]
#
# Examples:
#   ./scripts/new-session.sh tight-filters
#   ./scripts/new-session.sh wide-entry
#   ./scripts/new-session.sh overnight-test
set -e
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
RPATH="${VPS_PATH:-/opt/polymarket-bot}"

if [ -z "$1" ] || [ "$1" == "--help" ]; then
    echo "Usage: ./scripts/new-session.sh SESSION_NAME"
    echo ""
    echo "Creates a new bot instance on the VPS with its own:"
    echo "  - systemd service"
    echo "  - log file"
    echo "  - data directory"
    echo ""
    echo "Active sessions:"
    ssh ${USER}@${HOST} "systemctl list-units 'polymarket-bot@*' --no-pager --no-legend 2>/dev/null" || echo "  (none)"
    exit 0
fi

NAME="$1"

echo "=== Creating session: ${NAME} ==="

# 1. Sync latest code
echo "[1/3] Syncing code..."
rsync -az --exclude '.venv' --exclude '__pycache__' --exclude 'historical' --exclude '.git' --exclude 'data' --exclude 'output' \
    ./ ${USER}@${HOST}:${RPATH}/
echo "  OK"

# 2. Create dirs and start
echo "[2/3] Starting instance..."
ssh ${USER}@${HOST} "mkdir -p ${RPATH}/data/${NAME} ${RPATH}/output/${NAME} && systemctl start polymarket-bot@${NAME}"
echo "  OK"

# 3. Tail log
echo "[3/3] Tailing log (Ctrl+C to stop watching)..."
echo ""
ssh ${USER}@${HOST} "timeout 15 tail -f /var/log/polymarket-bot/${NAME}.log" || true

echo ""
echo "=== Session '${NAME}' is running ==="
echo "  Status: ./scripts/status.sh ${NAME}"
echo "  Logs:   ./scripts/logs.sh ${NAME}"
echo "  Stop:   ./scripts/stop.sh ${NAME}"
echo "  Data:   ./scripts/pull-data.sh ${NAME}"
