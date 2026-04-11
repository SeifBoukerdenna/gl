"""
Post-fix performance review.

The NO sizing bug was fixed at ~16:36 UTC on 2026-04-10. All sessions were
nuked and restarted. This script analyses the ~7 hours of clean data that
followed against the BTC market context over the same period.

Looks for:
  1. Per-session PnL, WR, $/tr (clean baseline)
  2. NO trade size validation — are notional figures actually capped near $200?
  3. Direction split (YES vs NO) — are NO trades less catastrophic now?
  4. Correlation with BTC moves (was the period a regime favoring/hurting oracle?)
  5. Whether titan / omega / pivot_1 actually fired and how
  6. Did the multi-venue gate prevent any trades on consensus / titan / pivot_1?
"""
import csv
import pandas as pd
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")

ACTIVE = [
    "oracle_1", "oracle_alpha", "oracle_arb", "oracle_chrono",
    "oracle_consensus", "oracle_echo", "oracle_omega", "oracle_titan",
    "pivot_1", "blitz_1", "edge_hunter", "test_ic_wide", "test_xp",
]

# Reset epoch (rough start of clean data — bot was nuked around 16:35-16:45 UTC)
RESET_TS = 1775837700  # 2026-04-10 16:15 UTC, generous lower bound


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
                    continue
                fp = float(r["fill_price"])
                d = r["direction"]
                te = fp if d == "YES" else 1 - fp
                out.append({
                    "ts": ts,
                    "fp": fp,
                    "te": te,  # true entry cost
                    "dir": d,
                    "size": float(r["filled_size"]),
                    "notional": float(r["notional"]),
                    "pnl": float(r["pnl_taker"]),
                    "delta": float(r.get("delta_bps", 0)),
                    "abs_delta": abs(float(r.get("delta_bps", 0))),
                    "tr": float(r["time_remaining"]),
                    "result": r["result"],
                    "btc": float(r.get("btc_price", 0)),
                })
            except (ValueError, KeyError):
                continue
    return sorted(out, key=lambda t: t["ts"])


def hr(s):
    print()
    print("─" * 90)
    print("  " + s)
    print("─" * 90)


