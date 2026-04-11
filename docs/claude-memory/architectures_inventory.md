---
name: Architectures Inventory
description: Complete catalog of every architecture in bot/architectures/ — active, inactive, thesis, fire trigger, key params, verdict from the Apr 10 audit
type: project
---

## How architectures work
- Files in `bot/architectures/*.py`, auto-discovered by `bot/architectures/__init__.py`
- Each exports an `ARCH_SPEC` dict with `name`, `combo_params`, `check_signals`, `extra_globals`, `on_tick`, `on_window_start`
- Configs in `configs/<session>.json` specify `"ARCHITECTURE": "<arch_name>"` and override extra_globals
- Engine (`bot/paper_trade_v2.py`) loads architecture by name and calls `check_signals` on every Binance tick

## Active architectures (running on VPS as of Apr 11 2026)

### Edge-table family (fire on historical win rate > breakeven)
| file | session name(s) | thesis | key params |
|---|---|---|---|
| `oracle.py` | oracle_1 | baseline: fire when table_wr - breakeven ≥ min_edge | phase1=0.08, phase2=0.05 |
| `oracle_alpha.py` | oracle_alpha | kitchen sink: edge + multi-exchange confirm + vol gate + dir WR floor + 45c entry cap + 6h DD halt + chrono blocks + 10pp edge | OR_MAX_ENTRY=0.45, ALPHA_MIN_VOL_PCT=25, ALPHA_MAX_VOL_PCT=80, ALPHA_DD_HALT_THRESHOLD=-800 |
| `oracle_arb.py` | oracle_arb | fair-value arbitrage: fire when `table_wr - market_wr ≥ 5pp`. Market-implied WR = YES entry price. Picks direction with biggest mispricing. | MISPRICING_MIN_PP=0.05, MAX=0.30, STRONG=0.10 |
| `oracle_chrono.py` | oracle_chrono | edge table + chrono bucket blocks/boosts. Blocks 3 historical losers, boosts 3 historical winners by 1.3×. | CHRONO_BLOCKED=["60-90\|NO","5-30\|NO","270-300\|NO"], CHRONO_BOOSTED=["210-240\|NO","150-180\|YES","180-210\|YES"] |
| `oracle_consensus.py` | oracle_consensus | edge table + LEVEL-based cross-venue consensus (V2 — momentum check was V1 bug). Any venue showing opposite side = hard veto. | IC_MIN_CONFIRMS=1, IC_MAX_PRICE_AGE_SEC=3, IC_LEVEL_TOLERANCE_BPS=0.5 |
| `oracle_echo.py` | oracle_echo | small variant, too few trades to characterize | — |
| `oracle_omega.py` | oracle_omega | chrono blocks + overnight skip + rolling halt + daily budget. Was the "validated optimum" but halt was broken until fixed Apr 10 | OMEGA_HALT_LOOKBACK=5, OMEGA_HALT_LOSS_THRESHOLD=-200, cooldown=90min, daily_budget=-$1500 |
| `oracle_titan.py` | oracle_titan | "defense in depth" kitchen sink: edge table + chrono + overnight + halt + daily budget + vol gate + LEVEL consensus + BOTH-venue bonus | TITAN_HALT_LOOKBACK=5, TITAN_HALT_LOSS_THRESHOLD=-200, TITAN_MIN_VOL_PCT=25, TITAN_MAX_VOL_PCT=80 |

### Non-oracle architectures (mechanism-diverse)
| file | session name(s) | thesis | key params |
|---|---|---|---|
| `pivot_1.py` | pivot_1 | half-window BTC autocorrelation. Fires at T 60-180s when \|delta\| 5-25bp, entry ≤0.75. Also has LEVEL consensus gate + halt + daily budget + vol-adaptive overnight | PIV_T_MIN=60, PIV_T_MAX=180, PIV_DELTA_MIN=5, PIV_DELTA_MAX=25, PIV_MAX_ENTRY=0.75 |
| `blitz.py` | blitz_1 | 3-phase: impulse_confirmed (T-295 to T-120), cross_pressure (T-120 to T-60), certainty_premium (T-60 to T-5). Up to 3 trades per window. | BZ_PHASE1_END=120, BZ_PHASE2_END=60 |
| `impulse_confirmed.py` | test_ic_wide | Binance impulse + 1-2 other exchanges confirming direction over 5-60s lookback | 12 combos varying threshold_bp (3-15) and lookback_s (10-60) |
| `oracle_lazy.py` | oracle_lazy | **V2** vol-adaptive direction-stable persistence. Queue signal, wait `clamp(3s, vol%×0.1, 12s)` min, then require 3 consecutive same-side 1s ticks. Edge must stay ≥80% of original. Max wait 25s. | LAZY_MIN_WAIT_FLOOR_SEC=3, LAZY_MIN_WAIT_CEIL_SEC=12, LAZY_VOL_SCALE=0.1, LAZY_STABLE_TICKS=3, LAZY_MAX_WAIT_SEC=25, LAZY_EDGE_DEGRADATION_MAX=0.20 |
| `volume_shift.py` | volume_shift_1 | Binance trade tick rate surge as leading indicator. Fires BEFORE price moves when 2s rate 3-5× 30s baseline. | VS_5x_6t, VS_4x_8t combos active |
| `piggyback.py` | piggyback_1 | Trade WITH PM market makers. Fires when ≥2 book signals agree (tight spread, depth shift, mid acceleration). Only arch trading WITH the book rather than against it. | PB_MIN_SIGNALS=2, PB_BASE_DOLLARS=150, PB_STRONG_DOLLARS=300 |

