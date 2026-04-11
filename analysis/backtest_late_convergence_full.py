"""
FULL counterfactual backtest of late_convergence using 60d of Binance klines.

Method
======
1. Walk every 5-minute PM window in the kline history
2. For each window, scan from window_open → window_close
3. Identify the FIRST moment where: T_remaining ≤ 60 AND |delta_bps| ≥ DELTA_MIN
4. Fire matching direction (YES if delta>0, NO if delta<0)
5. Determine outcome from BTC at window_close (deterministic, no PM book needed)
6. The WR is purely a function of BTC dynamics — no assumption about PM prices

Then we ask the SECOND question separately:
  Given that WR, what entry price do we need to make this profitable?

By splitting "outcome predictability" from "entry price assumption", we
isolate the structural finding from the implementation question.
"""

import csv
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path

DATA = Path("data")

WINDOW_SEC = 300
PM_WINDOW_BOUNDARY_SEC = 300  # PM windows align to every 5 minutes on the wall clock


def load_klines():
    df = pd.read_parquet("data/binance_klines_60d.parquet")
    df["ts_sec"] = (df["timestamp"] / 1000).astype(int)
    df = df.set_index("ts_sec").sort_index()
    return df


def simulate(klines, delta_min, t_max, t_min=5):
    """
    For each PM window in klines, find the first moment with
    T_remaining ≤ t_max AND T_remaining ≥ t_min AND |delta| ≥ delta_min.
    Fire matching direction. Return list of trade dicts.
    """
    fires = []
    closes = klines["close"]

    # Find all PM window boundaries
    first_ts = closes.index[0]
    last_ts = closes.index[-1]
    # Round first window start to next 5-min boundary
    win_start = ((first_ts // PM_WINDOW_BOUNDARY_SEC) + 1) * PM_WINDOW_BOUNDARY_SEC

    while win_start + WINDOW_SEC <= last_ts:
        win_end = win_start + WINDOW_SEC
        # Get strike (price at window_start)
        try:
            strike = float(closes.loc[win_start])
        except KeyError:
            win_start += WINDOW_SEC
            continue
        if strike <= 0:
            win_start += WINDOW_SEC
            continue

        # Scan window: find first moment in [t_min, t_max]
        # T_remaining at second t = win_end - t
        # We want win_end - t in [t_min, t_max] → t in [win_end - t_max, win_end - t_min]
        scan_start = win_end - t_max
        scan_end = win_end - t_min
        try:
            scan_slice = closes.loc[scan_start:scan_end]
        except KeyError:
            win_start += WINDOW_SEC
            continue

        fired = False
        for ts, price in scan_slice.items():
            delta_bps = (price - strike) / strike * 10000
            if abs(delta_bps) >= delta_min:
                # Fire here
                t_remaining = win_end - ts
                # Outcome: BTC at win_end vs strike
                try:
                    btc_close = float(closes.loc[win_end])
                except KeyError:
                    btc_close = float(closes.iloc[closes.index.get_indexer([win_end], method='nearest')[0]])
                outcome = "Up" if btc_close > strike else "Down" if btc_close < strike else "Flat"
                direction = "YES" if delta_bps > 0 else "NO"
                won = (direction == "YES" and outcome == "Up") or (direction == "NO" and outcome == "Down")
                fires.append({
                    "ts": ts,
                    "win_start": win_start,
                    "strike": strike,
                    "fire_btc": float(price),
                    "close_btc": btc_close,
                    "delta_bps": delta_bps,
                    "abs_delta": abs(delta_bps),
                    "t_remaining": t_remaining,
                    "direction": direction,
                    "outcome": outcome,
                    "won": won,
                })
                fired = True
                break

        win_start += WINDOW_SEC

    return fires


def pnl_at_entry(fires, entry_price, fee_rate=0.072):
    """
    Compute total PnL assuming a fixed entry price.
    A YES bet at entry_price wins (1 - entry) per share, loses entry per share.
    Notional = $200, shares = 200 / entry.

    Polymarket fee: applied to the *winnings*, not the notional. Approximated as
    fee_rate * entry * (1 - entry) per share.
    """
    notional = 200
    pnls = []
    for f in fires:
        shares = notional / entry_price
        if f["won"]:
            gross = (1 - entry_price) * shares
        else:
            gross = -entry_price * shares
        fee = entry_price * (1 - entry_price) * fee_rate * shares
        pnls.append(gross - fee)
    total = sum(pnls)
    return total, pnls


def hr(s):
    print()
    print("─" * 100)
    print("  " + s)
    print("─" * 100)


def main():
    print("\n  Loading 60d of 1-second BTC klines...")
    klines = load_klines()
    print(f"  Loaded {len(klines):,} ticks "
          f"({datetime.utcfromtimestamp(klines.index[0])} → {datetime.utcfromtimestamp(klines.index[-1])})")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass A: Sweep delta thresholds, T_max=60s, observe WR")
    print()
    print(f"  {'delta_min':<12} {'fires':<8} {'WR':<10} {'avg |delta|':<14} {'fires/day':<10}")
    print("  " + "-" * 60)

    days = (klines.index[-1] - klines.index[0]) / 86400
    results = {}
    for delta_min in [2, 3, 5, 8, 10, 15, 20]:
        fires = simulate(klines, delta_min, t_max=60)
        if not fires:
            continue
        wr = sum(1 for f in fires if f["won"]) / len(fires) * 100
        avg_d = sum(f["abs_delta"] for f in fires) / len(fires)
        per_day = len(fires) / days
        results[delta_min] = fires
        print(f"  ≥{delta_min}bp{'':<7} {len(fires):<8} {wr:>7.1f}%   {avg_d:>9.1f} bp     {per_day:>6.1f}")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass B: Sweep T_max with delta=5, observe WR vs trade volume")
    print()
    print(f"  {'T_max':<8} {'fires':<8} {'WR':<10} {'fires/day':<10}")
    print("  " + "-" * 50)
    for t_max in [15, 30, 45, 60, 90, 120]:
        fires = simulate(klines, delta_min=5, t_max=t_max)
        if not fires: continue
        wr = sum(1 for f in fires if f["won"]) / len(fires) * 100
        per_day = len(fires) / days
        print(f"  ≤{t_max}s{'':<3} {len(fires):<8} {wr:>7.1f}%   {per_day:>6.1f}")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass C: Profitability at different assumed entry prices")
    # Use the strongest signal: delta>=5, T<=60
    fires = results.get(5) or simulate(klines, 5, 60)
    n = len(fires)
    wins = sum(1 for f in fires if f["won"])
    wr = wins / n * 100
    print(f"\n  Test set: delta≥5, T≤60   →  n={n}  WR={wr:.1f}%")
    print()
    print(f"  {'entry':<8} {'$/trade':<12} {'total PnL':<14} {'profitable?':<12}")
    print("  " + "-" * 50)
    for entry in [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.92]:
        total, pnls = pnl_at_entry(fires, entry)
        per_tr = total / n
        sym = "YES" if total > 0 else "no"
        print(f"  {entry*100:>5.0f}c   ${per_tr:>+8.2f}    ${total:>+10,.0f}    {sym}")

    # Break-even entry price (binary search)
    lo, hi = 0.50, 0.99
    for _ in range(40):
        mid = (lo + hi) / 2
        total, _ = pnl_at_entry(fires, mid)
        if total > 0:
            lo = mid
        else:
            hi = mid
    print(f"\n  → Break-even entry price: {lo*100:.1f}c")
    print(f"  → Strategy is profitable as long as the market gives us better than {lo*100:.1f}c on average")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass D: WR by |delta| bucket (does the convergence strengthen with delta?)")
    fires = simulate(klines, delta_min=2, t_max=60)
    print()
    print(f"  {'delta range':<14} {'n':<7} {'WR':<10}")
    print("  " + "-" * 40)
    for lo, hi in [(2, 3), (3, 5), (5, 8), (8, 12), (12, 20), (20, 50), (50, 999)]:
        sub = [f for f in fires if lo <= f["abs_delta"] < hi]
        if not sub: continue
        wr = sum(1 for f in sub if f["won"]) / len(sub) * 100
        print(f"  {lo}-{hi if hi<999 else '∞'}bp{'':<8} {len(sub):<7} {wr:>7.1f}%")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass E: WR by T_remaining bucket (does later = more deterministic?)")
    fires = simulate(klines, delta_min=5, t_max=120)
    print()
    print(f"  {'T range':<12} {'n':<7} {'WR':<10}")
    print("  " + "-" * 40)
    for lo, hi in [(5, 15), (15, 30), (30, 45), (45, 60), (60, 90), (90, 120)]:
        sub = [f for f in fires if lo <= f["t_remaining"] < hi]
        if not sub: continue
        wr = sum(1 for f in sub if f["won"]) / len(sub) * 100
        print(f"  T-{lo}-{hi}s{'':<5} {len(sub):<7} {wr:>7.1f}%")

    # ═══════════════════════════════════════════════════════════════
    hr("Verdict")
    fires_strong = results.get(5) or simulate(klines, 5, 60)
    n = len(fires_strong)
    wr = sum(1 for f in fires_strong if f["won"]) / n * 100
    per_day = n / days

    # Profitability at the empirical 75c entry from oracle_1
    total_75, _ = pnl_at_entry(fires_strong, 0.75)
    total_80, _ = pnl_at_entry(fires_strong, 0.80)
    total_85, _ = pnl_at_entry(fires_strong, 0.85)

    print(f"\n  Counterfactual: fire on EVERY (T≤60, |delta|≥5) moment over 60 days")
    print(f"  Trades:        {n:,}  ({per_day:.0f} per day)")
    print(f"  Win rate:      {wr:.1f}%")
    print(f"  At entry=75c:  ${total_75:+,.0f}  total ({total_75/n:+.2f}/trade)")
    print(f"  At entry=80c:  ${total_80:+,.0f}  total ({total_80/n:+.2f}/trade)")
    print(f"  At entry=85c:  ${total_85:+,.0f}  total ({total_85/n:+.2f}/trade)")
    print()
    if total_80 > 0:
        print(f"  → STRONG: profitable even at conservative 80c entry")
    elif total_75 > 0:
        print(f"  → MODERATE: profitable at empirical 75c, fragile to entry assumption")
    else:
        print(f"  → WEAK: WR not high enough to overcome typical entry costs")


if __name__ == "__main__":
    main()
