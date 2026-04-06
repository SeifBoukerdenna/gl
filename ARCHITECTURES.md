v# Polymarket 5-Min BTC Bot — Architecture Guide

## Market Structure

Polymarket runs 5-minute binary markets on BTC: "Will BTC be above $X at the end of this window?" Every 5 minutes, a new window opens with a new strike price (the "price to beat" from Chainlink). At settlement, if BTC is above the strike → YES wins ($1/share), otherwise NO wins ($1/share). The losing side gets $0.

The bot monitors Binance BTC/USDT for real-time price data and Polymarket's order book via WebSocket. All trades are paper-traded with realistic execution simulation (orderbook walk, fees, slippage).

**Price convention:** The bot records all prices as YES token prices. For NO trades, actual cost = 1 - recorded price. A "fill at 30c" on a NO trade means you paid 70c per share for the NO token.

**Settlement:** PM settles based on book mid > 0.50 at window close. The reference price is Chainlink BTC/USD, NOT Binance. There's a ~$55 offset between Binance and Chainlink. The bot tracks this offset.

---

## Architecture 1: impulse_lag (the original, proven strategy)

**Edge:** Binance BTC/USDT leads Polymarket's order book by 2-10 seconds. When BTC moves sharply, PM's market makers haven't repriced yet.

**How it works:**
- Monitors Binance tick-by-tick for "impulses" — sharp BTC moves (>5 basis points in 10-30 seconds)
- When an impulse is detected, immediately buys that direction on PM before the book catches up
- Example: BTC jumps $40 in 15 seconds → buy YES on PM while the YES token is still cheap

**When it fires:** Mid-window (T-210 to T-90 for the optimized configs), when a fresh Binance impulse occurs.

**Key filters:**
- Actual entry cost 65-80c (you're buying the side PM already favors — the mispricing is PM hasn't fully repriced)
- Fast lookback combos only (10s, 15s, 30s) — 60s lookbacks detect stale signals PM already absorbed
- Impulse cap at 15bp — extreme moves often reverse

**Combos:** A_5bp_30s, B_7bp_30s, C_10bp_30s, E_5bp_15s, F_7bp_15s, H_5bp_10s (threshold_lookback)

**Sessions:**
- `champion_25_45` — proven best config from overnight testing (entry 25-45c YES price)
- `down_bias_25_55` — wider entry range, captures both cheap and mid-price
- `tight_core_30_45` — tighter entry, removes 25-30c risk zone
- `trend_rider` — uses spread/cooldown instead of dead zone
- `hc_core` — high conviction: actual cost 65-80c, T>210s, best combos only
- `hc_fast_only` — only 10s and 15s lookback combos (highest win rates)
- `hc_wider` — slightly wider entry (60-85c actual cost) for more volume

---

## Architecture 2: delta_sniper

**Edge:** Historical data (13,856 windows) tells us the exact probability of UP for any given (BTC delta, time remaining) pair. When PM's book price deviates from this known fair value, there's a mispricing.

**How it works:**
- Maintains a lookup table: for each combination of "how far BTC is from the strike" and "how much time is left", we know the historical win rate
- Every second, computes the current delta bucket and time bucket, looks up the fair probability
- Compares fair probability to PM's current book price
- When PM's price is 2-5c below fair value → buy the underpriced side

**When it fires:** Any time during the window when PM's book lags the delta table's fair value.

**Key difference from impulse_lag:** Doesn't need a sudden BTC move. Works on static mispricings — BTC hasn't moved at all, but at T-60s with +5bp delta, fair value is 82% and PM is quoting 76c.

**Combos:** DS_2c_gap, DS_3c_gap, DS_5c_gap, DS_3c_strong, DS_5c_strong, DS_2c_extreme (gap size + conviction)

**Sessions:**
- `ds_core` — 2c+ gaps, broader coverage

---

## Architecture 3: gamma_snipe

**Edge:** In the last 60 seconds of a window, the contract's "gamma" (sensitivity of fair value to BTC price) explodes. A tiny BTC move shifts fair value dramatically, but PM's book doesn't reprice fast enough.

**How it works:**
- Computes a proper probability model: P(UP) = Φ((BTC - strike) / (σ × √T)) where σ is realized BTC volatility
- Only active in the last 60 seconds of each window
- Compares the model's fair value to PM's book price
- Trades when the gap exceeds 2-5 cents

**When it fires:** Last 60 seconds of every window. Works especially well when BTC is flat — low realized volatility means even a 1-2bp delta becomes near-certain at T-15s, but PM's book still quotes 70-80c.

**Why it's regime-independent:** In flat markets, σ is tiny, so even small deltas produce extreme probabilities. The model says "95% UP" but PM quotes 80c → 15c edge.

**Combos:** GS_2c_10s through GS_8c_60s (gap size + time window)

**Sessions:**
- `gs_core` — 2-3c gaps in last 30-60s
- `gs_wide` — all 8 combos for maximum coverage

---

## Architecture 4: lottery_fade

**Edge:** Favorite-longshot bias. When the outcome is near-certain (z-score > 2), the losing token should be worth 2-4c. It regularly trades at 8-15c because retail buys lottery tickets hoping for a reversal.

**How it works:**
- Computes z-score: how many standard deviations BTC is from the strike, adjusted for remaining time
- When z > 2 and the losing token is overpriced by 3c+ relative to its fair value → buy the winning side
- This is equivalent to "selling" the overpriced lottery ticket

**When it fires:** Last 120 seconds of the window, when BTC is comfortably on one side of the strike.

