"""
Quick correlation + recent-regime analysis on the live sessions.

Goal: the user noticed the dashboard shows highly correlated sessions.
We want to know:
  1. Pairwise hourly-PnL correlation among the 7 active oracle/* + blitz/test sessions
  2. Same correlation but restricted to the last 24h (regime-of-now check)
  3. Same restricted to the window since oracle_omega launched (~02:30 UTC Apr 10)
  4. Per-session PnL in those windows + a "diversification value" score
  5. Whether oracle_omega is doing anything different from oracle_1
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone
import math

DATA = Path("data")

ACTIVE = [
    "oracle_1", "oracle_arb", "oracle_consensus", "oracle_chrono",
    "oracle_alpha", "oracle_omega", "oracle_echo",
    "blitz_1", "edge_hunter", "test_xp", "test_ic_wide",
]


def load_trades(name):
    p = DATA / name / "trades.csv"
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for row in csv.DictReader(f):
            try:
                out.append({
                    "ts": float(row["timestamp"]),
                    "pnl": float(row["pnl_taker"]),
                    "direction": row.get("direction", ""),
                })
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


def hourly_pnl(trades, t_start=None, t_end=None):
    """Bucket pnl by floor(ts/3600). Returns dict {hour_epoch: pnl}."""
    out = defaultdict(float)
    for t in trades:
        if t_start is not None and t["ts"] < t_start:
            continue
        if t_end is not None and t["ts"] > t_end:
            continue
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


def aligned_series(h_a, h_b):
    """Return two lists aligned on common hours (only hours where both have a value)."""
    common = sorted(set(h_a.keys()) & set(h_b.keys()))
    return [h_a[h] for h in common], [h_b[h] for h in common], common


def fmt_corr(c):
    if c is None: return "  -- "
    if c >= 0.7:  return "\033[91m{:+.2f}\033[0m".format(c)  # red — highly correlated
    if c >= 0.4:  return "\033[93m{:+.2f}\033[0m".format(c)  # yellow — moderate
    if c >= -0.4: return "\033[92m{:+.2f}\033[0m".format(c)  # green — independent
    return "\033[94m{:+.2f}\033[0m".format(c)                # blue — anti-correlated


def section(title, t_start, t_end, sessions):
    print()
    print("=" * 100)
    if t_start is not None:
        s_str = datetime.fromtimestamp(t_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        e_str = datetime.fromtimestamp(t_end, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t_end else "now"
        print(f"  {title}  [{s_str} → {e_str}]")
    else:
        print(f"  {title}  [ALL DATA]")
    print("=" * 100)

    # Per-session PnL in window
    print(f"\n  {'Session':<22} {'Trades':>7} {'PnL':>10} {'WR':>7} {'$/tr':>8}")
    series = {}
    for name, trs in sessions.items():
        sub = [t for t in trs if (t_start is None or t["ts"] >= t_start) and (t_end is None or t["ts"] <= t_end)]
        if not sub:
            continue
        n = len(sub)
        pnl = sum(t["pnl"] for t in sub)
        wins = sum(1 for t in sub if t["pnl"] > 0)
        wr = wins / n * 100 if n else 0
        series[name] = sub
        print(f"  {name:<22} {n:>7} {pnl:>+10,.0f} {wr:>6.1f}% {pnl/n if n else 0:>+8.2f}")

    # Hourly buckets per session
    hourly = {n: hourly_pnl(trs, t_start, t_end) for n, trs in series.items()}

    names = list(hourly.keys())
    if len(names) < 2:
        return

    # Pairwise corr matrix
    print(f"\n  pairwise hourly-PnL correlation (red≥0.7, yellow≥0.4, green=ind, blue=anti)\n")
    short = {n: n.replace("oracle_", "or_").replace("test_", "t_")[:11] for n in names}
    header = " " * 14 + "".join(f"{short[n]:>13}" for n in names)
    print(header)
    pairs_corr = []
    for n1 in names:
        row = f"  {short[n1]:<12}"
        for n2 in names:
            if n1 == n2:
                row += f"{'  --':>13}"
                continue
            a, b, common = aligned_series(hourly[n1], hourly[n2])
            c = pearson(a, b)
            if n1 < n2 and c is not None and len(common) >= 5:
                pairs_corr.append((n1, n2, c, len(common)))
            row += "         " + fmt_corr(c)
        print(row)

    # Top correlated pairs
    if pairs_corr:
        pairs_corr.sort(key=lambda x: -abs(x[2]))
        print(f"\n  most correlated pairs (n_hours ≥ 5):")
        for n1, n2, c, nh in pairs_corr[:8]:
            tag = "highly correlated" if abs(c) >= 0.7 else "moderate" if abs(c) >= 0.4 else "independent"
            print(f"    {n1:<22} ↔ {n2:<22}  r={c:+.2f}  nh={nh:>3}  {tag}")

    # Effective N: 1 / (1 + (k-1)*ravg) — how many independent sessions are we really running?
    if pairs_corr:
        ravg = sum(c for _, _, c, _ in pairs_corr) / len(pairs_corr)
        k = len(names)
        eff = k / (1 + (k - 1) * max(0, ravg))
        print(f"\n  Average pairwise correlation: \033[1m{ravg:+.2f}\033[0m")
        print(f"  Effective independent sessions: \033[1m{eff:.1f}\033[0m of {k}  "
              f"(diversification {eff/k*100:.0f}%)")
        if ravg >= 0.6:
            print(f"  → all sessions are essentially the same bet right now")
        elif ravg >= 0.3:
            print(f"  → moderate overlap, some diversification")
        else:
            print(f"  → genuinely diversified, sessions are different bets")


def main():
    sessions = {}
    for name in ACTIVE:
        trs = load_trades(name)
        if trs:
            sessions[name] = trs

    if not sessions:
        print("no sessions found")
        return

    now = max(t["ts"] for trs in sessions.values() for t in trs)
    print(f"\n  Latest trade timestamp: {datetime.fromtimestamp(now, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Sessions loaded: {len(sessions)}")

    # All time
    section("ALL DATA", None, None, sessions)

    # Last 24h
    section("LAST 24 HOURS", now - 86400, now, sessions)

    # Last 6h (the regime-of-now)
    section("LAST 6 HOURS", now - 6 * 3600, now, sessions)

    # Since omega launched (~ 08:08 UTC Apr 10)
    omega_start = 1775808504  # Apr 10 08:08 UTC
    section("SINCE OMEGA LAUNCH", omega_start, now, sessions)


if __name__ == "__main__":
    main()
