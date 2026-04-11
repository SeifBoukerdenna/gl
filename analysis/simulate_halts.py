"""
Post-hoc halt + daily-budget simulator.

Reads any session's trades.csv, walks trades chronologically, and applies
the halt logic exactly as the live engine does. Outputs the counterfactual
PnL and metrics under the halt config.

USAGE
=====
Single session, default halt config (5/-$200/90min/-$1500):
    python analysis/simulate_halts.py oracle_arb

Single session, custom halt config:
    python analysis/simulate_halts.py oracle_arb \
        --lookback 8 --threshold -400 --cooldown 60 --budget -2000

All sessions, same config (mass comparison):
    python analysis/simulate_halts.py --all

Single session, grid sweep (find best config):
    python analysis/simulate_halts.py oracle_arb --sweep

All sessions, grid sweep, save summary CSV:
    python analysis/simulate_halts.py --all --sweep --out halt_sweep.csv
"""
import argparse
import csv
import math
from collections import deque, defaultdict
from datetime import datetime
from pathlib import Path

DATA = Path("data")


# ════════════════════════════════════════════════════════════════════
# DATA LOADING
# ════════════════════════════════════════════════════════════════════
def load_trades(name):
    p = DATA / name / "trades.csv"
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for r in csv.DictReader(f):
            try:
                out.append({
                    "ts": float(r["timestamp"]),
                    "pnl": float(r["pnl_taker"]),
                    "direction": r.get("direction", ""),
                    "window": int(float(r.get("window_start", 0))),
                })
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


