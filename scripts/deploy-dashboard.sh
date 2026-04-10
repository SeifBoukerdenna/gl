#!/bin/bash
# Deploy dashboard + Cloudflare tunnel to VPS WITHOUT restarting any bot sessions.
# Usage: ./scripts/deploy-dashboard.sh
set -e

source .env 2>/dev/null || true
HOST="${VPS_HOST:-167.172.50.38}"
USER="${VPS_USER:-root}"
RPATH="${VPS_PATH:-/opt/polymarket-bot}"

echo "=== Deploy Dashboard + HTTPS Tunnel to ${USER}@${HOST} ==="
echo "  (Bot sessions will NOT be touched)"
echo ""

# 1. Syntax check
echo "[1/7] Syntax check..."
python3 -m py_compile dashboard.py
echo "  OK"

# 2. Sync files
echo "[2/7] Syncing dashboard files..."
ssh ${USER}@${HOST} "mkdir -p ${RPATH}/deploy"
rsync -az \
    dashboard.py \
    requirements.txt \
    ${USER}@${HOST}:${RPATH}/
rsync -az \
    deploy/dashboard.service \
    deploy/cloudflare-tunnel.service \
    ${USER}@${HOST}:${RPATH}/deploy/
echo "  Synced"

# 3. Install Flask
echo "[3/7] Installing dependencies..."
ssh ${USER}@${HOST} "cd ${RPATH} && .venv/bin/pip install -q flask"
echo "  OK"

# 4. Install cloudflared
echo "[4/7] Installing cloudflared..."
ssh ${USER}@${HOST} '
    if command -v cloudflared &>/dev/null; then
        echo "  Already installed: $(cloudflared --version)"
    else
        echo "  Downloading cloudflared..."
        curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
        dpkg -i /tmp/cloudflared.deb
        rm /tmp/cloudflared.deb
        echo "  Installed: $(cloudflared --version)"
    fi
'

# 5. Install dashboard service
echo "[5/7] Setting up dashboard service..."
ssh ${USER}@${HOST} "
    cp ${RPATH}/deploy/dashboard.service /etc/systemd/system/polymarket-dashboard.service
    systemctl daemon-reload
    systemctl enable polymarket-dashboard
    systemctl restart polymarket-dashboard
"
echo "  OK"

# 6. Install tunnel service
echo "[6/7] Setting up Cloudflare tunnel..."
ssh ${USER}@${HOST} "
    cp ${RPATH}/deploy/cloudflare-tunnel.service /etc/systemd/system/cloudflare-tunnel.service
    systemctl daemon-reload
    systemctl enable cloudflare-tunnel
    systemctl restart cloudflare-tunnel

    # Make sure port 5555 is NOT open (traffic goes through tunnel only)
    if command -v ufw &>/dev/null && ufw status | grep -q 'active'; then
        ufw delete allow 5555/tcp 2>/dev/null || true
    fi
"
echo "  OK"

# 7. Get the tunnel URL
echo "[7/7] Waiting for tunnel URL..."
sleep 5
TUNNEL_URL=$(ssh ${USER}@${HOST} "grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' /var/log/polymarket-bot/tunnel.log | tail -1")

if [ -z "$TUNNEL_URL" ]; then
    echo "  Tunnel starting... checking again in 5s"
    sleep 5
    TUNNEL_URL=$(ssh ${USER}@${HOST} "grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' /var/log/polymarket-bot/tunnel.log | tail -1")
fi

echo ""
echo "=========================================="
echo "  Dashboard deployed!"
echo "=========================================="
if [ -n "$TUNNEL_URL" ]; then
    echo ""
    echo "  URL:  ${TUNNEL_URL}"
    echo ""
    echo "  Auth: admin / changeme123"
    echo ""
    echo "  Bookmark this on your phone!"
else
    echo ""
    echo "  Tunnel is starting. Get the URL with:"
    echo "    ssh ${USER}@${HOST} grep trycloudflare /var/log/polymarket-bot/tunnel.log | tail -1"
fi
echo ""
echo "  To change the password:"
echo "    ssh ${USER}@${HOST}"
echo "    Edit DASH_PASS in /etc/systemd/system/polymarket-dashboard.service"
echo "    systemctl daemon-reload && systemctl restart polymarket-dashboard"
echo ""
echo "  To check tunnel URL later:"
echo "    ssh ${USER}@${HOST} grep trycloudflare /var/log/polymarket-bot/tunnel.log | tail -1"
echo ""
echo "  Bot sessions were NOT touched."
