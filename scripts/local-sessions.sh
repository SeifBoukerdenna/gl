#!/bin/bash
# Show status of all LOCAL running bot sessions
# Usage: ./scripts/local-sessions.sh
echo ""
echo "  ══════════════════════════════════════════════════════════════"
echo "  LOCAL SESSIONS"
echo "  ══════════════════════════════════════════════════════════════"
echo ""

PIDS=$(pgrep -f "paper_trade_v2.py" 2>/dev/null)

if [ -z "$PIDS" ]; then
    echo "  No local sessions running."
    echo ""
    echo "  Start one: ./scripts/local-start.sh SESSION_NAME"
    echo ""
    exit 0
fi

printf "  %-15s %-8s %-7s %-7s %-10s %s\n" "SESSION" "PID" "TRADES" "WIN%" "PNL(tk)" "CSV"
echo "  ─────────────────────────────────────────────────────────────────────"

for PID in $PIDS; do
    # Extract instance name from command line
    CMD=$(ps -p $PID -o args= 2>/dev/null)
    INST=$(echo "$CMD" | grep -oP '(?<=--instance )\S+' || echo "default")

    # Find CSV
    if [ "$INST" = "default" ]; then
        CSV="data/paper_trades_v2.csv"
    else
        CSV="data/${INST}/paper_trades_v2.csv"
    fi

    if [ -f "$CSV" ]; then
        TRADES=$(tail -n +2 "$CSV" 2>/dev/null | wc -l | tr -d ' ')
        if [ "$TRADES" -gt 0 ]; then
            WINS=$(tail -n +2 "$CSV" | awk -F',' '{print $NF}' | grep -c "WIN" 2>/dev/null || echo 0)
            WR=$(echo "scale=1; $WINS * 100 / $TRADES" | bc 2>/dev/null || echo "?")
            PNL=$(tail -n +2 "$CSV" | awk -F',' '{s+=$22} END {printf "%.0f", s}' 2>/dev/null || echo "?")
        else
            WR="--"; PNL="--"
        fi
    else
        TRADES="--"; WR="--"; PNL="--"
    fi

    printf "  %-15s %-8s %-7s %-7s %-10s %s\n" "$INST" "$PID" "$TRADES" "${WR}%" "\$${PNL}" "$CSV"
done

echo ""
echo "  Stop a session: kill -INT PID"
echo "  ══════════════════════════════════════════════════════════════"
echo ""
