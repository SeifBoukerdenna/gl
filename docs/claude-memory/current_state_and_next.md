---
name: Current State & Next Steps (Apr 11 2026 ~04:00 UTC)
description: Where the project is right now, what's running, what's pending, what the next decision points are. If you wake up in a fresh session and need to know what's happening, read this first.
type: project
---

## Where we are right now

**Time:** ~04:00 UTC Apr 11, 2026 (~12:00 AM ET). User is about to go to sleep (or should).

**Phase:** exploration mode — paper trading only, collecting data to validate strategies before live deployment. **NO halts on most sessions** (data preservation over damage protection).

**Last actions taken:**
1. Deployed 3 new architectures: oracle_lazy (V2, vol-adaptive direction-stable persistence), volume_shift_1, piggyback_1
2. Built and ran the post-hoc halt simulator (`analysis/simulate_halts.py`) — validated that per-session halt tuning produces big gains
3. Built time-of-day heatmap (`analysis/time_of_day.py`)
4. Built post-fix correlation matrix (`analysis/correlation_post_fix.py`)
5. Updated dashboard header with BTC 1h Δ% + realized vol widget
6. Deployed dashboard fix to VPS via `./scripts/deploy-dashboard.sh`

## Active VPS sessions (14 total)

### Oracle family (7)
- oracle_1, oracle_alpha, oracle_arb, oracle_chrono, oracle_consensus, oracle_echo, oracle_omega, oracle_titan

### Multi-venue (2)
- blitz_1, test_ic_wide

### Non-oracle (5)
- pivot_1 (half-window BTC autocorrelation — restarted with overnight skip disabled)
- oracle_lazy (V2 vol-adaptive persistence — deployed tonight)
- volume_shift_1 (Binance message rate leading indicator — deployed tonight)
- piggyback_1 (follows PM market makers — deployed tonight)

### Killed (archived)
- edge_hunter, test_xp

### Sessions with HALT mechanism (3)
- oracle_omega, oracle_titan, pivot_1 — all have rolling halt (5/-$200/90min) + daily budget (-$1500) + overnight skip DISABLED for tonight

### Sessions WITHOUT halts (11)
- Everything else — pure exploration, raw data collection

## The "exploration mode" decision

User explicitly decided NOT to add halts to the other 11 sessions because:
1. Need raw tail data to understand true distributions
2. Can post-hoc simulate any halt config with `simulate_halts.py`
3. When going live later, use simulator to pick optimal per-session halt config
4. Paper losses are meaningless during exploration

**Only revisit this if:**
- Any session reaches 500+ trades (enough for post-hoc analysis)
- A real bloodbath hits while user is away
- User decides to go live

## Known regime-shift risk

The portfolio is NOT bloodshed-proof. Rough exposure:
- Top 6 earners have NO halts → ~$12-25k max bleed in a 5h bloodbath
- Only omega/titan/pivot_1 are capped (at ~$1500/session/day via daily budget)
- User accepted this tradeoff in exchange for clean data

## Key insights to remember

1. **Effective N = 1.6 out of 8** (portfolio is mostly redundant)
2. **oracle_arb is THE breakout** — 0.30 correlation with oracle_1, +$38/tr, genuinely unique mechanism
3. **oracle_chrono is the strictly-better version of oracle_1** (0.95 correlated, 3× PnL) — if forced to pick one, pick chrono
4. **test_ic_wide is the only true 4/4 viable session** right now
5. **blitz_1 and test_ic_wide are actually independent from each other (0.18)** — they share "multi-venue" label but fire on different triggers
6. **Halt configs are not transferable between sessions** — each session has its own optimum, must use the simulator to find it
7. **22 UTC was a hot hour** (BTC -41bp, $9.1k of portfolio PnL from one hour, 8 of 14 sessions participating)
8. **20 UTC and 23 UTC look like dead zones** (-$2.4k combined portfolio losses in those 2 hours)
9. **Overnight skip is currently disabled** on omega/titan/pivot_1 for tonight to get full overnight data
10. **The NO sizing bug** was systemic and affected every NO trade; fixed Apr 10 ~16:36 UTC. All post-fix data is clean. Pre-fix data should be treated as contaminated.

## What to do tomorrow (in priority order)

### High priority
1. **Run the scorecard first** (`python analysis/session_scorecard.py`) to see overnight performance
2. **Sync data** (`rsync -az root@167.172.50.38:/opt/polymarket-bot/data/ data/`)
3. **Check if any sessions crossed 500 trades** — if so, re-run the halt sweep on them for more robust estimates
4. **Check if the 3 new sessions (oracle_lazy, volume_shift_1, piggyback_1) have meaningful data** — 10+ trades each would be enough for an initial read

