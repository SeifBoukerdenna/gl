---
name: Viability Framework — the metrics we trust, the thresholds, the tiers
description: How to read session performance metrics (R², Calmar, WR, $/tr, 6h+, worst_6h), what values mean, when to trust a session, tier definitions for deployment readiness
type: reference
---

## The core metrics (what each tells you)

### R² — linearity of the equity curve (-1 to +1)
Measures how straight-line-up the cumulative PnL chart is. High R² = steady grind. Low R² = lumpy.

| range | meaning |
|---|---|
| 0.95+ | pristine straight line up, savings-account behavior |
| 0.85-0.95 | solid accumulation, small wiggles |
| 0.70-0.85 | clear uptrend with some dips, acceptable for live |
| 0.50-0.70 | bumpy, lots of variance around the line |
| 0.30-0.50 | weak trend, half the moves are noise |
| 0.00-0.30 | random walk |
| negative | downtrend (losing strategy) |

**Caution:** R² is a GLOBAL smoothness measure. A strategy can have high R² and great PnL but still have one terrible 6-hour window. Always combine with worst_6h.

### Calmar — total PnL / max drawdown
The one-number risk-adjusted return. Most important single metric.

| range | meaning |
|---|---|
| 10+ | exceptional, verify sample size |
| 5-10 | excellent, scalable |
| 3-5 | strong, trustworthy through bad weeks |
| 2-3 | acceptable, margins meaningful |
| 1-2 | marginal — a single bad streak erases gains |
| 0.5-1 | dangerous |
| <0.5 | catastrophic exposure |
| 0 / negative | dead strategy |

### WR — win rate %
| range | meaning |
|---|---|
| 90%+ | convergence trades (lottery_fade, certainty_premium) |
| 75-90% | high-conviction, few losses |
| 60-75% | solid statistical edge |
| 55-60% | marginal edge, needs asymmetric payoffs |
| 45-55% | below random, needs 2-3× wins-to-losses |
| <45% | broken OR deep-fade strategy |

### $/trade — avg PnL per trade
| range | meaning |
|---|---|
| $50+ | high conviction, low volume, big edge |
| $20-50 | strong edge, meaningful per-fire return |
| $10-20 | solid, scales with volume (oracle_1 territory) |
| $5-10 | edge exists but needs volume |
| $1-5 | marginal, fees eat half |
| negative | broken or regime mismatch |

### 6h+% — fraction of rolling 6-hour windows that ended positive
| range | meaning |
|---|---|
| 90%+ | almost any entry moment works |
| 80-90% | very stable, rare bad windows |
| 75-80% | mostly stable, ~1 in 5 losers |
| 65-75% | moderate, ~1 in 3 losers |
| 55-65% | coin-flip-ish, entry timing matters |
| <55% | strategy lacks temporal stability |

### worst_6h — worst 6-hour rolling PnL window
THE single metric for "don't get screwed entering at a bad moment." If worst_6h = -$500, that's the literal worst case for someone who entered at the worst moment.

## Viability framework — deploy-ready thresholds

A session is "live-deployable" if it passes ALL FOUR:
```
R² ≥ 0.85           (equity curve is actually a line)
6h+% ≥ 75%          (most 6h windows are profitable)
worst_6h ≥ -$500    (no 6h window has been catastrophic)
Calmar ≥ 2.0        (gains dwarf max drawdown)
```

All four must pass AND be stable across the last 200-300 trades (not just overall).

### Tier classification (used in `session_scorecard.py`)
| tier | criteria |
|---|---|
| VIABLE | passes 4/4 |
| PROMISING | passes 3/4 |
| BORDERLINE | passes 2/4 |
| NOT VIABLE | passes ≤1/4 |

## Trust tiers based on data size

| tier | trades | duration | what to trust | what NOT to trust |
|---|---|---|---|---|
| 1. First look | 50-100 | 6-12h | rough PnL direction (+/-) | everything else |
| 2. Directional | 200-500 | 1-3 days | WR ±5pp, $/tr ±$15 | R², Calmar, worst_6h |
| 3. Statistical | 500-1000 | 3-7 days | R², Calmar, worst_6h | regime resilience |
| 4. Regime-tested | 1000+ | 7-14 days + bad day | everything | — |

