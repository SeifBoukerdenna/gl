"""
"Random fighting random with random" — does first-half strike-crossing count
predict second-half outcome?

For each 5-min PM window in 60d of klines:
  1. Count how many times BTC crossed the strike during T-300 → T-150 (first half)
  2. Record direction at exactly T-150 (which side is BTC on at the half)
  3. Record outcome at T-0 (which side won)

Then test:
  - Conditional P(YES wins) given (cross_count, half_direction)
  - Is chop (high cross count) followed by mean reversion or trend?
  - Is "no chop" (0 crosses) followed by trend continuation?
  - Random-bet baseline for comparison
"""
import pandas as pd
import random
from datetime import datetime
from pathlib import Path

WINDOW = 300
HALF = 150


def load_klines():
    df = pd.read_parquet("data/binance_klines_60d.parquet")
    df["ts_sec"] = (df["timestamp"] / 1000).astype(int)
    return df.set_index("ts_sec").sort_index()


def simulate(klines):
    closes = klines["close"]
    first = closes.index[0]
    last = closes.index[-1]
    win_start = ((first // WINDOW) + 1) * WINDOW

    rows = []
    while win_start + WINDOW <= last:
        try:
            strike = float(closes.loc[win_start])
            half_price = float(closes.loc[win_start + HALF])
            close_price = float(closes.loc[win_start + WINDOW])
            scan = closes.loc[win_start:win_start + HALF]
        except KeyError:
            win_start += WINDOW
            continue

        if strike <= 0 or len(scan) < 100:
            win_start += WINDOW
            continue

        # Count crossings in first half
        signs = [(p - strike > 0) for p in scan.values]
        crosses = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i-1])

        half_dir = "YES" if half_price > strike else "NO" if half_price < strike else "FLAT"
        outcome = "YES" if close_price > strike else "NO" if close_price < strike else "FLAT"
        if half_dir == "FLAT" or outcome == "FLAT":
            win_start += WINDOW
            continue

        rows.append({
            "crosses": crosses,
            "half_dir": half_dir,
            "outcome": outcome,
            "half_delta_bps": (half_price - strike) / strike * 10000,
        })
        win_start += WINDOW
    return rows


def main():
    print("\nLoading klines...")
    k = load_klines()
    print(f"  {len(k):,} ticks")
    print("\nSimulating windows...")
    rows = simulate(k)
    n = len(rows)
    print(f"  {n:,} valid windows\n")

    # ═══════════════════════════════════════════════
    print("─" * 70)
    print("  1. Distribution of first-half cross counts")
    print("─" * 70)
    from collections import Counter
    c = Counter(r["crosses"] for r in rows)
    for k_, v in sorted(c.items()):
        if k_ > 12: continue
        bar = "█" * int(v / n * 200)
        print(f"  {k_:>2} crosses  {v:>5}  ({v/n*100:>4.1f}%)  {bar}")
    high = sum(v for kk, v in c.items() if kk > 12)
    if high: print(f"  >12       {high:>5}  ({high/n*100:>4.1f}%)")

    # ═══════════════════════════════════════════════
    print()
    print("─" * 70)
    print("  2. P(half_dir wins) by cross count — momentum vs reversion?")
    print("─" * 70)
    print(f"\n  {'crosses':<10} {'n':<6} {'momentum WR':<15} {'reversion WR':<15}")
    print("  " + "-" * 55)
    buckets = [(0,0),(1,1),(2,2),(3,4),(5,7),(8,12),(13,99)]
    for lo, hi in buckets:
        sub = [r for r in rows if lo <= r["crosses"] <= hi]
        if len(sub) < 20: continue
        mom = sum(1 for r in sub if r["half_dir"] == r["outcome"]) / len(sub) * 100
        rev = 100 - mom
        label = f"{lo}" if lo == hi else f"{lo}-{hi}"
        print(f"  {label:<10} {len(sub):<6} {mom:>5.1f}%        {rev:>5.1f}%")

    # ═══════════════════════════════════════════════
    print()
    print("─" * 70)
    print("  3. Stratify by half_delta magnitude (how far from strike at T-150)")
    print("─" * 70)
    print(f"\n  {'|half_delta|':<14} {'crosses':<10} {'n':<6} {'momentum WR':<12}")
    for dlo, dhi in [(0,3),(3,6),(6,12),(12,25),(25,999)]:
        for clo, chi in [(0,0),(1,2),(3,5),(6,99)]:
            sub = [r for r in rows
                   if dlo <= abs(r["half_delta_bps"]) < dhi
                   and clo <= r["crosses"] <= chi]
            if len(sub) < 20: continue
            mom = sum(1 for r in sub if r["half_dir"] == r["outcome"]) / len(sub) * 100
            d = f"{dlo}-{dhi if dhi<999 else '∞'}bp"
            c = f"{clo}" if clo == chi else f"{clo}-{chi}"
            print(f"  {d:<14} {c:<10} {len(sub):<6} {mom:>5.1f}%")

    # ═══════════════════════════════════════════════
    print()
    print("─" * 70)
    print("  4. Random baselines (sanity check)")
    print("─" * 70)
    random.seed(42)
    yes_rate = sum(1 for r in rows if r["outcome"] == "YES") / n * 100
    print(f"\n  Unconditional P(YES wins) = {yes_rate:.2f}%   (n={n})")
    print(f"  → if BTC drift is zero, this should be ≈50%")

    # Test "buy random direction" expected outcome
    rand_wr = sum(1 for r in rows if random.choice(["YES","NO"]) == r["outcome"]) / n * 100
    print(f"  Random bet WR = {rand_wr:.2f}%   (sanity, should be ≈50%)")

    # ═══════════════════════════════════════════════
    print()
    print("─" * 70)
    print("  5. The actual 'random fighting random' test")
    print("─" * 70)
    print()
    print("  Strategy: if first-half had ≥3 crosses (chop), bet HALF DIRECTION at T-150.")
    print("  Hypothesis: when chop is high, the side BTC happens to be on at the half")
    print("  is NOT predictive — if it WERE predictive, that's a real edge.")
    print()

    chop = [r for r in rows if r["crosses"] >= 3]
    smooth = [r for r in rows if r["crosses"] == 0]
    one = [r for r in rows if r["crosses"] == 1]
    two = [r for r in rows if r["crosses"] == 2]

    for label, sub in [("0 crosses (trending)", smooth),
                       ("1 cross", one),
                       ("2 crosses", two),
                       ("3+ crosses (chop)", chop)]:
        if len(sub) < 20: continue
        mom = sum(1 for r in sub if r["half_dir"] == r["outcome"]) / len(sub) * 100
        n_ = len(sub)
        # Naive expected if random: 50%
        # Z-test: significance of deviation from 50
        import math
        se = math.sqrt(0.5 * 0.5 / n_) * 100
        z = (mom - 50) / se
        sig = "***" if abs(z) > 3 else "**" if abs(z) > 2 else "*" if abs(z) > 1 else " "
        print(f"  {label:<22} n={n_:>5}  WR={mom:>5.1f}%  z={z:>+5.2f} {sig}")


if __name__ == "__main__":
    main()
