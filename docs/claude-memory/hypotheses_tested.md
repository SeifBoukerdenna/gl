---
name: Hypotheses Tested in the Apr 10-11 Exploration
description: Every strategy hypothesis we tested, what we tried, what we found, what's validated or disproven. Use this to avoid re-testing the same ideas.
type: project
---

## Validated (tested and confirmed working)

### H1: Edge table V2 has real predictive power
- **Test:** oracle_1 baseline (edge table → fire on abs edge)
- **Result:** ~60% WR, +$15/tr across 100+ trades. Steady positive edge.
- **Status:** confirmed. Edge table is the foundation of most oracle-family architectures.

### H2: Chrono bucket blocks and boosts add real alpha
- **Hypothesis:** Specific (time_bucket × direction) cells are persistent winners/losers. Blocking losers and boosting winners improves risk-adjusted return.
- **Test:** oracle_chrono with blocks [60-90|NO, 5-30|NO, 270-300|NO] and boosts [210-240|NO, 150-180|YES, 180-210|YES] × 1.3×
- **Result:** $68/tr vs oracle_1's $15/tr, R² 0.90 vs 0.62. Rolling 12-trade blocks show 4 of 5 consecutive blocks profitable — not a single lucky hour.
- **Status:** confirmed. Chrono is "oracle_1 with selective leverage" — strictly better execution of the same underlying signal.

### H3: Mispricing arbitrage (table_wr vs market_wr) is a real edge
- **Hypothesis:** Fire only when historical win rate exceeds market-implied probability by ≥5pp.
- **Test:** oracle_arb
- **Result:** $38/tr (higher than oracle_1), 0.30 correlation with oracle_1 (genuinely different signal). Block bootstrap CI excludes $0. Monotonic delta-WR scaling: 0-2bp=52%, 2-5bp=52%, 5-10bp=40% (small n), 10-20bp=100% (tiny n).
- **Status:** confirmed. arb is the #1 diversification play in the portfolio.

### H4: Multi-venue CONSENSUS as a filter works (when done correctly with LEVEL check)
- **Hypothesis:** Require Coinbase/Bybit to agree with Binance on which side of the strike BTC is on. Filters single-venue spike artifacts.
- **V1 test (broken):** Used 5s momentum direction instead of level. Caused anti-selection on NO trades (47% NO WR).
- **V2 test (fixed):** Level-based check with hard veto on disagreement. Post-fix NO WR 63% — +16pp improvement from the fix alone.
- **Status:** V2 confirmed. V1 is obsolete and should never be used.

### H5: Late-window BTC autocorrelation → 90%+ WR
- **Hypothesis:** In the last 60 seconds of a window, if |delta| ≥ 5bp, BTC stays on that side ~90% of the time.
- **Test:** Kline counterfactual across 69 days (19,968 windows). Fire on T≤60, |delta|≥5, deterministic outcome from BTC at window close.
- **Result:** 13,890 fires, **91.7% WR**, 232 fires/day. WR scales monotonically with delta (0-3bp=58%, 3-6bp=70%, 6-12bp=82%, 12-25bp=91%, 25+bp=99%).
- **Status:** confirmed as a physical fact. BUT — see H11 for the entry price caveat that makes this hard to exploit.

### H6: Peer session confirmation would improve signal quality
- **Hypothesis:** If 3+ independent architectures agree on firing, the signal is higher quality.
- **Test:** Conceptual via correlation analysis + smooth_seeker architecture (not deployed yet)
- **Status:** unconfirmed — needs testing. smooth_seeker in `bot/architectures/` is a candidate but hasn't been run on recent data.

### H7: Halt + daily budget protects against regime shift
- **Hypothesis:** Rolling N-trade cumulative loss halt + daily loss ceiling catches bleed clusters early.
- **Test (backtest):** Applied to oracle_1's historical trades. Omega's validated config (8/-$400/60min) reduced bloodbath magnitude by ~70%.
- **Test (live, after fix):** Halt mechanism was BROKEN until Apr 10 (dict vs object access bug). Now fixed. Post-hoc simulator confirms it would work on high-variance sessions.
- **Status:** mechanism confirmed. BUT (see H12) — per-session tuning is required, no universal config.

### H8: Per-session halt tuning beats universal halt config
- **Hypothesis:** Different sessions need different halt lookback/threshold/cooldown. A universal default is suboptimal.
- **Test:** Grid sweep via `simulate_halts.py --sweep` on oracle_arb, oracle_1, oracle_chrono, test_ic_wide
- **Result:** Each session has a completely different optimum. oracle_chrono wants lookback=3 threshold=-$150 cooldown=30min. test_ic_wide wants lookback=3 threshold=-$400 cooldown=30min (much looser). oracle_1 wants lookback=12.
- **Status:** strongly confirmed. Per-session sweeping is the right approach.

## Disproven / failed

### D1: "Adding more oracle variants diversifies the portfolio"
- **Hypothesis:** Running oracle_1, oracle_chrono, oracle_consensus, oracle_echo, etc. gives diversification.
- **Result:** Correlations 0.75-0.95 within the oracle family. Running 6 oracle variants = running 1 bet 6 times.
- **Status:** disproven. Oracle variants are redundant for diversification purposes. Keep the best-executed one (chrono), drop the rest.

