"""Compute the key viability metrics for each active session and classify."""
import csv
from pathlib import Path
from datetime import datetime, timezone

DATA = Path("data")
ACTIVE = [
    "oracle_1","oracle_alpha","oracle_arb","oracle_chrono","oracle_consensus",
    "oracle_echo","oracle_omega","oracle_titan","pivot_1",
    "blitz_1","test_ic_wide",
    # killed 2026-04-10 (bleeders): edge_hunter, test_xp
]


def load(name):
    p = DATA / name / "trades.csv"
    if not p.exists(): return []
    out = []
    with open(p) as f:
        for r in csv.DictReader(f):
            try:
                out.append({"ts": float(r["timestamp"]), "pnl": float(r["pnl_taker"])})
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


def metrics(trades):
    n = len(trades)
    if n < 5:
        return None
    pnls = [t["pnl"] for t in trades]
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n * 100
    dpt = total / n

    # Cum series + max DD
    cum = []
    c = 0.0
    for t in trades:
        c += t["pnl"]
        cum.append((t["ts"], c))
    peak = 0
    max_dd = 0
    for _, x in cum:
        if x > peak: peak = x
        if peak - x > max_dd: max_dd = peak - x

    # R² vs time
    times = [x[0] for x in cum]
    cums = [x[1] for x in cum]
    t0 = times[0]
    norm_t = [t - t0 for t in times]
    np_ = len(norm_t)
    if np_ < 2:
        r2 = 0
    else:
        sum_t = sum(norm_t); sum_p = sum(cums)
        sum_tt = sum(x*x for x in norm_t); sum_tp = sum(x*p for x, p in zip(norm_t, cums))
        mean_t = sum_t / np_; mean_p = sum_p / np_
        denom = sum_tt - np_ * mean_t**2
        if denom <= 0:
            r2 = 0
        else:
            slope = (sum_tp - np_ * mean_t * mean_p) / denom
            intercept = mean_p - slope * mean_t
            ss_tot = sum((p - mean_p)**2 for p in cums)
            ss_res = sum((p - (intercept + slope*t))**2 for t, p in zip(norm_t, cums))
            r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
            if slope < 0: r2 = -abs(r2)

    # Rolling 6h
    duration_h = (cum[-1][0] - cum[0][0]) / 3600
    rolling_pct = None
    worst_6h = None
    if duration_h >= 6:
        pos = tot = 0
        worst = float("inf")
        first = cum[0][0]; last = cum[-1][0]
        t = first
        while t + 6*3600 <= last:
            ws, we = t, t + 6*3600
            sp = ep = None
            for ts, x in cum:
                if sp is None and ts >= ws: sp = x
                if ts <= we: ep = x
                else: break
            if sp is not None and ep is not None:
                wpnl = ep - sp
                if wpnl > 0: pos += 1
                if wpnl < worst: worst = wpnl
                tot += 1
            t += 1800  # 30-min steps for more samples in short windows
        if tot > 0:
            rolling_pct = pos / tot * 100
            worst_6h = worst

    calmar = total / max_dd if max_dd > 0 else None

    return {
        "n": n, "pnl": total, "wr": wr, "dpt": dpt,
        "r2": r2, "max_dd": max_dd,
        "calmar": calmar, "rolling_pct": rolling_pct, "worst_6h": worst_6h,
        "dur_h": duration_h,
    }


def archetype(m):
    if m is None:
        return "too few trades"
    r2 = m["r2"]; cal = m["calmar"] or 0; wr = m["wr"]; dpt = m["dpt"]
    pos_pct = m["rolling_pct"] or 0
    pnl = m["pnl"]

    if pnl < 0:
        return "BLEEDER — losing"
    if cal is None or cal == 0:
        return "no drawdown data"
    if r2 < 0:
        return "BLEEDER — trend down"
    # Lucky outlier: high $/tr but R² low
    if dpt > 30 and r2 < 0.70:
        return "LUCKY OUTLIER — few big wins, watch closely"
    # Variance trap: calmar low despite decent R²
    if r2 > 0.60 and cal < 2:
        return "VARIANCE TRAP — bumpy, low risk-adjusted"
    # Grinder: smooth, steady
    if r2 > 0.85 and cal >= 5 and pos_pct >= 80:
        return "GRINDER — reliable, boring, safe"
    # High-conviction sniper: high $/tr + solid R²
    if dpt >= 30 and r2 >= 0.70 and cal >= 3:
        return "HIGH-CONVICTION SNIPER"
    # Workhorse
    if r2 > 0.75 and cal >= 3 and 55 <= wr <= 70 and 5 <= dpt <= 25:
        return "WORKHORSE ORACLE"
    # Volatile winner
    if r2 > 0.60 and cal >= 2:
        return "VOLATILE WINNER — profitable but bumpy"
    # Marginal
    if r2 > 0.30 and cal >= 1:
        return "MARGINAL — weak trend"
    return "RANDOM WALK — no clear edge"


def main():
    rows = []
    for name in ACTIVE:
        trades = load(name)
        if not trades:
            rows.append((name, None))
            continue
        m = metrics(trades)
        rows.append((name, m))

    # Print scorecard
    print()
    print("=" * 125)
    print(f"  {'session':<20} {'n':<4} {'dur':<6} {'pnl':<10} {'R²':<7} {'Cal':<7} {'WR':<7} {'$/tr':<8} {'6h+':<7} {'w6h':<9}  archetype")
    print("=" * 125)
    for name, m in rows:
        if m is None:
            print(f"  {name:<20} (no data)")
            continue
        r2_s = f"{m['r2']:.2f}" if m['r2'] is not None else " — "
        cal_s = f"{m['calmar']:.1f}" if m['calmar'] is not None else " — "
        pos_s = f"{m['rolling_pct']:.0f}%" if m['rolling_pct'] is not None else "  — "
        w6h_s = f"${m['worst_6h']:+.0f}" if m['worst_6h'] is not None else "  — "
        arch = archetype(m)
        print(f"  {name:<20} {m['n']:<4} {m['dur_h']:>4.1f}h  ${m['pnl']:>+7,.0f}  {r2_s:<6} {cal_s:<6} {m['wr']:>5.1f}% ${m['dpt']:>+5.1f}  {pos_s:<6} {w6h_s:<8}  {arch}")

    print()
    print("  ⚠ All durations are small (under 24h). Treat as early-read, not final verdict.")
    print("  ⚠ Worst_6h needs ≥6h of data; for sessions <6h old it reports the longest available window.")


if __name__ == "__main__":
    main()
