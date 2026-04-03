"""
Live validation: compare delta table fair probabilities against
Polymarket's live order book in real time.

Answers: is the 8-cent gap real or a stale-price artifact?

Usage: python live_validation.py
       Press Ctrl+C to stop and run full analysis.
"""

import asyncio
import csv
import json
import signal
import sys
import time
from pathlib import Path

import httpx
import websockets

# ── Config ────────────────────────────────────────────────────────
SNAPSHOT_INTERVAL = 5
WINDOW_BUFFER = 10
FLUSH_INTERVAL = 60
MIN_DATA_FOR_ANALYSIS = 100
DELTA_TABLE_PATH = Path("data/delta_table_corrected.csv")
OUTPUT_PATH = Path("data/live_validation.csv")
GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_URL = "https://clob.polymarket.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST = "https://api.binance.com/api/v3/klines"

TIME_BUCKETS = [270, 240, 210, 180, 150, 120, 90, 60, 30, 10]

CSV_COLS = [
    "timestamp", "window_start", "time_remaining", "time_bucket",
    "binance_raw", "offset", "btc_corrected", "window_open",
    "delta_bps", "delta_bucket", "fair_prob",
    "best_bid", "best_ask", "mid", "spread", "last_trade",
    "bid_depth_3", "ask_depth_3",
    "gap_mid", "gap_ask", "gap_bid", "gap_last", "stale_vs_mid",
    "outcome", "final_price",
]


# ── Bucket functions (must match Phase 1) ─────────────────────────
def get_delta_bucket(delta_bps):
    if delta_bps < -15: return "<-15"
    elif delta_bps < -10: return "[-15,-10)"
    elif delta_bps < -7: return "[-10,-7)"
    elif delta_bps < -5: return "[-7,-5)"
    elif delta_bps < -3: return "[-5,-3)"
    elif delta_bps < -1: return "[-3,-1)"
    elif delta_bps < 1: return "[-1,1)"
    elif delta_bps < 3: return "[1,3)"
    elif delta_bps < 5: return "[3,5)"
    elif delta_bps < 7: return "[5,7)"
    elif delta_bps < 10: return "[7,10)"
    elif delta_bps < 15: return "[10,15)"
    else: return ">15"


def snap_time_bucket(secs):
    return min(TIME_BUCKETS, key=lambda b: abs(b - secs))


def load_delta_table():
    import polars as pl
    dt = pl.read_csv(DELTA_TABLE_PATH)
    lookup = {}
    for row in dt.iter_rows(named=True):
        lookup[(row["delta_bucket"], row["time_bucket"])] = row["win_rate"]
    return lookup


# ── Shared state ──────────────────────────────────────────────────
class State:
    def __init__(self):
        self.binance_price = None
        self.running = True
        self.all_snapshots = []
        self.csv_buffer = []
        self.completed_windows = []
        self.pending_windows = []   # window_starts awaiting outcome resolution
        self.window_start = 0
        self.window_end = 0
        self.window_open = None
        self.offset = 35.0
        self.token_id = None
        self.errors = {"ws": 0, "api": 0, "book": 0}


state = State()


# ── Binance WebSocket ─────────────────────────────────────────────
async def binance_ws_loop():
    while state.running:
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                async for msg in ws:
                    if not state.running:
                        return
                    data = json.loads(msg)
                    state.binance_price = float(data["p"])
        except asyncio.CancelledError:
            return
        except Exception:
            state.errors["ws"] += 1
            if state.running:
                print("  [WARN] Binance WS disconnected, reconnecting in 2s...")
                await asyncio.sleep(2)


# ── Polymarket helpers ────────────────────────────────────────────
async def fetch_event(client, slug):
    """Returns (token_id, priceToBeat, finalPrice, outcome)."""
    try:
        resp = await client.get(GAMMA_URL, params={"slug": slug, "limit": 1})
        resp.raise_for_status()
        events = resp.json()
        if not events:
            return None, None, None, None

        event = events[0]

        meta_raw = event.get("eventMetadata")
        if isinstance(meta_raw, dict):
            meta = meta_raw
        elif isinstance(meta_raw, str) and meta_raw:
            meta = json.loads(meta_raw)
        else:
            meta = {}

        ptb = float(meta["priceToBeat"]) if meta.get("priceToBeat") is not None else None
        fp = float(meta["finalPrice"]) if meta.get("finalPrice") is not None else None

        token_id = None
        outcome = None
        markets = event.get("markets", [])
        if markets:
            m = markets[0]
            tokens_raw = m.get("clobTokenIds", "[]")
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else (tokens_raw or [])
            token_id = tokens[0] if tokens else None

            ops = json.loads(m.get("outcomePrices", "[]"))
            ocs = json.loads(m.get("outcomes", "[]"))
            for o, p in zip(ocs, ops):
                if str(p) == "1":
                    outcome = o

        return token_id, ptb, fp, outcome
    except Exception:
        state.errors["api"] += 1
        return None, None, None, None


