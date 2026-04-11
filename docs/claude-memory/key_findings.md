---
name: Key Findings from Apr 10-11 Exploration Session
description: The insights we've discovered, correlation analysis, chrono's real mechanism, portfolio truths, time-of-day patterns, halt-sweep results. What's actually working and why.
type: project
---

## The big findings

### 1. The portfolio is NOT diversified
**Effective N = 1.6 out of 8 sessions with enough data.** Running 14 sessions gives the diversification of ~2 independent bets. 80% of the portfolio is redundant.

### 2. The oracle family is 6 variations of ONE bet
Correlation matrix on post-NO-fix clean data:
- oracle_1 ↔ oracle_chrono = **+0.95**
- oracle_1 ↔ oracle_consensus = **+0.89** (even after V2 fix)
- oracle_chrono ↔ oracle_consensus = +0.75
- oracle_chrono ↔ oracle_echo = +0.94
- oracle_1 ↔ oracle_echo = +0.93

They all share the edge table. Adding more oracle variants = running the same bet more times, not diversifying.

### 3. oracle_arb is genuinely independent
Correlation with oracle_1 = **+0.30**. Its mispricing detection mechanism (fire when `table_wr - market_wr ≥ 5pp`) is structurally different from "fire on absolute edge over breakeven." arb is the ONLY "profitable" session with low correlation to the oracle family.

### 4. blitz_1 and test_ic_wide are independent from EACH OTHER
- oracle_1 ↔ blitz_1 = +0.47
- oracle_1 ↔ test_ic_wide = +0.58
- **blitz_1 ↔ test_ic_wide = +0.18** ← almost uncorrelated

They're both "multi-venue" architectures but fire on completely different conditions. These two are the most diversification-dense pair in the portfolio.

### 5. The NO sizing bug was adding UNCORRELATED noise
Pre-fix avg correlation: +0.44-0.49. Post-fix: +0.56. The bug was making the portfolio look more diverse than it actually was (noise = variance = apparent independence). With clean data, the underlying mechanism correlation is more obvious.

### 6. Correlation ≠ redundancy in PnL terms
oracle_1 and oracle_chrono are 0.95 correlated AND chrono makes 3x more money ($5.1k vs $1.9k). This is NOT a contradiction:
- Correlation measures DIRECTION of hourly PnL changes
- Magnitude is independent

Chrono = oracle_1 + (a) skip historically bad cells + (b) 1.3× size on historically good cells. Same direction at same times (high correlation), bigger magnitude (higher PnL). chrono is strictly better — **it's the same bet executed more efficiently.** The correct interpretation: if you have to pick one of the oracle variants, pick chrono.

### 7. Single-hour PnL concentration
**22 UTC (2 PM ET) produced ~50% of total portfolio PnL** across 14 sessions in 10 hours. That hour had a BTC -41 bps drop. 8 of 14 sessions participated with average +$109/trade and 79.8% WR.

**Other dead zones:**
- 20 UTC (4 PM ET): -$1,007, 50% WR, 7 sessions bleeding
- 23 UTC (7 PM ET): -$1,357, 50% WR, 8 sessions bleeding
- Together: -$2,364 of correlated losses

**Peak hour diversity: 36% (LOW).** 5 of 14 sessions peak at 22 UTC, 3 more at 02 UTC, 4 more at 03 UTC. Most sessions make money in the same 22-03 UTC window (6 PM - 11 PM ET).

### 8. "Multi-venue confirmation" is NOT enough protection against a regime shift
The bloodbath on Apr 9 was coordinated across all venues (not a single-venue artifact). Multi-venue consensus filters pass coordinated bleeds. The only defenses against regime shifts are:
1. Rolling cum-loss halt (catches bleed early)
2. Daily loss budget (hard ceiling)
3. Direction-aware WR floor (notices regime change via realized WR)

Of those, only alpha has all three. omega, titan, pivot_1 have halt + budget but not direction WR floor. The other 10 sessions have NO regime-shift protection.

