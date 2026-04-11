---
name: Analysis Scripts — what each one does and when to use it
description: Complete inventory of analysis/ scripts built during the Apr 10-11 session, organized by purpose. How to run each and what output to expect.
type: reference
---

## Core simulators (built for tuning and validation)

### `analysis/simulate_halts.py` (THE post-hoc halt simulator)
The main tool for halt parameter tuning WITHOUT affecting live sessions. Reads any session's `data/<name>/trades.csv` and applies halt + daily budget logic exactly like the live engine does.

**Usage:**
```bash
# Single session, default config
python analysis/simulate_halts.py oracle_arb

# Custom halt
python analysis/simulate_halts.py oracle_arb --lookback 5 --threshold -200 --cooldown 60 --budget -1500

# Grid sweep to find best config
python analysis/simulate_halts.py oracle_arb --sweep

# All sessions with default halt
python analysis/simulate_halts.py --all

# All sessions sweep, save CSV
python analysis/simulate_halts.py --all --sweep --out halt_sweep.csv
```

**Output:** side-by-side comparison of original vs protected metrics (n, PnL, R², Calmar, 6h+, w6h), halt trip count, trades saved/foregone broken down by reason (halt_tripped, halt_cooldown, daily_budget).

**Key insight:** halt configs are not transferable between sessions. Use this to find per-session optimal configs before going live.

### `analysis/backtest_omega.py` (omega strategy replay)
Tests the oracle_omega hypothesis against oracle_1's historical trades. Applies chrono filters + rolling halt + trend sizing as counterfactual overlays. Outputs comparison of baseline / chrono / omega metrics.

### `analysis/backtest_filter.py` (generic counterfactual filter)
CLI tool for applying ad-hoc filters to historical trades. Supports bucket-direction blocks, delta floors/ceilings, entry caps, hour skips, loss streak halts, DD halts. Use this to test "what if I had filter X" without touching live sessions.

### `analysis/backtest_brainstorm.py` (multi-strategy backtest)
Tests several new strategy ideas (streak, spike, settle, smart) as counterfactual transformations on oracle_1's trades. Used during the brainstorm phase.

### `analysis/backtest_late_convergence.py` + `backtest_late_convergence_full.py`
Tests the late-window convergence hypothesis. The simple version uses oracle_1's existing fires. The "_full.py" version uses 69 days of Binance klines to do a proper counterfactual over 19,968 windows. **Finding: 91.7% WR but break-even entry ~91c, mostly unexploitable because market already prices it in.**

## Scorecard and monitoring

### `analysis/session_scorecard.py`
Computes the viability metrics for each active session and classifies into archetypes (WORKHORSE, HIGH-CONVICTION SNIPER, VARIANCE TRAP, LUCKY OUTLIER, BLEEDER, etc). Run this whenever you want a "what's working right now" read.

**Output:** one row per session with n, R², Cal, WR, $/tr, 6h+, worst_6h, and an archetype label.

**ACTIVE list:** already filters out killed sessions (edge_hunter, test_xp).

### `analysis/time_of_day.py` (NEW — built this session)
UTC hour × session heatmap showing $/trade per cell, plus hour-total PnL across the portfolio, best/worst hour per session, and peak-hour diversity.

**Output:**
- 14-row × 14-col heatmap colored by $/trade
- Hour totals with bars
- Per-session best/worst hour
- Peak-hour diversity metric (% of sessions peaking in different hours)

### `analysis/correlation_post_fix.py` (NEW — built this session)
Correlation matrix on POST-NO-sizing-fix clean data only. Computes hourly-PnL Pearson correlations between all sessions, reports top correlated pairs, and computes effective independent sessions (N / (1 + (N-1)*r_avg)).

**Key filter:** `RESET_TS = 1775836800` (Apr 10 ~16:00 UTC) — excludes pre-fix contaminated data.

**Output:** per-session trade counts, pairwise correlation matrix, most-correlated pairs, effective N, family-level aggregate correlations.

### `analysis/correlation_today.py`
Older version of the correlation analysis, ran on mixed pre/post-fix data. Kept for reference but superseded by `correlation_post_fix.py`.

