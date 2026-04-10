"""
Counterfactual Filter Backtest

Takes a session's trade history and applies hypothetical filters to compute
"what would have happened if we'd had this filter all along."

This is REPLAY-only — we filter existing fired trades, we don't generate new
ones. That means we can only test filters that REMOVE trades, not filters that
would have CHANGED the timing or direction. Despite that limitation, this is
massively faster than deploying experimental sessions and waiting days.

Filter types supported:
  - bucket-direction blocks (e.g. "block T-60-90 NO")
  - delta floor (e.g. "skip trades with |delta| < 4bp")
  - entry price band (e.g. "skip true entry > 60c")
  - time of day (e.g. "skip 12-5AM ET")
  - direction (e.g. "skip all YES trades")
  - combo name (e.g. "skip OR_early")
  - rolling loss streak (skip after N consecutive losses)
  - 6h drawdown halt (skip if rolling 6h PnL < threshold)
  - peer confirmation requirement (skip if no peer fired same direction recently)

Output: viability metrics, comparison vs original, top counterfactual saves.

Usage:
    python3 analysis/backtest_filter.py oracle_1 \\
        --block-bucket "60-90:NO" \\
        --block-bucket "5-30:NO" \\
        --min-delta 4

    python3 analysis/backtest_filter.py oracle_1 \\
        --max-true-entry 0.60 \\
        --skip-overnight

    python3 analysis/backtest_filter.py --all
"""

import argparse
import csv
import sys
from datetime import datetime
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path("data")


def load_trades(session_name):
    """Load all trades for a session with all relevant features computed."""
    p = DATA_DIR / session_name / "trades.csv"
    if not p.exists():
        return []
    trades = []
    with open(p) as f:
        for row in csv.DictReader(f):
            try:
                ts = float(row["timestamp"])
                pnl = float(row["pnl_taker"])
                tr = float(row["time_remaining"])
                fill_price = float(row["fill_price"])
                direction = row["direction"]
                # True entry cost: fill_price for YES, (1-fill_price) for NO
                true_entry = fill_price if direction == "YES" else 1.0 - fill_price
                trade = {
                    "ts": ts,
                    "pnl": pnl,
                    "result": row["result"],
                    "direction": direction,
                    "delta_bps": float(row.get("delta_bps", 0)),
                    "abs_delta": abs(float(row.get("delta_bps", 0))),
                    "time_remaining": tr,
                    "fill_price": fill_price,
                    "true_entry": true_entry,
                    "combo": row.get("combo", ""),
                    "spread": float(row.get("spread", 0)),
                    "btc_price": float(row.get("btc_price", 0)),
                    # Computed bucket label (matches oracle_chrono format)
                    "bucket": _bucket_label(tr),
                    # Hour of day (ET)
                    "hour_et": datetime.fromtimestamp(ts).hour,
                }
                trades.append(trade)
            except (ValueError, KeyError):
                continue
    return sorted(trades, key=lambda t: t["ts"])


def _bucket_label(tr):
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


