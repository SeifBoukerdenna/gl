"""
Is oracle_arb's breakout real, or a small-sample fluke?

oracle_arb is the table-vs-market MISPRICING architecture. It only fires when
the historical table predicts a higher WR than the current market price implies
(i.e. the book is stale relative to history). Fundamentally different mechanism
from oracle_1, which fires on absolute edge over breakeven.

Things we want to know:
  1. PnL distribution: a few outliers, or broad-based?
  2. Performance over time: consistent across days, or recent burst?
  3. Bootstrap CI on $/trade (how confident is the +$40-51 figure?)
  4. WR vs oracle_1 on equivalent regimes (oracle_arb's WR is only 53% — is its
     edge in the SIZE per win, or the WR?)
  5. Direction breakdown (is one side carrying it?)
  6. Time-bucket breakdown (any hour-of-day pattern?)
  7. Win/loss magnitudes (are wins big and losses small, or symmetric?)
  8. Compare to oracle_1 trade-by-trade: when both fire in the same window,
     who wins?
  9. The strike-distance / delta_bps profile when it fires
"""

import csv
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, mean, stdev

DATA = Path("data")


def load(name):
    p = DATA / name / "trades.csv"
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for row in csv.DictReader(f):
            try:
                out.append({
                    "ts": float(row["timestamp"]),
                    "window": int(float(row["window_start"])),
                    "pnl": float(row["pnl_taker"]),
                    "direction": row["direction"],
                    "fill_price": float(row["fill_price"]),
                    "filled_size": float(row.get("filled_size", 0)),
                    "delta_bps": float(row.get("delta_bps", 0)),
                    "abs_delta": abs(float(row.get("delta_bps", 0))),
                    "tr": float(row["time_remaining"]),
                    "btc": float(row.get("btc_price", 0)),
                    "result": row["result"],
                    "book_age_ms": float(row.get("book_age_ms", 0)),
                    "spread": float(row.get("spread", 0)),
                    "notional": float(row.get("notional", 0)),
                })
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


def pct(x, q):
    s = sorted(x)
    if not s: return None
    k = (len(s) - 1) * q
    f = math.floor(k)
    c = math.ceil(k)
    if f == c: return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def hr(s):
    print()
    print("─" * 90)
    print("  " + s)
    print("─" * 90)


