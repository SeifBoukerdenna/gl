"""
Backtest the new brainstormed strategies against oracle_1 + omega baselines.

Strategies tested:
  oracle_1       baseline (no filters, no sizing changes)
  omega          chrono blocks + chrono boosts + overnight skip + rolling halt
  oracle_streak  rolling-16-trade WR continuous size scaler (replaces binary halt)
  oracle_spike   |delta_bps| ≥ 8 only, size ladder with delta magnitude
  oracle_settle  skip first 30s of every window (T > 270 rejected)
  oracle_smart   streak + spike + settle + omega chrono + overnight (kitchen sink)

All metrics use the standard viability framework:
  R² ≥ 0.85, 6h+ ≥ 75%, worst 6h ≥ -$500, Calmar ≥ 2.0
"""

import csv
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data")


def load_trades(name):
    p = DATA_DIR / name / "trades.csv"
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for row in csv.DictReader(f):
            try:
                out.append({
                    "ts": float(row["timestamp"]),
                    "pnl": float(row["pnl_taker"]),
                    "result": row["result"],
                    "direction": row["direction"],
                    "delta_bps": float(row.get("delta_bps", 0)),
                    "abs_delta": abs(float(row.get("delta_bps", 0))),
                    "time_remaining": float(row["time_remaining"]),
                    "fill_price": float(row["fill_price"]),
                    "filled_size": float(row.get("filled_size", 0)),
                    "hour_et": datetime.fromtimestamp(float(row["timestamp"])).hour,
                })
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


# ═══════════════════════════════════════════════════════════════════════
# Chrono buckets (omega filters)
# ═══════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════
# Strategy implementations
# ═══════════════════════════════════════════════════════════════════════

def strat_omega(trades):
    """omega = chrono + overnight + rolling halt (validated)."""
    out = []
    recent_pnl = []
    halt_until = 0.0

    for t in trades:
        # halt cooldown
        if t["ts"] < halt_until:
            continue

        # chrono block
        bucket = chrono_bucket(t["time_remaining"])
        if (bucket, t["direction"]) in CHRONO_BLOCKED:
            continue

        # overnight 12-5 ET skip
        if 0 <= t["hour_et"] < 5:
            continue

        # rolling halt check
        if len(recent_pnl) >= 8 and sum(recent_pnl[-8:]) < -400:
            halt_until = t["ts"] + 60 * 60
            recent_pnl = []
            continue

        # chrono boost
        boost = 1.3 if (bucket, t["direction"]) in CHRONO_BOOSTED else 1.0

        new_pnl = t["pnl"] * boost
        recent_pnl.append(t["pnl"])  # track ORIGINAL pnl for halt logic
        out.append({**t, "pnl_new": new_pnl})
    return out


def strat_streak(trades, lookback=16):
    """oracle_streak = continuous WR-adaptive sizing replaces binary halt.

    Tiers (rolling lookback-trade WR):
      WR > 65%       → 1.4x
      55% < WR ≤ 65% → 1.2x
      45% < WR ≤ 55% → 1.0x
      35% < WR ≤ 45% → 0.6x
      WR ≤ 35%       → halt 60min
    """
    out = []
    recent_pnl = []
    halt_until = 0.0

    for t in trades:
        if t["ts"] < halt_until:
            continue

        if len(recent_pnl) >= lookback:
            window = recent_pnl[-lookback:]
            wr = sum(1 for p in window if p > 0) / len(window)
            if wr > 0.65:   mult = 1.4
            elif wr > 0.55: mult = 1.2
            elif wr > 0.45: mult = 1.0
            elif wr > 0.35: mult = 0.6
            else:
                halt_until = t["ts"] + 60 * 60
                recent_pnl = []
                continue
        else:
            mult = 1.0

        new_pnl = t["pnl"] * mult
        recent_pnl.append(t["pnl"])
        out.append({**t, "pnl_new": new_pnl})
    return out


def strat_spike(trades, min_delta=8):
    """oracle_spike = |delta_bps| ≥ min_delta only, size ladder with delta.
      8-15 bp:  1.0x
      15-25 bp: 1.5x
      25+ bp:   2.0x
    """
    out = []
    for t in trades:
        if t["abs_delta"] < min_delta:
            continue
        d = t["abs_delta"]
        if d >= 25:   mult = 2.0
        elif d >= 15: mult = 1.5
        else:         mult = 1.0
        new_pnl = t["pnl"] * mult
        out.append({**t, "pnl_new": new_pnl})
    return out