**Critical:** 1000 trades in a calm market are worth LESS than 500 trades including a real bleed event. The regime test matters more than the sample count. Don't declare a session "deploy-ready" until you've seen it survive at least one -$800+ drawdown.

## Reading a session in 30 seconds

Order of metrics to scan:
1. **worst_6h first** → "would I survive my worst day?"
2. **Calmar** → "do gains dwarf drawdowns?"
3. **R²** → "is the curve a line or noise?"
4. **6h+%** → "if I entered at random, how often up?"
5. **WR** → "are most trades winning or am I dependent on big ones?"
6. **$/tr** → "is each fire pulling its weight?"
7. **PnL LAST** → it's the outcome, not the quality measure

If any of (worst_6h, Calmar, R²) fail → ignore PnL. You can't trust a session you can't enter at random moments.

## Two warning combinations

| combo | what it means |
|---|---|
| High PnL + High WR + LOW R² (<0.6) | Gains came from 1-2 lucky windows. Not a strategy. |
| High R² + low Calmar (<2) | Line LOOKS straight but slope is so gentle any drawdown is significant. Straightness is a mirage. |

## Archetype taxonomy (from `session_scorecard.py`)

| archetype | profile |
|---|---|
| GRINDER | R² 0.90+, Cal 5+, WR 60-70%, $/tr $5-15, 6h+ 85%+ — reliable, boring, safe |
| LOTTERY-FADE | R² 0.85+, Cal 3+, WR **90%+**, $/tr $1-5, 6h+ 80%+ — few big losses, many small wins |
| HIGH-CONVICTION SNIPER | R² 0.80+, Cal 4+, WR 70-80%, $/tr **$30+**, 6h+ 75%+ — rare fires, each a winner |
| WORKHORSE ORACLE | R² 0.85+, Cal 3-5, WR 55-60%, $/tr $10-20, 6h+ 75-80% — standard alpha |
| VOLATILE WINNER | R² 0.70-0.85, Cal 2-3, WR 55-60%, $/tr $15-30, 6h+ 65-75% — profitable but bumpy |
| VARIANCE TRAP | R² 0.60-0.85, Cal **1-2**, WR 60%, $/tr $20+, 6h+ 60-70% — looks good, actually dangerous |
| LUCKY OUTLIER | R² 0.50-0.70, Cal 5+, WR 65%, $/tr $40+, 6h+ 60-70% — 1-2 huge trades, rest meh |
| RANDOM WALK | R² 0.0-0.5, Cal 1-2, WR 45-55%, $/tr $0-5, 6h+ 50-60% — no edge |
| BLEEDER | R² negative, Cal <1, $/tr negative — shut down |

## Current session archetypes (Apr 11 2026)

Based on 10 hours of post-NO-fix clean data:

| session | archetype | notes |
|---|---|---|
| test_ic_wide | **WORKHORSE** | only true 4/4 viable session |
| oracle_chrono | HIGH-CONVICTION SNIPER | $68/tr, R² 0.90, Cal 4.76 — top performer |
| oracle_arb | VOLATILE WINNER | $38/tr, R² 0.77, Cal 2.7 — unique mechanism |
| oracle_alpha | VARIANCE TRAP | $42/tr but Cal 1.3 — one drawdown will erase gains |
| oracle_consensus | LUCKY OUTLIER | R² 0.48, Cal 1.0 — PnL is from few good windows |
| oracle_1 | marginal | R² 0.35, Cal 1.2 — middle of the road |
| blitz_1 | marginal | R² 0.54, Cal 1.5 — give it 24h more |
| oracle_omega/titan/pivot_1 | too few trades to judge | just restarted |
| oracle_lazy/volume_shift_1/piggyback_1 | too few trades to judge | deployed tonight |

**Only 1 of 14 sessions passes the full viability bar.**