### D2: Trend-aligned sizing multiplier
- **Hypothesis:** Size trades by BTC trend direction — bigger when BTC trending in bet direction, smaller against.
- **Test:** `backtest_omega.py` with trend sizing overlay on oracle_1 trades
- **Result:** R² and Calmar got worse despite PnL being similar. Disproven on temporal monotonicity grounds.
- **Status:** disproven.

### D3: Continuous WR-adaptive sizing (oracle_streak concept)
- **Hypothesis:** Scale size by rolling WR — bigger when recent WR high, smaller when low.
- **Test:** `backtest_brainstorm.py` with strat_streak
- **Result:** Boosted PnL but WORSENED worst_6h (-$3,159 vs baseline -$2,889). Continuous sizing amplifies regime risk at the worst possible moment.
- **Status:** disproven. Soft scalers are NOT a substitute for hard halts.

### D4: First-half strike-crossing count predicts second-half direction
- **Hypothesis (user's idea):** If BTC crosses the strike multiple times in the first half, it signals chop and we can exploit it.
- **Test:** `cross_count_predict.py` on 69 days of klines (19,968 windows)
- **Result:** Cross count has only marginal predictive value. **Direction-at-half is the dominant signal regardless of chop count** — even at 13+ crossings (max chop), half-direction still wins 69% of the time. Zero crossings (trending) gives 78%.
- **Status:** disproven as a primary signal. Half-direction alone is simpler and nearly as good.

### D5: "Universal halt config works for all sessions"
- Already covered in H8 — disproven.

### D6: V3 edge table (with vol regime feature) is better than V2
- **Hypothesis:** Adding a vol regime feature to the edge table improves calibration.
- **Test:** `build_edge_table_v3.py`, compared Brier score vs V2
- **Result:** V3 was slightly WORSE. Feature doesn't help.
- **Status:** abandoned. V2 is the current edge table.

### D7: oracle_consensus V1 ("momentum-direction agreement") adds signal quality
- See H4 — V1 was anti-selecting good NO trades. V2 (level-based) is the correct implementation.

### D8: oracle_lazy V1 ("fixed 10s wait") is a meaningful persistence filter
- **User critique:** 10s is arbitrary. In calm markets it's too long; in volatile markets it's too short. Wall-clock time doesn't verify anything.
- **V2 design:** Vol-adaptive min wait + require 3 consecutive same-side 1s ticks AFTER the min wait. Max wait 25s. Edge must stay ≥80% of original.
- **Status:** V2 is the current correct design. V1 was naive.

### D9: "Add halts to all sessions now during exploration"
- **User decision:** No. Post-hoc simulation preserves data and lets us try any config later.
- **Status:** exploration mode decision — keep raw data, add halts only when going live.

## Untested / in progress

### U1: `piggyback.py` — trade WITH PM market makers
- Deployed Apr 10 ~02:00 UTC. Insufficient data.
- Tests if following PM MM confidence signals (tight spreads, depth shifts) is a cleaner signal than trying to front-run them.

### U2: `volume_shift.py` — Binance message rate surge as leading indicator
- Deployed Apr 10 ~02:00 UTC. Insufficient data.
- Tests if BTC tape rate 3-5× baseline over 2s predicts moves before PM reprices.

### U3: `oracle_lazy.py` V2 — vol-adaptive direction-stable persistence
- Deployed Apr 10 ~02:00 UTC. Insufficient data.
- Tests if filtering for "BTC stays on one side for 3 consecutive ticks after min wait" filters whipsaws better than fixed 10s wait.

### U4: `certainty_premium.py` — z>1.5 in final 30-60s, buy near-certain winners
- NOT YET DEPLOYED. Candidate for next deployment. Similar in spirit to late_convergence thesis (H5) but uses z-score trigger instead of raw delta.

### U5: `smooth_seeker.py` — 3+ peer architectures must agree
- NOT YET DEPLOYED. Uses blitz_1, test_ic_wide, etc. as voters. Meta-strategy.

### U6: `oracle_breaker.py` — edge table + adaptive sizing (halt at WR<45%, size down 55-62%, press at 70%)
- NOT YET DEPLOYED. Continuous version of omega halt. Might be better than binary halt.

## Meta-hypotheses (about the research process)

### M1: Halts should be tuned post-hoc, not pre-deployed
- **Argument:** You don't know the right config without data. Running without halts during exploration preserves the data you need to find the right config.
- **Status:** adopted as working principle.

### M2: Correlation preserves direction, not magnitude
- **Observation:** oracle_1 and oracle_chrono are 0.95 correlated AND chrono makes 3× more money. The correlation proves they're the same bet; the PnL difference proves chrono executes it better.
- **Implication:** when pruning, keep the best-PnL version of a correlated group, not an arbitrary one.
- **Status:** adopted as working principle.

### M3: Regime diversity > sample count
- **Argument:** 1000 trades in a calm regime tell you less than 500 trades that include a bloodbath.
- **Status:** adopted. Won't declare anything "deploy ready" until it survives a regime shift.

### M4: Structural risk protection (halt, daily budget, direction-WR floor) beats mechanism filters (multi-venue, vol gate, chrono blocks)
- **Argument:** The bloodbath was coordinated across venues and regimes. Multi-venue and vol gates would have LET the bloodbath trades through. Only halt/budget/WR-floor would have caught it.
- **Implication:** when designing defenses, prioritize rolling state-based halts over static filters.
- **Status:** adopted as design principle.
