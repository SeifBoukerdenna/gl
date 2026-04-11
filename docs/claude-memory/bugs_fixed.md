---
name: Bugs Fixed in This Session
description: Critical bugs found and fixed on Apr 10-11 2026 — NO sizing, halt mechanism, overnight skip, momentum lookup, consensus V2. Context behind each and the fix.
type: project
---

## 1. NO sizing bug (CRITICAL — systemic, affected all architectures)

**Location:** `bot/paper_trade_v2.py` — `Combo.compute_position_size` used `dollars / entry_price` as share calculation. For YES trades this is correct (entry_price = cost per share). For NO trades, `entry_price` was the YES bid, but the actual NO cost per share is `(1 - YES_bid)`.

**Impact:** NO trades were systematically oversized. At YES_bid = 0.13 (very favored NO), the bot bought ~2× the intended notional. Over-sizing factor scaled from 1× (at YES_bid = 0.50) up to ~2× (at MAX_SHARES cap at YES_bid ≤ 0.20). Every NO trade since the bot's existence was either correctly sized, under-sized, or over-sized depending on YES bid.

**Effect:** inflated PnL magnitudes in BOTH directions on NO trades. The oracle bloodbath was amplified. Validation metrics (R², Calmar, worst_6h) were tuned to inflated data. Validated halt parameters (e.g., omega's -$400/8trades) were wrong for clean data.

**Fix (Apr 10 ~16:36 UTC):** in `execute_paper_trade`, transform entry_price for NO direction before calling compute_position_size:
```python
cost_per_share = (1.0 - entry_price) if direction == "NO" else entry_price
trade_size = combo.compute_position_size(cost_per_share, override_dollars)
```

**Post-fix:** nuked and restarted ALL 13 sessions. Lost all pre-fix trade history for clean-slate data.

## 2. oracle_omega and pivot_1 halt mechanism was DEAD

**Location:** Both files used `getattr(tr, "settled", False)` and `getattr(tr, "pnl_taker", 0.0)` on `combo.trades` items. But `combo.trades` is a list of **dicts**, not objects. `getattr(dict, key, default)` always returns the default.

**Impact:** The `_recent_pnl` deque was NEVER populated. The halt condition `sum(_recent_pnl) < -$400` was never true. Omega's signature "rolling halt" protection was silently doing nothing since deployment. Same for pivot_1 (I copied the broken pattern when building it).

**Fix (Apr 10):** use `combo.total_trades` as monotonic counter (survives the 50-trade cap in `combo.trades`), slice with `combo.trades[-new_count:]`, access via `t.get("pnl_taker", 0.0)`. Pattern borrowed from `oracle_alpha.py:258` which uses it correctly.

**Note:** `oracle_alpha.py` and `oracle_breaker.py` use the correct `t.get(...)` pattern — their halts work. Only omega and pivot_1 had the bug.

## 3. Momentum lookup bug (minor — only affects warmup)

**Location:** 12+ architecture files used the pattern:
```python
target_ts = time.time() - 30
for ts, px in state.price_buffer:
    if ts >= target_ts:
        price_30ago = px
        break
```

**Impact:** The loop picks the FIRST tick newer than 30s ago. With a full 120-tick buffer (at 1Hz), this is approximately correct (tick ~29-30s old). But after websocket reconnect or fresh session start, the buffer is thin and the loop picks much-too-recent prices (e.g., 5s old). Momentum is then computed over 5s of noise instead of 30s of trend, causing wrong edge-table cell lookups during warmup.

**Fix (Apr 10):** iterate oldest-to-newest, keep the LATEST tick still ≤ target_ts, break past target:
```python
target_ts = time.time() - 30
price_30ago = None
for ts, px in state.price_buffer:
    if ts <= target_ts:
        price_30ago = px  # keep the LATEST tick still ≥ 30s old
    else:
        break
```
If the buffer doesn't go back 30s, price_30ago stays None and momentum defaults to "flat".

**Applied to:** oracle.py, oracle_alpha.py, oracle_arb.py, oracle_chrono.py, oracle_consensus.py, oracle_echo.py, oracle_omega.py, oracle_lazy.py, oracle_lazy_reverse.py, oracle_pulse.py (had 2 instances), oracle_breaker.py, smooth_seeker.py, bot/paper_trade_v2.py (legacy path).

## 4. oracle_consensus V1 used MOMENTUM instead of LEVEL for the "consensus" gate

**Location:** `oracle_consensus.py` V1 called `_get_exchange_momentum(exchange, 5)` which returns 5-second price delta in bps. Then checked if that delta was in the same direction as the bet. This is a momentum continuation filter, not a level-consensus filter.

**Impact:** The architecture was supposed to verify "do other venues also show BTC on the same side of the strike?" (level-based — catches single-venue artifacts). Instead it verified "are other venues moving in the same direction RIGHT NOW?" (momentum-based — forces momentum-continuation regime, anti-selects good NO trades).

**Evidence:**
- Pre-fix NO WR: 47%
- Post-fix NO WR: 63%
- +16pp improvement from the same architecture, same edge table

**Fix (Apr 10):** rewrote `_check_level_consensus` to compare each venue's LATEST price against the strike directly. Added staleness check (reject venue prices older than 3s), abstain zone (±0.5bp of strike), and hard veto on disagreement (any venue showing opposite side blocks the trade).

## 5. Overnight skip was using UTC instead of ET (omega/titan/pivot_1)

**Location:** Three files had:
```python
hour_et = datetime.now().hour  # ← variable named "et" but returns LOCAL time
```
VPS is on UTC. So `hour_et` was actually `hour_utc`. The filter "skip 0-5" was blocking 0-5 UTC = 8 PM – 1 AM ET (during DST), the prime US evening hours instead of the intended 12-5 AM ET overnight low-liquidity period.

**Symptom:** After the NO-sizing nuke at 16:52 UTC (still hour 16, filter not active), sessions fired normally. After the second nuke at 00:20 UTC (hour 0, filter tripped), omega/titan/pivot_1 were silent for 2+ hours. I initially misdiagnosed this as a filter-starvation issue.

**Fix (Apr 11 ~02:10 UTC):** use `zoneinfo.ZoneInfo("America/New_York")`:
```python
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None
# ...
hour_et = datetime.now(_ET).hour if _ET else datetime.utcnow().hour
```

## Meta-pattern: I keep introducing bugs in features I add

The halt, NO sizing (existing, didn't add but didn't catch), and overnight skip bugs were all code paths I was either writing or modifying without sanity-checking basic assumptions (dict vs object access, YES vs NO convention, UTC vs ET).

**Lesson:** after ANY edit to an architecture file, run a 30-90 second foreground smoke test (not just `systemctl is-active`). `systemctl is-active` only tells you the process didn't crash — it doesn't tell you if check_signals is silently erroring or filtering everything.

Foreground test pattern:
```bash
ssh root@167.172.50.38 '
  systemctl stop polymarket-bot@<session>
  sleep 1
  cd /opt/polymarket-bot
  timeout 90 /opt/polymarket-bot/.venv/bin/python -u bot/paper_trade_v2.py --instance <session> 2>&1 | tail -40
  systemctl start polymarket-bot@<session>'
```