def apply_filters(trades, filters):
    """Apply a set of filters to trades. Returns (kept, blocked) lists.

    filters dict keys (all optional):
        block_buckets: list of "bucket:direction" strings to skip
        min_delta: float, skip trades with |delta_bps| below this
        max_delta: float, skip trades with |delta_bps| above this
        min_true_entry: float, skip if true_entry < this
        max_true_entry: float, skip if true_entry > this
        skip_directions: list of directions to skip
        skip_combos: list of combo names to skip
        skip_hours: list of (hour_start, hour_end) tuples in ET to skip
        loss_streak_halt: int, skip after N consecutive losses
        loss_cooldown_min: int, after a loss skip for N minutes
        dd_halt_threshold: float, skip if rolling 6h PnL < this
    """
    kept = []
    blocked = []
    consec_losses = 0
    last_loss_ts = None
    rolling_pnl = []  # (ts, pnl) for 6h rolling DD check

    for t in trades:
        skip_reason = None

        # Bucket-direction block
        if "block_buckets" in filters:
            for spec in filters["block_buckets"]:
                if "|" in spec:
                    b, d = spec.split("|", 1)
                elif ":" in spec:
                    b, d = spec.split(":", 1)
                else:
                    continue
                if t["bucket"] == b and t["direction"] == d:
                    skip_reason = "blocked_bucket"
                    break

        # Delta filters
        if not skip_reason and "min_delta" in filters:
            if t["abs_delta"] < filters["min_delta"]:
                skip_reason = "delta_too_small"
        if not skip_reason and "max_delta" in filters:
            if t["abs_delta"] > filters["max_delta"]:
                skip_reason = "delta_too_big"

        # True entry cost filters
        if not skip_reason and "min_true_entry" in filters:
            if t["true_entry"] < filters["min_true_entry"]:
                skip_reason = "entry_too_low"
        if not skip_reason and "max_true_entry" in filters:
            if t["true_entry"] > filters["max_true_entry"]:
                skip_reason = "entry_too_high"

        # Direction filter
        if not skip_reason and "skip_directions" in filters:
            if t["direction"] in filters["skip_directions"]:
                skip_reason = "direction_blocked"

        # Combo filter
        if not skip_reason and "skip_combos" in filters:
            if t["combo"] in filters["skip_combos"]:
                skip_reason = "combo_blocked"

        # Hour-of-day filter
        if not skip_reason and "skip_hours" in filters:
            for h_start, h_end in filters["skip_hours"]:
                if h_start <= t["hour_et"] < h_end:
                    skip_reason = "hour_blocked"
                    break

        # Loss streak halt
        if not skip_reason and "loss_streak_halt" in filters:
            if consec_losses >= filters["loss_streak_halt"]:
                skip_reason = "loss_streak"

        # Loss cooldown
        if not skip_reason and "loss_cooldown_min" in filters:
            if last_loss_ts is not None:
                cooldown_secs = filters["loss_cooldown_min"] * 60
                if t["ts"] - last_loss_ts < cooldown_secs:
                    skip_reason = "loss_cooldown"

        # 6h drawdown halt
        if not skip_reason and "dd_halt_threshold" in filters:
            cutoff = t["ts"] - 21600  # 6h
            recent = [(ts, p) for ts, p in rolling_pnl if ts >= cutoff]
            recent_total = sum(p for _, p in recent)
            if len(recent) >= 5 and recent_total < filters["dd_halt_threshold"]:
                skip_reason = "dd_halt"

        # Apply
        if skip_reason:
            blocked.append({**t, "blocked_reason": skip_reason})
            # Don't update streak counters on blocked trades
        else:
            kept.append(t)
            rolling_pnl.append((t["ts"], t["pnl"]))
            # Drop old entries
            cutoff = t["ts"] - 21600
            rolling_pnl = [(ts, p) for ts, p in rolling_pnl if ts >= cutoff]
            # Update consecutive losses
            if t["result"] == "LOSS":
                consec_losses += 1
                last_loss_ts = t["ts"]
            else:
                consec_losses = 0
                # last_loss_ts stays — we still want to apply cooldown after a win
                # Actually for cooldown logic, reset on win:
                last_loss_ts = None

    return kept, blocked


def compute_metrics(trades):
    """Compute viability metrics from a list of trades."""
    if not trades:
        return None
    n = len(trades)
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    wr = wins / n * 100

    # Cumulative PnL
    cum = []
    c = 0.0
    for t in sorted(trades, key=lambda x: x["ts"]):
        c += t["pnl"]
        cum.append((t["ts"], c))

    # Max drawdown
    peak = 0
    max_dd = 0
    for _, c in cum:
        if c > peak:
            peak = c
        dd = peak - c
        if dd > max_dd:
            max_dd = dd

    # R²
    times = [t for t, _ in cum]
    pnls_c = [p for _, p in cum]
    t0 = times[0]
    norm_t = [t - t0 for t in times]
    n_pts = len(norm_t)
    sum_t = sum(norm_t)
    sum_p = sum(pnls_c)
    sum_tt = sum(t * t for t in norm_t)
    sum_tp = sum(t * p for t, p in zip(norm_t, pnls_c))
    mean_t = sum_t / n_pts
    mean_p = sum_p / n_pts
    denom = sum_tt - n_pts * mean_t * mean_t
    if denom <= 0:
        r2 = 0
    else:
        slope = (sum_tp - n_pts * mean_t * mean_p) / denom
        intercept = mean_p - slope * mean_t
        ss_tot = sum((p - mean_p) ** 2 for p in pnls_c)
        ss_res = sum((p - (intercept + slope * t)) ** 2 for t, p in zip(norm_t, pnls_c))
        r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        if slope < 0:
            r2 = -abs(r2)

    # Rolling 6h windows
    duration_h = (cum[-1][0] - cum[0][0]) / 3600
    rolling_pct = None
    worst_6h = None
    if duration_h >= 12:
        positive = 0
        total = 0
        worst = float("inf")
        first_ts = cum[0][0]
        last_ts = cum[-1][0]
        t = first_ts
        while t + 6 * 3600 <= last_ts:
            ws, we = t, t + 6 * 3600
            sp = ep = None
            for ts, c in cum:
                if sp is None and ts >= ws:
                    sp = c
                if ts <= we:
                    ep = c
                else:
                    break
            if sp is not None and ep is not None:
                wpnl = ep - sp
                if wpnl > 0:
                    positive += 1
                if wpnl < worst:
                    worst = wpnl
                total += 1
            t += 3600
        if total > 0:
            rolling_pct = positive / total * 100
            worst_6h = worst

    calmar = pnl / max_dd if max_dd > 0 else None

    return {
        "n": n, "wins": wins, "wr": wr, "pnl": pnl,
        "max_dd": max_dd,
        "r2": r2,
        "rolling_pct": rolling_pct,
        "worst_6h": worst_6h,
        "calmar": calmar,
        "duration_h": duration_h,
    }


