#!/bin/bash
# Analyze local session data (no VPS pull needed)
# Usage: ./scripts/local-analyze.sh                  # default session
#        ./scripts/local-analyze.sh SESSION_NAME      # specific session
#        ./scripts/local-analyze.sh --all             # all local sessions
set -e

SESSION="${1:-default}"

if [ "$SESSION" = "--all" ]; then
    for csv in data/*/paper_trades_v2.csv data/paper_trades_v2.csv; do
        if [ -f "$csv" ]; then
            NAME=$(dirname "$csv" | sed 's|data/||')
            [ "$NAME" = "data" ] && NAME="default"
            LINES=$(tail -n +2 "$csv" 2>/dev/null | wc -l | tr -d ' ')
            if [ "$LINES" -gt 0 ]; then
                echo ""
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                echo "  SESSION: $NAME ($LINES trades)"
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                ANALYZE_CSV="$csv" python3 analysis/analyze_paper.py
            fi
        fi
    done
else
    if [ "$SESSION" = "default" ]; then
        CSV="data/paper_trades_v2.csv"
    else
        CSV="data/${SESSION}/paper_trades_v2.csv"
    fi

    if [ ! -f "$CSV" ]; then
        echo "No data at $CSV"
        exit 1
    fi

    ANALYZE_CSV="$CSV" python3 analysis/analyze_paper.py
fi

# Open charts on macOS
if [[ "$OSTYPE" == "darwin"* ]]; then
    open output/paper_cum_pnl.png 2>/dev/null || true
fi