## Inactive architectures (audited Apr 10 — Explore agent)

### HIGH promise (not yet deployed in current exploration — but delta_sniper was explicitly rejected)
| file | thesis | why interesting | status |
|---|---|---|---|
| `delta_sniper.py` | Historical delta table for fair-value arb vs PM mispricing. Pure statistical arb. | Tests delta-only table vs full edge table. | **REJECTED** — too similar to oracle_arb (both do fair-value arb, would be correlated) |

### MEDIUM promise
| file | thesis | notes |
|---|---|---|
| `certainty_premium.py` | Buy near-certain winners (z>1.5) in final 30-60s at 90-94c. Liquidity vacuum exploit. | Similar to the `late_convergence` thesis we validated in kline counterfactual — but trigger is z-score, not raw delta |
| `lottery_fade.py` | Retail buys losing-side tokens at 8-15c when z>2. Buy winner. | Complementary to certainty_premium |
| `phoenix.py` | Buy cheap loser tokens (5-20c) for 5x-10x payout when multi-exchange momentum says flip likely | Experimental |
| `hybrid.py` | Merge impulse_confirmed + certainty_premium phases. | Already in blitz — redundant |
| `clutch.py` | Extract 2 proven phases from blitz (xp + cp) | Redundant with blitz |
| `accel.py` | 3-timeframe momentum acceleration (fast > mid > slow) | Minor variation on impulse |
| `book_fade.py` | Book imbalance (3x+ bid/ask ratio) without big BTC move — fade overreaction | Untested thesis |
| `cross_pressure.py` | 3-exchange composite momentum (was test_xp, killed — underperformed in current regime) | Killed, in archive |
| `evergreen.py` | Continuous z-score monitoring + multi-venue + 50-85c entry | z-score based |
| `flow_front.py` | Detect large orders mid-formation via trade clusters (5+ trades in 500ms) | Requires tick-level detection |
| `micro_scalp.py` | Stack weak signals (delta + imbalance + fair gap). 5-10x trades of sniper strategies | Lots of small trades |
| `oracle_breaker.py` | Edge table + adaptive sizing: halt at WR<45%, size down 55-62%, press at WR>70% | Continuous version of omega halt |
| `oracle_pulse.py` | Edge table + BTC impulse gate (3bp+ in 15s) | Variant of edge+impulse |
| `settlement_drift.py` | Mid converges to settlement in final 60s — sticky jumps | Late-window specialist |
| `smooth_seeker.py` | Meta: 3+ peer architectures must agree + $75 sizing + 6h DD halt | Uses other sessions as voters |

### LOW promise
| file | thesis | why low |
|---|---|---|
| `impulse_lag.py` | BTC impulse > 3-15bp in 10-60s | Dominated by impulse_confirmed |
| `oracle_lazy_reverse.py` | Contrarian oracle_lazy — fire opposite direction | Already tested, lost in bloodbath |
| `sniper.py` | Complex conviction-weighted signal stacking | Probably dominated by blitz |

### Not audited / less relevant
- `evergreen.py` variants, specific combo configs, etc.

## Key config naming conventions
- `configs/oracle_*.json` — use oracle architectures
- `configs/test_*.json` — experimental configs
- `configs/<name>_1.json`, `<name>_2.json` — numbered variants with different parameters
- `STARTING_BANKROLL=999999` on all non-legacy sessions (effectively unbounded)
- `MAX_SHARES=500-600` (critical — this is what caps NO size inflation at ~2×)
- `SIGNAL_CHECK_MS=50` on fast-firing sessions (default 200)

## Killed/archived
- `_killed_edge_hunter_<timestamp>/` — impulse_lag variant, was bleeding
- `_killed_test_xp_<timestamp>/` — cross_pressure, regime mismatch in current low-vol period
- Dashboard auto-skips dirs starting with `_`
