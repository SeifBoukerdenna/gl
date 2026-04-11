"""
Dig into Strategy B: mid-window value cell.
Cell: T 90-150s, true_entry 55-75c. ~76-78% WR over ~120 trades across all oracle sessions.

Questions:
  1. Is one session driving the whole thing? (selection bias)
  2. Direction / |delta_bps| profile?
  3. Time-stable (split-half)?
  4. Bootstrap CI on $/tr
  5. Does omega/chrono already capture these?
"""
import csv
import random
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA = Path("data")
ORACLE = sorted(d.name for d in DATA.iterdir() if d.is_dir() and d.name.startswith("oracle_"))


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
                    "session": name,
                    "ts": float(r["timestamp"]),
                    "te": te, "tr": float(r["time_remaining"]),
                    "pnl": float(r["pnl_taker"]),
                    "ad": abs(float(r.get("delta_bps", 0))),
                    "delta": float(r.get("delta_bps", 0)),
                    "dir": d,
                    "result": r["result"],
                })
            except (ValueError, KeyError):
                continue
    return out


def in_cell(t):
    return 90 <= t["tr"] < 150 and 0.55 <= t["te"] < 0.75


def main():
    all_t = []
    for s in ORACLE: all_t.extend(load(s))

    cell = [t for t in all_t if in_cell(t)]
    n = len(cell)
    pnl = sum(t["pnl"] for t in cell)
    wins = sum(1 for t in cell if t["pnl"] > 0)
    wr = wins / n * 100
    print(f"\nCell: T 90-150s, entry 55-75c   n={n}  WR={wr:.1f}%  $/tr=${pnl/n:+.2f}  total=${pnl:+,.0f}\n")

    # 1. Session contribution
    print("─" * 70)
    print("  1. Per-session contribution (is one session carrying it?)")
    print("─" * 70)
    print(f"  {'session':<25} {'n':<5} {'WR':<8} {'pnl':<10}")
    by_s = defaultdict(list)
    for t in cell: by_s[t["session"]].append(t)
    for s in sorted(by_s.keys(), key=lambda k: -len(by_s[k])):
        sub = by_s[s]
        if len(sub) < 3: continue
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        print(f"  {s:<25} {len(sub):<5} {swr:>5.1f}%  ${spnl:>+8,.0f}")

    # 2. Direction & delta profile
    print()
    print("─" * 70)
    print("  2. Direction × |delta_bps| profile")
    print("─" * 70)
    for d in ("YES", "NO"):
        sub = [t for t in cell if t["dir"] == d]
        if not sub: continue
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        print(f"  {d:<5}  n={len(sub):<3}  WR={swr:>5.1f}%  pnl=${spnl:>+7,.0f}  $/tr=${spnl/len(sub):+.2f}")

    print()
    print(f"  {'|delta|':<12} {'n':<5} {'WR':<8} {'$/tr':<10}")
    for lo, hi in [(0,2),(2,4),(4,6),(6,9),(9,15),(15,999)]:
        sub = [t for t in cell if lo <= t["ad"] < hi]
        if not sub: continue
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        print(f"  {lo}-{hi if hi<999 else '∞'}bp{'':<5} {len(sub):<5} {swr:>5.1f}%  ${spnl/len(sub):+7.2f}")

    # 3. Time stability
    print()
    print("─" * 70)
    print("  3. Time stability (split-half)")
    print("─" * 70)
    cell_s = sorted(cell, key=lambda t: t["ts"])
    h = len(cell_s) // 2
    f, s = cell_s[:h], cell_s[h:]
    for label, sub in [("first half", f), ("second half", s)]:
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        st = datetime.fromtimestamp(sub[0]["ts"]).strftime("%m-%d %H:%M")
        et = datetime.fromtimestamp(sub[-1]["ts"]).strftime("%m-%d %H:%M")
        print(f"  {label:<12} {st}→{et}  n={len(sub):<3}  WR={swr:>5.1f}%  $/tr=${spnl/len(sub):+.2f}")

    # 4. Bootstrap CI on $/tr
    print()
    print("─" * 70)
    print("  4. Bootstrap 95% CI on $/trade")
    print("─" * 70)
    pnls = [t["pnl"] for t in cell]
    random.seed(42)
    boots = sorted(sum(random.choice(pnls) for _ in range(n)) / n for _ in range(2000))
    lo, hi = boots[50], boots[1949]
    p_pos = sum(1 for b in boots if b > 0) / len(boots) * 100
    print(f"  point: ${pnl/n:+.2f}/tr   95% CI: [${lo:+.2f}, ${hi:+.2f}]   P(>0)={p_pos:.1f}%")

    # 5. Sub-cell refinement: 55-65 vs 65-75
    print()
    print("─" * 70)
    print("  5. Refine the cell — is one half doing all the work?")
    print("─" * 70)
    for elo, ehi in [(0.55,0.65),(0.65,0.75)]:
        sub = [t for t in cell if elo <= t["te"] < ehi]
        if not sub: continue
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        ate = sum(t["te"] for t in sub) / len(sub)
        print(f"  entry {elo*100:.0f}-{ehi*100:.0f}c  n={len(sub):<4} WR={swr:>5.1f}%  $/tr=${spnl/len(sub):+.2f}  avg_te={ate*100:.1f}c")

    # 6. T 90-120 vs 120-150
    print()
    for tlo, thi in [(90,120),(120,150)]:
        sub = [t for t in cell if tlo <= t["tr"] < thi]
        if not sub: continue
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        print(f"  T {tlo}-{thi}s   n={len(sub):<4} WR={swr:>5.1f}%  $/tr=${spnl/len(sub):+.2f}")


if __name__ == "__main__":
    main()