async def fetch_book(client, token_id):
    """Returns (best_bid, best_ask, bid_depth_3, ask_depth_3)."""
    try:
        resp = await client.get(
            "{}/book".format(CLOB_URL), params={"token_id": token_id}
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None, None, None, None

        bid_levels = sorted(
            [(float(b["price"]), float(b["size"])) for b in bids],
            key=lambda x: -x[0],
        )
        ask_levels = sorted(
            [(float(a["price"]), float(a["size"])) for a in asks],
            key=lambda x: x[0],
        )
        return (
            bid_levels[0][0],
            ask_levels[0][0],
            sum(s for _, s in bid_levels[:3]),
            sum(s for _, s in ask_levels[:3]),
        )
    except Exception:
        state.errors["book"] += 1
        return None, None, None, None


async def fetch_last_trade(client, token_id):
    try:
        resp = await client.get(
            "{}/last-trade-price".format(CLOB_URL),
            params={"token_id": token_id},
        )
        resp.raise_for_status()
        return float(resp.json().get("price", 0))
    except Exception:
        return None


# ── Bootstrap offset ──────────────────────────────────────────────
async def bootstrap_offset(client):
    """Find recent window with priceToBeat, fetch Binance at that time, compute exact offset."""
    now = int(time.time())
    ws = now - (now % 300)

    for i in range(1, 100):
        ts = ws - (i * 300)
        slug = "btc-updown-5m-{}".format(ts)
        _, ptb, _, _ = await fetch_event(client, slug)

        if ptb:
            # Fetch what Binance was at that exact time
            try:
                resp = await client.get(
                    BINANCE_REST,
                    params={
                        "symbol": "BTCUSDT",
                        "interval": "1s",
                        "startTime": ts * 1000,
                        "limit": 1,
                    },
                )
                klines = resp.json()
                if klines:
                    binance_then = float(klines[0][4])  # close
                    offset = binance_then - ptb
                    return offset, ptb, ts
            except Exception:
                pass

            # Fallback: rough estimate from current price
            if state.binance_price:
                return state.binance_price - ptb, ptb, ts
            return 35.0, ptb, ts

        await asyncio.sleep(0.15)

    return 35.0, None, None


# ── CSV persistence ───────────────────────────────────────────────
def flush_csv():
    if not state.csv_buffer:
        return
    exists = OUTPUT_PATH.exists()
    with open(OUTPUT_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for snap in state.csv_buffer:
            writer.writerow(snap)
    state.csv_buffer.clear()


def rewrite_csv():
    """Rewrite full CSV with outcomes filled in."""
    with open(OUTPUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        writer.writeheader()
        for snap in state.all_snapshots:
            writer.writerow(snap)


# ── Async sleep that respects shutdown ────────────────────────────
async def wait(seconds):
    end = time.time() + seconds
    while state.running and time.time() < end:
        await asyncio.sleep(min(0.5, max(0, end - time.time())))


# ── Rolling summary ───────────────────────────────────────────────
def print_rolling_summary():
    recent_ws = {w["window_start"] for w in state.completed_windows[-10:]}
    recent = [
        s for s in state.all_snapshots
        if s["window_start"] in recent_ws and s["outcome"]
    ]
    if not recent:
        return

    n = len(recent)
    offsets = [s["offset"] for s in recent]
    stales = [abs(s["stale_vs_mid"]) for s in recent if s["stale_vs_mid"] is not None]

    print("\n\u2550\u2550 ROLLING {} WINDOWS ".format(
        len(state.completed_windows[-10:])
    ) + "\u2550" * 55)

    stale_str = ""
    if stales:
        stale_str = " | Stale: |last-mid| = {:.1f}\u00a2".format(
            sum(stales) / len(stales) * 100
        )
    print(" {} snaps | Avg offset: ${:.0f}{}".format(
        n, sum(offsets) / len(offsets), stale_str
    ))

    print("{:>16} {:>10} {:>10} {:>10} {:>10}".format(
        "", "Mid", "Ask", "Bid", "Last"
    ))

    for label, fn in [
        ("Mean |gap|:", lambda v: "${:.1f}\u00a2".format(sum(v) / len(v) * 100) if v else "N/A"),
        ("Med |gap|:", lambda v: "${:.1f}\u00a2".format(sorted(v)[len(v) // 2] * 100) if v else "N/A"),
        ("|gap|>3c:", lambda v: "{:.1f}%".format(sum(1 for x in v if x > 0.03) / len(v) * 100) if v else "N/A"),
    ]:
        row = " {:>15}".format(label)
        for g in ["mid", "ask", "bid", "last"]:
            vals = [abs(s["gap_{}".format(g)]) for s in recent if s["gap_{}".format(g)] is not None]
            row += " {:>10}".format(fn(vals))
        print(row)

    print("\u2550" * 70)


# ── Background outcome resolver ───────────────────────────────────
async def outcome_resolver_loop():
    """Background task that periodically resolves outcomes for completed windows.
    Polymarket takes 5-10 minutes to settle, so we poll every 30s for unresolved windows."""
    http = httpx.AsyncClient(
        timeout=10.0, headers={"User-Agent": "btc-live-val/1.0"}
    )
    try:
        while state.running:
            await wait(30)
            if not state.running:
                break

            # Find unresolved windows (older than 60s)
            now = int(time.time())
            resolved_ws = {w["window_start"] for w in state.completed_windows}
            unresolved = [
                ws for ws in state.pending_windows
                if ws not in resolved_ws and (now - ws) > 360  # at least 6 min old
            ]

            for ws in unresolved:
                if not state.running:
                    break
                slug = "btc-updown-5m-{}".format(ws)
                _, _, fp, outcome = await fetch_event(http, slug)
                if outcome:
                    for s in state.all_snapshots:
                        if s["window_start"] == ws and s["outcome"] is None:
                            s["outcome"] = outcome
                            s["final_price"] = fp
                    snap_count = sum(1 for s in state.all_snapshots if s["window_start"] == ws)
                    state.completed_windows.append({
                        "window_start": ws,
                        "outcome": outcome,
                        "final_price": fp,
                        "snap_count": snap_count,
                    })

                    # Compute avg gaps for this window
                    w_snaps = [s for s in state.all_snapshots if s["window_start"] == ws]
                    avgs = {}
                    for g in ["mid", "ask", "last"]:
                        vals = [s["gap_{}".format(g)] for s in w_snaps if s["gap_{}".format(g)] is not None]
                        avgs[g] = sum(vals) / len(vals) if vals else 0

                    print("-- WIN {} {:>4} | {} snaps | "
                          "gap(mid){:+.3f} gap(ask){:+.3f} gap(last){:+.3f}"
                          " | offset ${:.0f}  [resolved]".format(
                              ws, outcome, snap_count,
                              avgs["mid"], avgs["ask"], avgs["last"],
                              state.offset,
                          ))

                    # Rolling summary every 10 resolved windows
                    if len(state.completed_windows) % 10 == 0:
                        print_rolling_summary()

                await wait(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        await http.aclose()


# ── Final resolution pass on shutdown ─────────────────────────────
async def resolve_all_pending():
    """Resolve all pending windows on shutdown. Tries each once."""
    resolved_ws = {w["window_start"] for w in state.completed_windows}
    unresolved = [ws for ws in state.pending_windows if ws not in resolved_ws]

    if not unresolved:
        return

    print("  Resolving {} pending windows...".format(len(unresolved)))
    resolved = 0
    async with httpx.AsyncClient(
        timeout=10.0, headers={"User-Agent": "btc-live-val/1.0"}
    ) as http:
        for ws in unresolved:
            slug = "btc-updown-5m-{}".format(ws)
            _, _, fp, outcome = await fetch_event(http, slug)
            if outcome:
                for s in state.all_snapshots:
                    if s["window_start"] == ws and s["outcome"] is None:
                        s["outcome"] = outcome
                        s["final_price"] = fp
                snap_count = sum(1 for s in state.all_snapshots if s["window_start"] == ws)
                state.completed_windows.append({
                    "window_start": ws,
                    "outcome": outcome,
                    "final_price": fp,
                    "snap_count": snap_count,
                })
                resolved += 1
            await asyncio.sleep(0.2)

    print("  Resolved {} / {} pending".format(resolved, len(unresolved)))


# ── Main snapshot loop ────────────────────────────────────────────
async def snapshot_loop(delta_lookup):
    http = httpx.AsyncClient(
        timeout=10.0, headers={"User-Agent": "btc-live-val/1.0"}
    )
    last_flush = time.time()
    win_snaps = 0
    win_gaps = {"mid": [], "ask": [], "bid": [], "last": []}

    try:
        while state.running:
            now = int(time.time())

            # ── Window transition ─────────────────────────────
            if now >= state.window_end and state.window_start > 0:
                # Print interim summary
                if win_snaps > 0:
                    avgs = {
                        k: sum(v) / len(v) if v else 0
                        for k, v in win_gaps.items()
                    }
                    sys.stdout.write(
                        "\n-- WIN {} pending | {} snaps | "
                        "gap(mid){:+.3f} gap(ask){:+.3f} gap(last){:+.3f}"
                        " | offset ${:.0f}\n".format(
                            state.window_start, win_snaps,
                            avgs["mid"], avgs["ask"], avgs["last"],
                            state.offset,
                        )
                    )
                    sys.stdout.flush()

                # Record this window as pending resolution
                state.pending_windows.append(state.window_start)

                # Brief pause for transition
                await wait(WINDOW_BUFFER)
                if not state.running:
                    break

                # Setup new window
                now = int(time.time())
                state.window_start = now - (now % 300)
                state.window_end = state.window_start + 300

                new_slug = "btc-updown-5m-{}".format(state.window_start)
                tid, ptb, _, _ = await fetch_event(http, new_slug)

                if not tid:
                    for _ in range(3):
                        await wait(2)
                        if not state.running:
                            break
                        tid, ptb, _, _ = await fetch_event(http, new_slug)
                        if tid:
                            break

                if tid:
                    state.token_id = tid
                    if ptb and state.binance_price:
                        state.offset = state.binance_price - ptb
                        state.window_open = ptb
                    else:
                        state.window_open = state.binance_price - state.offset
                else:
                    print("  [WARN] No market for {}, skipping".format(new_slug))
                    state.window_open = state.binance_price - state.offset if state.binance_price else None

                win_snaps = 0
                win_gaps = {"mid": [], "ask": [], "bid": [], "last": []}

                # Buffer at start of new window
                elapsed = int(time.time()) - state.window_start
                if elapsed < WINDOW_BUFFER:
                    await wait(WINDOW_BUFFER - elapsed)
                continue

            # ── Guards ────────────────────────────────────────
            if not state.token_id or not state.binance_price or not state.window_open:
                await wait(1)
                continue

            elapsed = now - state.window_start
            if elapsed < WINDOW_BUFFER:
                await wait(1)
                continue

            time_remaining = state.window_end - now
            if time_remaining <= 0:
                await asyncio.sleep(0.2)
                continue

            # ── Compute delta ─────────────────────────────────
            btc_corr = state.binance_price - state.offset
            delta_bps = (btc_corr - state.window_open) / state.window_open * 10000
            delta_bucket = get_delta_bucket(delta_bps)
            time_bucket = snap_time_bucket(time_remaining)
            fair_prob = delta_lookup.get((delta_bucket, time_bucket))

            # ── Fetch Polymarket ──────────────────────────────
            (best_bid, best_ask, bid_d3, ask_d3), last_trade = await asyncio.gather(
                fetch_book(http, state.token_id),
                fetch_last_trade(http, state.token_id),
            )

            mid = None
            spread = None
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2
                spread = best_ask - best_bid

            gap_mid = (fair_prob - mid) if fair_prob is not None and mid is not None else None
            gap_ask = (fair_prob - best_ask) if fair_prob is not None and best_ask is not None else None
            gap_bid = (fair_prob - best_bid) if fair_prob is not None and best_bid is not None else None
            gap_last = (fair_prob - last_trade) if fair_prob is not None and last_trade is not None else None
            stale = (last_trade - mid) if last_trade is not None and mid is not None else None

            # ── Record ────────────────────────────────────────
            snap = {
                "timestamp": int(time.time() * 1000),
                "window_start": state.window_start,
                "time_remaining": time_remaining,
                "time_bucket": time_bucket,
                "binance_raw": round(state.binance_price, 2),
                "offset": round(state.offset, 2),
                "btc_corrected": round(btc_corr, 2),
                "window_open": round(state.window_open, 2),
                "delta_bps": round(delta_bps, 2),
                "delta_bucket": delta_bucket,
                "fair_prob": round(fair_prob, 6) if fair_prob is not None else None,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": round(mid, 4) if mid is not None else None,
                "spread": round(spread, 4) if spread is not None else None,
                "last_trade": last_trade,
                "bid_depth_3": round(bid_d3, 1) if bid_d3 is not None else None,
                "ask_depth_3": round(ask_d3, 1) if ask_d3 is not None else None,
                "gap_mid": round(gap_mid, 6) if gap_mid is not None else None,
                "gap_ask": round(gap_ask, 6) if gap_ask is not None else None,
                "gap_bid": round(gap_bid, 6) if gap_bid is not None else None,
                "gap_last": round(gap_last, 6) if gap_last is not None else None,
                "stale_vs_mid": round(stale, 6) if stale is not None else None,
                "outcome": None,
                "final_price": None,
            }

            state.all_snapshots.append(snap)
            state.csv_buffer.append(snap)
            win_snaps += 1

            for g in ["mid", "ask", "bid", "last"]:
                v = snap["gap_{}".format(g)]
                if v is not None:
                    win_gaps[g].append(v)

            # ── Print compact line ────────────────────────────
            t_str = time.strftime("%H:%M:%S", time.localtime())
            fp_str = "{:.1f}".format(fair_prob * 100) if fair_prob else "?"
            bid_s = "{:.0f}".format(best_bid * 100) if best_bid is not None else "?"
            ask_s = "{:.0f}".format(best_ask * 100) if best_ask is not None else "?"
            mid_s = "{:.1f}".format(mid * 100) if mid is not None else "?"
            last_s = "{:.0f}".format(last_trade * 100) if last_trade is not None else "?"
            spr_s = "{:.0f}".format(spread * 100) if spread is not None else "?"
            gm_s = "{:+.1f}".format(gap_mid * 100) if gap_mid is not None else "?"
            ga_s = "{:+.1f}".format(gap_ask * 100) if gap_ask is not None else "?"

            print(
                "{} T-{:>3}s D{:+.1f}bp Fair:{}% Bid:{} Ask:{} Mid:{}"
                " Last:{} Spr:{}c Gap(m){}c Gap(a){}c".format(
                    t_str, time_remaining, delta_bps, fp_str,
                    bid_s, ask_s, mid_s, last_s, spr_s, gm_s, ga_s,
                )
            )

            # ── Periodic flush ────────────────────────────────
            if time.time() - last_flush >= FLUSH_INTERVAL:
                flush_csv()
                last_flush = time.time()

            await wait(SNAPSHOT_INTERVAL)

    finally:
        await http.aclose()


# ── Final analysis ────────────────────────────────────────────────
def run_analysis():
    completed_ws = {w["window_start"] for w in state.completed_windows}
    data = [
        s for s in state.all_snapshots
        if s["window_start"] in completed_ws and s["outcome"]
    ]

    total_snaps = len(state.all_snapshots)
    total_wins = len(state.completed_windows)

    if len(data) < MIN_DATA_FOR_ANALYSIS:
        print(
            "\nInsufficient data for full analysis ({} snapshots with outcomes, need {})".format(
                len(data), MIN_DATA_FOR_ANALYSIS
            )
        )
        print("Completed windows: {}  |  Total snapshots: {}".format(total_wins, total_snaps))
        if data:
            print("Partial stats:")
            for g in ["mid", "ask", "last"]:
                vals = [abs(s["gap_{}".format(g)]) for s in data if s["gap_{}".format(g)] is not None]
                if vals:
                    print("  gap({}): mean |gap| = ${:.3f}".format(g, sum(vals) / len(vals)))
        return

    n = len(data)
    hours = (data[-1]["timestamp"] - data[0]["timestamp"]) / 3_600_000

    print("\n\n{}".format("=" * 70))
    print("FINAL ANALYSIS  |  {} snapshots  |  {} windows  |  {:.1f} hours".format(
        n, total_wins, hours
    ))
    print("=" * 70)

    # 1. Offset stability
    offsets = [s["offset"] for s in data]
    mean_off = sum(offsets) / len(offsets)
    std_off = (sum((x - mean_off) ** 2 for x in offsets) / len(offsets)) ** 0.5

    print("\n=== OFFSET STABILITY ===")
    print("  Windows: {}  |  Mean: ${:.2f}  |  Std: ${:.2f}  |  Range: ${:.0f} - ${:.0f}".format(
        total_wins, mean_off, std_off, min(offsets), max(offsets)
    ))
    print("  Offset is {} — correction is {}".format(
        "STABLE" if std_off < 10 else "DRIFTING",
        "RELIABLE" if std_off < 10 else "APPROXIMATE",
    ))

    # 2. Staleness
    stales = [abs(s["stale_vs_mid"]) for s in data if s["stale_vs_mid"] is not None]
    mean_stale = sum(stales) / len(stales) if stales else 0

    print("\n=== STALENESS ===")
    print("  Mean |last_trade - mid|: ${:.3f}".format(mean_stale))
    print("  Phase 2 measured gaps against last-traded price (median ~$0.08).")
    print("  Staleness explains ~${:.3f} of that gap.".format(mean_stale))

    # 3. Gap distribution
    gap_types = ["mid", "ask", "bid", "last"]

    def get_gaps(dataset, gtype):
        return [abs(s["gap_{}".format(gtype)]) for s in dataset if s["gap_{}".format(gtype)] is not None]

    def median(vals):
        v = sorted(vals)
        return v[len(v) // 2] if v else 0

    print("\n=== GAP DISTRIBUTION {}".format("=" * 45))
    header = "  {:>20} {:>10} {:>10} {:>10} {:>10}".format("", "Mid", "Ask", "Bid", "Last")
    print(header)

    row = "  {:>20}".format("Mean |gap|:")
    for g in gap_types:
        v = get_gaps(data, g)
        row += " ${:>8.3f}".format(sum(v) / len(v)) if v else " {:>9}".format("N/A")
    print(row)

    row = "  {:>20}".format("Median |gap|:")
    for g in gap_types:
        v = get_gaps(data, g)
        row += " ${:>8.3f}".format(median(v)) if v else " {:>9}".format("N/A")
    print(row)

    for thresh in [0.01, 0.02, 0.03, 0.05]:
        row = "  {:>20}".format("|gap| > ${:.2f}:".format(thresh))
        for g in gap_types:
            v = get_gaps(data, g)
            pct = sum(1 for x in v if x > thresh) / len(v) * 100 if v else 0
            row += " {:>8.1f}%".format(pct) if v else " {:>9}".format("N/A")
        print(row)

    # 4. Strong-signal zones
    strong = [
        s for s in data
        if s["fair_prob"] is not None and (s["fair_prob"] > 0.60 or s["fair_prob"] < 0.40)
    ]

    if strong:
        print("\n=== GAP IN STRONG-SIGNAL ZONES (fair prob >60% or <40%) ===")
        print("  Observations: {} / {} ({:.1f}%)".format(len(strong), n, len(strong) / n * 100))

        row = "  {:>20}".format("Mean |gap|:")
        for g in gap_types:
            v = get_gaps(strong, g)
            row += " ${:>8.3f}".format(sum(v) / len(v)) if v else " {:>9}".format("N/A")
        print(row)

        row = "  {:>20}".format("Median |gap|:")
        for g in gap_types:
            v = get_gaps(strong, g)
            row += " ${:>8.3f}".format(median(v)) if v else " {:>9}".format("N/A")
        print(row)

    # 5. Gap by time remaining
    print("\n=== GAP BY TIME REMAINING ===")
    print("  {:>6} | {:>8} | {:>8} | {:>9} | {:>6} | {:>6}".format(
        "Time", "gap(mid)", "gap(ask)", "gap(last)", "spread", "depth"
    ))
    print("  {} | {} | {} | {} | {} | {}".format(
        "-" * 6, "-" * 8, "-" * 8, "-" * 9, "-" * 6, "-" * 6
    ))

    for tb in TIME_BUCKETS:
        bd = [s for s in data if s["time_bucket"] == tb]
        if not bd:
            continue
        gm = get_gaps(bd, "mid")
        ga = get_gaps(bd, "ask")
        gl = get_gaps(bd, "last")
        sp = [s["spread"] for s in bd if s["spread"] is not None]
        dp = [s["bid_depth_3"] for s in bd if s["bid_depth_3"] is not None]

        print("  {:>4}s | {:>7} | {:>7} | {:>8} | {:>5} | {:>6}".format(
            tb,
            "{:.1f}c".format(sum(gm) / len(gm) * 100) if gm else "N/A",
            "{:.1f}c".format(sum(ga) / len(ga) * 100) if ga else "N/A",
            "{:.1f}c".format(sum(gl) / len(gl) * 100) if gl else "N/A",
            "{:.1f}c".format(sum(sp) / len(sp) * 100) if sp else "N/A",
            "{:.0f}".format(sum(dp) / len(dp)) if dp else "N/A",
        ))

    # 6. Spread analysis
    spreads = [s["spread"] for s in data if s["spread"] is not None]
    if spreads:
        print("\n=== SPREAD ANALYSIS ===")
        print("  Mean: {:.1f}c  |  Median: {:.1f}c  |  Min: {:.1f}c  |  Max: {:.1f}c".format(
            sum(spreads) / len(spreads) * 100,
            median(spreads) * 100,
            min(spreads) * 100,
            max(spreads) * 100,
        ))

    # 7. PnL simulation
    print("\n=== PnL SIMULATION (actual Polymarket outcomes) ===")
    print("  {:>8} | {:>5} | {:>6} | {:>5} | {:>8} | {:>8}".format(
        "Thresh", "Entry", "Trades", "Win%", "Avg PnL", "Total"
    ))
    print("  {} | {} | {} | {} | {} | {}".format(
        "-" * 8, "-" * 5, "-" * 6, "-" * 5, "-" * 8, "-" * 8
    ))

    for thresh in [0.03, 0.05]:
        for entry_key, label in [("best_ask", "Ask"), ("mid", "Mid"), ("best_bid", "Bid")]:
            trades = []
            for s in data:
                if s["fair_prob"] is None or s[entry_key] is None or s["outcome"] is None:
                    continue
                gap = s["fair_prob"] - s[entry_key]
                if gap > thresh:
                    entry = s[entry_key]
                    won = s["outcome"] == "Up"
                    trades.append((won, (1.0 - entry) if won else -entry))
                elif gap < -thresh:
                    entry = 1.0 - s[entry_key]
                    won = s["outcome"] == "Down"
                    trades.append((won, (1.0 - entry) if won else -entry))

            if not trades:
                print("  {:>8.2f} | {:>5} | {:>6} | {:>5} | {:>8} | {:>8}".format(
                    thresh, label, 0, "N/A", "N/A", "N/A"
                ))
                continue

            wins = sum(1 for w, _ in trades if w)
            wr = wins / len(trades)
            avg_pnl = sum(p for _, p in trades) / len(trades)
            total_pnl = sum(p for _, p in trades)

            print("  {:>8.2f} | {:>5} | {:>6} | {:>4.1f}% | ${:>7.4f} | ${:>7.2f}".format(
                thresh, label, len(trades), wr * 100, avg_pnl, total_pnl
            ))

    # 8. Final verdict
    print("\n{}".format("=" * 70))
    print("=== FINAL VERDICT ===")
    print("=" * 70)

    print("\n  Data: {:.1f} hours | {} windows | {} snapshots".format(
        hours, total_wins, n
    ))
    print("  BTC tracking: Binance with offset correction (avg: ${:.0f})".format(mean_off))

    gap_mid_vals = get_gaps(data, "mid")
    real_gap_mid = sum(gap_mid_vals) / len(gap_mid_vals) if gap_mid_vals else 0

    print("\n  Phase 2 gap (~$0.08) decomposition:")
    print("    Stale artifact:  ~${:.3f}".format(mean_stale))
    print("    Real gap vs mid: ~${:.3f}".format(real_gap_mid))

    for gtype, glabel in [("mid", "Mid"), ("ask", "Ask")]:
        strong_vals = get_gaps(strong, gtype) if strong else []
        med_val = median(strong_vals) if strong_vals else 0

        if med_val > 0.03:
            verdict = "EDGE EXISTS"
        elif med_val > 0.01:
            verdict = "MARGINAL"
        else:
            verdict = "NO EDGE"

        print("\n  Live gap (strong-signal zones):")
        print("    vs {}: median ${:.3f} -> {}".format(glabel, med_val, verdict))

    print("\n  Thresholds: EDGE > $0.03 | MARGINAL $0.01-$0.03 | NO EDGE < $0.01")

    mid_strong = get_gaps(strong, "mid") if strong else []
    ask_strong = get_gaps(strong, "ask") if strong else []
    mid_med = median(mid_strong) if mid_strong else 0
    ask_med = median(ask_strong) if ask_strong else 0

    print("\n  Next step:")
    if ask_med > 0.03:
        print("    EDGE vs Ask -> Build a taker bot. Even paying spread, you profit.")
    elif mid_med > 0.03:
        print("    EDGE vs Mid -> Build a maker bot. Post limits, capture the gap.")
    elif mid_med > 0.01:
        print("    MARGINAL -> Edge exists but thin. Needs careful execution.")
    else:
        print("    NO EDGE -> Stop. Phase 2 was a stale-price artifact.")

    print("{}".format("=" * 70))


# ── Entry point ───────────────────────────────────────────────────
async def run():
    print("Loading delta table...")
    delta_lookup = load_delta_table()
    print("  {} cells from {}".format(len(delta_lookup), DELTA_TABLE_PATH))

    print("Connecting to Binance WebSocket...")
    ws_task = asyncio.create_task(binance_ws_loop())

    for _ in range(100):
        if state.binance_price:
            break
        await asyncio.sleep(0.1)

    if not state.binance_price:
        print("Failed to connect to Binance WebSocket")
        ws_task.cancel()
        return

    print("  Binance BTC/USDT: ${:,.2f}".format(state.binance_price))

    print("Bootstrapping Chainlink-Binance offset...")
    async with httpx.AsyncClient(
        timeout=15.0, headers={"User-Agent": "btc-live-val/1.0"}
    ) as client:
        offset, ref_ptb, ref_ts = await bootstrap_offset(client)

    state.offset = offset
    if ref_ptb:
        print("  Ref window: btc-updown-5m-{} | priceToBeat=${:,.2f}".format(ref_ts, ref_ptb))
    print("  Offset: ${:.2f} (Binance - Chainlink)".format(offset))

    now = int(time.time())
    state.window_start = now - (now % 300)
    state.window_end = state.window_start + 300
    state.window_open = state.binance_price - state.offset

    print("Discovering current market...")
    async with httpx.AsyncClient(
        timeout=15.0, headers={"User-Agent": "btc-live-val/1.0"}
    ) as client:
        slug = "btc-updown-5m-{}".format(state.window_start)
        tid, ptb, _, _ = await fetch_event(client, slug)
        if tid:
            state.token_id = tid
            if ptb:
                state.offset = state.binance_price - ptb
                state.window_open = ptb
            print("  Market: {}".format(slug))
        else:
            print("  [WARN] {} not found, will retry on next transition".format(slug))

    print("\nStarted | Window {} | Open ~${:,.2f} | Binance ${:,.2f} | Offset ${:.2f}".format(
        state.window_start, state.window_open, state.binance_price, state.offset
    ))
    print("-" * 90)

    # Start background outcome resolver
    resolver_task = asyncio.create_task(outcome_resolver_loop())

    try:
        await snapshot_loop(delta_lookup)
    except asyncio.CancelledError:
        pass
    finally:
        resolver_task.cancel()
        ws_task.cancel()
        for t in [resolver_task, ws_task]:
            try:
                await t
            except asyncio.CancelledError:
                pass

        print("\n\nShutting down... resolving pending outcomes.")
        await resolve_all_pending()
        flush_csv()
        rewrite_csv()
        print("Saved {} snapshots to {}".format(len(state.all_snapshots), OUTPUT_PATH))
        print("Resolved windows: {} / {} pending".format(
            len(state.completed_windows), len(state.pending_windows)
        ))
        print("Errors: ws={} api={} book={}".format(
            state.errors["ws"], state.errors["api"], state.errors["book"]
        ))
        run_analysis()


def main():
    def handle_sig(sig, frame):
        state.running = False

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
