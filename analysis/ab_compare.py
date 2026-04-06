#!/usr/bin/env python3
"""
A/B Comparison Tool for Polymarket Bot Sessions.

Groups sessions by architecture, computes per-session stats,
runs statistical tests between variants, and prints a clear verdict.

Usage:
    python3 analysis/ab_compare.py              # analyze local data/
    python3 analysis/ab_compare.py --pull       # rsync from server first
"""

import csv
import json
import math
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

# ---------------------------------------------------------------------------
# Scipy import (optional)
# ---------------------------------------------------------------------------
try:
    from scipy.stats import chi2_contingency, ttest_ind
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

# ---------------------------------------------------------------------------
# Manual chi-squared fallback
# ---------------------------------------------------------------------------

def _chi2_cdf_approx(x, k):
    """Wilson-Hilferty normal approximation to chi-squared CDF."""
    if x <= 0:
        return 0.0
    z = ((x / k) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * k))) / math.sqrt(2.0 / (9.0 * k))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def manual_chi2_test(wins_a, losses_a, wins_b, losses_b):
    """2x2 chi-squared test, returns (chi2_stat, p_value)."""
    table = [[wins_a, losses_a], [wins_b, losses_b]]
    row_totals = [sum(r) for r in table]
    col_totals = [table[0][j] + table[1][j] for j in range(2)]
    grand = sum(row_totals)
    if grand == 0:
        return 0.0, 1.0
    chi2 = 0.0
    for i in range(2):
        for j in range(2):
            expected = row_totals[i] * col_totals[j] / grand
            if expected == 0:
                return 0.0, 1.0
            chi2 += (table[i][j] - expected) ** 2 / expected
    p = 1.0 - _chi2_cdf_approx(chi2, 1)
    return chi2, max(p, 0.0)


def manual_welch_t(data_a, data_b):
    """Welch's t-test, returns (t_stat, p_value_approx)."""
    n_a, n_b = len(data_a), len(data_b)
    if n_a < 2 or n_b < 2:
        return 0.0, 1.0
    mean_a = sum(data_a) / n_a
    mean_b = sum(data_b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in data_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in data_b) / (n_b - 1)
    se = math.sqrt(var_a / n_a + var_b / n_b) if (var_a / n_a + var_b / n_b) > 0 else 1e-12
    t = (mean_a - mean_b) / se
    # Welch-Satterthwaite degrees of freedom
    num = (var_a / n_a + var_b / n_b) ** 2
    den = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = num / den if den > 0 else 1
    # Approximate p-value using normal for large df, otherwise rough t-approx
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return t, p

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def find_project_root():
    """Find the project root (contains data/ and configs/)."""
    # Try cwd first, then walk up
    p = Path.cwd()
    for _ in range(5):
        if (p / "data").is_dir() and (p / "configs").is_dir():
            return p
        p = p.parent
    # Fallback: assume script is in analysis/
    return Path(__file__).resolve().parent.parent


def load_trades(csv_path):
    """Load trades from a CSV, return list of dicts with normalized keys."""
    trades = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                pnl = float(row.get("pnl_taker", 0))
            except (ValueError, TypeError):
                continue
            result = row.get("result", "").strip().upper()
            if result not in ("WIN", "LOSS"):
                continue
            trades.append({
                "combo": row.get("combo", "unknown"),
                "direction": row.get("direction", "?").strip().upper(),
                "pnl": pnl,
                "result": result,
                "fill_price": float(row.get("fill_price", 0)),
                "notional": float(row.get("notional", 0) or 0),
            })
    return trades


def get_architecture(session_name, data_dir, configs_dir):
    """Resolve architecture for a session."""
    # 1. stats.json in data dir
    stats_path = data_dir / session_name / "stats.json"
    if stats_path.exists():
        try:
            with open(stats_path) as f:
                stats = json.load(f)
            arch = stats.get("architecture")
            if arch:
                return arch
        except (json.JSONDecodeError, KeyError):
            pass

    # 2. config JSON
    config_path = configs_dir / f"{session_name}.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            arch = cfg.get("ARCHITECTURE")
            if arch:
                return arch
        except (json.JSONDecodeError, KeyError):
            pass

    # 3. Infer from trades CSV header (architecture column)
    for fname in ("trades.csv", "paper_trades_v2.csv"):
        csv_path = data_dir / session_name / fname
        if csv_path.exists():
            try:
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    row = next(reader, None)
                    if row and row.get("architecture"):
                        return row["architecture"]
            except Exception:
                pass

    return "impulse_lag"


