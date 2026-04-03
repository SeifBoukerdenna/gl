#!/bin/bash
# Pull data from VPS and run analysis locally
# Usage: ./scripts/analyze.sh [INSTANCE]
set -e

INSTANCE="${1:-default}"

echo "=== Pull + Analyze ==="

# 1. Pull data
./scripts/pull-data.sh ${INSTANCE}

# 2. Run analysis
echo ""
echo "Running analysis..."
python3 analysis/analyze_paper.py

# 3. Open charts if on macOS
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo ""
    echo "Opening charts..."
    open output/paper_cum_pnl.png 2>/dev/null || true
    open output/paper_entry_analysis.png 2>/dev/null || true
    open output/paper_time_analysis.png 2>/dev/null || true
fi
