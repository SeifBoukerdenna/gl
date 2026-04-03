#!/bin/bash
# One-time VPS setup. Run on the VPS as root.
# Usage: bash setup-vps.sh

set -e

echo "=== Polymarket Bot VPS Setup ==="

# 1. System packages
echo "Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git > /dev/null

# 2. Create directories
echo "Creating directories..."
mkdir -p /opt/polymarket-bot
mkdir -p /var/log/polymarket-bot
mkdir -p /opt/polymarket-bot/data
mkdir -p /opt/polymarket-bot/output

# 3. Clone or pull repo
if [ -d /opt/polymarket-bot/.git ]; then
    echo "Repo exists, pulling latest..."
    cd /opt/polymarket-bot && git pull
else
    echo "NOTE: Clone your repo to /opt/polymarket-bot manually:"
    echo "  git clone YOUR_REPO_URL /opt/polymarket-bot"
    echo "  OR: copy files manually for now"
fi

# 4. Python venv
echo "Setting up Python venv..."
cd /opt/polymarket-bot
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# 5. Systemd service
echo "Installing systemd service..."
cp deploy/polymarket-bot@.service /etc/systemd/system/
systemctl daemon-reload

# 6. Logrotate
echo "Setting up log rotation..."
cp deploy/logrotate.conf /etc/logrotate.d/polymarket-bot

# 7. Create data dirs for default instance
mkdir -p /opt/polymarket-bot/data
mkdir -p /opt/polymarket-bot/output

echo ""
echo "=== Setup complete ==="
echo "Start bot:  systemctl start polymarket-bot@default"
echo "Stop bot:   systemctl stop polymarket-bot@default"
echo "Status:     systemctl status polymarket-bot@default"
echo "Logs:       journalctl -u polymarket-bot@default -f"
echo "            tail -f /var/log/polymarket-bot/default.log"
