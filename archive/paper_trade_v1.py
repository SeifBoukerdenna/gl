"""
Paper trading bot for Heuristic C (Binance impulse → PM lag).
Runs multiple parameter combos in parallel with realistic execution simulation.

Usage: caffeinate -i python -u paper_trade.py
       Ctrl+C to stop and see full analysis.
"""

import asyncio
import csv
import json
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

# ── Config ────────────────────────────────────────────────────────
STARTING_BANKROLL = 1000  # $ per combo
BACKTEST_WIN_RATES = {    # from 48-day backtest + interpolated for new combos
    "A_5bp_30s": 0.718,
    "B_7bp_30s": 0.747,
    "C_10bp_30s": 0.779,
    "D_15bp_15s": 0.812,
    "E_5bp_15s": 0.688,
    "F_7bp_15s": 0.729,
    "G_3bp_10s": 0.636,   # from backtest: 3bp/10s
    "H_5bp_10s": 0.679,   # from backtest: 5bp/10s
    "I_10bp_15s": 0.770,  # from backtest: 10bp/15s
    "J_7bp_45s": 0.755,   # interpolated between 30s and 60s
    "K_5bp_60s": 0.754,   # from backtest: 5bp/60s
    "L_10bp_60s": 0.833,  # from backtest: 10bp/60s
}
SIGNAL_CHECK_S = 0.5
WINDOW_BUFFER_START = 10
WINDOW_BUFFER_END = 10
PRINT_STATUS_INTERVAL = 10
FLUSH_INTERVAL = 60

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_URL = "https://clob.polymarket.com"
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"

# Chainlink BTC/USD on Polygon — read once per window for priceToBeat
POLYGON_RPC = "https://polygon-mainnet.g.alchemy.com/v2/EcRCiNAXJXzC22dzlU1gc"
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

OUTPUT_PATH = Path("data/paper_trades.csv")
CSV_COLS = [
    "timestamp", "window_start", "combo", "direction", "impulse_bps",
    "time_remaining", "best_bid", "best_ask", "mid", "spread",
    "fill_price", "levels_consumed", "slippage", "fee",
    "effective_entry_taker", "effective_entry_maker", "filled_size",
    "btc_price", "delta_bps", "book_source", "book_age_ms",
    "outcome", "pnl_taker", "pnl_maker", "result",
]

COMBO_PARAMS = [
    # Original combos — proven in backtest
    {"name": "A_5bp_30s", "btc_threshold_bp": 5, "lookback_s": 30},
    {"name": "B_7bp_30s", "btc_threshold_bp": 7, "lookback_s": 30},
    {"name": "C_10bp_30s", "btc_threshold_bp": 10, "lookback_s": 30},
    {"name": "D_15bp_15s", "btc_threshold_bp": 15, "lookback_s": 15},
    {"name": "E_5bp_15s", "btc_threshold_bp": 5, "lookback_s": 15},
    {"name": "F_7bp_15s", "btc_threshold_bp": 7, "lookback_s": 15},
    # New combos — exploring fast lookbacks and wider windows
    {"name": "G_3bp_10s", "btc_threshold_bp": 3, "lookback_s": 10},   # very fast, high trade count
    {"name": "H_5bp_10s", "btc_threshold_bp": 5, "lookback_s": 10},   # fast, moderate filter
    {"name": "I_10bp_15s", "btc_threshold_bp": 10, "lookback_s": 15}, # tight filter, fast lookback
    {"name": "J_7bp_45s", "btc_threshold_bp": 7, "lookback_s": 45},   # wider window, catches sustained moves
    {"name": "K_5bp_60s", "btc_threshold_bp": 5, "lookback_s": 60},   # 1min lookback, high trade count
    {"name": "L_10bp_60s", "btc_threshold_bp": 10, "lookback_s": 60}, # 1min lookback, filtered
]


# ── Data structures ───────────────────────────────────────────────
@dataclass
class BookState:
    bids: list = field(default_factory=list)  # [(price, size)] highest first
    asks: list = field(default_factory=list)  # [(price, size)] lowest first
    best_bid: float = 0
    best_ask: float = 0
    mid: float = 0
    spread: float = 0
    updated_at: float = 0
    source: str = "none"


@dataclass
class Combo:
    name: str
    btc_threshold_bp: float
    lookback_s: int
    unsettled: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    total_pnl_taker: float = 0
    total_pnl_maker: float = 0
    bankroll: float = STARTING_BANKROLL

    @property
    def win_rate_est(self):
        """Running win rate — use actual if enough data, else backtest estimate."""
        n = len(self.trades)
        if n >= 20:
            return sum(1 for t in self.trades if t.get("result") == "WIN") / n
        return BACKTEST_WIN_RATES.get(self.name, 0.65)

    def half_kelly_size(self, entry_price):
        """Half-Kelly bet size with hard caps to prevent blowup."""
        # HARD CAP 1: Don't trade at extreme prices — no lag left to exploit
        if entry_price < 0.20 or entry_price > 0.80:
            return 0

        p = self.win_rate_est
        q = 1.0 - p
        b = (1.0 / entry_price) - 1.0
        kelly = (p * b - q) / b
        if kelly <= 0:
            return 0
        half_kelly = kelly * 0.5

        bet_dollars = half_kelly * self.bankroll

        # HARD CAP 2: Max risk = 10% of bankroll
        max_risk = self.bankroll * 0.10
        risk_per_share = entry_price  # lose entry_price per share if wrong
        max_shares_by_risk = max_risk / risk_per_share

        # HARD CAP 3: Max 500 shares per trade
        shares = min(
            bet_dollars / entry_price,
            max_shares_by_risk,
            500,
        )

        return max(5, round(shares, 1)) if shares >= 5 else 0

    def has_position_in_window(self, ws):
        """Check if this combo already traded in the given window."""
        for t in self.unsettled:
            if t["window_start"] == ws:
                return True
        for t in self.trades:
            if t["window_start"] == ws:
                return True
        return False