def main():
    sessions = {n: load(n) for n in ACTIVE}
    sessions = {k: v for k, v in sessions.items() if v}

    print()
    print("=" * 90)
    print("  POST-FIX PERFORMANCE REVIEW")
    print("=" * 90)

    # Determine actual data window from any non-empty session
    all_ts = [t["ts"] for trs in sessions.values() for t in trs]
    if not all_ts:
        print("\n  no trades since reset")
        return
    t_start = min(all_ts)
    t_end = max(all_ts)
    duration_h = (t_end - t_start) / 3600
    print(f"\n  Reset bound: {datetime.fromtimestamp(RESET_TS, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  First trade: {datetime.fromtimestamp(t_start, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Last trade:  {datetime.fromtimestamp(t_end, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Duration:    {duration_h:.1f} hours")

    # ═══════════════════════════════════════════════════════════════
    hr("BTC market context — what did the price do during this window?")
    klines = pd.read_parquet("data/binance_klines_60d.parquet")
    klines["ts_sec"] = (klines["timestamp"] / 1000).astype(int)
    k = klines.set_index("ts_sec").sort_index()

    try:
        btc_start = float(k.loc[int(t_start), "close"])
    except KeyError:
        btc_start = float(k.iloc[k.index.get_indexer([int(t_start)], method='nearest')[0]]["close"])
    try:
        btc_end = float(k.loc[int(t_end), "close"])
    except KeyError:
        btc_end = float(k.iloc[k.index.get_indexer([int(t_end)], method='nearest')[0]]["close"])

    drift_dollars = btc_end - btc_start
    drift_bps = drift_dollars / btc_start * 10000
    print(f"\n  BTC start: ${btc_start:>10,.2f}")
    print(f"  BTC end:   ${btc_end:>10,.2f}")
    print(f"  Drift:     ${drift_dollars:+,.2f}  ({drift_bps:+.0f} bps over {duration_h:.1f}h)")

    # Sample BTC trajectory
    print(f"\n  BTC trajectory (1h samples):")
    cur = int(t_start)
    last_btc = btc_start
    while cur <= int(t_end):
        try:
            px = float(k.loc[cur, "close"])
        except KeyError:
            cur += 3600
            continue
        delta = (px - last_btc) / last_btc * 10000
        bar = "█" * max(1, int(abs(delta) / 5))
        sign = "▲" if delta >= 0 else "▼"
        print(f"    {datetime.fromtimestamp(cur, tz=timezone.utc).strftime('%H:%M')}  ${px:>10,.2f}  {sign}{abs(delta):>5.1f}bp  {bar}")
        last_btc = px
        cur += 3600

    # Realized vol over the window
    closes = k.loc[int(t_start):int(t_end), "close"].values
    if len(closes) > 60:
        rets = [(closes[i] - closes[i-1])/closes[i-1] for i in range(1, len(closes))]
        import statistics
        vol_1s = statistics.stdev(rets) * 10000  # bps per second
        vol_1m_annualized = vol_1s * (60 * 60 * 24 * 365) ** 0.5 / 10000 * 100
        print(f"\n  Realized vol over window: {vol_1s:.2f} bps/s  (~{vol_1m_annualized:.0f}% annualized)")

    # ═══════════════════════════════════════════════════════════════
    hr("Per-session results (clean data only)")
    print()
    print(f"  {'session':<22} {'n':<5} {'pnl':<10} {'wr':<8} {'$/tr':<10} {'avg notional':<14}")
    print("  " + "-" * 75)
    rows = []
    for name in ACTIVE:
        sub = sessions.get(name, [])
        if not sub:
            print(f"  {name:<22} 0     (no trades since reset)")
            continue
        n = len(sub)
        pnl = sum(t["pnl"] for t in sub)
        wins = sum(1 for t in sub if t["pnl"] > 0)
        wr = wins / n * 100
        avg_not = sum(t["notional"] for t in sub) / n
        rows.append((name, n, pnl, wr, avg_not))
        print(f"  {name:<22} {n:<5} ${pnl:>+8,.0f}  {wr:>5.1f}%  ${pnl/n:>+7.2f}  ${avg_not:>10.0f}")

    total_n = sum(r[1] for r in rows)
    total_pnl = sum(r[2] for r in rows)
    print(f"  {'-' * 50}")
    print(f"  {'PORTFOLIO TOTAL':<22} {total_n:<5} ${total_pnl:>+8,.0f}")

    # ═══════════════════════════════════════════════════════════════
    hr("NO sizing fix validation — are NO trade notionals capped near base?")
    print(f"\n  {'session':<20} {'NO n':<6} {'NO avg notional':<18} {'NO max notional':<18} {'NO win rate':<12}")
    for name, sub in sessions.items():
        no = [t for t in sub if t["dir"] == "NO"]
        if not no:
            continue
        avg = sum(t["notional"] for t in no) / len(no)
        mx = max(t["notional"] for t in no)
        wr = sum(1 for t in no if t["pnl"] > 0) / len(no) * 100
        flag = "  ⚠ over $300" if mx > 300 else ""
        print(f"  {name:<20} {len(no):<6} ${avg:<16.0f} ${mx:<16.0f} {wr:>5.1f}%{flag}")

    # ═══════════════════════════════════════════════════════════════
    hr("YES vs NO direction split (do NO bets still bleed disproportionately?)")
    print()
    print(f"  {'session':<20} {'YES n':<6} {'YES PnL':<10} {'YES WR':<8} {'NO n':<6} {'NO PnL':<10} {'NO WR':<8}")
    for name, sub in sessions.items():
        yes = [t for t in sub if t["dir"] == "YES"]
        no = [t for t in sub if t["dir"] == "NO"]
        yp = sum(t["pnl"] for t in yes) if yes else 0
        np_ = sum(t["pnl"] for t in no) if no else 0
        ywr = sum(1 for t in yes if t["pnl"] > 0) / len(yes) * 100 if yes else 0
        nwr = sum(1 for t in no if t["pnl"] > 0) / len(no) * 100 if no else 0
        print(f"  {name:<20} {len(yes):<6} ${yp:>+8,.0f}  {ywr:>5.1f}%  {len(no):<6} ${np_:>+8,.0f}  {nwr:>5.1f}%")

    # ═══════════════════════════════════════════════════════════════
    hr("Oracle family vs multi-venue family aggregate")
    oracle_fam = ["oracle_1","oracle_alpha","oracle_arb","oracle_chrono","oracle_consensus","oracle_echo","oracle_omega","oracle_titan"]
    mv_fam = ["blitz_1","test_ic_wide","test_xp","edge_hunter"]
    pivot_fam = ["pivot_1"]

    for label, fam in [("oracle (8 sessions)", oracle_fam),
                        ("multi-venue (4 sessions)", mv_fam),
                        ("pivot (1 session)", pivot_fam)]:
        all_t = []
        for n in fam:
            all_t.extend(sessions.get(n, []))
        if not all_t:
            print(f"  {label}: no trades")
            continue
        n = len(all_t)
        pnl = sum(t["pnl"] for t in all_t)
        wins = sum(1 for t in all_t if t["pnl"] > 0)
        avg_not = sum(t["notional"] for t in all_t) / n
        print(f"  {label:<28} n={n:<4} pnl=${pnl:>+8,.0f}  WR={wins/n*100:.1f}%  $/tr=${pnl/n:+.2f}  avg_not=${avg_not:.0f}")

    # ═══════════════════════════════════════════════════════════════
    hr("Defensive layer activity (titan / omega / pivot_1)")
    # Just look at fire counts vs expected
    for name in ["oracle_omega", "oracle_titan", "pivot_1", "oracle_chrono", "oracle_consensus"]:
        sub = sessions.get(name, [])
        if not sub:
            continue
        n = len(sub)
        per_hour = n / duration_h if duration_h > 0 else 0
        print(f"  {name:<20} {n:>3} fires  ({per_hour:.1f}/hr, projected {per_hour*24:.0f}/day)")

    # ═══════════════════════════════════════════════════════════════
    hr("Hour-by-hour PnL alongside BTC drift")
    print()
    print(f"  {'hour UTC':<10} {'btc move':<12} {'top 3 winners':<35} {'top 3 losers':<35}")
    # Bucket trades by hour
    by_hour_pnl = defaultdict(lambda: defaultdict(float))
    for name, sub in sessions.items():
        for t in sub:
            h = int(t["ts"] // 3600) * 3600
            by_hour_pnl[h][name] += t["pnl"]

    for h in sorted(by_hour_pnl.keys()):
        try:
            btc_h_start = float(k.loc[h, "close"])
            btc_h_end = float(k.loc[h + 3600, "close"])
            move_bps = (btc_h_end - btc_h_start) / btc_h_start * 10000
        except KeyError:
            move_bps = 0
        scores = sorted(by_hour_pnl[h].items(), key=lambda x: -x[1])
        winners = scores[:3]
        losers = list(reversed(scores[-3:]))
        w_str = ", ".join(f"{n[:8]}+{int(p)}" for n, p in winners if p > 0)[:33]
        l_str = ", ".join(f"{n[:8]}{int(p):+d}" for n, p in losers if p < 0)[:33]
        print(f"  {datetime.fromtimestamp(h, tz=timezone.utc).strftime('%H:%M'):<10} {move_bps:+6.0f}bp     {w_str:<35} {l_str:<35}")


if __name__ == "__main__":
    main()
