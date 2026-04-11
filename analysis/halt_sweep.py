"""
Sweep the halt parameters on omega+settle to find a config that recovers
full 4/4 viability on the latest data.

The validated original was: lookback=8, threshold=-400, cooldown=60min.
With newer data, that misses a bleed cluster. We sweep:
  lookback ∈ {5, 6, 8, 10, 12}
  threshold ∈ {-200, -300, -400, -500}
  cooldown ∈ {30, 60, 90}
"""

import csv
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
                    "direction": row["direction"],
                    "abs_delta": abs(float(row.get("delta_bps", 0))),
                    "time_remaining": float(row["time_remaining"]),
                    "hour_et": datetime.fromtimestamp(float(row["timestamp"])).hour,
                })
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


def chrono_bucket(tr):
    if tr >= 270: return "270-300"
    if tr >= 240: return "240-270"
    if tr >= 210: return "210-240"
    if tr >= 180: return "180-210"
    if tr >= 150: return "150-180"
    if tr >= 120: return "120-150"
    if tr >= 90:  return "90-120"
    if tr >= 60:  return "60-90"
    if tr >= 30:  return "30-60"
    if tr >= 5:   return "5-30"
    return "0-5"


CHRONO_BLOCKED = {("60-90", "NO"), ("5-30", "NO"), ("270-300", "NO")}
CHRONO_BOOSTED = {("210-240", "NO"), ("150-180", "YES"), ("180-210", "YES")}


def run(trades, lookback, threshold, cooldown_min):
    out = []
    recent_pnl = []
    halt_until = 0.0
    for t in trades:
        if t["ts"] < halt_until: continue
        bucket = chrono_bucket(t["time_remaining"])
        if (bucket, t["direction"]) in CHRONO_BLOCKED: continue
        if 0 <= t["hour_et"] < 5: continue
        if t["time_remaining"] > 270: continue  # settle filter
        if len(recent_pnl) >= lookback and sum(recent_pnl[-lookback:]) < threshold:
            halt_until = t["ts"] + cooldown_min * 60
            recent_pnl = []
            continue
        boost = 1.3 if (bucket, t["direction"]) in CHRONO_BOOSTED else 1.0
        new_pnl = t["pnl"] * boost
        recent_pnl.append(t["pnl"])
        out.append({"ts": t["ts"], "pnl_new": new_pnl})
    return out


def metrics(trades):
    if len(trades) < 30: return None
    n = len(trades)
    pnls = [t["pnl_new"] for t in trades]
    total = sum(pnls)
    sorted_t = sorted(trades, key=lambda x: x["ts"])
    cum = []
    c = 0.0
    for t in sorted_t:
        c += t["pnl_new"]
        cum.append((t["ts"], c))

    peak = 0
    max_dd = 0
    for _, x in cum:
        if x > peak: peak = x
        if peak - x > max_dd: max_dd = peak - x

    times = [x[0] for x in cum]
    cums = [x[1] for x in cum]
    t0 = times[0]
    norm_t = [t - t0 for t in times]
    n_p = len(norm_t)
    if n_p < 2: return None
    sum_t = sum(norm_t); sum_p = sum(cums)
    sum_tt = sum(x*x for x in norm_t); sum_tp = sum(x*p for x, p in zip(norm_t, cums))
    mean_t = sum_t / n_p; mean_p = sum_p / n_p
    denom = sum_tt - n_p * mean_t**2
    if denom <= 0:
        r2 = 0
    else:
        slope = (sum_tp - n_p * mean_t * mean_p) / denom
        intercept = mean_p - slope * mean_t
        ss_tot = sum((p - mean_p)**2 for p in cums)
        ss_res = sum((p - (intercept + slope * t))**2 for t, p in zip(norm_t, cums))
        r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        if slope < 0: r2 = -abs(r2)

    duration_h = (cum[-1][0] - cum[0][0]) / 3600
    rolling_pct = None; worst_6h = None
    if duration_h >= 12:
        positive = total_w = 0
        worst = float("inf")
        first_ts = cum[0][0]; last_ts = cum[-1][0]
        t = first_ts
        while t + 6*3600 <= last_ts:
            ws, we = t, t + 6*3600
            sp = ep = None
            for ts, x in cum:
                if sp is None and ts >= ws: sp = x
                if ts <= we: ep = x
                else: break
            if sp is not None and ep is not None:
                wpnl = ep - sp
                if wpnl > 0: positive += 1
                if wpnl < worst: worst = wpnl
                total_w += 1
            t += 3600
        if total_w > 0:
            rolling_pct = positive / total_w * 100
            worst_6h = worst

    calmar = total / max_dd if max_dd > 0 else None
    return {"n": n, "pnl": total, "r2": r2, "rolling_pct": rolling_pct,
            "worst_6h": worst_6h, "calmar": calmar, "max_dd": max_dd}


def passes(m):
    if m is None: return 0
    p = 0
    if m["r2"] >= 0.85: p += 1
    if m["rolling_pct"] is not None and m["rolling_pct"] >= 75: p += 1
    if m["worst_6h"] is not None and m["worst_6h"] >= -500: p += 1
    if m["calmar"] is not None and m["calmar"] >= 2.0: p += 1
    return p


def main():
    trades = load_trades("oracle_1")
    print(f"\n  Loaded {len(trades)} trades, sweeping omega+settle halt parameters\n")
    print(f"  {'lookback':<9} {'thresh':<8} {'cooldown':<10} {'n':<5} {'pnl':<10} {'R²':<7} {'6h+':<7} {'w6h':<10} {'cal':<6} {'pass'}")
    print("  " + "-" * 90)

    best = None
    rows = []
    for lookback in [5, 6, 8, 10, 12]:
        for thresh in [-200, -300, -400, -500]:
            for cooldown in [30, 60, 90]:
                m = metrics(run(trades, lookback, thresh, cooldown))
                if m is None: continue
                p = passes(m)
                rows.append((p, m["pnl"], lookback, thresh, cooldown, m))
                tag = "VIABLE" if p == 4 else "PROM" if p == 3 else "BORD" if p == 2 else "----"
                print(f"  {lookback:<9} {thresh:<8} {cooldown:<10} {m['n']:<5} ${m['pnl']:>+8,.0f}  {m['r2']:.3f}  {m['rolling_pct']:>5.1f}% ${m['worst_6h']:>+7,.0f}  {m['calmar']:.2f}  {p}/4 {tag}")

    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    print()
    print("  TOP 5 by viability score then PnL:")
    for i, (p, pnl, lb, th, cd, m) in enumerate(rows[:5], 1):
        print(f"    {i}. lookback={lb}, thresh={th}, cooldown={cd}min  →  "
              f"{p}/4   ${pnl:+,.0f}   R²={m['r2']:.3f}   6h+={m['rolling_pct']:.1f}%   "
              f"w6h=${m['worst_6h']:+,.0f}   cal={m['calmar']:.2f}")

    viable = [r for r in rows if r[0] == 4]
    if viable:
        print(f"\n  → {len(viable)} configs reach VIABLE (4/4)")
    else:
        # Best 3/4
        prom = [r for r in rows if r[0] == 3]
        print(f"\n  → No 4/4 configs found. Best is {prom[0][0] if prom else 0}/4. "
              f"The new bleed cluster is unfilterable by halt-tuning alone.")


if __name__ == "__main__":
    main()
