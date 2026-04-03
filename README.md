# Polymarket BTC 5-Min Trading Bot

Paper trading bot for Polymarket 5-minute BTC prediction markets.
Exploits Binance → Polymarket price lag (Heuristic C).

## Quick Start

```bash
# Deploy and start default session on VPS
./scripts/deploy.sh

# Check all sessions
./scripts/sessions.sh

# Pull data and analyze
./scripts/analyze.sh
```

## Project Structure

```
bot/                — Active trading bot (DO NOT REFACTOR)
analysis/           — Post-session analysis scripts
configs/            — JSON config files per session
scripts/            — Operational scripts
deploy/             — VPS setup and systemd configs
archive/            — Old scripts, v1, experiments
data/               — Trade CSVs (gitignored)
output/             — Charts (gitignored)
```

## Sessions

Each session runs the same bot code with different filter configs. Sessions are independent — own data, own logs, own systemd service.

### Create a new session

1. Create a config file:
```bash
cp configs/default.json configs/my-session.json
```

2. Edit the knobs:
```bash
vim configs/my-session.json
```

3. Start it:
```bash
./scripts/new-session.sh my-session
```

That's it. The bot reads `configs/my-session.json` on startup and overrides the defaults.

### Config knobs (configs/*.json)

```json
{
    "_description": "What this session tests",

    "BASE_TRADE_DOLLARS": 100,       // $ per trade (flat sizing)
    "MAX_SHARES": 500,               // max shares per trade
    "MAX_RISK_PCT": 0.10,            // max % of bankroll per trade
    "STARTING_BANKROLL": 1000,       // starting $ per combo

    "MIN_ENTRY_PRICE": 0.20,         // don't buy below this (0-1 scale)
    "MAX_ENTRY_PRICE": 0.80,         // don't buy above this
    "DOWN_MIN_ENTRY": 0.25,          // extra floor for BUY DOWN trades

    "MAX_IMPULSE_BP": 25,            // skip if BTC moved > this many bp
    "MAX_SPREAD": 0.03,              // skip if PM spread > 3c

    "DEAD_ZONE_START": 90,           // skip trades between T-90s and T-210s
    "DEAD_ZONE_END": 210,

    "WINDOW_BUFFER_START": 10,       // skip first 10s of window
    "WINDOW_BUFFER_END": 10,         // skip last 10s of window

    "COOLDOWN_RANGE_BP": 50,         // pause if window had > 50bp range
    "COOLDOWN_DURATION": 120,        // pause duration in seconds

    "PRINT_STATUS_INTERVAL": 15      // status line every N seconds
}
```

You only need to include the knobs you want to change — missing keys use the defaults from the code.

### Example configs

**`configs/default.json`** — Wide filters, let the strategy trade freely
```json
{
    "MIN_ENTRY_PRICE": 0.20,
    "MAX_ENTRY_PRICE": 0.80,
    "MAX_IMPULSE_BP": 25
}
```

**`configs/tight-filters.json`** — Only the best zones from 22h analysis
```json
{
    "MIN_ENTRY_PRICE": 0.30,
    "MAX_ENTRY_PRICE": 0.55,
    "MAX_IMPULSE_BP": 15,
    "DEAD_ZONE_START": 60,
    "DEAD_ZONE_END": 210
}
```

**`configs/wide-entry.json`** — Maximum trade count
```json
{
    "MIN_ENTRY_PRICE": 0.15,
    "MAX_ENTRY_PRICE": 0.85,
    "MAX_IMPULSE_BP": 30
}
```

### Manage sessions

```bash
# See all running sessions with PnL, trades, win%
./scripts/sessions.sh

# Check one session
./scripts/status.sh my-session

# View live logs
./scripts/logs.sh my-session

# Stop gracefully (runs final analysis)
./scripts/stop.sh my-session

# Stop and remove
./scripts/kill-session.sh my-session

# Pull data for local analysis
./scripts/pull-data.sh my-session

# Analyze one session
./scripts/analyze.sh my-session

# Analyze ALL sessions
./scripts/analyze.sh --all
```

## Deploy Workflow

```bash
# 1. Edit config or code locally
vim configs/my-session.json

# 2. Deploy (syncs code, restarts bot, shows log)
./scripts/deploy.sh

# 3. Check it's running
./scripts/sessions.sh
```

`deploy.sh` does: syntax check → git commit → rsync to VPS → pip install → restart systemd → tail log.

## First-Time VPS Setup

```bash
# 1. SSH to VPS
ssh root@YOUR_VPS_IP

# 2. Create directory
mkdir -p /opt/polymarket-bot

# 3. Exit, sync code from local
rsync -az --exclude '.venv' --exclude '.git' --exclude 'historical' ./ root@YOUR_VPS_IP:/opt/polymarket-bot/

# 4. SSH back, run setup
ssh root@YOUR_VPS_IP
cd /opt/polymarket-bot
bash deploy/setup-vps.sh

# 5. Start default session
systemctl start polymarket-bot@default
```

## Architecture

- **Binance WebSocket** → real-time BTC price (signal source)
- **Polymarket WebSocket** → real-time order book (execution)
- **Polymarket REST** → market discovery + settlement (once per window)
- **Chainlink RPC** → approximate priceToBeat (once per window)
- Signal fires synchronously from Binance tick handler — zero polling delay
- 12 parameter combos run in parallel on shared data feeds
- Flat $100/trade sizing with 10% bankroll risk cap