def strat_settle(trades, max_tr=270):
    """oracle_settle = skip first 30s of every window (T_remaining > 270 → reject)."""
    out = []
    for t in trades:
        if t["time_remaining"] > max_tr:
            continue
        out.append({**t, "pnl_new": t["pnl"]})
    return out


def strat_smart(trades, lookback=16, min_delta=5, max_tr=270):
    """oracle_smart = stack everything: chrono + overnight + settle + spike(soft) + streak.

    Uses a softer spike threshold (5bp) since we're combining many filters.
    """
    out = []
    recent_pnl = []
    halt_until = 0.0

    for t in trades:
        if t["ts"] < halt_until:
            continue

        # omega filters
        bucket = chrono_bucket(t["time_remaining"])
        if (bucket, t["direction"]) in CHRONO_BLOCKED:
            continue
        if 0 <= t["hour_et"] < 5:
            continue

        # settle filter (skip first 30s)
        if t["time_remaining"] > max_tr:
            continue

        # spike filter (delta floor)
        if t["abs_delta"] < min_delta:
            continue

        # streak: continuous size scaler
        if len(recent_pnl) >= lookback:
            window = recent_pnl[-lookback:]
            wr = sum(1 for p in window if p > 0) / len(window)
            if wr > 0.65:   streak_mult = 1.4
            elif wr > 0.55: streak_mult = 1.2
            elif wr > 0.45: streak_mult = 1.0
            elif wr > 0.35: streak_mult = 0.6
            else:
                halt_until = t["ts"] + 60 * 60
                recent_pnl = []
                continue
        else:
            streak_mult = 1.0

        # spike size ladder
        d = t["abs_delta"]
        if d >= 25:   spike_mult = 2.0
        elif d >= 15: spike_mult = 1.5
        elif d >= 8:  spike_mult = 1.2
        else:         spike_mult = 1.0

        # chrono boost
        boost = 1.3 if (bucket, t["direction"]) in CHRONO_BOOSTED else 1.0

        combined = streak_mult * spike_mult * boost
        new_pnl = t["pnl"] * combined
        recent_pnl.append(t["pnl"])
        out.append({**t, "pnl_new": new_pnl})
    return out


# ═══════════════════════════════════════════════════════════════════════
# Metrics (identical to backtest_omega.py for direct comparability)
# ═══════════════════════════════════════════════════════════════════════

def compute_metrics(trades, pnl_field="pnl"):
    if len(trades) < 30:
        return None
    pnls = [t[pnl_field] for t in trades]
    n = len(pnls)
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n * 100

    sorted_t = sorted(trades, key=lambda x: x["ts"])
    cum_series = []
    c = 0.0
    for t in sorted_t:
        c += t[pnl_field]
        cum_series.append((t["ts"], c))

    peak = 0
    max_dd = 0
    for _, c in cum_series:
        if c > peak: peak = c
        dd = peak - c
        if dd > max_dd: max_dd = dd

    times = [t for t, _ in cum_series]
    cums = [c for _, c in cum_series]
    t0 = times[0]
    norm_t = [t - t0 for t in times]
    n_pts = len(norm_t)
    sum_t = sum(norm_t)
    sum_p = sum(cums)
    sum_tt = sum(x * x for x in norm_t)
    sum_tp = sum(x * p for x, p in zip(norm_t, cums))
    mean_t = sum_t / n_pts
    mean_p = sum_p / n_pts
    denom = sum_tt - n_pts * mean_t * mean_t
    if denom <= 0:
        r2 = 0
    else:
        slope = (sum_tp - n_pts * mean_t * mean_p) / denom
        intercept = mean_p - slope * mean_t
        ss_tot = sum((p - mean_p) ** 2 for p in cums)
        ss_res = sum((p - (intercept + slope * x)) ** 2 for x, p in zip(norm_t, cums))
        r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        if slope < 0: r2 = -abs(r2)

    duration_h = (cum_series[-1][0] - cum_series[0][0]) / 3600
    rolling_pct = None
    worst_6h = None
    if duration_h >= 12:
        positive = 0
        total = 0
        worst = float("inf")
        first_ts = cum_series[0][0]
        last_ts = cum_series[-1][0]
        t = first_ts
        while t + 6 * 3600 <= last_ts:
            ws, we = t, t + 6 * 3600
            sp = ep = None
            for ts, c in cum_series:
                if sp is None and ts >= ws:
                    sp = c
                if ts <= we:
                    ep = c
                else:
                    break
            if sp is not None and ep is not None:
                wpnl = ep - sp
                if wpnl > 0: positive += 1
                if wpnl < worst: worst = wpnl
                total += 1
            t += 3600
        if total > 0:
            rolling_pct = positive / total * 100
            worst_6h = worst

    calmar = total_pnl / max_dd if max_dd > 0 else None

    return {
        "n": n, "wr": wr, "pnl": total_pnl, "max_dd": max_dd,
        "r2": r2, "rolling_pct": rolling_pct, "worst_6h": worst_6h, "calmar": calmar,
    }