def print_comparison(label, original, filtered):
    """Print side-by-side comparison of original vs filtered metrics."""
    print(f"\n  {label}")
    print("  " + "─" * 70)
    print(f"  {'Metric':<18} {'Original':>14} {'Filtered':>14} {'Δ':>14}")
    print("  " + "─" * 70)

    def fmt(v, fmt_str="{:.0f}"):
        if v is None:
            return "—"
        return fmt_str.format(v)

    def delta(o, f, fmt_str="{:+.0f}"):
        if o is None or f is None:
            return "—"
        return fmt_str.format(f - o)

    rows = [
        ("Trades",          original["n"],       filtered["n"],       "{:d}",   "{:+d}"),
        ("Wins",            original["wins"],    filtered["wins"],    "{:d}",   "{:+d}"),
        ("Win Rate",        original["wr"],      filtered["wr"],      "{:.1f}%","{:+.1f}pp"),
        ("Total PnL",       original["pnl"],     filtered["pnl"],     "${:+,.0f}","${:+,.0f}"),
        ("Max DD",          original["max_dd"],  filtered["max_dd"],  "${:,.0f}","${:+,.0f}"),
        ("R²",              original["r2"],      filtered["r2"],      "{:.3f}", "{:+.3f}"),
        ("% 6h positive",   original["rolling_pct"], filtered["rolling_pct"], "{:.1f}%","{:+.1f}pp"),
        ("Worst 6h",        original["worst_6h"], filtered["worst_6h"], "${:+,.0f}","${:+,.0f}"),
        ("Calmar",          original["calmar"], filtered["calmar"], "{:.2f}", "{:+.2f}"),
    ]
    for name, o, f, ofmt, dfmt in rows:
        ostr = fmt(o, ofmt)
        fstr = fmt(f, ofmt)
        dstr = delta(o, f, dfmt)
        # Color-ish marker
        marker = ""
        if o is not None and f is not None:
            if name in ("Total PnL", "R²", "Win Rate", "% 6h positive", "Calmar"):
                if f > o: marker = " ✓"
                elif f < o: marker = " ✗"
            elif name in ("Max DD", "Worst 6h"):  # for these, less negative / smaller is better
                if name == "Max DD":
                    if f < o: marker = " ✓"
                    elif f > o: marker = " ✗"
                else:  # Worst 6h
                    if f > o: marker = " ✓"
                    elif f < o: marker = " ✗"
        print(f"  {name:<18} {ostr:>14} {fstr:>14} {dstr:>14}{marker}")


def parse_args():
    p = argparse.ArgumentParser(description="Counterfactual filter backtest")
    p.add_argument("session", nargs="?", help="Session name (e.g. oracle_1)")
    p.add_argument("--all", action="store_true", help="Run on all sessions")
    p.add_argument("--block-bucket", action="append", default=[],
                   help='Block bucket-direction combo, e.g. "60-90:NO". Can repeat.')
    p.add_argument("--min-delta", type=float, help="Skip trades with |delta_bps| below this")
    p.add_argument("--max-delta", type=float, help="Skip trades with |delta_bps| above this")
    p.add_argument("--min-true-entry", type=float, help="Skip true_entry < this (e.g. 0.20)")
    p.add_argument("--max-true-entry", type=float, help="Skip true_entry > this (e.g. 0.60)")
    p.add_argument("--skip-yes", action="store_true", help="Skip all YES trades")
    p.add_argument("--skip-no", action="store_true", help="Skip all NO trades")
    p.add_argument("--skip-combo", action="append", default=[],
                   help="Skip a combo by name. Can repeat.")
    p.add_argument("--skip-overnight", action="store_true",
                   help="Skip 0-5 AM ET")
    p.add_argument("--skip-hours", help='Hour ranges to skip, e.g. "0-5,12-14"')
    p.add_argument("--loss-streak", type=int, help="Halt after N consecutive losses")
    p.add_argument("--loss-cooldown", type=int, help="After a loss, cooldown for N minutes")
    p.add_argument("--dd-halt", type=float, help="Halt if rolling 6h PnL < threshold")
    return p.parse_args()


