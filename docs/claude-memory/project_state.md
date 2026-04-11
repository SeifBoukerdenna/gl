---
name: Project State
description: Comprehensive current state of the Polymarket bot — 14 active sessions post-NO-sizing-fix, dashboard updated, 3 new architectures just deployed (Apr 10-11 2026)
type: project
---

## High-level summary
- Polymarket 5-min BTC binary prediction bot, paper trading only
- VPS: `root@167.172.50.38` at `/opt/polymarket-bot`, systemd units `polymarket-bot@<session>`
- Dashboard: Flask + SSE, deployed to VPS via `./scripts/deploy-dashboard.sh`, accessed via Cloudflare tunnel
- Local dev: `/Users/sboukerdenna/perso/gl`

## Current active sessions (14, as of Apr 11 2026 ~03:30 UTC)

### Oracle family (7 sessions, all edge-table-based, all highly correlated r≈0.85+)
- **oracle_1**: baseline oracle (edge table → fire on abs edge). ~120 trades, +$1.9k, $15/tr, R²=0.62, Cal=1.54
- **oracle_alpha**: kitchen-sink oracle with 8 defensive layers incl. 45c entry cap, vol gate, DD halt, dir WR floor. Low volume (~30 trades), +$1.3k, $42/tr, R²=0.69, Cal=2.10
- **oracle_arb**: mispricing architecture (table_wr − market_wr ≥ 5pp). **TOP $/TR PERFORMER.** ~130 trades, +$4.9k, $38/tr, R²=0.70, Cal=2.70
- **oracle_chrono**: edge table + bucket blocks/boosts. ~75 trades, **+$5.1k**, $68/tr, R²=0.90, Cal=4.76 — best by most metrics
- **oracle_consensus**: edge table + LEVEL-based cross-venue consensus (V2 fix, Apr 10). ~95 trades, +$3.7k, $39/tr
- **oracle_echo**: small, barely running (~7 trades)
- **oracle_omega**: chrono + overnight skip + halt. Just restarted with tightened halts. Small sample
- **oracle_titan**: kitchen sink — chrono + consensus + halt + vol gate. Just restarted. Very low fire rate (over-filtered)

### Multi-venue family (2 sessions)
- **blitz_1**: 3-phase (impulse_confirmed / cross_pressure / certainty_premium). ~50 trades, +$1.5k, $29/tr, Cal=2.43
- **test_ic_wide**: impulse_confirmed. The only pristine WORKHORSE. ~70 trades, +$1.7k, $24/tr, **R²=0.87, Cal=4.84** (4/4 viability)

### Non-oracle architectures
- **pivot_1**: half-window BTC autocorrelation (T 60-180s, |delta|≥5, entry≤0.75). New. Just restarted with timezone fix. Low volume in calm regime.
- **oracle_lazy**: V2 vol-adaptive direction-stable persistence (not the old 10s fixed wait). Deployed tonight. Very small sample.
- **volume_shift_1**: Binance message rate surge as leading indicator. Deployed tonight. Small sample.
- **piggyback_1**: follows PM market makers (tight spreads + depth shifts). Deployed tonight. Small sample.

### Killed (archived as `_killed_*`)
- **edge_hunter**: was bleeding, killed Apr 10 ~02:00 UTC
- **test_xp**: cross_pressure, was bleeding in current regime, killed same time

## Current portfolio PnL (post-NO-sizing-fix clean data, ~10h)
~$16,000 total across 14 sessions. oracle_chrono + oracle_arb account for ~60% of PnL.

## Effective independent sessions: 1.6 out of 8
Not ~11 as the dashboard might suggest. 80% of the portfolio is redundant because the oracle family is 0.75-0.95 correlated internally. The only genuinely independent bets are:
1. Oracle family (counts as 1 bet despite 7 sessions)
2. oracle_arb (mispricing — 0.29 correlation with oracle family)
3. blitz_1 (multi-venue — 0.47 with oracle family)
4. test_ic_wide (impulse_confirmed — 0.58 with oracle family, 0.18 with blitz_1 — genuinely independent from blitz)
- The 6 new/restarted sessions don't have enough data yet to compute correlations

## Edge table
- V2 loaded at `data/edge_table.json`. Hierarchical L1/L2/L3 lookup: delta_bucket × time_bucket × momentum × side. Min cell count = 50.
- V3 was attempted and ABANDONED — vol regime feature made Brier score slightly worse.
- Used by: oracle_1, oracle_alpha, oracle_arb, oracle_chrono, oracle_consensus, oracle_echo, oracle_omega, oracle_titan, oracle_lazy

## Current regime (as of ~03:30 UTC Apr 11)
- BTC ~$72,900, drifted +5 bps over 10h (mostly sideways)
- Realized vol ~22% annualized (low-vol regime)
- The 22% vol is BELOW the 25% gate used by titan/alpha/consensus — those sessions are partially silenced by the vol regime filter
- Single "hot hour" in the data: 22 UTC (2 PM ET) when BTC dropped -41 bps, oracle_chrono caught +$4k of it in two trades
