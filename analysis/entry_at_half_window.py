"""
What does the empirical PM entry price look like at T-150 (mid-window) with
|delta| ≥ 6bp?

Pulls all trades from all oracle/* sessions where:
  120 ≤ time_remaining ≤ 180  (loose half-window band)
  |delta_bps| ≥ 6
And reports entry price distribution by delta bucket. Tells us the realistic
entry price the half_window strategy could expect.
"""
import csv
from collections import defaultdict
from pathlib import Path
from statistics import median, mean

DATA = Path("data")
SESSIONS = sorted([d.name for d in DATA.iterdir() if d.is_dir() and
                   (d.name.startswith("oracle_") or d.name in ("blitz_1", "edge_hunter"))])


def load(name):
    p = DATA / name / "trades.csv"
    if not p.exists(): return []
    out = []
    with open(p) as f:
        for r in csv.DictReader(f):
            try:
                fp = float(r["fill_price"]); d = r["direction"]
                te = fp if d == "YES" else 1 - fp
                out.append({
                    "te": te, "tr": float(r["time_remaining"]),
                    "ad": abs(float(r.get("delta_bps", 0))),
                    "pnl": float(r["pnl_taker"]),
                    "dir": d, "session": name,
                })
            except (ValueError, KeyError):
                continue
    return out


def pct(xs, q):
    s = sorted(xs); k = (len(s) - 1) * q
    f = int(k); c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def main():
    all_t = []
    for s in SESSIONS: all_t.extend(load(s))
    print(f"Loaded {len(all_t)} trades from {len(SESSIONS)} sessions\n")

    # Tight band: T 120-180s (around T-150)
    band = [t for t in all_t if 120 <= t["tr"] < 180]
    print(f"In half-window band (T 120-180s): n={len(band)}\n")

    print("─" * 75)
    print("  Entry-price distribution by |delta_bps| in T 120-180s")
    print("─" * 75)
    print(f"  {'|delta|':<11} {'n':<6} {'med te':<9} {'mean te':<10} {'p10':<7} {'p90':<7} {'WR':<7}  {'$/tr':<8}")
    for lo, hi in [(2,4),(4,6),(6,9),(9,15),(15,25),(25,999)]:
        sub = [t for t in band if lo <= t["ad"] < hi]
        if len(sub) < 5: continue
        tes = [t["te"] for t in sub]
        wr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        avg_pnl = sum(t["pnl"] for t in sub) / len(sub)
        rng = f"{lo}-{hi if hi<999 else '∞'}bp"
        print(f"  {rng:<11} {len(sub):<6} {median(tes)*100:>5.1f}c   {mean(tes)*100:>5.1f}c   "
              f"{pct(tes,0.10)*100:>4.0f}c  {pct(tes,0.90)*100:>4.0f}c  {wr:>5.1f}%  ${avg_pnl:>+6.2f}")

    print()
    print("─" * 75)
    print("  Same with the WIDER half-window band (T 90-180s) for more sample")
    print("─" * 75)
    band2 = [t for t in all_t if 90 <= t["tr"] < 180]
    print(f"  n={len(band2)}\n")
    print(f"  {'|delta|':<11} {'n':<6} {'med te':<9} {'mean te':<10} {'p10':<7} {'p90':<7} {'WR':<7}  {'$/tr':<8}")
    for lo, hi in [(2,4),(4,6),(6,9),(9,15),(15,25),(25,999)]:
        sub = [t for t in band2 if lo <= t["ad"] < hi]
        if len(sub) < 5: continue
        tes = [t["te"] for t in sub]
        wr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        avg_pnl = sum(t["pnl"] for t in sub) / len(sub)
        rng = f"{lo}-{hi if hi<999 else '∞'}bp"
        print(f"  {rng:<11} {len(sub):<6} {median(tes)*100:>5.1f}c   {mean(tes)*100:>5.1f}c   "
              f"{pct(tes,0.10)*100:>4.0f}c  {pct(tes,0.90)*100:>4.0f}c  {wr:>5.1f}%  ${avg_pnl:>+6.2f}")

    # Profitability at the median entry — given the 76-82% counterfactual WR
    print()
    print("─" * 75)
    print("  Profitability check: counterfactual WR × empirical median entry")
    print("─" * 75)
    print()
    print("  Reminder: kline counterfactual at T-150, |delta|≥6bp gave ~76-82% WR")
    print()
    target = [t for t in band2 if t["ad"] >= 6]
    if target:
        med_te = median([t["te"] for t in target])
        # Compute expected PnL at this entry assuming 78% WR
        for assumed_wr in [0.70, 0.75, 0.78, 0.80, 0.85]:
            notional = 200
            shares = notional / med_te
            ev_per_share = assumed_wr * (1 - med_te) - (1 - assumed_wr) * med_te
            fee = med_te * (1 - med_te) * 0.072
            ev_per_trade = (ev_per_share - fee) * shares
            print(f"  At assumed WR={assumed_wr*100:.0f}%, median entry={med_te*100:.1f}c → ${ev_per_trade:+.2f}/trade")


if __name__ == "__main__":
    main()