def build_filters(args):
    filters = {}
    if args.block_bucket:
        filters["block_buckets"] = args.block_bucket
    if args.min_delta is not None:
        filters["min_delta"] = args.min_delta
    if args.max_delta is not None:
        filters["max_delta"] = args.max_delta
    if args.min_true_entry is not None:
        filters["min_true_entry"] = args.min_true_entry
    if args.max_true_entry is not None:
        filters["max_true_entry"] = args.max_true_entry
    if args.skip_yes:
        filters.setdefault("skip_directions", []).append("YES")
    if args.skip_no:
        filters.setdefault("skip_directions", []).append("NO")
    if args.skip_combo:
        filters["skip_combos"] = args.skip_combo
    if args.skip_overnight:
        filters.setdefault("skip_hours", []).append((0, 5))
    if args.skip_hours:
        for spec in args.skip_hours.split(","):
            if "-" in spec:
                a, b = spec.split("-")
                filters.setdefault("skip_hours", []).append((int(a), int(b)))
    if args.loss_streak is not None:
        filters["loss_streak_halt"] = args.loss_streak
    if args.loss_cooldown is not None:
        filters["loss_cooldown_min"] = args.loss_cooldown
    if args.dd_halt is not None:
        filters["dd_halt_threshold"] = args.dd_halt
    return filters


def describe_filters(filters):
    if not filters:
        return "(no filters)"
    parts = []
    if "block_buckets" in filters:
        parts.append("block: " + ", ".join(filters["block_buckets"]))
    if "min_delta" in filters:
        parts.append(f"|delta| >= {filters['min_delta']}bp")
    if "max_delta" in filters:
        parts.append(f"|delta| <= {filters['max_delta']}bp")
    if "min_true_entry" in filters:
        parts.append(f"true_entry >= {filters['min_true_entry']}")
    if "max_true_entry" in filters:
        parts.append(f"true_entry <= {filters['max_true_entry']}")
    if "skip_directions" in filters:
        parts.append(f"skip dirs: {','.join(filters['skip_directions'])}")
    if "skip_combos" in filters:
        parts.append(f"skip combos: {','.join(filters['skip_combos'])}")
    if "skip_hours" in filters:
        parts.append(f"skip hours: {filters['skip_hours']}")
    if "loss_streak_halt" in filters:
        parts.append(f"halt after {filters['loss_streak_halt']} losses")
    if "loss_cooldown_min" in filters:
        parts.append(f"{filters['loss_cooldown_min']}min cooldown after loss")
    if "dd_halt_threshold" in filters:
        parts.append(f"halt if 6h PnL < ${filters['dd_halt_threshold']}")
    return " | ".join(parts)


def run_session(name, filters):
    trades = load_trades(name)
    if len(trades) < 30:
        print(f"\n  {name}: insufficient data ({len(trades)} trades)")
        return

    original_metrics = compute_metrics(trades)
    kept, blocked = apply_filters(trades, filters)
    filtered_metrics = compute_metrics(kept) if kept else None

    print("\n" + "=" * 80)
    print(f"  SESSION: {name}")
    print(f"  FILTERS: {describe_filters(filters)}")
    print("=" * 80)

    if filtered_metrics is None:
        print(f"\n  All {len(trades)} trades were filtered out — nothing to compare.")
        return

    print_comparison("RESULTS", original_metrics, filtered_metrics)

    # Per-block-reason summary
    if blocked:
        print("\n  Blocked trades by reason:")
        by_reason = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for t in blocked:
            by_reason[t["blocked_reason"]]["n"] += 1
            by_reason[t["blocked_reason"]]["pnl"] += t["pnl"]
        for reason, stats in sorted(by_reason.items(), key=lambda x: x[1]["pnl"]):
            n = stats["n"]
            tot = stats["pnl"]
            avg = tot / n if n else 0
            sign = "saved" if tot < 0 else "missed"
            print(f"    {reason:<22} {n:>5} trades, ${tot:+,.0f} total ({sign} ${abs(tot):,.0f}), ${avg:+.0f}/trade")


def main():
    args = parse_args()

    filters = build_filters(args)
    if not filters:
        print("\n  WARNING: No filters specified. Use --help to see options.")
        print("  Example: python3 analysis/backtest_filter.py oracle_1 --block-bucket '60-90:NO'\n")
        return

    if args.all:
        all_sessions = sorted([d.name for d in DATA_DIR.iterdir()
                               if d.is_dir() and not d.name.startswith("_")
                               and (d / "trades.csv").exists()])
        for name in all_sessions:
            run_session(name, filters)
    elif args.session:
        run_session(args.session, filters)
    else:
        print("Usage: python3 analysis/backtest_filter.py <session_name> [filters]")
        print("       python3 analysis/backtest_filter.py --all [filters]")
        print("       python3 analysis/backtest_filter.py --help")
        sys.exit(1)


if __name__ == "__main__":
    main()
