"""WR by (true_entry, time_remaining) bucket across ALL oracle sessions."""
import csv
from collections import defaultdict
from pathlib import Path

DATA = Path("data")
ORACLE = [d.name for d in DATA.iterdir() if d.is_dir() and d.name.startswith("oracle_")]


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
                    "pnl": float(r["pnl_taker"]),
                    "ad": abs(float(r.get("delta_bps", 0))),
                })
            except (ValueError, KeyError):
                continue
    return out


def main():
    all_trades = []
    for s in ORACLE:
        all_trades.extend(load(s))
    print(f"Loaded {len(all_trades)} trades from {len(ORACLE)} oracle_* sessions\n")

    print("="*78)
    print("  WR x $/tr by ENTRY × TIME bucket (all oracle sessions, all delta)")
    print("="*78)
    e_bins = [(0,55),(55,65),(65,75),(75,85),(85,92),(92,99)]
    t_bins = [(0,15),(15,30),(30,45),(45,60),(60,90),(90,150),(150,300)]

    print(f"\n  {'entry':<10}", end="")
    for tlo, thi in t_bins:
        print(f" T{tlo:>3}-{thi:<3}", end="")
    print()
    print("  " + "-" * 78)

    for elo, ehi in e_bins:
        print(f"  {elo:>3}-{ehi:<3}c  ", end="")
        for tlo, thi in t_bins:
            sub = [x for x in all_trades
                   if elo/100 <= x["te"] < ehi/100
                   and tlo <= x["tr"] < thi]
            if len(sub) < 5:
                print(f"     {' .':>5}", end=" ")
                continue
            wr = sum(1 for x in sub if x["pnl"] > 0) / len(sub) * 100
            print(f"{wr:>4.0f}%/{len(sub):>3}", end=" ")
        print()
    print()
    print("  cells: WR% / n   ('.' = n<5)")

    # Now look for the sweet spots: WR > 75%, entry <= 85c
    print()
    print("="*78)
    print("  SWEET SPOTS — high WR AND affordable entry")
    print("="*78)
    print()
    print(f"  {'cell':<25} {'n':<5} {'WR':<8} {'$/tr':<10} {'avg te':<8}")
    print("  " + "-" * 60)
    found = []
    for elo, ehi in e_bins:
        if elo >= 88: continue  # too expensive
        for tlo, thi in t_bins:
            sub = [x for x in all_trades
                   if elo/100 <= x["te"] < ehi/100
                   and tlo <= x["tr"] < thi]
            if len(sub) < 10: continue
            wins = sum(1 for x in sub if x["pnl"] > 0)
            wr = wins / len(sub) * 100
            if wr < 75: continue
            pnl = sum(x["pnl"] for x in sub)
            avg_te = sum(x["te"] for x in sub) / len(sub)
            found.append((wr, len(sub), elo, ehi, tlo, thi, pnl, avg_te))

    found.sort(key=lambda x: -x[0])
    for wr, n, elo, ehi, tlo, thi, pnl, ate in found[:20]:
        cell = f"entry {elo}-{ehi}c, T{tlo}-{thi}s"
        print(f"  {cell:<25} {n:<5} {wr:>5.1f}%  ${pnl/n:>+7.2f}  {ate*100:>5.1f}c")

    if not found:
        print("  (none — every late-window high-entry cell has WR < 75%)")

    # The killer question: in the late window, what does entry look like?
    print()
    print("="*78)
    print("  Entry distribution for trades that fired in T<=60")
    print("="*78)
    late = [x for x in all_trades if x["tr"] <= 60]
    print(f"\n  Late-window trades (T<=60): n={len(late)}")
    for elo, ehi in e_bins:
        sub = [x for x in late if elo/100 <= x["te"] < ehi/100]
        if not sub: continue
        wr = sum(1 for x in sub if x["pnl"] > 0) / len(sub) * 100
        pnl = sum(x["pnl"] for x in sub)
        print(f"  entry {elo:>3}-{ehi:<3}c  n={len(sub):>4}  WR={wr:>5.1f}%  $/tr=${pnl/len(sub):>+7.2f}")


if __name__ == "__main__":
    main()
