"""
Post-NO-sizing-fix correlation matrix.

The original correlation analysis (analysis/correlation_today.py) was run on
data contaminated by the NO sizing bug, where NO trades were systematically
oversized by 1.2-2.7x. That bug made the oracle family LOOK more correlated
because losing NO trades on all oracle variants were amplified in the same
direction.

This script redoes the analysis on CLEAN data only — trades after the NO
sizing fix was deployed (Apr 10 ~16:40 UTC). Hypothesis: the clean effective N
should be higher than the ~2 we found before.
"""
import csv
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")
RESET_TS = 1775836800  # Apr 10 2026 ~16:00 UTC — generous lower bound for post-fix data

# Active sessions (11 active + 3 new = 14)
ACTIVE = [
    "oracle_1", "oracle_alpha", "oracle_arb", "oracle_chrono",
    "oracle_consensus", "oracle_echo", "oracle_omega", "oracle_titan",
    "pivot_1", "blitz_1", "test_ic_wide",
    "oracle_lazy", "volume_shift_1", "piggyback_1",
]


def load(name):
    p = DATA / name / "trades.csv"
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for r in csv.DictReader(f):
            try:
                ts = float(r["timestamp"])
                if ts < RESET_TS:
                    continue  # exclude pre-fix contaminated data
                out.append({"ts": ts, "pnl": float(r["pnl_taker"])})
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