def discover_sessions(data_dir, configs_dir):
    """
    Return dict: session_name -> {architecture, trades: [...], csv_path}.
    """
    sessions = {}
    if not data_dir.is_dir():
        return sessions

    for d in sorted(data_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        csv_path = None
        for fname in ("trades.csv", "paper_trades_v2.csv"):
            candidate = d / fname
            if candidate.exists():
                csv_path = candidate
                break
        if csv_path is None:
            continue

        trades = load_trades(csv_path)
        if not trades:
            continue

        arch = get_architecture(d.name, data_dir, configs_dir)
        sessions[d.name] = {
            "architecture": arch,
            "trades": trades,
            "csv_path": str(csv_path),
        }

    return sessions

# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def compute_stats(trades):
    """Compute detailed stats for a list of trade dicts."""
    n = len(trades)
    if n == 0:
        return None

    wins = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    n_wins = len(wins)
    n_losses = len(losses)
    wr = n_wins / n * 100 if n > 0 else 0

    pnls = [t["pnl"] for t in trades]
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / n

    win_pnls = [t["pnl"] for t in wins]
    loss_pnls = [t["pnl"] for t in losses]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    # Sharpe (per-trade)
    if n > 1:
        mean_pnl = avg_pnl
        std_pnl = math.sqrt(sum((p - mean_pnl) ** 2 for p in pnls) / (n - 1))
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0
    else:
        sharpe = 0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Per-combo breakdown
    combos = defaultdict(list)
    for t in trades:
        combos[t["combo"]].append(t)
    combo_stats = {}
    for combo, ctrades in sorted(combos.items()):
        cn = len(ctrades)
        cw = sum(1 for t in ctrades if t["result"] == "WIN")
        cpnl = sum(t["pnl"] for t in ctrades)
        combo_stats[combo] = {
            "trades": cn,
            "wins": cw,
            "wr": cw / cn * 100 if cn > 0 else 0,
            "pnl": cpnl,
            "avg": cpnl / cn if cn > 0 else 0,
        }

    # YES vs NO split
    yes_trades = [t for t in trades if t["direction"] == "YES"]
    no_trades = [t for t in trades if t["direction"] == "NO"]
    direction_split = {
        "YES": {"trades": len(yes_trades), "pnl": sum(t["pnl"] for t in yes_trades)},
        "NO": {"trades": len(no_trades), "pnl": sum(t["pnl"] for t in no_trades)},
    }

    return {
        "n": n,
        "wins": n_wins,
        "losses": n_losses,
        "wr": wr,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "rr": rr,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "combo_stats": combo_stats,
        "direction_split": direction_split,
        "pnls": pnls,
    }

# ---------------------------------------------------------------------------
# Statistical tests
# ---------------------------------------------------------------------------

def run_tests(stats_a, stats_b):
    """Run chi2 + Welch t between two session stats. Returns dict."""
    # Chi-squared on win rates
    if HAS_SCIPY:
        table = [
            [stats_a["wins"], stats_a["losses"]],
            [stats_b["wins"], stats_b["losses"]],
        ]
        try:
            chi2, chi2_p, _, _ = chi2_contingency(table, correction=True)
        except ValueError:
            chi2, chi2_p = 0.0, 1.0
    else:
        chi2, chi2_p = manual_chi2_test(
            stats_a["wins"], stats_a["losses"],
            stats_b["wins"], stats_b["losses"],
        )

    # Welch's t-test on PnL distributions
    if HAS_SCIPY:
        t_stat, t_p = ttest_ind(stats_a["pnls"], stats_b["pnls"], equal_var=False)
    else:
        t_stat, t_p = manual_welch_t(stats_a["pnls"], stats_b["pnls"])

    return {
        "chi2": chi2,
        "chi2_p": chi2_p,
        "t_stat": t_stat,
        "t_p": t_p,
    }

# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def color_pnl(val, fmt=".2f"):
    s = f"{val:{fmt}}"
    if val > 0:
        return f"{GREEN}+{s}{RESET}"
    elif val < 0:
        return f"{RED}{s}{RESET}"
    return s


def color_wr(wr):
    if wr >= 55:
        return f"{GREEN}{wr:.1f}%{RESET}"
    elif wr >= 50:
        return f"{YELLOW}{wr:.1f}%{RESET}"
    else:
        return f"{RED}{wr:.1f}%{RESET}"


def color_p(p):
    if p < 0.01:
        return f"{GREEN}{p:.4f} ***{RESET}"
    elif p < 0.05:
        return f"{GREEN}{p:.4f} *{RESET}"
    elif p < 0.10:
        return f"{YELLOW}{p:.4f}{RESET}"
    else:
        return f"{DIM}{p:.4f}{RESET}"


def print_session_stats(name, stats, indent=2):
    pad = " " * indent
    n = stats["n"]
    print(f"{pad}{BOLD}{name}{RESET}  ({n} trades)")
    print(f"{pad}  WR: {color_wr(stats['wr'])}  ({stats['wins']}W / {stats['losses']}L)")
    print(f"{pad}  PnL: {color_pnl(stats['total_pnl'])}   avg/trade: {color_pnl(stats['avg_pnl'])}")
    print(f"{pad}  Avg win: {color_pnl(stats['avg_win'])}  Avg loss: {color_pnl(stats['avg_loss'])}  R:R: {stats['rr']:.2f}")
    print(f"{pad}  Sharpe: {stats['sharpe']:.3f}   Max DD: {RED}-{stats['max_dd']:.2f}{RESET}")

    # Direction split
    ds = stats["direction_split"]
    print(f"{pad}  Direction: YES {ds['YES']['trades']}t {color_pnl(ds['YES']['pnl'])}  |  NO {ds['NO']['trades']}t {color_pnl(ds['NO']['pnl'])}")

    # Combo breakdown
    if stats["combo_stats"]:
        print(f"{pad}  {DIM}{'Combo':<22} {'Trades':>6} {'WR':>7} {'PnL':>10} {'Avg':>8}{RESET}")
        for combo, cs in stats["combo_stats"].items():
            wr_str = f"{cs['wr']:.1f}%"
            print(f"{pad}  {DIM}{combo:<22} {cs['trades']:>6} {wr_str:>7} {cs['pnl']:>+10.2f} {cs['avg']:>+8.2f}{RESET}")


def print_comparison(name_a, stats_a, name_b, stats_b, tests):
    """Print head-to-head comparison between two sessions."""
    print(f"\n  {BOLD}{name_a}{RESET}  vs  {BOLD}{name_b}{RESET}")
    print(f"  {'':>22} {'':>3}{'A':^18} {'B':^18} {'Delta':^14}")
    print(f"  {'Trades':<22}    {stats_a['n']:>8}       {stats_b['n']:>8}")

    wr_delta = stats_a["wr"] - stats_b["wr"]
    print(f"  {'Win Rate':<22}    {stats_a['wr']:>7.1f}%      {stats_b['wr']:>7.1f}%     {wr_delta:>+.1f}pp")

    pnl_delta = stats_a["total_pnl"] - stats_b["total_pnl"]
    print(f"  {'Total PnL':<22}    {stats_a['total_pnl']:>+8.2f}      {stats_b['total_pnl']:>+8.2f}     {color_pnl(pnl_delta)}")

    avg_delta = stats_a["avg_pnl"] - stats_b["avg_pnl"]
    print(f"  {'Avg PnL/trade':<22}    {stats_a['avg_pnl']:>+8.2f}      {stats_b['avg_pnl']:>+8.2f}     {color_pnl(avg_delta)}")

    print(f"  {'Sharpe':<22}    {stats_a['sharpe']:>8.3f}      {stats_b['sharpe']:>8.3f}")
    print(f"  {'Max DD':<22}    {stats_a['max_dd']:>8.2f}      {stats_b['max_dd']:>8.2f}")

    # Test results
    print(f"\n  {BOLD}Statistical Tests:{RESET}")
    print(f"    Chi2 (win rate):  {color_p(tests['chi2_p'])}   (chi2={tests['chi2']:.2f})")
    print(f"    Welch t (PnL):    {color_p(tests['t_p'])}   (t={tests['t_stat']:.2f})")


def determine_verdict(name_a, stats_a, name_b, stats_b, tests):
    """Return (verdict_str, color) for the pair."""
    min_trades = 30
    if stats_a["n"] < min_trades or stats_b["n"] < min_trades:
        return "INSUFFICIENT DATA", YELLOW

    significant = tests["chi2_p"] < 0.05 or tests["t_p"] < 0.05

    if not significant:
        return "NO SIGNIFICANT DIFFERENCE", YELLOW

    # Decide who wins based on avg PnL (primary) and WR (secondary)
    a_better_pnl = stats_a["avg_pnl"] > stats_b["avg_pnl"]
    a_better_wr = stats_a["wr"] > stats_b["wr"]

    if a_better_pnl:
        return f"{name_a} WINS", GREEN
    else:
        return f"{name_b} WINS", GREEN

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Handle --pull flag
    if "--pull" in sys.argv:
        print(f"{CYAN}{BOLD}Pulling latest data from server...{RESET}\n")
        cmd = [
            "rsync", "-az",
            "root@167.172.50.38:/opt/polymarket-bot/data/",
            "data/",
            "--exclude=*.parquet",
            "--exclude=*.log",
        ]
        try:
            subprocess.run(cmd, check=True)
            print(f"{GREEN}Sync complete.{RESET}\n")
        except subprocess.CalledProcessError as e:
            print(f"{RED}rsync failed: {e}{RESET}")
            sys.exit(1)
        except FileNotFoundError:
            print(f"{RED}rsync not found. Install it or pull data manually.{RESET}")
            sys.exit(1)

    root = find_project_root()
    data_dir = root / "data"
    configs_dir = root / "configs"

    print(f"{BOLD}{'=' * 70}{RESET}")
    print(f"{BOLD}  A/B Session Comparison Tool{RESET}")
    print(f"{BOLD}{'=' * 70}{RESET}")
    print(f"  Data dir:    {data_dir}")
    print(f"  Configs dir: {configs_dir}")
    if not HAS_SCIPY:
        print(f"  {YELLOW}scipy not found -- using approximate chi2/t-test{RESET}")
    print()

    # Discover sessions
    sessions = discover_sessions(data_dir, configs_dir)
    if not sessions:
        print(f"{RED}No sessions with trade data found in {data_dir}{RESET}")
        sys.exit(1)

    print(f"  Found {BOLD}{len(sessions)}{RESET} sessions with trade data\n")

    # Group by architecture
    arch_groups = defaultdict(dict)
    for name, info in sessions.items():
        arch_groups[info["architecture"]][name] = info

    verdicts = []

    for arch in sorted(arch_groups.keys()):
        group = arch_groups[arch]
        if len(group) < 2:
            continue

        print(f"\n{BOLD}{'=' * 70}{RESET}")
        print(f"{CYAN}{BOLD}  Architecture: {arch}{RESET}  ({len(group)} sessions)")
        print(f"{BOLD}{'=' * 70}{RESET}")

        # Compute stats for each session
        session_stats = {}
        for name in sorted(group.keys()):
            trades = group[name]["trades"]
            stats = compute_stats(trades)
            if stats:
                session_stats[name] = stats

        # Print individual session summaries
        print(f"\n{BOLD}  Session Summaries:{RESET}")
        ranked = sorted(session_stats.items(), key=lambda x: x[1]["avg_pnl"], reverse=True)
        for name, stats in ranked:
            print()
            print_session_stats(name, stats, indent=4)

        # Pairwise comparisons
        names = sorted(session_stats.keys())
        if len(names) < 2:
            continue

        print(f"\n{BOLD}  {'- ' * 35}{RESET}")
        print(f"{BOLD}  Pairwise Comparisons:{RESET}")

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                name_a, name_b = names[i], names[j]
                sa, sb = session_stats[name_a], session_stats[name_b]
                tests = run_tests(sa, sb)
                print_comparison(name_a, sa, name_b, sb, tests)

                verdict_str, verdict_color = determine_verdict(
                    name_a, sa, name_b, sb, tests
                )
                print(f"\n  {BOLD}VERDICT: {verdict_color}{verdict_str}{RESET}")
                verdicts.append((arch, name_a, name_b, sa, sb, tests, verdict_str, verdict_color))

        # If group has >2 sessions, also print an overall ranking
        if len(ranked) > 2:
            print(f"\n{BOLD}  Overall Ranking (by avg PnL/trade):{RESET}")
            for rank, (name, stats) in enumerate(ranked, 1):
                marker = f"{GREEN}<<< BEST{RESET}" if rank == 1 else ""
                pnl_color = GREEN if stats["avg_pnl"] > 0 else RED
                print(
                    f"    {rank}. {BOLD}{name:<24}{RESET}  "
                    f"avg: {pnl_color}{stats['avg_pnl']:>+7.2f}{RESET}  "
                    f"WR: {color_wr(stats['wr'])}  "
                    f"Sharpe: {stats['sharpe']:.3f}  "
                    f"N={stats['n']}  {marker}"
                )

    # ---------------------------------------------------------------------------
    # Summary table
    # ---------------------------------------------------------------------------
    if not verdicts:
        print(f"\n{YELLOW}No architecture groups with 2+ sessions to compare.{RESET}")
        return

    print(f"\n\n{BOLD}{'=' * 70}{RESET}")
    print(f"{BOLD}  SUMMARY TABLE{RESET}")
    print(f"{BOLD}{'=' * 70}{RESET}")
    print(
        f"  {'Arch':<18} {'A':<16} {'B':<16} "
        f"{'A WR':>6} {'B WR':>6} {'A Avg':>8} {'B Avg':>8} "
        f"{'p(WR)':>8} {'p(PnL)':>8}  {'Verdict'}"
    )
    print(f"  {'-' * 110}")

    for arch, na, nb, sa, sb, tests, verdict_str, verdict_color in verdicts:
        print(
            f"  {arch:<18} {na:<16} {nb:<16} "
            f"{sa['wr']:>5.1f}% {sb['wr']:>5.1f}% "
            f"{sa['avg_pnl']:>+8.2f} {sb['avg_pnl']:>+8.2f} "
            f"{tests['chi2_p']:>8.4f} {tests['t_p']:>8.4f}  "
            f"{verdict_color}{verdict_str}{RESET}"
        )

    print(f"\n  {DIM}p < 0.05 = significant  |  p < 0.01 = highly significant  |  min 30 trades per side{RESET}")
    print()


if __name__ == "__main__":
    main()