class State:
    def __init__(self):
        self.running = True
        # Binance
        self.binance_price = None
        self.binance_ts = 0
        self.price_buffer = deque(maxlen=120)  # (epoch_s, price)
        self.last_recorded_second = 0
        # PM book
        self.book = BookState()
        self.yes_token_id = None
        self.no_token_id = None
        # Window
        self.window_start = 0
        self.window_end = 0
        self.window_open = None       # priceToBeat (Chainlink) — exact or estimated
        self.window_open_source = "?" # "chainlink" if from PM, "estimated" if binance-offset
        self.offset = 35.0
        self.window_active = False
        self.pending_windows = []
        self.completed_windows = []
        self.chained_ptb = None       # finalPrice from last resolved window = next priceToBeat
        # Combos
        self.combos = [Combo(**p) for p in COMBO_PARAMS]
        # Persistence
        self.csv_buffer = []
        # Errors
        self.errors = {"binance_ws": 0, "pm_ws": 0, "pm_rest": 0, "api": 0}


state = State()


# ── ANSI colors ───────────────────────────────────────────────────
G = "\033[32m"    # green
R = "\033[31m"    # red
Y = "\033[33m"    # yellow
B = "\033[34m"    # blue
C = "\033[36m"    # cyan
M = "\033[35m"    # magenta
W = "\033[97m"    # white bold
DIM = "\033[2m"   # dim
BOLD = "\033[1m"  # bold
RST = "\033[0m"   # reset


# ── Helpers ───────────────────────────────────────────────────────
def fmt_time():
    return time.strftime("%H:%M:%S", time.localtime())


async def wait(seconds):
    end = time.time() + seconds
    while state.running and time.time() < end:
        await asyncio.sleep(min(0.5, max(0, end - time.time())))


def find_price_at(buf, target_s):
    """Find price closest to target_s in the buffer."""
    best = None
    best_dist = float("inf")
    for ts, px in buf:
        d = abs(ts - target_s)
        if d < best_dist:
            best_dist = d
            best = px
    return best if best_dist <= 2 else None


def walk_book(levels, size):
    """Walk order book levels to compute VWAP fill. Returns (vwap, filled, levels_hit) or None."""
    remaining = size
    total_cost = 0
    levels_hit = 0
    for price, qty in levels:
        take = min(remaining, qty)
        total_cost += take * price
        remaining -= take
        levels_hit += 1
        if remaining <= 0:
            break
    filled = size - remaining
    if filled == 0:
        return None
    return total_cost / filled, filled, levels_hit


# ── Binance WebSocket ─────────────────────────────────────────────
async def binance_ws_loop():
    while state.running:
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                async for msg in ws:
                    if not state.running:
                        return
                    data = json.loads(msg)
                    price = float(data["p"])
                    ts_ms = int(data["T"])

                    state.binance_price = price
                    state.binance_ts = ts_ms

                    # Record 1 price per second into buffer
                    current_s = ts_ms // 1000
                    if current_s != state.last_recorded_second:
                        state.price_buffer.append((current_s, price))
                        state.last_recorded_second = current_s

                        # TRIGGER signal check immediately on new price data
                        # This is the fastest possible — no polling delay
                        check_signals_sync(current_s)
        except asyncio.CancelledError:
            return
        except Exception:
            state.errors["binance_ws"] += 1
            if state.running:
                print("  [WARN] Binance WS reconnecting...")
                state.price_buffer.clear()
                await asyncio.sleep(2)


# ── Polymarket book — WebSocket with REST fallback ────────────────
def parse_snapshot(data):
    """Parse initial book snapshot (first WS message, a list)."""
    if isinstance(data, list) and data:
        snap = data[0]
    else:
        snap = data

    raw_bids = snap.get("bids", [])
    raw_asks = snap.get("asks", [])

    bids = sorted(
        [(float(b["price"]), float(b["size"])) for b in raw_bids],
        key=lambda x: -x[0],
    )
    asks = sorted(
        [(float(a["price"]), float(a["size"])) for a in raw_asks],
        key=lambda x: x[0],
    )

    book = state.book
    book.bids = bids
    book.asks = asks
    if bids and asks:
        book.best_bid = bids[0][0]
        book.best_ask = asks[0][0]
        book.mid = (book.best_bid + book.best_ask) / 2
        book.spread = book.best_ask - book.best_bid
    book.updated_at = time.time()
    book.source = "websocket"


