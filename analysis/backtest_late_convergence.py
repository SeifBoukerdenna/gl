"""
Backtest the late_convergence hypothesis.

Hypothesis: in the last 60 seconds of a 5-min window, if BTC is meaningfully
far from strike, the outcome is essentially deterministic. PM sometimes prices
the certain side at a discount because retail traders aren't actively quoting
the last minute. Buying the certain side at any discount is free money.

This is COMPLETELY ORTHOGONAL to oracle:
  - No edge table
  - No statistical patterns
  - No momentum / mom_type
  - The signal is a directly observable physical fact (BTC vs strike, time left)
  - Latency-tolerant (10-30 second convergence lag, not ms)

Pass 1: Look at oracle_1's existing late-window fires. Even though oracle_1
has its own filters, this gives us empirical $/trade and WR for trades that
happen to land in this regime. If the regime is profitable, the next pass
will simulate firing on EVERY moment that meets our condition (using Binance
klines + a simple entry-price model).
"""

import csv
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data")


def load_trades(name):
    p = DATA_DIR / name / "trades.csv"
    out = []
    with open(p) as f:
        for row in csv.DictReader(f):
            try:
                out.append({
                    "ts": float(row["timestamp"]),
                    "pnl": float(row["pnl_taker"]),
                    "result": row["result"],
                    "direction": row["direction"],
                    "delta_bps": float(row.get("delta_bps", 0)),
                    "abs_delta": abs(float(row.get("delta_bps", 0))),
                    "time_remaining": float(row["time_remaining"]),
                    "fill_price": float(row["fill_price"]),
                    "filled_size": float(row.get("filled_size", 0)),
                    "outcome": row.get("outcome", ""),
                    "btc_price": float(row.get("btc_price", 0)),
                })
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


def hr(s):
    print()
    print("─" * 100)
    print("  " + s)
    print("─" * 100)


