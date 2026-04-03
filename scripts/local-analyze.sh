#!/bin/bash
# Analyze local session data (no VPS pull needed)
# Usage: ./scripts/local-analyze.sh                  # default session
#        ./scripts/local-analyze.sh SESSION_NAME      # specific session
#        ./scripts/local-analyze.sh --all             # all local sessions
set -e

SESSION="${1:-default}"

if [ "$SESSION" = "--all" ]; then
    ANALYZE_ALL=1 python3 analysis/analyze_paper.py

    # Open comparison charts on macOS
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo ""
        echo "Opening comparison charts..."
        open output/comparison/*.png 2>/dev/null || true
    fi
else
    if [ "$SESSION" = "default" ]; then
        CSV="data/paper_trades_v2.csv"
    else
        # Try new naming first, then old
        if [ -f "data/${SESSION}/trades.csv" ]; then
            CSV="data/${SESSION}/trades.csv"
        else
            CSV="data/${SESSION}/paper_trades_v2.csv"
        fi
    fi

    if [ ! -f "$CSV" ]; then
        echo "No data at $CSV"
        exit 1
    fi

    ANALYZE_CSV="$CSV" ANALYZE_SESSION="$SESSION" python3 analysis/analyze_paper.py

    if [[ "$OSTYPE" == "darwin"* ]]; then
        open "output/${SESSION}/cum_pnl.png" 2>/dev/null || true
    fi
fi