VIABLE_R2 = 0.85
VIABLE_6H = 75
VIABLE_W6H = -500
VIABLE_CAL = 2.0


def viability_passes(m):
    if m is None: return 0
    p = 0
    if m["r2"] >= VIABLE_R2: p += 1
    if m["rolling_pct"] is not None and m["rolling_pct"] >= VIABLE_6H: p += 1
    if m["worst_6h"] is not None and m["worst_6h"] >= VIABLE_W6H: p += 1
    if m["calmar"] is not None and m["calmar"] >= VIABLE_CAL: p += 1
    return p


def fmt_row(label, m):
    if m is None:
        return f"  {label:<22} (insufficient data)"
    r2 = f"{m['r2']:.3f}" if m['r2'] is not None else "  -- "
    six = f"{m['rolling_pct']:>5.1f}%" if m['rolling_pct'] is not None else "  -- "
    w6 = f"${m['worst_6h']:>+6,.0f}" if m['worst_6h'] is not None else "  -- "
    cal = f"{m['calmar']:.2f}" if m['calmar'] is not None else " -- "
    passes = viability_passes(m)
    tier = "VIABLE" if passes == 4 else "PROM" if passes == 3 else "BORD" if passes == 2 else "----"
    return (f"  {label:<22} n={m['n']:<5} pnl=${m['pnl']:>+8,.0f}  "
            f"R²={r2}  6h+={six}  w6h={w6}  Cal={cal}  [{passes}/4 {tier}]")


