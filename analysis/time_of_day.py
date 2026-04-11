"""
Time-of-day analysis for all active sessions.

Builds a UTC hour × session heatmap showing $/trade, WR, and trade count.
Answers: which hours work for which sessions, are they all the same hours
(correlated) or different (diversified)?

Also shows UTC→ET conversion so we can sanity-check the overnight skip windows.
"""
import csv
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA = Path("data")

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
                out.append({"ts": ts, "pnl": float(r["pnl_taker"])})
            except (ValueError, KeyError):
                continue
    return out


def utc_hour(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).hour


# Very rough DST detection — good enough for a display hint
def utc_to_et_label(utc_h):
    # On Apr 10 2026, US is in EDT (UTC-4)
    et = (utc_h - 4) % 24
    return et


def color_cell(pnl_per_tr, n):
    """Return ANSI color code for a cell based on $/trade."""
    if n == 0:
        return "\033[90m"  # dim grey
    if pnl_per_tr >= 25:   return "\033[92;1m"  # bright green
    if pnl_per_tr >= 10:   return "\033[92m"    # green
    if pnl_per_tr >= 0:    return "\033[37m"    # white
    if pnl_per_tr >= -10:  return "\033[93m"    # yellow
    if pnl_per_tr >= -25:  return "\033[91m"    # red
    return "\033[91;1m"                          # bright red