def apply_update(data):
    """Apply incremental book update from WS."""
    pcs = data.get("price_changes", [])
    for pc in pcs:
        aid = pc.get("asset_id")

        # ONLY process updates for our YES token — NO token has
        # inverted best_bid/best_ask that would corrupt our book
        if aid != state.yes_token_id:
            continue

        bb = pc.get("best_bid")
        ba = pc.get("best_ask")
        if bb is not None and ba is not None:
            try:
                state.book.best_bid = float(bb)
                state.book.best_ask = float(ba)
                state.book.mid = (state.book.best_bid + state.book.best_ask) / 2
                state.book.spread = state.book.best_ask - state.book.best_bid
                state.book.updated_at = time.time()
                state.book.source = "websocket"
            except (ValueError, TypeError):
                pass

        # Update the specific level in our local book
        if aid == state.yes_token_id:
            price = float(pc["price"])
            size = float(pc["size"])
            side = pc.get("side", "")
            if side == "BUY":
                # Update bid level
                state.book.bids = [
                    (p, s) for p, s in state.book.bids if p != price
                ]
                if size > 0:
                    state.book.bids.append((price, size))
                state.book.bids.sort(key=lambda x: -x[0])
            elif side == "SELL":
                # Update ask level
                state.book.asks = [
                    (p, s) for p, s in state.book.asks if p != price
                ]
                if size > 0:
                    state.book.asks.append((price, size))
                state.book.asks.sort(key=lambda x: x[0])


async def pm_book_ws_loop():
    """Maintain live book via PM WebSocket. Falls back to REST on failure."""
    ws_failures = 0

    while state.running:
        # Wait for token ID
        while state.running and not state.yes_token_id:
            await asyncio.sleep(0.5)
        if not state.running:
            return

        token_id = state.yes_token_id

        # Try WebSocket
        try:
            async with websockets.connect(PM_WS_URL, ping_interval=20, close_timeout=5) as ws:
                sub = json.dumps({
                    "type": "subscribe",
                    "channel": "book",
                    "assets_ids": [token_id],
                })
                await ws.send(sub)
                ws_failures = 0

                async for raw in ws:
                    if not state.running:
                        return
                    # Token changed — need to resubscribe
                    if state.yes_token_id != token_id:
                        break

                    data = json.loads(raw)
                    if isinstance(data, list):
                        parse_snapshot(data)
                    elif isinstance(data, dict):
                        apply_update(data)

        except asyncio.CancelledError:
            return
        except Exception:
            ws_failures += 1
            state.errors["pm_ws"] += 1

        if not state.running:
            return

        # If token changed, loop back to resubscribe
        if state.yes_token_id != token_id:
            continue

        # If WS keeps failing, fall back to REST
        if ws_failures >= 3:
            print("  [WARN] PM WS failed {}x, using REST".format(ws_failures))
            await pm_book_rest_poll()
            return

        await asyncio.sleep(1)


async def pm_book_rest_poll():
    """Fallback: poll PM book via REST every second."""
    async with httpx.AsyncClient(timeout=10) as client:
        while state.running:
            if state.yes_token_id:
                try:
                    resp = await client.get(
                        "{}/book".format(CLOB_URL),
                        params={"token_id": state.yes_token_id},
                    )
                    resp.raise_for_status()
                    raw = resp.json()
                    raw_bids = raw.get("bids", [])
                    raw_asks = raw.get("asks", [])
                    bids = sorted(
                        [(float(b["price"]), float(b["size"])) for b in raw_bids],
                        key=lambda x: -x[0],
                    )
                    asks = sorted(
                        [(float(a["price"]), float(a["size"])) for a in raw_asks],
                        key=lambda x: x[0],
                    )
                    book = state.book
                    book.bids = bids
                    book.asks = asks
                    if bids and asks:
                        book.best_bid = bids[0][0]
                        book.best_ask = asks[0][0]
                        book.mid = (book.best_bid + book.best_ask) / 2
                        book.spread = book.best_ask - book.best_bid
                    book.updated_at = time.time()
                    book.source = "rest_poll"
                except Exception:
                    state.errors["pm_rest"] += 1
            await asyncio.sleep(1.0)


# ── Polymarket REST (discovery + settlement) ──────────────────────
async def fetch_event(client, slug):
    try:
        resp = await client.get(GAMMA_URL, params={"slug": slug, "limit": 1})
        resp.raise_for_status()
        events = resp.json()
        if not events:
            return None, None, None, None, None

        event = events[0]
        meta_raw = event.get("eventMetadata")
        if isinstance(meta_raw, dict):
            meta = meta_raw
        elif isinstance(meta_raw, str) and meta_raw:
            meta = json.loads(meta_raw)
        else:
            meta = {}

        ptb = float(meta["priceToBeat"]) if meta.get("priceToBeat") else None
        fp = float(meta["finalPrice"]) if meta.get("finalPrice") else None

        token_id = None
        no_token = None
        outcome = None
        markets = event.get("markets", [])
        if markets:
            m = markets[0]
            tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(
                m.get("clobTokenIds"), str
            ) else (m.get("clobTokenIds") or [])
            token_id = tokens[0] if tokens else None
            no_token = tokens[1] if len(tokens) > 1 else None
            ops = json.loads(m.get("outcomePrices", "[]"))
            ocs = json.loads(m.get("outcomes", "[]"))
            for o, p in zip(ocs, ops):
                if str(p) == "1":
                    outcome = o

        return token_id, no_token, ptb, fp, outcome
    except Exception:
        state.errors["api"] += 1
        return None, None, None, None, None



