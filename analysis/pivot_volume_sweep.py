"""
Sweep pivot_1 filter parameters to find max volume that still keeps the edge.

Tests:
  - T window width (narrow → wide)
  - delta floor (6 → 4 → 3 → 2)
  - allowing multiple fires per window
  - allowing fires earlier (T > 180) and later (T < 120)
"""
import pandas as pd
from datetime import datetime
from pathlib import Path

WINDOW = 300


def load_klines():
    df = pd.read_parquet("data/binance_klines_60d.parquet")
    df["ts_sec"] = (df["timestamp"] / 1000).astype(int)
    return df.set_index("ts_sec").sort_index()


def simulate_once_per_window(klines, t_lo, t_hi, delta_min, delta_max=999):
    """Fire once per window at the FIRST moment hitting condition.
    Returns list of fires."""
    closes = klines["close"]
    first = closes.index[0]
    last = closes.index[-1]
    win_start = ((first // WINDOW) + 1) * WINDOW

    fires = []
    while win_start + WINDOW <= last:
        win_end = win_start + WINDOW
        try:
            strike = float(closes.loc[win_start])
            close_p = float(closes.loc[win_end])
        except KeyError:
            win_start += WINDOW
            continue
        if strike <= 0:
            win_start += WINDOW
            continue

        # Scan window from earliest valid moment to latest
        scan_start = win_end - t_hi
        scan_end = win_end - t_lo
        try:
            scan = closes.loc[scan_start:scan_end]
        except KeyError:
            win_start += WINDOW
            continue

        for ts, p in scan.items():
            d = (p - strike) / strike * 10000
            ad = abs(d)
            if delta_min <= ad < delta_max:
                outcome = "YES" if close_p > strike else "NO"
                direction = "YES" if d > 0 else "NO"
                fires.append({
                    "won": direction == outcome,
                    "ad": ad,
                    "tr": win_end - ts,
                })
                break
        win_start += WINDOW
    return fires


def simulate_multi(klines, t_lo, t_hi, delta_min, delta_max, cooldown_s=20):
    """Allow multiple fires per window with cooldown."""
    closes = klines["close"]
    first = closes.index[0]
    last = closes.index[-1]
    win_start = ((first // WINDOW) + 1) * WINDOW

    fires = []
    while win_start + WINDOW <= last:
        win_end = win_start + WINDOW
        try:
            strike = float(closes.loc[win_start])
            close_p = float(closes.loc[win_end])
            scan = closes.loc[win_end - t_hi:win_end - t_lo]
        except KeyError:
            win_start += WINDOW
            continue
        if strike <= 0:
            win_start += WINDOW
            continue

        last_fire_ts = -1e9
        for ts, p in scan.items():
            if ts - last_fire_ts < cooldown_s:
                continue
            d = (p - strike) / strike * 10000
            ad = abs(d)
            if delta_min <= ad < delta_max:
                outcome = "YES" if close_p > strike else "NO"
                direction = "YES" if d > 0 else "NO"
                fires.append({"won": direction == outcome, "ad": ad, "tr": win_end - ts})
                last_fire_ts = ts
        win_start += WINDOW
    return fires


def report(label, fires, days):
    if not fires:
        print(f"  {label:<48} (no fires)")
        return
    n = len(fires)
    wr = sum(1 for f in fires if f["won"]) / n * 100
    per_day = n / days
    # Quick EV: assume 67c entry, $200 notional
    entry = 0.67
    notional = 200
    shares = notional / entry
    ev = (wr/100 * (1-entry) - (1-wr/100) * entry - entry*(1-entry)*0.072) * shares
    print(f"  {label:<48} n={n:>6}  WR={wr:>5.1f}%  {per_day:>5.1f}/day  ev~${ev:+.1f}/tr")


def main():
    k = load_klines()
    days = (k.index[-1] - k.index[0]) / 86400
    print(f"\nKlines: {days:.1f} days, {len(k):,} ticks\n")

    print("─" * 80)
    print("  ONE FIRE PER WINDOW — sweep T window and delta floor")
    print("─" * 80)
    for delta_min in [3, 4, 5, 6, 8]:
        print()
        for t_lo, t_hi in [(120,180), (90,180), (60,180), (90,210), (60,240), (30,270)]:
            label = f"T{t_lo:>3}-{t_hi:<3}  |delta|≥{delta_min}bp"
            fires = simulate_once_per_window(k, t_lo, t_hi, delta_min)
            report(label, fires, days)

    print()
    print("─" * 80)
    print("  MULTIPLE FIRES PER WINDOW — relax the one-per-window cap")
    print("─" * 80)
    print()
    for t_lo, t_hi in [(60,240), (30,270), (5,290)]:
        for delta_min in [4, 6]:
            for cooldown in [10, 20, 30]:
                label = f"T{t_lo:>3}-{t_hi:<3}  ≥{delta_min}bp  cool{cooldown}s"
                fires = simulate_multi(k, t_lo, t_hi, delta_min, 999, cooldown)
                report(label, fires, days)
            print()

    print()
    print("─" * 80)
    print("  Best volume × edge tradeoffs (sorted by volume)")
    print("─" * 80)
    print("  → look for high fires/day with WR still ≥73% to stay profitable at ~67c entry")


if __name__ == "__main__":
    main()