**Risk profile:** Wins ~95% of the time with small gains (buying 90-95c winners that settle at $1). Loses big on the rare reversal. The edge is the spread between fair loser price (2-4c) and market loser price (8-15c).

**Combos:** LF_z2_120s, LF_z2_90s, LF_z2.5_120s, LF_z3_120s, LF_z2_60s (z-threshold + time window)

**Sessions:**
- `lf_base` — all 5 combos
- `lf_tight` — high conviction only (z>2.5 or last 60s)

---

## Architecture 5: overreaction_fade

**Edge:** In the first 60 seconds of a window, PM's book overreacts to BTC moves. A $30-40 BTC move shifts the true probability by only a few percent (4+ minutes of random walk remain), but PM's book moves 15-20 cents. Short-term BTC returns have near-zero autocorrelation — minute 1 doesn't predict minutes 2-5.

**How it works:**
- At window open, snapshots BTC price and PM book state
- Monitors BTC and PM for the first 60 seconds
- When BTC moves >$30 AND PM's price shift is >2x the model's fair shift → buy the hammered side
- Example: BTC drops $40, PM YES goes from 50c to 30c. Model says fair shift is only 7c (50c to 43c). Overreaction ratio = 20c/7c = 2.9x. Buy YES.

**When it fires:** First 60 seconds of the window only. Anti-correlated with impulse_lag (they buy the dominant side, this buys the hammered side).

**Key insight:** This only fires when PM overreacts relative to what the model says is justified. If BTC moves $100 and PM moves 30c, that might be a fair repricing — the model checks.

**Combos:** OF_30usd_60s, OF_50usd_60s, OF_30usd_45s, OF_30usd_60s_3x, OF_30usd_60s_vol (move threshold + time + overreaction ratio + volume filter)

**Sessions:**
- `of_base` — all 5 combos
- `of_filtered` — volume-filtered + big moves only (skip potential real news)

---

## Architecture 6: certainty_premium

**Edge:** In the final 30-60 seconds, when the outcome is near-certain (z > 1.5), the winning token trades at 90-94c instead of 97-99c. Market makers pull quotes near expiry creating a liquidity vacuum. Buy the near-certainty at a discount.

**How it works:**
- In the last 60 seconds, computes z-score
- When z > 1.5 AND the winning token costs less than 95c → buy it
- You're paying 90-94c for something that settles at $1 in 30-60 seconds
- 6% return in 60 seconds at 98% probability

**When it fires:** Last 60 seconds only, when outcome is near-certain but market maker liquidity has thinned.

**Difference from lottery_fade:** Lottery_fade looks at the loser being overpriced. Certainty_premium looks at the winner being underpriced. Same z-score conditions but different entry logic and different combos.

**Combos:** CP_z1.5_60s_95c, CP_z2_60s_95c, CP_z1.5_30s_95c, CP_z1.5_60s_92c, CP_z2_30s_93c (z-threshold + time + max price)

**Sessions:**
- `cp_base` — all 5 combos
- `cp_safe` — ultra safe (z>2 + short time only)

---

## Architecture 7: delta_sniper (disabled variants)

Additional architectures that were tested but stopped to save VPS memory:

- **book_fade** — Fades PM orderbook imbalance when BTC hasn't moved enough to justify it
- **settlement_drift** — Trades late-window 0.50 crossovers and early-window momentum carry
- **micro_scalp** — Combines weak signals (delta + imbalance + fair gap + impulse) for high frequency

These can be re-enabled by starting their sessions: `systemctl start polymarket-bot@bf_test` etc.

---

## Shared Infrastructure

All architectures share:
- **Binance WebSocket** — real-time BTC/USDT tick stream
- **PM WebSocket** — real-time order book updates
- **Execution engine** — `walk_book()` for realistic fills, fee computation, slippage tracking
- **Settlement** — `settle_window()` resolves trades at window close
- **Volatility module** (`bot/shared/volatility.py`) — rolling realized vol from Binance ticks, z-score computation, fair value probability model. Used by lottery_fade, overreaction_fade, certainty_premium, and gamma_snipe.
- **CSV logging** — every trade tagged with `architecture` column
- **Stats JSON** — live session state for the dashboard

---

## When Each Architecture Fires (Timeline Within a 5-Min Window)

```
T-300 ─── Window Opens ───────────────────────────────────────
  │
  │  overreaction_fade (first 60s only)
  │  "PM overreacted to the opening BTC move — fade it"
  │
T-240 ─────────────────────────────────────────────────────────
  │
  │  impulse_lag / delta_sniper (mid-window)
  │  "Binance just moved, PM hasn't caught up"
  │  "PM's price doesn't match the delta table"
  │
T-120 ─────────────────────────────────────────────────────────
  │
  │  lottery_fade (last 120s)
  │  "The losing token is a lottery ticket — it's overpriced"
  │
T-60  ─────────────────────────────────────────────────────────
  │
  │  gamma_snipe + certainty_premium (last 60s)
  │  "Fair value is extreme but PM hasn't converged"
  │  "The winning token is a near-certainty at a discount"
  │
T-0   ─── Settlement ─────────────────────────────────────────
```

---

## How to Monitor

```bash
# Dashboard (live)
pm ui                              # opens http://localhost:5555

# Tail any session's logs
pm logs SESSION_NAME               # auto-detects local vs VPS

# Pull data and run full analysis
pm pull && pm analyze --all        # generates terminal output + HTML report + CSV report

# Check VPS health
pm status                          # or use the Status tab in the dashboard
```