# ── Execution ─────────────────────────────────────────────────────
def execute_paper_trade(combo, direction, impulse_bps, time_remaining):
    book = state.book
    if not book.bids or not book.asks:
        return

    # Estimate entry price for Kelly sizing
    est_entry = book.best_ask if direction == "YES" else book.best_bid
    trade_size = combo.half_kelly_size(est_entry)
    if trade_size <= 0:
        return

    if direction == "YES":
        result = walk_book(book.asks, trade_size)
    else:
        result = walk_book(book.bids, trade_size)

    if result is None:
        return

    fill_price, filled_size, levels_hit = result

    # Polymarket taker fee: fee = p * (1-p) * rate
    # PM's feeSchedule.rate = 0.072 for crypto markets (from API)
    # Peaks at ~1.8c/share when p=0.50, drops at extremes
    fee = fill_price * (1 - fill_price) * 0.072

    if direction == "YES":
        # Buying YES: we pay ask + fee
        eff_taker = fill_price + fee
        slippage = fill_price - book.best_ask  # extra cost beyond best ask
    else:
        # Buying NO = selling YES: we receive bid - fee
        eff_taker = fill_price - fee
        slippage = book.best_bid - fill_price  # negative = worse than best bid

    # Maker: post at mid, receive 20% rebate on the taker fee
    maker_rebate = book.mid * (1 - book.mid) * 0.072 * 0.20
    eff_maker = book.mid - maker_rebate if direction == "YES" else book.mid + maker_rebate

    btc = state.binance_price or 0
    delta_bps = 0
    if state.window_open and state.window_open > 0:
        corrected = btc - state.offset
        delta_bps = (corrected - state.window_open) / state.window_open * 10000

    trade = {
        "combo": combo.name,
        "window_start": state.window_start,
        "timestamp": time.time(),
        "time_remaining": round(time_remaining, 1),
        "direction": direction,
        "impulse_bps": round(impulse_bps, 2),
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "mid": round(book.mid, 4),
        "spread": round(book.spread, 4),
        "fill_price": round(fill_price, 4),
        "levels_consumed": levels_hit,
        "slippage": round(slippage, 4),
        "fee": round(fee, 4),
        "effective_entry_taker": round(eff_taker, 4),
        "effective_entry_maker": round(eff_maker, 4),
        "filled_size": round(filled_size, 1),
        "btc_price": round(btc, 2),
        "delta_bps": round(delta_bps, 2),
        "book_source": book.source,
        "book_age_ms": round((time.time() - book.updated_at) * 1000, 0),
        "outcome": None,
        "pnl_taker": None,
        "pnl_maker": None,
        "result": None,
    }

    combo.unsettled.append(trade)

    # Colorized trade output
    if direction == "YES":
        dir_color = G
        dir_label = "BUY UP"
        arrow = "^"
    else:
        dir_color = R
        dir_label = "BUY DOWN"
        arrow = "v"

    print(
        "  {}{} {} [{}]{} @ {}{:.1f}c{} x{:.0f} (${:.0f}) "
        "| {:+.1f}bp/{}s | T-{:.0f}s | slip {:.1f}c fee {:.1f}c".format(
            dir_color, arrow, dir_label, combo.name, RST,
            BOLD, fill_price * 100, RST,
            filled_size, filled_size * fill_price,
            impulse_bps, combo.lookback_s,
            time_remaining,
            abs(slippage) * 100, fee * 100,
        )
    )