def main():
    arb = load("oracle_arb")
    or1 = load("oracle_1")
    if not arb:
        print("no oracle_arb data")
        return

    print()
    print("=" * 90)
    print(f"  ORACLE_ARB DEEP DIVE   ({len(arb)} trades, {len(or1)} oracle_1 reference)")
    print("=" * 90)

    # ═══════════════════════════════════════════════════════════════
    hr("1. Headline numbers")
    pnls = [t["pnl"] for t in arb]
    n = len(arb)
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = n - wins
    wr = wins / n * 100
    print(f"  trades:       {n}")
    print(f"  total PnL:    ${total:+,.0f}")
    print(f"  $/trade:      ${total/n:+.2f}")
    print(f"  WR:           {wr:.1f}%   ({wins}W / {losses}L)")
    print(f"  median PnL:   ${median(pnls):+.2f}")
    print(f"  stdev:        ${stdev(pnls):.2f}")
    print()
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    print(f"  avg win:      ${mean(win_pnls):+.2f}    (med ${median(win_pnls):+.2f})")
    print(f"  avg loss:     ${mean(loss_pnls):+.2f}    (med ${median(loss_pnls):+.2f})")
    print(f"  win/loss ratio: {abs(mean(win_pnls)/mean(loss_pnls)):.2f}x")

    # ═══════════════════════════════════════════════════════════════
    hr("2. PnL distribution percentiles")
    print(f"  p1   ${pct(pnls, 0.01):+8,.0f}    p99   ${pct(pnls, 0.99):+8,.0f}")
    print(f"  p5   ${pct(pnls, 0.05):+8,.0f}    p95   ${pct(pnls, 0.95):+8,.0f}")
    print(f"  p10  ${pct(pnls, 0.10):+8,.0f}    p90   ${pct(pnls, 0.90):+8,.0f}")
    print(f"  p25  ${pct(pnls, 0.25):+8,.0f}    p75   ${pct(pnls, 0.75):+8,.0f}")
    print(f"  p50  ${pct(pnls, 0.50):+8,.0f}")

    # Top 5 / bottom 5 contribution
    sorted_pnls = sorted(pnls, reverse=True)
    top5 = sum(sorted_pnls[:5])
    bot5 = sum(sorted_pnls[-5:])
    print()
    print(f"  Top 5 trades   contribute  ${top5:+,.0f}  ({top5/total*100:+.0f}% of total)")
    print(f"  Bot 5 trades   contribute  ${bot5:+,.0f}  ({bot5/total*100:+.0f}% of total)")
    print(f"  Top 10 trades  contribute  ${sum(sorted_pnls[:10]):+,.0f}  "
          f"({sum(sorted_pnls[:10])/total*100:+.0f}% of total)")
    rest = total - sum(sorted_pnls[:10])
    rest_n = n - 10
    print(f"  Other {rest_n} trades   contribute  ${rest:+,.0f}  ({rest/rest_n:+.2f}/trade)")

    # ═══════════════════════════════════════════════════════════════
    hr("3. Bootstrap 95% CI on $/trade")
    random.seed(42)
    boots = []
    for _ in range(2000):
        sample = [random.choice(pnls) for _ in range(n)]
        boots.append(sum(sample) / n)
    boots.sort()
    lo, hi = boots[50], boots[1949]
    print(f"  $/trade point estimate: ${total/n:+.2f}")
    print(f"  95% bootstrap CI:       [${lo:+.2f}, ${hi:+.2f}]")
    print(f"  CI excludes $0?         {'YES (real edge)' if lo > 0 else 'NO (could be noise)'}")
    boots_pos = sum(1 for b in boots if b > 0)
    print(f"  P($/trade > 0):         {boots_pos/len(boots)*100:.1f}%")

    # Block bootstrap (5-trade contiguous blocks) to handle autocorrelation
    block_n = 5
    n_blocks = n // block_n
    blocks = [pnls[i*block_n:(i+1)*block_n] for i in range(n_blocks)]
    boots2 = []
    for _ in range(2000):
        sampled = []
        for _ in range(n_blocks):
            sampled.extend(random.choice(blocks))
        boots2.append(sum(sampled) / len(sampled))
    boots2.sort()
    lo2, hi2 = boots2[50], boots2[1949]
    print()
    print(f"  Block bootstrap (5-trade blocks) 95% CI: [${lo2:+.2f}, ${hi2:+.2f}]")
    print(f"  CI excludes $0?  {'YES' if lo2 > 0 else 'NO — likely noise'}")

    # ═══════════════════════════════════════════════════════════════
    hr("4. Performance over time (split by halves)")
    arb_sorted = sorted(arb, key=lambda t: t["ts"])
    half = len(arb_sorted) // 2
    first = arb_sorted[:half]
    second = arb_sorted[half:]
    f_pnl = sum(t["pnl"] for t in first)
    s_pnl = sum(t["pnl"] for t in second)
    f_wr = sum(1 for t in first if t["pnl"] > 0) / len(first) * 100
    s_wr = sum(1 for t in second if t["pnl"] > 0) / len(second) * 100
    fts = datetime.fromtimestamp(first[0]["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
    fte = datetime.fromtimestamp(first[-1]["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
    sts = datetime.fromtimestamp(second[0]["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
    ste = datetime.fromtimestamp(second[-1]["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
    print(f"  First half:   {fts} → {fte}   n={len(first):>3}  PnL ${f_pnl:+8,.0f}  WR {f_wr:.1f}%   $/tr ${f_pnl/len(first):+.2f}")
    print(f"  Second half:  {sts} → {ste}   n={len(second):>3}  PnL ${s_pnl:+8,.0f}  WR {s_wr:.1f}%   $/tr ${s_pnl/len(second):+.2f}")
    if f_pnl > 0 and s_pnl > 0:
        print(f"  → BOTH halves profitable. Monotonic.")
    elif f_pnl > 0 or s_pnl > 0:
        print(f"  → only one half profitable — strong concentration risk")
    else:
        print(f"  → both halves losing (?)")

    # 4-quartile split
    print()
    print("  Quartile split:")
    q = len(arb_sorted) // 4
    quartiles = [arb_sorted[i*q:(i+1)*q] for i in range(4)]
    quartiles[3] = arb_sorted[3*q:]  # last quartile gets remainder
    for i, qtile in enumerate(quartiles, 1):
        qpnl = sum(t["pnl"] for t in qtile)
        qwr = sum(1 for t in qtile if t["pnl"] > 0) / len(qtile) * 100
        st = datetime.fromtimestamp(qtile[0]["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        et = datetime.fromtimestamp(qtile[-1]["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        print(f"    Q{i} {st}→{et}  n={len(qtile):>3}  ${qpnl:+8,.0f}  WR {qwr:>5.1f}%  $/tr ${qpnl/len(qtile):+.2f}")

    # ═══════════════════════════════════════════════════════════════
    hr("5. Direction breakdown")
    for d in ("YES", "NO"):
        sub = [t for t in arb if t["direction"] == d]
        if not sub: continue
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        print(f"  {d}:  n={len(sub):>3}  PnL ${spnl:+8,.0f}  WR {swr:.1f}%  $/tr ${spnl/len(sub):+.2f}")

    # ═══════════════════════════════════════════════════════════════
    hr("6. Time-remaining bucket breakdown")
    def tbucket(tr):
        if tr >= 240: return "240-300"
        if tr >= 180: return "180-240"
        if tr >= 120: return "120-180"
        if tr >= 60:  return "60-120"
        return "0-60"
    by_tr = defaultdict(list)
    for t in arb:
        by_tr[tbucket(t["tr"])].append(t)
    for b in ["240-300", "180-240", "120-180", "60-120", "0-60"]:
        sub = by_tr.get(b, [])
        if not sub: continue
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        print(f"  T-{b:<8}  n={len(sub):>3}  PnL ${spnl:+8,.0f}  WR {swr:>5.1f}%  $/tr ${spnl/len(sub):+.2f}")

    # ═══════════════════════════════════════════════════════════════
    hr("7. Hour-of-day breakdown (UTC)")
    by_hour = defaultdict(list)
    for t in arb:
        h = datetime.fromtimestamp(t["ts"], tz=timezone.utc).hour
        by_hour[h].append(t)
    profitable_hours = 0
    for h in sorted(by_hour.keys()):
        sub = by_hour[h]
        spnl = sum(t["pnl"] for t in sub)
        if spnl > 0: profitable_hours += 1
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        bar = "█" * max(1, int(abs(spnl) / 50))
        sign = "+" if spnl >= 0 else "-"
        print(f"  {h:02d}:00 UTC   n={len(sub):>3}  ${spnl:+8,.0f}  {sign}{bar}")
    print(f"  → {profitable_hours}/{len(by_hour)} hours profitable")

    # ═══════════════════════════════════════════════════════════════
    hr("8. Strike-distance (delta_bps) profile when firing")
    deltas = [t["abs_delta"] for t in arb]
    print(f"  abs(delta_bps) when firing:")
    print(f"    median {median(deltas):>6.1f}   mean {mean(deltas):>6.1f}")
    print(f"    p25 {pct(deltas, 0.25):>6.1f}   p75 {pct(deltas, 0.75):>6.1f}")
    print(f"    p10 {pct(deltas, 0.10):>6.1f}   p90 {pct(deltas, 0.90):>6.1f}")

    # PnL by delta bucket
    def dbucket(d):
        if d < 2: return "0-2"
        if d < 5: return "2-5"
        if d < 10: return "5-10"
        if d < 20: return "10-20"
        return "20+"
    by_d = defaultdict(list)
    for t in arb:
        by_d[dbucket(t["abs_delta"])].append(t)
    print()
    for b in ["0-2", "2-5", "5-10", "10-20", "20+"]:
        sub = by_d.get(b, [])
        if not sub: continue
        spnl = sum(t["pnl"] for t in sub)
        swr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        print(f"  delta {b:<6}bp  n={len(sub):>3}  ${spnl:+8,.0f}  WR {swr:.1f}%  $/tr ${spnl/len(sub):+.2f}")

    # ═══════════════════════════════════════════════════════════════
    hr("9. oracle_arb vs oracle_1 head-to-head (same window comparison)")
    # For each window where BOTH fired, who won?
    arb_by_w = defaultdict(list)
    or1_by_w = defaultdict(list)
    for t in arb: arb_by_w[t["window"]].append(t)
    for t in or1: or1_by_w[t["window"]].append(t)
    common = sorted(set(arb_by_w.keys()) & set(or1_by_w.keys()))
    if common:
        arb_won = 0
        or1_won = 0
        ties = 0
        arb_total = 0
        or1_total = 0
        for w in common:
            a = sum(t["pnl"] for t in arb_by_w[w])
            o = sum(t["pnl"] for t in or1_by_w[w])
            arb_total += a
            or1_total += o
            if a > o: arb_won += 1
            elif o > a: or1_won += 1
            else: ties += 1
        print(f"  Windows where BOTH fired: {len(common)}")
        print(f"  oracle_arb total in those windows:  ${arb_total:+,.0f}")
        print(f"  oracle_1   total in those windows:  ${or1_total:+,.0f}")
        print(f"  arb won {arb_won}  /  or1 won {or1_won}  /  ties {ties}")
        print(f"  arb's edge in head-to-head: ${(arb_total - or1_total):+,.0f}")
    else:
        print("  no overlapping windows")

    # How selective is arb vs or1?
    print()
    print(f"  oracle_arb fires in:  {len(arb_by_w)} unique windows")
    print(f"  oracle_1   fires in:  {len(or1_by_w)} unique windows")
    print(f"  Selectivity ratio: arb fires {len(arb_by_w)/len(or1_by_w):.0%} as often as or1")

    # ═══════════════════════════════════════════════════════════════
    hr("10. Verdict")
    has_real_edge = lo2 > 0
    monotonic = f_pnl > 0 and s_pnl > 0
    not_outlier_driven = sum(sorted_pnls[:10]) / total < 0.6
    profitable_majority_hours = profitable_hours / len(by_hour) >= 0.6

    checks = [
        ("Block-bootstrap CI excludes $0", has_real_edge),
        ("Both halves profitable", monotonic),
        ("Top 10 trades < 60% of total PnL", not_outlier_driven),
        ("Majority of hours (≥60%) profitable", profitable_majority_hours),
    ]
    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {name}")
    print()
    print(f"  Passed: {passed}/4")
    if passed == 4:
        print(f"  → REAL EDGE — oracle_arb's mispricing detection appears to be a genuine alpha source")
    elif passed >= 3:
        print(f"  → LIKELY REAL but small sample, monitor closely")
    elif passed >= 2:
        print(f"  → AMBIGUOUS — could be real, could be regime")
    else:
        print(f"  → NOT YET CONFIRMED — sample too small or PnL too concentrated")


if __name__ == "__main__":
    main()
