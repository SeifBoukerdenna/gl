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

    # Find all session CSVs
    echo ""
    echo "=== Analyzing all sessions ==="
    for csv in data/*/paper_trades_v2.csv data/paper_trades_v2.csv; do
        if [ -f "$csv" ]; then
            NAME=$(dirname "$csv" | sed 's|data/||')
            [ "$NAME" = "data" ] && NAME="default"
            echo ""
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            echo "  SESSION: $NAME"
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ANALYZE_CSV="$csv" python3 analysis/analyze_paper.py
        fi
    done
else
    # Pull specific session
    ./scripts/pull-data.sh ${SESSION}

    # Point analysis at right CSV
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
    echo ""
    echo "Opening charts..."
    open output/paper_cum_pnl.png 2>/dev/null || true
fi