def main():
    # Load all sessions, bucket by UTC hour
    by_session_hour = defaultdict(lambda: defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0}))
    all_data = {}
    for name in ACTIVE:
        trades = load(name)
        if not trades:
            continue
        all_data[name] = trades
        for t in trades:
            h = utc_hour(t["ts"])
            bucket = by_session_hour[name][h]
            bucket["n"] += 1
            bucket["pnl"] += t["pnl"]
            if t["pnl"] > 0:
                bucket["wins"] += 1

    if not all_data:
        print("no data")
        return

    # Determine which hours have ANY trades across ALL sessions
    active_hours = set()
    for session, hours in by_session_hour.items():
        active_hours.update(hours.keys())
    hours_sorted = sorted(active_hours)

    RST = "\033[0m"

    # ═══════════════════════════════════════════════════════════════
    print()
    print("=" * 120)
    print("  TIME-OF-DAY HEATMAP — $/trade by UTC hour × session")
    print("=" * 120)
    print()
    print("  color legend: bright green ≥ $25/tr | green ≥ $10 | white ≥ $0 | yellow ≥ -$10 | red ≥ -$25 | bright red < -$25")
    print()

    # Header row: UTC hours + ET labels
    print(f"  {'session':<18}", end="")
    for h in hours_sorted:
        print(f" {h:>4}", end="")
    print(f"   │ total")

    # ET labels on a separate line
    print(f"  {'(ET hour)':<18}", end="")
    for h in hours_sorted:
        et = utc_to_et_label(h)
        # Mark 0-5 ET as overnight
        tag = "*" if 0 <= et < 5 else " "
        print(f" {et:>3}{tag}", end="")
    print()
    print(f"  {'':<18} {'─' * (5 * len(hours_sorted))}")

    # Body: one row per session
    session_rank = sorted(all_data.keys(), key=lambda s: -sum(b["pnl"] for b in by_session_hour[s].values()))
    for session in session_rank:
        row_total_pnl = 0
        row_total_n = 0
        line = f"  {session:<18}"
        for h in hours_sorted:
            b = by_session_hour[session].get(h, {"n": 0, "pnl": 0, "wins": 0})
            row_total_pnl += b["pnl"]
            row_total_n += b["n"]
            if b["n"] == 0:
                line += "    ."
            else:
                pt = b["pnl"] / b["n"]
                col = color_cell(pt, b["n"])
                # Show $/tr as 3 chars (e.g., +42, -18)
                line += f" {col}{pt:>+4.0f}{RST}"
        line += f"   │ ${row_total_pnl:>+6,.0f}"
        print(line)

    # ═══════════════════════════════════════════════════════════════
    print()
    print("=" * 120)
    print("  HOUR TOTALS ACROSS PORTFOLIO (which hours are the portfolio winning/losing?)")
    print("=" * 120)
    print()
    hour_totals = defaultdict(lambda: {"n": 0, "pnl": 0, "wins": 0, "sessions": set()})
    for session, hours in by_session_hour.items():
        for h, b in hours.items():
            ht = hour_totals[h]
            ht["n"] += b["n"]
            ht["pnl"] += b["pnl"]
            ht["wins"] += b["wins"]
            if b["n"] > 0:
                ht["sessions"].add(session)

    print(f"  {'UTC':<5} {'ET':<4} {'n':<6} {'pnl':<10} {'$/tr':<8} {'WR':<7} {'sessions':<9} bar")
    print("  " + "─" * 110)
    for h in hours_sorted:
        b = hour_totals[h]
        if b["n"] == 0:
            continue
        pt = b["pnl"] / b["n"]
        wr = b["wins"] / b["n"] * 100
        et = utc_to_et_label(h)
        tag = " *" if 0 <= et < 5 else "  "
        # Bar graph of PnL
        bar_len = max(1, int(abs(b["pnl"]) / 100))
        bar_char = "█" if b["pnl"] >= 0 else "█"
        col = "\033[92m" if b["pnl"] > 0 else "\033[91m"
        bar = col + (bar_char * min(bar_len, 60)) + RST
        print(f"  {h:>3}  {et:>2}{tag}  {b['n']:<6} ${b['pnl']:>+7,.0f}  ${pt:>+5.1f}  {wr:>5.1f}%  {len(b['sessions']):>3}/14   {bar}")
    print()
    print("  * = hour currently inside the '0-5 ET overnight skip' window (sessions with the filter pause here)")

    # ═══════════════════════════════════════════════════════════════
    # Best/worst hour per session
    print()
    print("=" * 120)
    print("  BEST + WORST HOUR PER SESSION (are sessions peaking at different times?)")
    print("=" * 120)
    print()
    print(f"  {'session':<18} {'best hr':<10} {'best $/tr':<11} {'worst hr':<10} {'worst $/tr':<11}")
    for session in session_rank:
        hours = by_session_hour[session]
        if not hours:
            continue
        cells = [(h, b["pnl"] / b["n"] if b["n"] else 0, b["n"]) for h, b in hours.items() if b["n"] >= 2]
        if not cells:
            continue
        cells.sort(key=lambda x: x[1])
        worst = cells[0]
        best = cells[-1]
        if best == worst:
            continue
        et_best = utc_to_et_label(best[0])
        et_worst = utc_to_et_label(worst[0])
        print(f"  {session:<18} {best[0]:>2}UTC/{et_best:>2}ET  ${best[1]:>+6.1f}   "
              f"{worst[0]:>2}UTC/{et_worst:>2}ET  ${worst[1]:>+6.1f}")

    # ═══════════════════════════════════════════════════════════════
    # "Peak diversity" — are sessions peaking at different hours?
    print()
    print("=" * 120)
    print("  PEAK-HOUR DIVERSITY — are sessions peaking in different hours or the same?")
    print("=" * 120)
    peak_hours = []
    for session in all_data:
        hours = by_session_hour[session]
        cells = [(h, b["pnl"] / b["n"] if b["n"] else 0) for h, b in hours.items() if b["n"] >= 2]
        if not cells:
            continue
        best_h, _ = max(cells, key=lambda x: x[1])
        peak_hours.append((session, best_h))

    from collections import Counter
    peak_dist = Counter(h for _, h in peak_hours)
    unique_peak_hours = len(peak_dist)
    n_sessions_with_peak = len(peak_hours)

    print(f"\n  {n_sessions_with_peak} sessions analyzed")
    print(f"  Unique peak hours: {unique_peak_hours}")
    if n_sessions_with_peak > 0:
        diversity = unique_peak_hours / n_sessions_with_peak * 100
        print(f"  Peak-hour diversity: {diversity:.0f}%  "
              f"({'HIGH — sessions peak at different times' if diversity >= 60 else 'MEDIUM — some clustering' if diversity >= 40 else 'LOW — sessions peak at the same hours'})")
    print()
    print("  peak distribution:")
    for h, count in sorted(peak_dist.items()):
        et = utc_to_et_label(h)
        bar = "▇" * count
        print(f"    {h:>2}UTC ({et:>2}ET):  {bar}  {count} session{'s' if count != 1 else ''}")


if __name__ == "__main__":
    main()