def hourly(trades):
    """Bucket PnL by floor(ts/3600)."""
    out = defaultdict(float)
    for t in trades:
        h = int(t["ts"] // 3600) * 3600
        out[h] += t["pnl"]
    return dict(out)


def pearson(a, b):
    if len(a) < 3:
        return None
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    sa = sum((x - ma) ** 2 for x in a)
    sb = sum((x - mb) ** 2 for x in b)
    if sa == 0 or sb == 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(sa * sb)


def aligned(h1, h2):
    common = sorted(set(h1.keys()) & set(h2.keys()))
    return [h1[h] for h in common], [h2[h] for h in common], len(common)


def fmt_corr(c):
    if c is None: return "  -- "
    if c >= 0.7:  return "\033[91m{:+.2f}\033[0m".format(c)
    if c >= 0.4:  return "\033[93m{:+.2f}\033[0m".format(c)
    if c >= -0.4: return "\033[92m{:+.2f}\033[0m".format(c)
    return "\033[94m{:+.2f}\033[0m".format(c)


def main():
    sessions = {}
    for name in ACTIVE:
        trades = load(name)
        if trades:
            sessions[name] = trades

    print()
    print("=" * 100)
    print("  CORRELATION MATRIX — POST-FIX CLEAN DATA ONLY")
    print("=" * 100)
    print(f"\n  Data: after Apr 10 {datetime.fromtimestamp(RESET_TS, tz=timezone.utc).strftime('%H:%M')} UTC")
    print(f"  Sessions with data: {len(sessions)}")

    # Per-session summary
    print()
    print(f"  {'session':<22} {'trades':<8} {'hours':<8} {'PnL':<10}")
    print("  " + "-" * 60)
    for name, trs in sorted(sessions.items(), key=lambda x: -len(x[1])):
        n = len(trs)
        pnl = sum(t["pnl"] for t in trs)
        hrs = (trs[-1]["ts"] - trs[0]["ts"]) / 3600 if n > 1 else 0
        print(f"  {name:<22} {n:<8} {hrs:<7.1f}h ${pnl:>+8,.0f}")

    # Compute hourly series
    hourly_map = {n: hourly(trs) for n, trs in sessions.items()}

    # Pairwise correlation
    names = list(hourly_map.keys())
    if len(names) < 2:
        print("\n  too few sessions")
        return

    print()
    print("=" * 100)
    print("  PAIRWISE HOURLY-PNL CORRELATION (red≥0.7 | yellow≥0.4 | green=ind | blue=anti)")
    print("=" * 100)
    print()

    short = {n: n.replace("oracle_", "or_").replace("volume_shift_1", "vshift").replace("piggyback_1", "pigbak")[:11] for n in names}
    header = " " * 13 + "".join(f"{short[n]:>12}" for n in names)
    print(header)
    pairs_corr = []
    for n1 in names:
        row = f"  {short[n1]:<11}"
        for n2 in names:
            if n1 == n2:
                row += "         --"
                continue
            a, b, nh = aligned(hourly_map[n1], hourly_map[n2])
            c = pearson(a, b)
            if n1 < n2 and c is not None and nh >= 4:
                pairs_corr.append((n1, n2, c, nh))
            row += "       " + fmt_corr(c)
        print(row)

    # Top correlated pairs
    if pairs_corr:
        pairs_corr.sort(key=lambda x: -abs(x[2]))
        print()
        print("  most-correlated pairs (n_hours ≥ 4):")
        for n1, n2, c, nh in pairs_corr[:10]:
            tag = "highly corr" if abs(c) >= 0.7 else "moderate" if abs(c) >= 0.4 else "independent"
            print(f"    {n1:<22} ↔ {n2:<22}  r={c:+.2f}  nh={nh}  {tag}")

    # Effective independent sessions
    if pairs_corr:
        # Only include sessions with enough data to compute correlation
        valid_names = set()
        for n1, n2, _, _ in pairs_corr:
            valid_names.add(n1); valid_names.add(n2)
        k = len(valid_names)

        ravg = sum(c for _, _, c, _ in pairs_corr) / len(pairs_corr)
        eff = k / (1 + (k - 1) * max(0, ravg))
        print()
        print("=" * 100)
        print("  EFFECTIVE INDEPENDENT SESSIONS")
        print("=" * 100)
        print(f"\n  Sessions with enough data (≥4 common hours with at least one other): {k}")
        print(f"  Average pairwise correlation: \033[1m{ravg:+.2f}\033[0m")
        print(f"  Effective independent sessions: \033[1m{eff:.1f}\033[0m of {k}")
        print(f"  Diversification: \033[1m{eff/k*100:.0f}%\033[0m")

        print()
        if ravg >= 0.6:
            print("  → HIGHLY CORRELATED — the portfolio is essentially one bet")
        elif ravg >= 0.4:
            print("  → MODERATE correlation — some diversification but top sessions move together")
        elif ravg >= 0.2:
            print("  → GOOD diversification — sessions are mostly independent")
        else:
            print("  → EXCELLENT diversification — truly orthogonal mechanisms")

        # Compare to the pre-fix finding from earlier run
        print()
        print("  For comparison: pre-fix run showed average correlation +0.44-0.49")
        print("                  effective N of 1.9-2.0 out of 11 (~18% diversification)")

    # Family-level aggregates
    print()
    print("=" * 100)
    print("  FAMILY-LEVEL VIEW — are mechanism families correlated with each other?")
    print("=" * 100)
    families = {
        "oracle_family (edge table)": ["oracle_1", "oracle_alpha", "oracle_chrono", "oracle_consensus", "oracle_omega", "oracle_titan"],
        "oracle_arb (mispricing)": ["oracle_arb"],
        "multi_venue (cross-exchange)": ["blitz_1", "test_ic_wide"],
        "pivot (half-window)": ["pivot_1"],
        "persistence (oracle_lazy)": ["oracle_lazy"],
        "msg_rate (volume_shift)": ["volume_shift_1"],
        "book_follow (piggyback)": ["piggyback_1"],
    }
    family_hourly = {}
    for fam_name, members in families.items():
        agg = defaultdict(float)
        for m in members:
            if m in hourly_map:
                for h, p in hourly_map[m].items():
                    agg[h] += p
        if agg:
            family_hourly[fam_name] = dict(agg)

    fam_names = list(family_hourly.keys())
    if len(fam_names) >= 2:
        print(f"\n  {'family':<32}", end="")
        for fn in fam_names:
            short_fn = fn.split(" ")[0][:8]
            print(f"{short_fn:>10}", end="")
        print()
        for fn1 in fam_names:
            short_fn1 = fn1.split(" ")[0][:8]
            print(f"  {fn1:<32}", end="")
            for fn2 in fam_names:
                if fn1 == fn2:
                    print("        --", end="")
                    continue
                a, b, nh = aligned(family_hourly[fn1], family_hourly[fn2])
                c = pearson(a, b)
                if c is None or nh < 4:
                    print("     .    ", end="")
                else:
                    print("     " + fmt_corr(c), end="")
            print()


if __name__ == "__main__":
    main()