# ── Settlement ────────────────────────────────────────────────────
def settle_window(window_start, outcome, final_price=None, silent=False):
    """Settle all combo positions for a window.
    final_price: Chainlink close price (if available) — chains to next priceToBeat.
    silent: if True, don't print (used by background resolver to avoid mid-window noise).
    """
    # Chain finalPrice → next window's priceToBeat
    if final_price is not None:
        state.chained_ptb = final_price

    for combo in state.combos:
        pos = None
        for i, t in enumerate(combo.unsettled):
            if t["window_start"] == window_start:
                pos = combo.unsettled.pop(i)
                break

        if pos is None:
            continue

        size = pos["filled_size"]
        if pos["direction"] == "YES":
            if outcome == "Up":
                pnl_tk = (1.0 - pos["effective_entry_taker"]) * size
                pnl_mk = (1.0 - pos["effective_entry_maker"]) * size
                won = True
            else:
                pnl_tk = -pos["effective_entry_taker"] * size
                pnl_mk = -pos["effective_entry_maker"] * size
                won = False
        else:  # NO
            if outcome == "Down":
                pnl_tk = pos["effective_entry_taker"] * size
                pnl_mk = pos["effective_entry_maker"] * size
                won = True
            else:
                pnl_tk = -(1.0 - pos["effective_entry_taker"]) * size
                pnl_mk = -(1.0 - pos["effective_entry_maker"]) * size
                won = False

        pos["outcome"] = outcome
        pos["pnl_taker"] = round(pnl_tk, 2)
        pos["pnl_maker"] = round(pnl_mk, 2)
        pos["result"] = "WIN" if won else "LOSS"

        combo.trades.append(pos)
        combo.total_pnl_taker += pnl_tk
        combo.total_pnl_maker += pnl_mk
        combo.bankroll += pnl_tk  # update bankroll
        state.csv_buffer.append(pos)

    if silent:
        return

    # Colorized settlement output
    out_color = G if outcome == "Up" else R
    print("\n  {}{}  RESULT: {}  {}{}\n".format(
        out_color, BOLD, outcome.upper(), RST, "-" * 50,
    ))

    for c in state.combos:
        n = len(c.trades)
        wins = sum(1 for t in c.trades if t.get("result") == "WIN")
        wr = wins / n * 100 if n > 0 else 0

        last = c.trades[-1] if c.trades else None
        traded_here = last and last["window_start"] == window_start

        if traded_here:
            pnl = last["pnl_taker"]
            pnl_color = G if pnl > 0 else R
            result_icon = G + "WIN " + RST if last["result"] == "WIN" else R + "LOSS" + RST
            bank_color = G if c.bankroll >= STARTING_BANKROLL else R
            print(
                "  {}{:>12}{}  {} @{:.1f}c x{:.0f}  {}  {}{:+.2f}{}"
                "  {}|{}  Bank: {}${:.0f}{} ({}/{} {:.0f}%)".format(
                    C, c.name, RST,
                    last["direction"], last["fill_price"] * 100, last["filled_size"],
                    result_icon,
                    pnl_color, pnl, RST,
                    DIM, RST,
                    bank_color, c.bankroll, RST,
                    wins, n, wr,
                )
            )
        else:
            bank_color = G if c.bankroll >= STARTING_BANKROLL else R
            if n > 0:
                print(
                    "  {}{:>12}{}  {}{:>30}{}  {}|{}  Bank: {}${:.0f}{} ({}/{} {:.0f}%)".format(
                        C, c.name, RST,
                        DIM, "-- no trade --", RST,
                        DIM, RST,
                        bank_color, c.bankroll, RST,
                        wins, n, wr,
                    )
                )
            else:
                print("  {}{:>12}{}  {}-- no trade --{}  {}|{}  Bank: ${:.0f}".format(
                    C, c.name, RST, DIM, RST, DIM, RST, c.bankroll
                ))
    print()

    # Rolling summary every 10 settled windows
    settled_count = len(state.completed_windows)
    if settled_count > 0 and settled_count % 10 == 0:
        print_rolling_summary()


def print_rolling_summary():
    print("\n  {}{}  SCOREBOARD -- {} windows  {}{}".format(
        M, BOLD, len(state.completed_windows), RST, M + "=" * 50 + RST,
    ))
    print(
        "  {}  {:>12} {:>6} {:>6} {:>10} {:>10} {:>5}{}".format(
            DIM, "Combo", "Trades", "Win%", "PnL(tk)", "PnL(mk)", "Slip", RST,
        )
    )
    for c in state.combos:
        if not c.trades:
            print("  {}{:>12}  -- no trades --{}".format(DIM, c.name, RST))
            continue
        n = len(c.trades)
        wins = sum(1 for t in c.trades if t["result"] == "WIN")
        wr = wins / n * 100
        avg_slip = sum(abs(t["slippage"]) for t in c.trades) / n * 100
        tc = G if c.total_pnl_taker > 0 else R
        mc = G if c.total_pnl_maker > 0 else R
        wc = G if wr >= 55 else Y if wr >= 50 else R
        print(
            "  {}{:>12}{}  {:>5}  {}{:>5.1f}%{}  {}{:>+9.2f}{}  {}{:>+9.2f}{}  {:.1f}c".format(
                C, c.name, RST, n,
                wc, wr, RST,
                tc, c.total_pnl_taker, RST,
                mc, c.total_pnl_maker, RST,
                avg_slip,
            )
        )
    print("  {}{}{}\n".format(M, "=" * 65, RST))


