# Polymarket BTC 5-Min Trading Bot

Paper trading bot for Polymarket 5-minute BTC prediction markets.
Exploits Binance → Polymarket price lag (Heuristic C).

## Quick Start

```bash
# Local testing
python -u bot/paper_trade_v2.py

# Deploy to VPS
./scripts/deploy.sh

# Check status
./scripts/status.sh

# Pull data and analyze
./scripts/analyze.sh

# View logs
./scripts/logs.sh
```

## Project Structure

```
bot/              — Active trading bot
analysis/         — Post-session analysis scripts
scripts/          — Operational scripts (deploy, status, logs)
deploy/           — VPS setup and systemd configs
archive/          — Old scripts and experiments
data/             — Trade CSVs (gitignored)
output/           — Charts (gitignored)
```

## Multi-Instance

```bash
# Default instance
./scripts/deploy.sh

# Experimental instance
./scripts/deploy.sh --instance experimental
./scripts/status.sh experimental
./scripts/logs.sh experimental
```
