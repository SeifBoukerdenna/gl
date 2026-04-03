#!/bin/bash
# Pull data and run analysis
# Usage: ./scripts/analyze.sh              (analyze default session)
#        ./scripts/analyze.sh SESSION_NAME  (analyze specific session)
#        ./scripts/analyze.sh --all         (analyze all sessions)
set -e

SESSION="${1:-default}"

if [ "$SESSION" = "--all" ]; then
    # Pull all data first
    ./scripts/pull-data.sh

    # Run unified analysis (handles all sessions + comparison)
    ANALYZE_ALL=1 python3 analysis/analyze_paper.py

    # Open comparison charts on macOS
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo ""
        echo "Opening comparison charts..."
        open output/comparison/*.png 2>/dev/null || true
    fi
else
    # Pull specific session
    ./scripts/pull-data.sh ${SESSION}

    # Find the CSV
    if [ "$SESSION" = "default" ]; then
        CSV="data/paper_trades_v2.csv"
    else
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
