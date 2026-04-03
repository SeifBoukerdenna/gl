#!/bin/bash
# Start a bot session locally
# Usage: ./scripts/local-start.sh [SESSION_NAME]
#
# Examples:
#   ./scripts/local-start.sh                  # default session
#   ./scripts/local-start.sh tight-filters    # named session
set -e

SESSION="${1:-default}"

echo "Starting local session: ${SESSION}"
echo "Config: configs/${SESSION}.json"
echo "Data:   data/${SESSION}/"
echo "Ctrl+C to stop (runs final analysis)"
echo ""

python3 -u bot/paper_trade_v2.py --instance ${SESSION}