def main():
    print("=" * 110)
    print("  BRAINSTORM BACKTEST  —  4 new strategies vs oracle_1 + omega baselines")
    print("=" * 110)

    trades = load_trades("oracle_1")
    print(f"\n  Loaded {len(trades):,} oracle_1 trades")
    print(f"  Span: {datetime.fromtimestamp(trades[0]['ts']).strftime('%Y-%m-%d %H:%M')} → "
          f"{datetime.fromtimestamp(trades[-1]['ts']).strftime('%Y-%m-%d %H:%M')}")

    # Run all strategies
    results = {}
    results["oracle_1 baseline"] = compute_metrics(trades, "pnl")
    results["omega (validated)"] = compute_metrics(strat_omega(trades), "pnl_new")
    results["oracle_streak"] = compute_metrics(strat_streak(trades), "pnl_new")
    results["oracle_spike(8bp)"] = compute_metrics(strat_spike(trades, min_delta=8), "pnl_new")
    results["oracle_spike(5bp)"] = compute_metrics(strat_spike(trades, min_delta=5), "pnl_new")
    results["oracle_settle"] = compute_metrics(strat_settle(trades), "pnl_new")
    results["oracle_smart"] = compute_metrics(strat_smart(trades), "pnl_new")

    # Combined strategies — the obvious next-gen omega
    def strat_omega_settle(trades):
        """omega + settle: chrono + overnight + halt + skip first 30s."""
        out = []
        recent_pnl = []
        halt_until = 0.0
        for t in trades:
            if t["ts"] < halt_until: continue
            bucket = chrono_bucket(t["time_remaining"])
            if (bucket, t["direction"]) in CHRONO_BLOCKED: continue
            if 0 <= t["hour_et"] < 5: continue
            if t["time_remaining"] > 270: continue  # NEW: settle filter
            if len(recent_pnl) >= 8 and sum(recent_pnl[-8:]) < -400:
                halt_until = t["ts"] + 60 * 60
                recent_pnl = []
                continue
            boost = 1.3 if (bucket, t["direction"]) in CHRONO_BOOSTED else 1.0
            new_pnl = t["pnl"] * boost
            recent_pnl.append(t["pnl"])
            out.append({**t, "pnl_new": new_pnl})
        return out

    def strat_omega_spike(trades, min_delta=5):
        """omega + spike(5bp): chrono + overnight + halt + delta floor + delta ladder."""
        out = []
        recent_pnl = []
        halt_until = 0.0
        for t in trades:
            if t["ts"] < halt_until: continue
            bucket = chrono_bucket(t["time_remaining"])
            if (bucket, t["direction"]) in CHRONO_BLOCKED: continue
            if 0 <= t["hour_et"] < 5: continue
            if t["abs_delta"] < min_delta: continue  # NEW: delta floor
            if len(recent_pnl) >= 8 and sum(recent_pnl[-8:]) < -400:
                halt_until = t["ts"] + 60 * 60
                recent_pnl = []
                continue
            boost = 1.3 if (bucket, t["direction"]) in CHRONO_BOOSTED else 1.0
            d = t["abs_delta"]
            spike_mult = 2.0 if d >= 25 else 1.5 if d >= 15 else 1.2 if d >= 8 else 1.0
            new_pnl = t["pnl"] * boost * spike_mult
            recent_pnl.append(t["pnl"])
            out.append({**t, "pnl_new": new_pnl})
        return out

    def strat_omega_settle_spike(trades, min_delta=5):
        """omega + settle + spike(5bp): all latency-tolerant filters stacked."""
        out = []
        recent_pnl = []
        halt_until = 0.0
        for t in trades:
            if t["ts"] < halt_until: continue
            bucket = chrono_bucket(t["time_remaining"])
            if (bucket, t["direction"]) in CHRONO_BLOCKED: continue
            if 0 <= t["hour_et"] < 5: continue
            if t["time_remaining"] > 270: continue
            if t["abs_delta"] < min_delta: continue
            if len(recent_pnl) >= 8 and sum(recent_pnl[-8:]) < -400:
                halt_until = t["ts"] + 60 * 60
                recent_pnl = []
                continue
            boost = 1.3 if (bucket, t["direction"]) in CHRONO_BOOSTED else 1.0
            d = t["abs_delta"]
            spike_mult = 2.0 if d >= 25 else 1.5 if d >= 15 else 1.2 if d >= 8 else 1.0
            new_pnl = t["pnl"] * boost * spike_mult
            recent_pnl.append(t["pnl"])
            out.append({**t, "pnl_new": new_pnl})
        return out

    results["omega + settle"] = compute_metrics(strat_omega_settle(trades), "pnl_new")
    results["omega + spike(5bp)"] = compute_metrics(strat_omega_spike(trades, 5), "pnl_new")
    results["omega+settle+spike"] = compute_metrics(strat_omega_settle_spike(trades, 5), "pnl_new")

    print()
    print("=" * 110)
    print("  RESULTS  (viability targets: R² ≥ 0.85, 6h+ ≥ 75%, w6h ≥ -$500, Calmar ≥ 2.0)")
    print("=" * 110)
    print()
    for label, m in results.items():
        print(fmt_row(label, m))

    print()
    print("  Tier legend: VIABLE = 4/4 passes | PROM = 3/4 | BORD = 2/4 | ---- = ≤1/4")

    # Highlight best new strategy by viability score, then by PnL as tiebreaker
    new_only = {k: v for k, v in results.items() if k.startswith("oracle_") and "baseline" not in k}
    if new_only:
        ranked = sorted(
            new_only.items(),
            key=lambda kv: (viability_passes(kv[1]), kv[1]["pnl"] if kv[1] else 0),
            reverse=True,
        )
        print()
        print("  Ranking of new strategies (viability score, then PnL):")
        for i, (name, m) in enumerate(ranked, 1):
            if m is None:
                print(f"    {i}. {name:<20} insufficient data")
                continue
            print(f"    {i}. {name:<20} {viability_passes(m)}/4   ${m['pnl']:+,.0f}   $/tr ${m['pnl']/m['n']:+.2f}")


if __name__ == "__main__":
    main()
