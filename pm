#!/bin/bash
# Polymarket Bot CLI — one command for everything
# Usage: pm [command] [args]
#
# Saves typing ./scripts/... for every operation.

set -e
cd "$(dirname "$0")"

# Colors
G="\033[32m"; R="\033[31m"; Y="\033[33m"; C="\033[36m"
M="\033[35m"; W="\033[97m"; DIM="\033[2m"; B="\033[1m"; RST="\033[0m"

CMD="${1:-help}"
shift 2>/dev/null || true

case "$CMD" in

# ── Dashboard ─────────────────────────────────────────────
dash|d|dashboard)
    clear
    echo -e "${B}${M}  ╔══════════════════════════════════════════════════╗${RST}"
    echo -e "${B}${M}  ║         POLYMARKET BOT DASHBOARD                ║${RST}"
    echo -e "${B}${M}  ╚══════════════════════════════════════════════════╝${RST}"
    echo ""

    # VPS sessions
    source .env 2>/dev/null || true
    HOST="${VPS_HOST:-167.172.50.38}"
    USER="${VPS_USER:-root}"

    echo -e "  ${B}${W}VPS Sessions${RST} ${DIM}(${HOST})${RST}"
    echo -e "  ${DIM}────────────────────────────────────────────────${RST}"
    ssh -q -o ConnectTimeout=3 ${USER}@${HOST} bash << 'REMOTE' 2>/dev/null || echo -e "  ${R}VPS unreachable${RST}"
INSTANCES=$(systemctl list-units 'polymarket-bot@*' --no-pager --no-legend 2>/dev/null | awk '{print $1}' | sed 's/polymarket-bot@//;s/\.service//')
if [ -z "$INSTANCES" ]; then
    echo "  (none running)"
else
    printf "  %-14s %-7s %-6s %-6s %-9s %s\n" "NAME" "STATUS" "TRADES" "WIN%" "PNL" "UPTIME"
    for INST in $INSTANCES; do
        ST=$(systemctl is-active polymarket-bot@${INST} 2>/dev/null || echo "dead")
        CSV="/opt/polymarket-bot/data/${INST}/paper_trades_v2.csv"
        [ ! -f "$CSV" ] && CSV="/opt/polymarket-bot/data/paper_trades_v2.csv"
        if [ -f "$CSV" ]; then
            N=$(tail -n +2 "$CSV" 2>/dev/null | wc -l | tr -d ' ')
            W=$(tail -n +2 "$CSV" 2>/dev/null | awk -F',' '{print $NF}' | grep -c WIN || echo 0)
            [ "$N" -gt 0 ] && WR=$(echo "scale=0; $W * 100 / $N" | bc) || WR="--"
            PNL=$(tail -n +2 "$CSV" 2>/dev/null | awk -F',' '{s+=$22} END {printf "%.0f", s}' || echo "?")
        else
            N="--"; WR="--"; PNL="--"
        fi
        UP=$(systemctl show polymarket-bot@${INST} --property=ActiveEnterTimestamp --value 2>/dev/null)
        if [ -n "$UP" ]; then
            SECS=$(($(date +%s) - $(date -d "$UP" +%s 2>/dev/null || echo $(date +%s))))
            UPT="$((SECS/3600))h$((SECS%3600/60))m"
        else
            UPT="--"
        fi
        [ "$ST" = "active" ] && SC="\033[32m" || SC="\033[31m"
        [ "$PNL" != "--" ] && [ "$PNL" -gt 0 ] 2>/dev/null && PC="\033[32m" || PC="\033[31m"
        printf "  %-14s ${SC}%-7s\033[0m %-6s %-6s ${PC}\$%-8s\033[0m %s\n" "$INST" "$ST" "$N" "${WR}%" "$PNL" "$UPT"
    done