# ════════════════════════════════════════════════════════════════════
# HALT SIMULATION — matches live engine logic exactly
# ════════════════════════════════════════════════════════════════════
def simulate_halt(trades, lookback=5, threshold=-200, cooldown_min=90,
                  daily_budget=-1500):
    """
    Walk trades chronologically, apply halt + daily budget.

    Returns a dict with:
      taken:      list of trades that would have been taken (pnl unchanged)
      skipped:    list of trades that would have been blocked (with reason)
      trips:      list of halt trip events
      days_stopped: count of days where daily budget was hit
    """
    recent_pnl = deque(maxlen=lookback)
    halt_until = 0.0
    daily_pnl = defaultdict(float)
    days_stopped = set()

    taken = []
    skipped = []
    trips = []

    for t in trades:
        ts = t["ts"]
        day_key = int(ts // 86400)

        # 1. Daily budget check
        if day_key in days_stopped:
            skipped.append({**t, "reason": "daily_budget"})
            continue
        if daily_pnl[day_key] < daily_budget:
            days_stopped.add(day_key)
            skipped.append({**t, "reason": "daily_budget"})
            continue

        # 2. Halt cooldown check
        if ts < halt_until:
            skipped.append({**t, "reason": "halt_cooldown"})
            continue

        # 3. Halt trigger check (fires BEFORE we take this trade)
        if len(recent_pnl) >= lookback and sum(recent_pnl) < threshold:
            halt_until = ts + cooldown_min * 60
            trips.append({
                "ts": ts,
                "cum_before": sum(recent_pnl),
                "day": day_key,
            })
            recent_pnl.clear()
            skipped.append({**t, "reason": "halt_tripped"})
            continue

        # 4. Take the trade
        taken.append(t)
        recent_pnl.append(t["pnl"])
        daily_pnl[day_key] += t["pnl"]

    return {
        "taken": taken,
        "skipped": skipped,
        "trips": trips,
        "days_stopped": len(days_stopped),
    }


# ════════════════════════════════════════════════════════════════════
# METRICS — viability framework
# ════════════════════════════════════════════════════════════════════
def metrics(trades):
    """Viability metrics for a trade list."""
    if len(trades) < 3:
        return None
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n * 100

    sorted_t = sorted(trades, key=lambda x: x["ts"])
    cum = []
    c = 0.0
    for t in sorted_t:
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

    # Rolling 6h (with 30-min step for short histories)
    duration_h = (cum[-1][0] - cum[0][0]) / 3600
    rolling_pct = None; worst_6h = None
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
            t += 1800
        if tot > 0:
            rolling_pct = pos / tot * 100
            worst_6h = worst

    calmar = total / max_dd if max_dd > 0 else None

    return {
        "n": n, "pnl": total, "wr": wr, "dpt": total/n if n else 0,
        "r2": r2, "max_dd": max_dd, "calmar": calmar,
        "rolling_pct": rolling_pct, "worst_6h": worst_6h,
        "dur_h": duration_h,
    }


# ════════════════════════════════════════════════════════════════════
# PRESENTATION
# ════════════════════════════════════════════════════════════════════
def fmt_m(m):
    """Short one-line summary of a metrics dict."""
    if m is None:
        return "(n<3)"
    r2 = f"{m['r2']:.2f}"
    cal = f"{m['calmar']:.1f}" if m['calmar'] is not None else "--"
    w6 = f"${m['worst_6h']:+.0f}" if m['worst_6h'] is not None else "--"
    six = f"{m['rolling_pct']:.0f}%" if m['rolling_pct'] is not None else "--"
    return (f"n={m['n']:<4} pnl=${m['pnl']:>+7,.0f}  WR={m['wr']:>4.1f}%  "
            f"$/tr=${m['dpt']:>+5.1f}  R²={r2}  Cal={cal}  6h+={six}  w6h={w6}")


def run_single(session, lookback, threshold, cooldown_min, daily_budget, verbose=True):
    """Simulate halts on one session, print comparison."""
    trades = load_trades(session)
    if not trades:
        print(f"  {session}: no data")
        return None

    orig_m = metrics(trades)
    result = simulate_halt(trades, lookback, threshold, cooldown_min, daily_budget)
    prot_m = metrics(result["taken"])

    if verbose:
        print(f"\n  ══ {session} ══")
        print(f"    halt config: lookback={lookback}, thresh=${threshold}, "
              f"cooldown={cooldown_min}min, daily_budget=${daily_budget}")
        print(f"    original:   {fmt_m(orig_m)}")
        print(f"    protected:  {fmt_m(prot_m)}")
        if orig_m and prot_m:
            d_pnl = prot_m['pnl'] - orig_m['pnl']
            d_n = prot_m['n'] - orig_m['n']
            d_dd = (prot_m['max_dd'] - orig_m['max_dd'])
            d_r2 = prot_m['r2'] - orig_m['r2']
            d_cal = (prot_m['calmar'] or 0) - (orig_m['calmar'] or 0)
            print(f"    delta:      n{d_n:+d}  pnl${d_pnl:+,.0f}  "
                  f"max_dd${d_dd:+,.0f}  R²{d_r2:+.2f}  Cal{d_cal:+.1f}")

        skipped = result["skipped"]
        skip_counts = defaultdict(int)
        skip_pnl_saved = defaultdict(float)
        for s in skipped:
            skip_counts[s["reason"]] += 1
            skip_pnl_saved[s["reason"]] += s["pnl"]  # pnl that WOULDN'T have happened

        print(f"    halt trips: {len(result['trips'])}")
        print(f"    days budget-stopped: {result['days_stopped']}")
        for reason in ("halt_tripped", "halt_cooldown", "daily_budget"):
            if skip_counts[reason]:
                pnl_avoided = -skip_pnl_saved[reason]  # positive = losses avoided
                sign = "saved" if pnl_avoided > 0 else "foregone"
                print(f"      {reason:<16} {skip_counts[reason]:>4} trades  "
                      f"${abs(pnl_avoided):>6,.0f} {sign}")

    return {
        "session": session, "orig": orig_m, "prot": prot_m, "sim": result,
        "config": (lookback, threshold, cooldown_min, daily_budget),
    }


def sweep_session(session, verbose=False):
    """Grid search halt configs on one session, return ranked list."""
    trades = load_trades(session)
    if not trades or len(trades) < 20:
        return []

    configs = []
    for lookback in [3, 5, 8, 12]:
        for threshold in [-150, -200, -300, -400]:
            for cooldown in [30, 60, 90]:
                for budget in [-1000, -1500, -2500]:
                    result = simulate_halt(trades, lookback, threshold, cooldown, budget)
                    prot_m = metrics(result["taken"])
                    if prot_m is None:
                        continue
                    configs.append({
                        "lookback": lookback, "threshold": threshold,
                        "cooldown": cooldown, "budget": budget,
                        "n": prot_m["n"], "pnl": prot_m["pnl"],
                        "r2": prot_m["r2"], "calmar": prot_m.get("calmar") or 0,
                        "worst_6h": prot_m.get("worst_6h") or 0,
                        "rolling_pct": prot_m.get("rolling_pct") or 0,
                        "halt_trips": len(result["trips"]),
                    })

    # Rank by: viability score (4/4 ideal), then by PnL
    def viability(c):
        v = 0
        if c["r2"] >= 0.85: v += 1
        if c["rolling_pct"] >= 75: v += 1
        if c["worst_6h"] >= -500: v += 1
        if c["calmar"] >= 2.0: v += 1
        return v

    for c in configs:
        c["viability"] = viability(c)

    configs.sort(key=lambda c: (c["viability"], c["pnl"]), reverse=True)
    return configs


def print_sweep_top(configs, session, n=10):
    print(f"\n  ══ Sweep for {session}: top {n} configs ══")
    print(f"    {'lookback':<9} {'thresh':<8} {'cool':<6} {'budget':<8} "
          f"{'n':<5} {'pnl':<10} {'R²':<6} {'Cal':<6} {'6h+':<6} {'w6h':<9} {'vi':<3}")
    for c in configs[:n]:
        print(f"    {c['lookback']:<9} ${c['threshold']:<7} {c['cooldown']:<6} "
              f"${c['budget']:<7} {c['n']:<5} ${c['pnl']:>+7,.0f}  "
              f"{c['r2']:<5.2f} {c['calmar']:<5.1f} "
              f"{c['rolling_pct']:<5.0f}% ${c['worst_6h']:>+6,.0f} {c['viability']}/4")


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?", default=None,
                    help="Session name (e.g., oracle_arb). Omit with --all.")
    ap.add_argument("--all", action="store_true",
                    help="Simulate on all sessions in data/ directory.")
    ap.add_argument("--sweep", action="store_true",
                    help="Grid-search halt configs instead of using a single config.")
    ap.add_argument("--out", help="Save sweep results to a CSV.")
    ap.add_argument("--lookback", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=-200)
    ap.add_argument("--cooldown", type=int, default=90, help="Cooldown minutes.")
    ap.add_argument("--budget", type=float, default=-1500, help="Daily loss budget.")
    args = ap.parse_args()

    if args.all:
        sessions = sorted([d.name for d in DATA.iterdir()
                          if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")])
    elif args.session:
        sessions = [args.session]
    else:
        ap.print_help()
        return

    print("=" * 120)
    print("  POST-HOC HALT SIMULATOR")
    print("=" * 120)

    if args.sweep:
        all_sweeps = []
        for s in sessions:
            sweep = sweep_session(s)
            if not sweep:
                continue
            print_sweep_top(sweep, s, n=5)
            if args.out:
                for c in sweep:
                    all_sweeps.append({"session": s, **c})
        if args.out and all_sweeps:
            with open(args.out, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(all_sweeps[0].keys()))
                w.writeheader()
                w.writerows(all_sweeps)
            print(f"\n  Saved {len(all_sweeps)} rows to {args.out}")
    else:
        for s in sessions:
            run_single(s, args.lookback, args.threshold, args.cooldown, args.budget,
                       verbose=True)


if __name__ == "__main__":
    main()