### Medium priority
5. **Re-run correlation matrix** on the new data to see if the new architectures are actually independent
6. **Re-run time-of-day heatmap** to see if 20/23 UTC dead zones hold with more data
7. **Consider testing `certainty_premium`** — the "late-window z>1.5" architecture, similar in spirit to our validated late_convergence thesis

### Low priority / optional
8. Document the findings in a proper writeup
9. Add more inactive architectures (`smooth_seeker`, `oracle_breaker`, etc) as research experiments
10. Build a cross-session alerting system for overnight bleed detection

## Deployment readiness

**NOTHING is ready to deploy live yet.** The closest is test_ic_wide (only session at 4/4 viability) but it only has ~65 trades — still Tier 1-2. Needs:
- 500-1000 trades minimum
- At least one observed drawdown of -$800+ that it recovered from
- All 4 viability metrics stable on the LAST 300 trades (not just overall)
- At least 7 days of data spanning multiple regimes

Earliest plausible deployment date: probably ~1 week from now if things go well and no new bugs surface.

## Critical files modified today (save this list)

### Architectures
- `bot/architectures/oracle.py` — momentum lookup fix
- `bot/architectures/oracle_alpha.py` — momentum lookup fix
- `bot/architectures/oracle_arb.py` — momentum lookup fix
- `bot/architectures/oracle_breaker.py` — momentum lookup fix
- `bot/architectures/oracle_chrono.py` — momentum lookup fix
- `bot/architectures/oracle_consensus.py` — momentum lookup fix + V2 level consensus + IC_MAX_PRICE_AGE_SEC / IC_LEVEL_TOLERANCE_BPS params
- `bot/architectures/oracle_echo.py` — momentum lookup fix
- `bot/architectures/oracle_lazy.py` — V2 vol-adaptive direction-stable persistence (complete rewrite of state machine)
- `bot/architectures/oracle_lazy_reverse.py` — momentum lookup fix
- `bot/architectures/oracle_omega.py` — momentum lookup fix + halt mechanism fix (dict access) + daily budget + overnight skip ET fix
- `bot/architectures/oracle_pulse.py` — momentum lookup fix (2 instances)
- `bot/architectures/oracle_titan.py` — NEW (defense-in-depth kitchen sink) + halt dict fix + daily budget + overnight skip ET fix
- `bot/architectures/pivot_1.py` — NEW (half-window autocorrelation) + cross-venue consensus gate + halt dict fix + daily budget + overnight skip ET fix
- `bot/architectures/smooth_seeker.py` — momentum lookup fix

### Engine
- `bot/paper_trade_v2.py` — **NO sizing bug fix** in execute_paper_trade (cost_per_share = 1 - entry_price for NO trades) + momentum lookup fix in legacy path

### Configs
- `configs/oracle_lazy.json` — updated to V2 params
- `configs/oracle_omega.json` — tightened halt (5/-$200/90min) + daily budget
- `configs/oracle_titan.json` — NEW
- `configs/pivot_1.json` — NEW, tightened halt, daily budget, overnight disabled
- `configs/volume_shift_1.json` — NEW
- `configs/oracle_consensus.json` — V2 params (level-based, no momentum)

### Analysis
- `analysis/simulate_halts.py` — NEW, post-hoc halt simulator
- `analysis/time_of_day.py` — NEW, UTC hour heatmap
- `analysis/correlation_post_fix.py` — NEW, correlation matrix on clean data
- `analysis/backtest_brainstorm.py` — NEW, multi-strategy counterfactual
- `analysis/backtest_late_convergence.py` + `_full.py` — NEW, late-window kline counterfactual
- `analysis/dig_arb.py` — NEW, oracle_arb deep dive
- `analysis/dig_strategyB.py` — NEW, mid-window value deep dive
- `analysis/wr_by_entry.py` — NEW
- `analysis/entry_at_half_window.py` — NEW
- `analysis/cross_count_predict.py` — NEW, strike-crossing predictor
- `analysis/pivot_volume_sweep.py` — NEW
- `analysis/halt_sweep.py` — NEW
- `analysis/post_fix_review.py` — NEW
- `analysis/extend_klines.py` — NEW
- `analysis/session_scorecard.py` — NEW

### Dashboard
- `dashboard.py` — added `/api/btc-regime` endpoint, 1H Δ + realized vol widget in header
- Deployed via `./scripts/deploy-dashboard.sh` to VPS
- Tunnel URL: `https://learned-restricted-manufacturer-accommodation.trycloudflare.com` (auth: admin/changeme123)