fi
echo "  Mem: $(free -m | awk 'NR==2{printf "%dMB/%dMB", $3, $2}')"
REMOTE

    echo ""

    # Local sessions
    echo -e "  ${B}${W}Local Sessions${RST}"
    echo -e "  ${DIM}────────────────────────────────────────────────${RST}"
    PIDS=$(pgrep -f "paper_trade_v2.py" 2>/dev/null || true)
    if [ -z "$PIDS" ]; then
        echo "  (none running)"
    else
        printf "  %-14s %-7s %-6s %-6s %-9s\n" "NAME" "PID" "TRADES" "WIN%" "PNL"
        for PID in $PIDS; do
            CMD_LINE=$(ps -p $PID -o args= 2>/dev/null || true)
            INST=$(echo "$CMD_LINE" | grep -oP '(?<=--instance )\S+' 2>/dev/null || echo "default")
            [ "$INST" = "default" ] && CSV="data/paper_trades_v2.csv" || CSV="data/${INST}/paper_trades_v2.csv"
            if [ -f "$CSV" ]; then
                N=$(tail -n +2 "$CSV" 2>/dev/null | wc -l | tr -d ' ')
                W=$(tail -n +2 "$CSV" 2>/dev/null | awk -F',' '{print $NF}' | grep -c WIN 2>/dev/null || echo 0)
                [ "$N" -gt 0 ] && WR=$(echo "scale=0; $W * 100 / $N" | bc 2>/dev/null) || WR="--"
                PNL=$(tail -n +2 "$CSV" 2>/dev/null | awk -F',' '{s+=$22} END {printf "%.0f", s}' 2>/dev/null || echo "?")
            else
                N="--"; WR="--"; PNL="--"
            fi
            printf "  %-14s %-7s %-6s %-6s \$%-8s\n" "$INST" "$PID" "$N" "${WR}%" "$PNL"
        done
    fi

    echo ""
    echo -e "  ${B}${W}Configs${RST}"
    echo -e "  ${DIM}────────────────────────────────────────────────${RST}"
    for f in configs/*.json; do
        NAME=$(basename "$f" .json)
        DESC=$(python3 -c "import json; print(json.load(open('$f')).get('_description',''))" 2>/dev/null || echo "")
        echo -e "  ${C}${NAME}${RST}  ${DIM}${DESC}${RST}"
    done
    echo ""
    ;;

# ── Start ─────────────────────────────────────────────────
start|s)
    NAME="${1:-default}"
    WHERE="${2:-local}"
    if [ "$WHERE" = "vps" ] || [ "$WHERE" = "remote" ]; then
        ./scripts/new-session.sh "$NAME"
    else
        echo -e "${G}Starting local: ${NAME}${RST}"
        ./scripts/local-start.sh "$NAME"
    fi
    ;;

# ── Stop ──────────────────────────────────────────────────
stop|x)
    NAME="${1:-default}"
    WHERE="${2:-}"
    if [ "$WHERE" = "vps" ] || [ "$WHERE" = "remote" ]; then
        ./scripts/stop.sh "$NAME"
    else
        # Try local first
        PID=$(pgrep -f "paper_trade_v2.py.*--instance ${NAME}" 2>/dev/null || pgrep -f "paper_trade_v2.py" 2>/dev/null | head -1)
        if [ -n "$PID" ]; then
            echo "Stopping local PID $PID (${NAME})..."
            kill -INT $PID
            echo "Sent SIGINT. Waiting for graceful shutdown..."
        else
            echo "No local session '${NAME}'. Trying VPS..."
            ./scripts/stop.sh "$NAME"
        fi
    fi
    ;;

# ── Logs ──────────────────────────────────────────────────
logs|l)
    NAME="${1:-default}"
    # Check if running locally first
    if pgrep -qf "paper_trade_v2.py.*--instance ${NAME}" 2>/dev/null; then
        echo -e "${G}Tailing local: ${NAME}${RST}"
        tail -f "data/${NAME}.log"
    else
        echo -e "${C}Tailing VPS: ${NAME}${RST}"
        source .env 2>/dev/null || true
        ssh ${VPS_USER:-root}@${VPS_HOST:-167.172.50.38} "tail -f /var/log/polymarket-bot/${NAME}.log"
    fi
    ;;

# ── Status ────────────────────────────────────────────────
status|st)
    NAME="${1:-default}"
    ./scripts/status.sh "$NAME"
    ;;

# ── Deploy ────────────────────────────────────────────────
deploy|dp)
    ./scripts/deploy.sh "$@"
    ;;

# ── Analyze ───────────────────────────────────────────────
analyze|a)
    SESSION="${1:---all}"
    if [ "$SESSION" = "--all" ] || [ "$SESSION" = "all" ]; then
        ./scripts/local-analyze.sh --all
    else
        ./scripts/local-analyze.sh "$SESSION"
    fi
    ;;

# ── Analyze VPS data ──────────────────────────────────────
pull|p)
    ./scripts/pull-data.sh "$@"
    ;;

analyze-vps|av)
    SESSION="${1:---all}"
    if [ "$SESSION" = "--all" ] || [ "$SESSION" = "all" ]; then
        ./scripts/analyze.sh --all
    else
        ./scripts/analyze.sh "$SESSION"
    fi
    ;;

# ── New config ────────────────────────────────────────────
config|cfg)
    NAME="${1}"
    if [ -z "$NAME" ]; then
        echo "Configs:"
        for f in configs/*.json; do
            echo "  $(basename $f .json)"
        done
        echo ""
        echo "Usage: pm config NAME          # view config"
        echo "       pm config NAME --edit   # edit config"
        echo "       pm config NAME --new    # create from default"
        exit 0
    fi
    if [ "$2" = "--new" ]; then
        cp configs/default.json "configs/${NAME}.json"
        echo "Created configs/${NAME}.json"
        ${EDITOR:-vim} "configs/${NAME}.json"
    elif [ "$2" = "--edit" ]; then
        ${EDITOR:-vim} "configs/${NAME}.json"
    else
        cat "configs/${NAME}.json" 2>/dev/null || echo "No config: configs/${NAME}.json"
    fi
    ;;

# ── Web UI ────────────────────────────────────────────────
ui|web)
    pkill -f "dashboard.py" 2>/dev/null; sleep 0.5
    echo -e "${G}Opening dashboard at http://localhost:5555${RST}"
    python3 dashboard.py &
    sleep 1
    open http://localhost:5555 2>/dev/null || xdg-open http://localhost:5555 2>/dev/null || echo "Open http://localhost:5555"
    wait
    ;;

# ── Kill session ──────────────────────────────────────────
kill|k)
    NAME="${1}"
    if [ -z "$NAME" ]; then
        echo "Usage: pm kill SESSION_NAME"
        exit 1
    fi
    ./scripts/kill-session.sh "$NAME"
    ;;

# ── SSH to VPS ────────────────────────────────────────────
ssh)
    source .env 2>/dev/null || true
    ssh ${VPS_USER:-root}@${VPS_HOST:-167.172.50.38}
    ;;

# ── Help ──────────────────────────────────────────────────
help|h|--help|-h|"")
    echo ""
    echo -e "  ${B}pm${RST} — Polymarket Bot CLI"
    echo ""
    echo -e "  ${B}${W}Dashboard${RST}"
    echo -e "    ${C}pm dash${RST}                    Show all sessions, PnL, configs"
    echo ""
    echo -e "  ${B}${W}Sessions${RST}"
    echo -e "    ${C}pm start${RST} NAME              Start local session"
    echo -e "    ${C}pm start${RST} NAME vps          Start VPS session"
    echo -e "    ${C}pm stop${RST} NAME               Stop session (local or VPS)"
    echo -e "    ${C}pm kill${RST} NAME               Kill VPS session permanently"
    echo -e "    ${C}pm logs${RST} NAME               Tail VPS logs"
    echo -e "    ${C}pm status${RST} NAME             Check VPS session status"
    echo ""
    echo -e "  ${B}${W}Analysis${RST}"
    echo -e "    ${C}pm analyze${RST}                  Analyze all local data"
    echo -e "    ${C}pm analyze${RST} NAME             Analyze one session"
    echo -e "    ${C}pm pull${RST}                     Pull VPS data to local"
    echo -e "    ${C}pm analyze-vps${RST}              Pull + analyze VPS data"
    echo ""
    echo -e "  ${B}${W}Config${RST}"
    echo -e "    ${C}pm config${RST}                   List all configs"
    echo -e "    ${C}pm config${RST} NAME              View a config"
    echo -e "    ${C}pm config${RST} NAME --new        Create new config"
    echo -e "    ${C}pm config${RST} NAME --edit       Edit existing config"
    echo ""
    echo -e "  ${B}${W}Infra${RST}"
    echo -e "    ${C}pm ui${RST}                       Open web dashboard"
    echo -e "    ${C}pm deploy${RST}                   Deploy to VPS"
    echo -e "    ${C}pm ssh${RST}                      SSH to VPS"
    echo ""
    ;;

*)
    echo "Unknown command: $CMD"
    echo "Run 'pm help' for usage."
    exit 1
    ;;
esac