### `analysis/post_fix_review.py`
Portfolio-wide review script that slices the post-fix data and compares per-session behavior, direction splits (YES vs NO), oracle-family vs multi-venue aggregates, hour-by-hour PnL, and BTC regime context.

## Research / one-off

### `analysis/dig_arb.py`
Deep-dive on oracle_arb: PnL distribution, quartile breakdown, direction split (YES vs NO), time-bucket profile, head-to-head vs oracle_1 in shared windows, bootstrap CI on $/trade. **Used to validate that arb's mispricing edge is real vs regime noise.**

### `analysis/dig_strategyB.py`
Deep-dive on the "mid-window value" finding (T 90-150, entry 55-75c). Session contribution, direction profile, time stability, bootstrap CI, sub-cell refinement. Concluded the edge is real but small.

### `analysis/wr_by_entry.py`
WR by (true_entry, time_remaining) bucket across all oracle sessions. Reveals which entry×time cells have real edge vs random.

### `analysis/entry_at_half_window.py`
Empirical entry-price distribution at T 90-180 with various delta bands. Used to determine realistic entry prices for pivot_1's design.

### `analysis/cross_count_predict.py`
First-half BTC strike-crossing count as a predictor of second-half outcome. Used 69 days of klines. **Finding: zero crossings (trending) gives 78% momentum WR; 13+ crossings (chop) still gives 69% WR. Direction-at-half is the dominant signal regardless of chop count.**

### `analysis/pivot_volume_sweep.py`
Grid sweep for pivot_1's trigger parameters (T window, delta range, multi-fire cooldowns) across 69 days of klines. Used to find the 60-180s / 5bp sweet spot for pivot_1.

### `analysis/backtest_brainstorm.py`, `halt_sweep.py`
See core simulators above.

## Data management

### `analysis/extend_klines.py`
Fetches Binance 1-second klines from the last timestamp in `data/binance_klines_60d.parquet` to now. Use this before running any kline-based counterfactual to get fresh data.

**Note:** the parquet is named "60d" but now contains more (69+ days and growing). It's updated incrementally.

### `analysis/fetch_data.py`
Original/legacy data fetcher. Has the Binance kline fetcher function but also does other stuff (settlements, price history synthesis) we don't use anymore.

## Configuration and operation

### `scripts/deploy-dashboard.sh`
Deploys dashboard.py + requirements.txt to VPS, restarts the `polymarket-dashboard` service, sets up Cloudflare tunnel for HTTPS access. **Use whenever dashboard.py changes.**

**Tunnel URL:** shown at end of deploy, something like `https://learned-restricted-manufacturer-accommodation.trycloudflare.com`. Auth: `admin / changeme123`.

### `scripts/pull-data.sh`, `scripts/sessions.sh`, `scripts/status.sh`, `scripts/stop.sh`, `scripts/kill-session.sh`, `scripts/new-session.sh`, `scripts/start.sh`, `scripts/logs.sh`
Operational helpers. `new-session.sh <name> <arch>` to spin up a new session. `kill-session.sh <name>` to shut one down.

### Local scripts
`local-analyze.sh`, `local-sessions.sh`, `local-start.sh` — same but for local testing.

## Data layout
```
data/
  <session>/
    trades.csv          ← primary source of truth for PnL
    skipped.csv         ← rejected signals (sampling)
    windows.csv         ← per-window outcomes
    stats.json          ← live snapshot updated by the bot process
  binance_klines_60d.parquet   ← 69+ days of 1s BTCUSDT klines
  edge_table.json              ← V2 edge table
  edge_table_v3.json           ← deprecated, V3 was abandoned
  _killed_<name>_<ts>/         ← archived killed sessions (hidden from dashboard)
```

## Quick reference: which script for which question

| question | script |
|---|---|
| what's working right now? | `session_scorecard.py` |
| is my portfolio diversified? | `correlation_post_fix.py` |
| what hours are good/bad? | `time_of_day.py` |
| what halt config should session X use? | `simulate_halts.py <name> --sweep` |
| is oracle_arb's edge real? | `dig_arb.py` |
| how would omega behave on historical data? | `backtest_omega.py` |
| what if I add filter X to session Y? | `backtest_filter.py` or modify `simulate_halts.py` |
| update kline data | `extend_klines.py` |
| deploy dashboard changes | `./scripts/deploy-dashboard.sh` |