### 9. Chrono's edge scaling is monotonic and real
WR by half-window delta on oracle trades, post-NO-fix:
- 2-4bp: 69% WR
- 4-6bp: **87%** WR
- 6-9bp: **92%** WR

This is the persistence finding: BTC at 6+bp from strike at T 90-180s stays there ~80-90% of the time. Validated across 19,968 windows in 69 days of kline data (92% WR in the 6-12bp band).

### 10. late-window convergence (90%+ WR) exists but is unexploitable for us
Kline counterfactual across 69 days: fire on T≤60, |delta|≥5 → **91.7% WR** over 13,890 fires. BUT empirical entry-price analysis showed:
- Late-window entries where our bot fired cluster at 75-92c (not below 60c)
- Break-even at the median empirical 67c entry requires ≥76% WR
- At 85-92c entries the WR is 100% but volume is tiny (15 trades over months of data)
- The "obvious" fire points are already priced in by the market — you pay for the convergence

## Halt simulator findings (post-hoc analysis)

**Key insight: halt configs are NOT transferable between sessions.** Each session needs its own tuning:

| session | optimal halt | result |
|---|---|---|
| oracle_arb | 5 / -$150 / 30min / -$1000 | $3.8k → **$5.5k** (+$1.8k, R² 0.77→0.86, Cal 2.1→5.9) |
| oracle_1 | **12** / -$150 / 60min | $1.3k → **$3.0k** (+$1.7k, R² 0.35→0.89, Cal 1.2→7.3) |
| oracle_chrono | **3** / -$150 / 30min | $4.0k → **$5.8k** (+$1.8k, R² 0.85→**0.96**, Cal 3.7→**10.5**) |
| test_ic_wide | 3 / **-$400** / 30min | $1.5k → $1.7k (marginal, R² stays 0.87) |

**Default "one size fits all" halt config (5/-$200/90min) was WRONG for every session:**
- Too tight for test_ic_wide (-$1.3k cost)
- Too loose for oracle_chrono (missed $1.8k)
- Wrong lookback for oracle_1 (needed 12 trades, not 5)

**Conclusion:** post-hoc simulator is the correct way to tune halts per session. Never deploy a universal halt config.

## Time-of-day heatmap findings

| metric | finding |
|---|---|
| Peak hour | 22 UTC (2 PM ET) = +$9,154 across 84 trades at $109/trade |
| Best for chrono | 1 UTC (9 PM ET) = +$189/trade (different from cluster) |
| Best for alpha | 2 UTC (10 PM ET) = +$176/trade (different from cluster) |
| Worst hours | 20 UTC (4 PM ET) and 23 UTC (7 PM ET) |
| Overnight skip (0-5 ET / 4-9 UTC) | **Poorly positioned — real dead zones are 16 ET and 19 ET, both INSIDE business hours** |

## What's working and what's not

### Working (strong evidence)
- **Edge table V2** — produces ~60% WR on oracle_1 baseline, validates the statistical approach
- **Chrono bucket biases** — the 3 blocked + 3 boosted cells lifted chrono to top performer
- **oracle_arb's mispricing detection** — 0.30 correlation with oracle_1, +$38/tr, mechanism is structurally unique
- **test_ic_wide's impulse+multi-venue** — only session passing 4/4 viability in clean data
- **omega/titan halt mechanism (now that it's fixed)** — simulator confirms it works on the sessions it's tuned for

### Not working / still uncertain
- **oracle_titan** — over-filtered kitchen sink, vol gate silences it in low-vol regimes, bleeds in current conditions
- **pivot_1** — delta ≥ 5 is too strict for calm markets, barely fires
- **volume_shift_1 / piggyback_1 / oracle_lazy** — just deployed, no meaningful data yet
- **Universal halt defaults** — they don't exist; each session needs custom tuning

### Known but unexploited insights
- Late-window convergence has real signal but entry costs are usually too high
- Order-book imbalance / flow-front / piggyback mechanisms are orthogonal but untested
- Time-of-day filters could prevent ~15% of losses if pointed at the right hours (20 UTC, 23 UTC currently look bad)