# ── CSV persistence ───────────────────────────────────────────────
def flush_csv():
    if not state.csv_buffer:
        return
    exists = OUTPUT_PATH.exists()
    with open(OUTPUT_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in state.csv_buffer:
            writer.writerow(row)
    state.csv_buffer.clear()


# ── Signal check — called on EVERY new Binance tick (1/sec) ──────
_last_status = [0.0]  # mutable for closure


def check_signals_sync(now_s):
    """Called from Binance WS on every new second. Zero delay."""
    if not state.window_active:
        return
    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < 3 or len(state.book.asks) < 3:
        return
    if state.book.spread > 0.05:
        return
    if len(state.price_buffer) < 5:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < WINDOW_BUFFER_END or time_remaining > (300 - WINDOW_BUFFER_START):
        return

    # Status line every N seconds
    now_t = time.time()
    if now_t - _last_status[0] >= PRINT_STATUS_INTERVAL:
        btc = state.binance_price or 0
        corrected = btc - state.offset
        delta = 0
        if state.window_open and state.window_open > 0:
            delta = (corrected - state.window_open) / state.window_open * 10000

        pm_up = state.book.best_ask * 100
        pm_down = (1 - state.book.best_bid) * 100
        delta_color = G if delta > 0 else R if delta < 0 else RST
        print(
            "  {}{}{} {}T-{:>3.0f}s{} | BTC ${:,.2f} {}{:+.1f}bp{} "
            "| {}Up:{:.0f}c{} {}Down:{:.0f}c{} Spr:{:.0f}c".format(
                DIM, fmt_time(), RST,
                DIM, time_remaining, RST,
                corrected,
                delta_color, delta, RST,
                G, pm_up, RST,
                R, pm_down, RST,
                state.book.spread * 100,
            )
        )
        _last_status[0] = now_t

    # Check each combo — fire instantly when threshold crossed
    price_now = state.binance_price
    if price_now is None:
        return

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        lookback_ts = now_s - combo.lookback_s
        price_ago = find_price_at(state.price_buffer, lookback_ts)
        if price_ago is None or price_ago <= 0:
            continue

        impulse_bps = (price_now - price_ago) / price_ago * 10000

        if abs(impulse_bps) >= combo.btc_threshold_bp:
            direction = "YES" if impulse_bps > 0 else "NO"
            execute_paper_trade(combo, direction, impulse_bps, time_remaining)


# ── Chainlink oracle read ─────────────────────────────────────────
async def read_chainlink_btc():
    """Read BTC/USD from Chainlink on Polygon. ONE call. Returns price as float or None."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(POLYGON_RPC, json={
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [
                    {"to": CHAINLINK_BTC_USD, "data": "0xfeaf968c"},
                    "latest",
                ],
                "id": 1,
            })
            data = resp.json()["result"][2:]
            answer = int(data[64:128], 16)
            return answer / 1e8
    except Exception:
        return None


# ── Helpers for display ───────────────────────────────────────────
def fmt_window_time(ws):
    """Format window_start epoch as 'HH:MM-HH:MM ET' local time."""
    import datetime
    start = datetime.datetime.fromtimestamp(ws)
    end = datetime.datetime.fromtimestamp(ws + 300)
    return "{}-{} ET".format(start.strftime("%I:%M"), end.strftime("%I:%M %p"))




# ── Window manager ────────────────────────────────────────────────
async def window_manager():
    http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "paper-trade/1.0"})

    try:
        while state.running:
            await asyncio.sleep(1)
            now = time.time()

            if state.window_active and state.window_end > 0 and now >= state.window_end:
                state.window_active = False
                prev_ws = state.window_start

                # ── Settle using PM book mid at close ─────────────
                # At window end, PM's book converges to the outcome.
                # mid > 0.50 = Up, mid < 0.50 = Down. This IS PM's verdict.
                pm_close = state.book.mid if state.book.mid > 0 else None
                if pm_close is not None:
                    outcome = "Up" if pm_close > 0.50 else "Down"
                    settle_window(prev_ws, outcome)
                    state.completed_windows.append({
                        "window_start": prev_ws, "outcome": outcome,
                    })
                else:
                    print("  {}[WARN] Can't settle {} — no book data{}".format(Y, prev_ws, RST))
                    state.pending_windows.append(prev_ws)

                if not state.running:
                    break

                # ── Setup next window ─────────────────────────
                now_i = int(time.time())
                state.window_start = now_i - (now_i % 300)
                state.window_end = state.window_start + 300
                slug = "btc-updown-5m-{}".format(state.window_start)

                tid, no_tid, ptb, _, _ = await fetch_event(http, slug)
                if tid:
                    old_token = state.yes_token_id
                    state.yes_token_id = tid
                    state.no_token_id = no_tid
                    if old_token != tid:
                        state.book = BookState()

                # ── Get priceToBeat ───────────────────────────
                # Priority: 1) PM API  2) Chainlink oracle  3) PM chain
                if ptb:
                    state.window_open = ptb
                    state.window_open_source = "pm"
                else:
                    # Read Chainlink on-chain — ONE call, the real price
                    cl_price = await read_chainlink_btc()
                    if cl_price:
                        state.window_open = cl_price
                        state.window_open_source = "chainlink"
                    elif state.chained_ptb:
                        state.window_open = state.chained_ptb
                        state.window_open_source = "chain"
                    else:
                        state.window_open = None
                        state.window_open_source = "?"

                if state.window_open and state.binance_price:
                    state.offset = state.binance_price - state.window_open

                # ── Print window header ───────────────────────
                time_range = fmt_window_time(state.window_start)
                if state.window_open:
                    ptb_str = "{}${:,.2f}{}".format(W, state.window_open, RST)
                else:
                    ptb_str = "{}pending — polling PM...{}".format(Y, RST)

                print(
                    "\n{}{}{}\n  {}WINDOW{}  {}{}{}  "
                    "Price To Beat: {}\n{}{}{}".format(
                        Y, "=" * 70, RST,
                        BOLD, RST,
                        M, time_range, RST,
                        ptb_str,
                        Y, "=" * 70, RST,
                    )
                )

                # Buffer
                elapsed = time.time() - state.window_start
                if elapsed < WINDOW_BUFFER_START:
                    await wait(WINDOW_BUFFER_START - elapsed)

                state.window_active = True
                continue

            # Periodic flush
            if time.time() % FLUSH_INTERVAL < 1:
                flush_csv()

    finally:
        await http.aclose()


# ── Background priceToBeat poller ─────────────────────────────────


# ── Background outcome resolver ──────────────────────────────────
async def outcome_resolver():
    """Polls PM every 30s for unresolved windows. PM uses Chainlink for
    settlement — outcomePrices shows '1'/'0' once resolved."""
    http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "paper-trade/1.0"})
    try:
        while state.running:
            await wait(30)
            if not state.running:
                break

            resolved_ws = {w["window_start"] for w in state.completed_windows}
            now = int(time.time())
            unresolved = [
                ws for ws in state.pending_windows
                if ws not in resolved_ws and (now - ws) > 300
            ]

            for ws in unresolved:
                if not state.running:
                    break
                slug = "btc-updown-5m-{}".format(ws)
                _, _, fp, _, outcome = await fetch_event(http, slug)
                if outcome:
                    settle_window(ws, outcome, final_price=fp, silent=True)
                    state.completed_windows.append({"window_start": ws, "outcome": outcome})
                await wait(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        await http.aclose()


# ── Shutdown resolver ─────────────────────────────────────────────
async def resolve_all_pending():
    """On shutdown, try to resolve all remaining pending windows."""
    resolved_ws = {w["window_start"] for w in state.completed_windows}
    unresolved = [ws for ws in state.pending_windows if ws not in resolved_ws]
    if not unresolved:
        return
    print("  Resolving {} pending windows...".format(len(unresolved)))
    resolved = 0
    async with httpx.AsyncClient(timeout=10) as http:
        for ws in unresolved:
            slug = "btc-updown-5m-{}".format(ws)
            _, _, fp, _, outcome = await fetch_event(http, slug)
            if outcome:
                settle_window(ws, outcome, final_price=fp)
                state.completed_windows.append({"window_start": ws, "outcome": outcome})
                resolved += 1
            await asyncio.sleep(0.2)
    print("  Resolved {} / {}".format(resolved, len(unresolved)))


# ── Final analysis ────────────────────────────────────────────────
def run_analysis():
    all_trades = []
    for c in state.combos:
        all_trades.extend(c.trades)

    settled = [t for t in all_trades if t.get("outcome")]
    hours = 0
    if settled:
        hours = (max(t["timestamp"] for t in settled) - min(t["timestamp"] for t in settled)) / 3600

    print("\n\n" + "=" * 70)
    print("FINAL PAPER TRADING RESULTS")
    print("Runtime: {:.1f}h | Windows: {} | Settled: {}".format(
        hours, len(state.completed_windows),
        len({t["window_start"] for t in settled}),
    ))
    print("=" * 70)

    print(
        "\n  {:>12} | {:>6} | {:>5} | {:>10} | {:>10} | {:>5} | {:>5} | {:>6}".format(
            "Combo", "Trades", "Win%", "PnL(tk)", "PnL(mk)", "Slip", "Fee", "Entry"
        )
    )
    print("  " + "-" * 72)

    for c in state.combos:
        trades = [t for t in c.trades if t.get("outcome")]
        if not trades:
            print("  {:>12} |      0 |  N/A |       N/A |       N/A |  N/A | N/A |   N/A".format(c.name))
            continue
        n = len(trades)
        wins = sum(1 for t in trades if t["result"] == "WIN")
        wr = wins / n * 100
        avg_slip = sum(abs(t["slippage"]) for t in trades) / n * 100
        avg_fee = sum(abs(t["fee"]) for t in trades) / n * 100
        avg_entry = sum(t["fill_price"] for t in trades) / n
        print(
            "  {:>12} | {:>6} | {:>4.1f}% | ${:>9.2f} | ${:>9.2f} | {:.1f}c | {:.1f}c | ${:.2f}".format(
                c.name, n, wr, c.total_pnl_taker, c.total_pnl_maker,
                avg_slip, avg_fee, avg_entry,
            )
        )

    # Execution quality
    if settled:
        print("\n=== EXECUTION QUALITY ===")
        print("  Book source: {}".format(settled[-1].get("book_source", "?")))
        ages = [t["book_age_ms"] for t in settled if t.get("book_age_ms") is not None]
        slips = [abs(t["slippage"]) for t in settled]
        fees = [abs(t["fee"]) for t in settled]
        spreads = [t["spread"] for t in settled if t.get("spread")]
        levels = [t["levels_consumed"] for t in settled]

        if ages:
            print("  Avg book age: {:.0f}ms".format(sum(ages) / len(ages)))
        if slips:
            print("  Avg slippage: {:.1f}c".format(sum(slips) / len(slips) * 100))
        if fees:
            print("  Avg taker fee: {:.1f}c".format(sum(fees) / len(fees) * 100))
        if spreads:
            print("  Avg spread: {:.1f}c".format(sum(spreads) / len(spreads) * 100))
        if levels:
            print("  Avg levels consumed: {:.1f}".format(sum(levels) / len(levels)))

    # Timing
    if settled:
        print("\n=== WHEN DO TRADES FIRE? ===")
        buckets = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 300)]
        for lo, hi in buckets:
            bt = [t for t in settled if lo < t["time_remaining"] <= hi]
            if bt:
                w = sum(1 for t in bt if t["result"] == "WIN") / len(bt) * 100
                print("  T-{}-{}s: {} trades ({:.1f}% win)".format(lo, hi, len(bt), w))

    # Comparison to backtest
    print("\n=== PAPER vs BACKTEST ===")
    bt_ref = {
        "A_5bp_30s": 71.8, "B_7bp_30s": 74.7, "C_10bp_30s": 77.9,
        "D_15bp_15s": 81.2, "E_5bp_15s": 68.8, "F_7bp_15s": 72.9,
    }
    print("  {:>12} {:>10} {:>12} {:>12}".format("", "Backtest", "Paper(tk)", "Paper(mk)"))
    for c in state.combos:
        trades = [t for t in c.trades if t.get("outcome")]
        if trades:
            n = len(trades)
            wr = sum(1 for t in trades if t["result"] == "WIN") / n * 100
            avg_tk = c.total_pnl_taker / n
            avg_mk = c.total_pnl_maker / n
            print("  {:>12} {:>9.1f}% {:>9.1f}% ${:.3f} {:>5.1f}% ${:.3f}".format(
                c.name, bt_ref.get(c.name, 0), wr, avg_tk, wr, avg_mk,
            ))

    # Recommendation
    best = None
    for c in state.combos:
        trades = [t for t in c.trades if t.get("outcome")]
        if len(trades) >= 5:
            avg_tk = c.total_pnl_taker / len(trades)
            if best is None or avg_tk > best[1]:
                best = (c, avg_tk, c.total_pnl_maker / len(trades), len(trades))

    print("\n=== RECOMMENDATION ===")
    if best:
        c, avg_tk, avg_mk, n = best
        wr = sum(1 for t in c.trades if t["result"] == "WIN") / n * 100
        daily_proj_tk = avg_tk * n / max(1, hours) * 24
        print("  Best combo: {}".format(c.name))
        print("  Win rate: {:.1f}%".format(wr))
        print("  Taker PnL/trade: ${:.2f}".format(avg_tk))
        print("  Maker PnL/trade: ${:.2f}".format(avg_mk))
        print("  Projected daily (taker): ${:.0f}".format(daily_proj_tk))
        if avg_tk > 0:
            print("\n  -> GO LIVE (taker)")
        elif avg_mk > 0:
            print("\n  -> GO LIVE (maker only)")
        else:
            print("\n  -> STOP — execution costs eat the edge")
    else:
        print("  Not enough trades for recommendation.")

    print("=" * 70)


# ── Entry point ───────────────────────────────────────────────────
async def run():
    print("Paper Trading Bot — Heuristic C")
    print("Combos: {}".format(", ".join(c.name for c in state.combos)))
    print()

    # Connect Binance
    print("Connecting to Binance WebSocket...")
    ws_task = asyncio.create_task(binance_ws_loop())
    for _ in range(100):
        if state.binance_price:
            break
        await asyncio.sleep(0.1)
    if not state.binance_price:
        print("Failed to connect to Binance")
        ws_task.cancel()
        return
    print("  BTC: ${:,.2f}".format(state.binance_price))

    # Discover current window
    now = int(time.time())
    state.window_start = now - (now % 300)
    state.window_end = state.window_start + 300

    async with httpx.AsyncClient(timeout=15) as client:
        slug = "btc-updown-5m-{}".format(state.window_start)
        tid, no_tid, ptb, _, _ = await fetch_event(client, slug)
        if tid:
            state.yes_token_id = tid
            state.no_token_id = no_tid
            print("  Market: {}".format(slug))

    # Get priceToBeat from Chainlink oracle (one call)
    print("Reading Chainlink BTC/USD oracle...")
    cl_price = await read_chainlink_btc()
    if ptb:
        state.window_open = ptb
        state.window_open_source = "pm"
    elif cl_price:
        state.window_open = cl_price
        state.window_open_source = "chainlink"
    else:
        state.window_open = None
        state.window_open_source = "?"

    if state.window_open and state.binance_price:
        state.offset = state.binance_price - state.window_open

    if cl_price:
        print("  Chainlink BTC/USD: {}${:,.2f}{}".format(G, cl_price, RST))

    time_range = fmt_window_time(state.window_start)
    ptb_str = "${:,.2f}".format(state.window_open) if state.window_open else "?"
    print("\n{}{}{}".format(Y, "=" * 70, RST))
    print("  {}WINDOW{}  {}{}{}  Price To Beat: {}{}{}".format(
        BOLD, RST, M, time_range, RST, W, ptb_str, RST,
    ))
    print("{}{}{}".format(Y, "=" * 70, RST))

    # Start PM book WebSocket
    book_task = asyncio.create_task(pm_book_ws_loop())
    # Wait for first book data
    for _ in range(50):
        if state.book.updated_at > 0:
            break
        await asyncio.sleep(0.1)
    if state.book.updated_at > 0:
        print("  PM book: {} | Bid:{:.0f} Ask:{:.0f} Spr:{:.0f}c".format(
            state.book.source,
            state.book.best_bid * 100, state.book.best_ask * 100,
            state.book.spread * 100,
        ))
    else:
        print("  [WARN] No book data yet")

    state.window_active = True

    # Signals fire inline from Binance WS — no separate signal task needed.
    # ws_task (Binance) calls check_signals_sync() on every new tick.
    resolver_task = asyncio.create_task(outcome_resolver())
    window_task = asyncio.create_task(window_manager())

    try:
        await asyncio.gather(ws_task, book_task, window_task)
    except asyncio.CancelledError:
        pass
    finally:
        for t in [resolver_task, book_task, ws_task, window_task]:
            t.cancel()
        for t in [resolver_task, book_task, ws_task]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        print("\n\nShutting down...")
        await resolve_all_pending()
        flush_csv()
        n_trades = sum(len(c.trades) for c in state.combos)
        print("Saved {} trades to {}".format(n_trades, OUTPUT_PATH))
        print("Errors: {}".format(state.errors))
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
