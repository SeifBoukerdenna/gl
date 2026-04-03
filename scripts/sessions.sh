#!/bin/bash
# Show real-time status of ALL running sessions
# Usage: ./scripts/sessions.sh
set -e
source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
RPATH="${VPS_PATH:-/opt/polymarket-bot}"

ssh ${USER}@${HOST} bash << 'REMOTE'
echo ""
echo "  ══════════════════════════════════════════════════════════════"
echo "  POLYMARKET BOT — ALL SESSIONS"
echo "  ══════════════════════════════════════════════════════════════"
echo ""

# Find all running instances
INSTANCES=$(systemctl list-units 'polymarket-bot@*' --no-pager --no-legend 2>/dev/null | awk '{print $1}' | sed 's/polymarket-bot@//;s/\.service//')

if [ -z "$INSTANCES" ]; then
    echo "  No sessions running."
    echo ""
    exit 0
fi

printf "  %-15s %-8s %-7s %-7s %-10s %-10s %s\n" "SESSION" "STATUS" "TRADES" "WIN%" "PNL(tk)" "UPTIME" "LAST"
echo "  ─────────────────────────────────────────────────────────────────────────"

for INST in $INSTANCES; do
    # Status
    STATUS=$(systemctl is-active polymarket-bot@${INST} 2>/dev/null || echo "dead")

    # Trade stats from CSV
    CSV="/opt/polymarket-bot/data/${INST}/paper_trades_v2.csv"
    if [ ! -f "$CSV" ]; then
        CSV="/opt/polymarket-bot/data/paper_trades_v2.csv"
    fi

    if [ -f "$CSV" ]; then
        TRADES=$(tail -n +2 "$CSV" | wc -l | tr -d ' ')
        if [ "$TRADES" -gt 0 ]; then
            WINS=$(tail -n +2 "$CSV" | awk -F',' '{print $NF}' | grep -c "WIN" || echo 0)
            WR=$(echo "scale=1; $WINS * 100 / $TRADES" | bc 2>/dev/null || echo "?")
            # Sum PnL (column 22 = pnl_taker)
            PNL=$(tail -n +2 "$CSV" | awk -F',' '{s+=$22} END {printf "%.0f", s}' 2>/dev/null || echo "?")
        else
            WINS=0; WR="--"; PNL="--"
        fi
    else
        TRADES="--"; WINS=0; WR="--"; PNL="--"
    fi

    # Uptime
    UPTIME=$(systemctl show polymarket-bot@${INST} --property=ActiveEnterTimestamp --value 2>/dev/null | xargs -I{} date -d "{}" +%s 2>/dev/null)
    if [ -n "$UPTIME" ]; then
        NOW=$(date +%s)
        DIFF=$((NOW - UPTIME))
        HOURS=$((DIFF / 3600))
        MINS=$(( (DIFF % 3600) / 60 ))
        UP="${HOURS}h${MINS}m"
    else
        UP="--"
    fi

    # Last log line timestamp
    LOG="/var/log/polymarket-bot/${INST}.log"
    if [ -f "$LOG" ]; then
        LAST=$(tail -1 "$LOG" | grep -oP '^\s*\d{2}:\d{2}:\d{2}' | head -1 || echo "--")
    else
        LAST="--"
    fi

    # Color status
    if [ "$STATUS" = "active" ]; then
        S="\033[32m${STATUS}\033[0m"
    else
        S="\033[31m${STATUS}\033[0m"
    fi

    # Color PnL
    if [ "$PNL" != "--" ] && [ "$PNL" -gt 0 ] 2>/dev/null; then
        P="\033[32m\$${PNL}\033[0m"
    elif [ "$PNL" != "--" ]; then
        P="\033[31m\$${PNL}\033[0m"
    else
        P="--"
    fi

    printf "  %-15s %-18b %-7s %-7s %-20b %-10s %s\n" "$INST" "$S" "$TRADES" "${WR}%" "$P" "$UP" "$LAST"
done

echo ""
echo "  Memory: $(free -m | awk 'NR==2{printf "%dMB / %dMB (%.0f%%)", $3, $2, $3/$2*100}')"
echo "  ══════════════════════════════════════════════════════════════"
echo ""
REMOTE