def main():
    trades = load_trades("oracle_1")
    print(f"\n  Loaded {len(trades)} oracle_1 trades")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass 1: Empirical late-window edge (using oracle_1's actual fires)")
    # For each (time_window, delta_threshold) combination, compute WR/PnL/n
    print()
    print(f"  {'T cap':<8} {'delta':<8} {'n':<5} {'wr':<8} {'pnl':<10} {'$/tr':<10} {'avg entry':<12}")
    print("  " + "-" * 70)

    sweep = []
    for t_cap in [30, 45, 60, 90, 120]:
        for delta_min in [0, 2, 3, 5, 8, 12]:
            sub = [t for t in trades if t["time_remaining"] <= t_cap
                   and t["abs_delta"] >= delta_min]
            if len(sub) < 5:
                continue
            n = len(sub)
            pnl = sum(t["pnl"] for t in sub)
            wins = sum(1 for t in sub if t["pnl"] > 0)
            wr = wins / n * 100
            # YES trades use fill_price, NO trades use 1-fill_price
            avg_true_entry = sum(
                t["fill_price"] if t["direction"] == "YES" else 1 - t["fill_price"]
                for t in sub
            ) / n
            sweep.append((t_cap, delta_min, n, wr, pnl, pnl/n, avg_true_entry))
            print(f"  T<={t_cap:<5} ≥{delta_min}bp   {n:<5} {wr:>6.1f}% ${pnl:>+8,.0f}  ${pnl/n:>+7.2f}  {avg_true_entry*100:>9.1f}c")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass 2: WR by delta bucket inside the late window (T<=60)")
    late = [t for t in trades if t["time_remaining"] <= 60]
    print(f"\n  Late-window n={len(late)}")
    print()
    print(f"  {'delta range':<14} {'n':<5} {'wr':<8} {'pnl':<10} {'$/tr':<10} {'avg entry':<12}")
    print("  " + "-" * 65)
    buckets = [(0, 1), (1, 2), (2, 3), (3, 5), (5, 8), (8, 12), (12, 20), (20, 999)]
    for lo, hi in buckets:
        sub = [t for t in late if lo <= t["abs_delta"] < hi]
        if not sub: continue
        n = len(sub)
        pnl = sum(t["pnl"] for t in sub)
        wins = sum(1 for t in sub if t["pnl"] > 0)
        wr = wins / n * 100
        avg_entry = sum(
            t["fill_price"] if t["direction"] == "YES" else 1 - t["fill_price"]
            for t in sub
        ) / n
        print(f"  {lo}-{hi if hi<999 else '∞'}bp{'':<8} {n:<5} {wr:>6.1f}% ${pnl:>+8,.0f}  ${pnl/n:>+7.2f}  {avg_entry*100:>9.1f}c")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass 3: Convergence test — if delta is large with little time left, do you ALWAYS win?")
    # The cleanest test: T<=30 AND |delta|>=5
    # If WR is near 100%, the convergence hypothesis is strongly supported
    test = [t for t in trades if t["time_remaining"] <= 30 and t["abs_delta"] >= 5]
    if test:
        n = len(test)
        wins = sum(1 for t in test if t["pnl"] > 0)
        pnl = sum(t["pnl"] for t in test)
        avg_entry = sum(t["fill_price"] if t["direction"] == "YES" else 1 - t["fill_price"] for t in test) / n
        print(f"\n  T<=30 AND |delta|>=5:  n={n}  WR={wins/n*100:.1f}%  PnL=${pnl:+,.0f}  $/tr=${pnl/n:+.2f}")
        print(f"  Average true entry: {avg_entry*100:.1f}c  (the more discount, the more profit per trade)")
        if wins/n >= 0.85:
            print("  → STRONG convergence: outcome is deterministic in this regime")
        elif wins/n >= 0.70:
            print("  → MODERATE convergence: still upside vs random")
        else:
            print("  → WEAK convergence: regime is not deterministic enough")

    # Even tighter: T<=15 AND |delta|>=5
    test2 = [t for t in trades if t["time_remaining"] <= 15 and t["abs_delta"] >= 5]
    if test2:
        n = len(test2)
        wins = sum(1 for t in test2 if t["pnl"] > 0)
        pnl = sum(t["pnl"] for t in test2)
        print(f"\n  T<=15 AND |delta|>=5:  n={n}  WR={wins/n*100:.1f}%  PnL=${pnl:+,.0f}  $/tr=${pnl/n:+.2f}")

    test3 = [t for t in trades if t["time_remaining"] <= 30 and t["abs_delta"] >= 8]
    if test3:
        n = len(test3)
        wins = sum(1 for t in test3 if t["pnl"] > 0)
        pnl = sum(t["pnl"] for t in test3)
        print(f"\n  T<=30 AND |delta|>=8:  n={n}  WR={wins/n*100:.1f}%  PnL=${pnl:+,.0f}  $/tr=${pnl/n:+.2f}")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass 4: Direction asymmetry — late-window YES vs NO")
    yes = [t for t in late if t["direction"] == "YES"]
    no  = [t for t in late if t["direction"] == "NO"]
    if yes:
        n = len(yes); wins = sum(1 for t in yes if t["pnl"] > 0); p = sum(t["pnl"] for t in yes)
        print(f"  YES (late):  n={n}  WR={wins/n*100:.1f}%  PnL=${p:+,.0f}  $/tr=${p/n:+.2f}")
    if no:
        n = len(no); wins = sum(1 for t in no if t["pnl"] > 0); p = sum(t["pnl"] for t in no)
        print(f"  NO  (late):  n={n}  WR={wins/n*100:.1f}%  PnL=${p:+,.0f}  $/tr=${p/n:+.2f}")

    # ═══════════════════════════════════════════════════════════════
    hr("Pass 5: Why do we lose late-window trades when delta is large?")
    # If |delta|>=5 with T<=30 fails to reach high WR, what's going wrong?
    # Look at the losers in this regime
    losers = [t for t in trades
              if t["time_remaining"] <= 30 and t["abs_delta"] >= 5 and t["pnl"] <= 0]
    if losers:
        print(f"\n  {len(losers)} losers in T<=30, |delta|>=5 regime:")
        print(f"  {'time_rem':<9} {'delta':<9} {'dir':<5} {'entry':<8} {'pnl':<10} {'outcome':<8}")
        for t in losers[:15]:
            print(f"  {t['time_remaining']:>7.1f}s {t['delta_bps']:>+7.1f}bp {t['direction']:<5} {t['fill_price']*100:>5.0f}c  ${t['pnl']:>+7.2f}  {t['outcome']:<8}")
        if len(losers) > 15:
            print(f"  ... and {len(losers)-15} more")
        # avg loser delta
        avg_loser_delta = sum(t["abs_delta"] for t in losers) / len(losers)
        print(f"\n  Average |delta| at the moment of these losing fires: {avg_loser_delta:.1f}bp")
        print(f"  (compare to: this means BTC was {avg_loser_delta:.1f}bp from strike but reverted before settlement)")


if __name__ == "__main__":
    main()
