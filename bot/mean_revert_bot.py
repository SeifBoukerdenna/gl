"""
Mean Reversion Bot — Mid-window exit trading for Polymarket 5-min BTC markets.

Unlike paper_trade_v2.py which holds to settlement, this bot:
- Buys depressed tokens (5-15c) when BTC temporarily wicks past the strike
- Sells mid-window when the book recovers (target: +8-15c profit per share)
- Never holds through settlement — exits before the gamma zone

Thesis: PM's 5-min books overreact to temporary BTC wicks. A YES token that dumps
from 40c to 8c on a -$50 BTC wick often recovers to 15-25c within 60-90 seconds as
BTC stabilizes. You profit from the recovery, not the settlement outcome.

Uses same infrastructure: Binance WS, PM WS/REST, same CSV format, same stats.json.
"""

import argparse
import asyncio
import csv
import json
import math
import os
import signal
import sys
import time
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

# Ensure project root is on path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

# Position sizing — SMALLER to reduce slippage on thin books
BASE_TRADE_DOLLARS = 50
MAX_SHARES = 200
STARTING_BANKROLL = 1000

# Entry filters
MR_ENTRY_MAX_PRICE = 0.18    # buy tokens below 18c
MR_ENTRY_MIN_PRICE = 0.03    # don't buy dust below 3c
MR_MIN_DROP_CENTS = 12       # token must have dropped at least 12c from window high
MR_MIN_BOOK_LEVELS = 1
MR_MAX_SPREAD = 0.05

# Exit parameters
MR_TARGET_CENTS = 6          # take profit at +6c (realistic on thin books)
MR_STOP_LOSS_CENTS = 8       # WIDER stop — don't get shaken out by noise
MR_MAX_HOLD_SEC = 120        # max hold time before forced exit
MR_EXIT_BEFORE_SEC = 45      # close all positions 45s before window end
MR_TRAILING_STOP_CENTS = 4   # trailing stop once in profit

# Timing — MUCH more conservative entry
MR_ENTRY_AFTER_SEC = 30      # wait 30s after window open (not 15)
MR_ENTRY_BEFORE_SEC = 90     # don't enter in last 90s (need time for recovery)
MR_COOLDOWN_AFTER_LOSS = 30  # wait 30s after a loss
MR_MAX_ENTRIES_PER_WINDOW = 2 # max 2 entries per window (was unlimited!)
MR_BOUNCE_CONFIRM_TICKS = 8  # price must be rising for 8 ticks (not 3)
MR_BOUNCE_MIN_RECOVERY = 3   # must have recovered 3c from the low before entering

# PM fee
FEE_RATE = 0.072

# Endpoints
GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_URL = "https://clob.polymarket.com"
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
POLYGON_RPC = "https://polygon-mainnet.g.alchemy.com/v2/EcRCiNAXJXzC22dzlU1gc"
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

# Display
G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; C = "\033[36m"
M = "\033[35m"; W = "\033[97m"; DIM = "\033[2m"; BOLD = "\033[1m"; RST = "\033[0m"

INSTANCE = "default"
TRADE_CSV = Path("data/default/mr_trades.csv")
STATS_PATH = Path("data/default/stats.json")

# CSV columns — compatible with paper_trade_v2 dashboard + extra MR fields
TRADE_COLS = [
    "timestamp", "window_start", "architecture", "combo", "direction",
    "impulse_bps", "time_remaining",
    "best_bid", "best_ask", "mid", "spread",
    "fill_price", "levels_consumed", "slippage", "fee",
    "effective_entry_taker", "effective_entry_maker", "filled_size",
    "notional", "total_fee", "total_slippage", "total_cost",
    "btc_price", "delta_bps", "book_source", "book_age_ms",
    "outcome", "pnl_taker", "pnl_maker", "result",
    # MR-specific
    "exit_price", "hold_seconds", "exit_reason",
]


def set_instance(name):
    global INSTANCE, TRADE_CSV, STATS_PATH
    INSTANCE = name
    base = Path("data") / name
    base.mkdir(parents=True, exist_ok=True)
    TRADE_CSV = base / "trades.csv"
    STATS_PATH = base / "stats.json"


def load_config(name):
    config_path = Path("configs") / "{}.json".format(name)
    if not config_path.exists():
        config_path = Path("configs/default.json")
    if not config_path.exists():
        return
    with open(config_path) as f:
        overrides = json.load(f)
    g = globals()
    applied = []
    for key, val in overrides.items():
        if key.startswith("_"):
            continue
        if key in g:
            g[key] = val
            applied.append(key)
    if applied:
        print("Config: {} ({} overrides)".format(config_path, len(applied)))


# ══════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════

@dataclass
class BookState:
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    best_bid: float = 0
    best_ask: float = 0
    mid: float = 0
    spread: float = 0
    updated_at: float = 0
    source: str = "none"


@dataclass
class Position:
    direction: str          # "YES" or "NO"
    entry_price: float      # fill price (YES space)
    entry_cost: float       # actual cost per share
    filled_size: int
    entry_time: float
    entry_fee: float
    btc_at_entry: float
    window_start: float
    # Tracking
    high_since_entry: float = 0  # highest book price since entry
    low_since_entry: float = 1   # lowest book price since entry


class State:
    def __init__(self):
        self.running = True
        self.binance_price = None
        self.binance_ts = 0
        self.price_buffer = deque(maxlen=120)
        self.last_recorded_second = 0
        self.book = BookState()
        self.yes_token_id = None
        self.no_token_id = None
        self.window_start = 0
        self.window_end = 0
        self.window_open = None
        self.window_open_source = "?"
        self.offset = 0
        self.window_active = False
        self.position = None  # single position at a time
        self.completed_trades = []
        self.trade_buffer = []  # for CSV writing
        self.bankroll = STARTING_BANKROLL
        self.total_pnl = 0
        self.wins = 0
        self.losses = 0
        self.errors = {"binance_ws": 0, "pm_ws": 0, "pm_rest": 0, "api": 0}
        # Token price tracking per window
        self.token_price_history = deque(maxlen=300)  # (ts, yes_price)
        self.token_high = 0
        self.token_low = 1


state = State()


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def fmt_time():
    return time.strftime("%H:%M:%S", time.localtime())


def compute_fee(price):
    return price * (1 - price) * FEE_RATE


def walk_book(levels, size):
    if not levels:
        return None
    remaining, total_cost, levels_hit = size, 0, 0
    for price, qty in levels:
        take = min(remaining, qty)
        total_cost += take * price
        remaining -= take
        levels_hit += 1
        if remaining <= 0:
            break
    filled = size - remaining
    if filled < 1:
        return None
    return (total_cost / filled, round(filled), levels_hit)


async def wait(seconds):
    end = time.time() + seconds
    while state.running and time.time() < end:
        await asyncio.sleep(min(0.5, max(0, end - time.time())))


async def fetch_event(client, slug):
    try:
        resp = await client.get(GAMMA_URL, params={"slug": slug})
        data = resp.json()
        if not data:
            return None, None, None, None, None
        mkt = data[0]["markets"][0]
        tokens = json.loads(mkt.get("clobTokenIds", "[]"))
        tid = tokens[0] if tokens else None
        no_tid = tokens[1] if len(tokens) > 1 else None
        meta = json.loads(mkt.get("eventMetadata", "{}") or "{}")
        ptb = float(meta.get("priceToBeat", 0)) or None
        fp = float(meta.get("finalPrice", 0)) or None
        outcomes = mkt.get("outcomes", [])
        prices = json.loads(mkt.get("outcomePrices", "[]") or "[]")
        outcome = None
        if prices and outcomes:
            for o, p in zip(outcomes, prices):
                if float(p) >= 0.99:
                    outcome = o
        return tid, no_tid, ptb, fp, outcome
    except Exception:
        state.errors["api"] += 1
        return None, None, None, None, None


async def read_chainlink_btc():
    try:
        payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [
            {"to": CHAINLINK_BTC_USD, "data": "0xfeaf968c"}, "latest"], "id": 1}
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(POLYGON_RPC, json=payload)
            d = r.json()
            raw = d["result"]
            price_hex = "0x" + raw[66:130]
            return int(price_hex, 16) / 1e8
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# BOOK MANAGEMENT (same as v2)
# ══════════════════════════════════════════════════════════════

def parse_snapshot(data):
    if isinstance(data, list) and data:
        snap = data[0]
    else:
        snap = data
    raw_bids = snap.get("bids", [])
    raw_asks = snap.get("asks", [])
    bids = sorted([(float(b["price"]), float(b["size"])) for b in raw_bids], key=lambda x: -x[0])
    asks = sorted([(float(a["price"]), float(a["size"])) for a in raw_asks], key=lambda x: x[0])
    book = state.book
    book.bids, book.asks = bids, asks
    if bids and asks:
        book.best_bid, book.best_ask = bids[0][0], asks[0][0]
        book.mid = (book.best_bid + book.best_ask) / 2
        book.spread = book.best_ask - book.best_bid
    book.updated_at = time.time()
    book.source = "websocket"


def apply_update(data):
    pcs = data.get("price_changes", [])
    for pc in pcs:
        aid = pc.get("asset_id")
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
        if aid == state.yes_token_id:
            try:
                price = float(pc.get("price", 0))
                size = float(pc.get("size", 0))
                side = pc.get("side", "")
                if side == "BUY" and price > 0:
                    state.book.bids = [(p, s) for p, s in state.book.bids if p != price]
                    if size > 0:
                        state.book.bids.append((price, size))
                    state.book.bids.sort(key=lambda x: -x[0])
                elif side == "SELL" and price > 0:
                    state.book.asks = [(p, s) for p, s in state.book.asks if p != price]
                    if size > 0:
                        state.book.asks.append((price, size))
                    state.book.asks.sort(key=lambda x: x[0])
            except (ValueError, TypeError, KeyError):
                pass


async def pm_book_ws_loop():
    ws_failures = 0
    while state.running:
        while state.running and not state.yes_token_id:
            await asyncio.sleep(0.5)
        token_id = state.yes_token_id
        try:
            async with websockets.connect(PM_WS_URL, ping_interval=30) as ws:
                ws_failures = 0
                sub = json.dumps({"type": "subscribe", "channel": "book",
                                  "assets_ids": [token_id]})
                await ws.send(sub)
                async for raw in ws:
                    if not state.running:
                        break
                    if state.yes_token_id != token_id:
                        break
                    data = json.loads(raw)
                    if isinstance(data, list):
                        parse_snapshot(data)
                    elif isinstance(data, dict):
                        apply_update(data)
        except Exception:
            ws_failures += 1
            state.errors["pm_ws"] += 1
        if not state.running:
            return
        if state.yes_token_id != token_id:
            continue
        if ws_failures >= 3:
            for _ in range(120):
                if not state.running:
                    return
                await asyncio.sleep(0.5)
            ws_failures = 0
            continue
        await asyncio.sleep(1)


async def pm_book_rest_poll():
    async with httpx.AsyncClient(timeout=10) as client:
        while state.running:
            if state.yes_token_id:
                try:
                    resp = await client.get("{}/book".format(CLOB_URL),
                                           params={"token_id": state.yes_token_id})
                    resp.raise_for_status()
                    raw = resp.json()
                    bids = sorted([(float(b["price"]), float(b["size"])) for b in raw.get("bids", [])],
                                  key=lambda x: -x[0])
                    asks = sorted([(float(a["price"]), float(a["size"])) for a in raw.get("asks", [])],
                                  key=lambda x: x[0])
                    book = state.book
                    book.bids, book.asks = bids, asks
                    if bids and asks:
                        book.best_bid, book.best_ask = bids[0][0], asks[0][0]
                        book.mid = (book.best_bid + book.best_ask) / 2
                        book.spread = book.best_ask - book.best_bid
                    book.updated_at = time.time()
                except Exception:
                    state.errors["pm_rest"] += 1
            await asyncio.sleep(0.5)


# ══════════════════════════════════════════════════════════════
# TRADING LOGIC
# ══════════════════════════════════════════════════════════════

_entries_this_window = [0]

def check_entry(now_s):
    """Smart mean reversion entry v2 — confirmed bounce required, no knife-catching."""
    if not state.window_active or state.position is not None:
        return
    if _entries_this_window[0] >= MR_MAX_ENTRIES_PER_WINDOW:
        return
    if not state.book.bids or not state.book.asks:
        return
    if state.book.spread > MR_MAX_SPREAD:
        return

    time_remaining = state.window_end - time.time()
    time_elapsed = 300 - time_remaining

    if time_elapsed < MR_ENTRY_AFTER_SEC or time_remaining < MR_ENTRY_BEFORE_SEC:
        return

    # Cooldown after a loss
    if state.completed_trades:
        last = state.completed_trades[-1]
        if last.get("result") == "LOSS" and time.time() - last.get("timestamp", 0) < MR_COOLDOWN_AFTER_LOSS:
            return

    # Need enough price history for bounce detection
    if len(state.token_price_history) < MR_BOUNCE_CONFIRM_TICKS + 3:
        return

    yes_price = state.book.best_ask
    no_price = 1.0 - state.book.best_bid

    # Check YES side
    if MR_ENTRY_MIN_PRICE <= yes_price <= MR_ENTRY_MAX_PRICE:
        drop = state.token_high - yes_price
        if drop >= MR_MIN_DROP_CENTS / 100:
            if _confirm_bounce_yes():
                _execute_entry("YES", yes_price)
                _entries_this_window[0] += 1
                return

    # Check NO side
    if MR_ENTRY_MIN_PRICE <= no_price <= MR_ENTRY_MAX_PRICE:
        drop = (1.0 - state.token_low) - no_price
        if drop >= MR_MIN_DROP_CENTS / 100:
            if _confirm_bounce_no():
                _execute_entry("NO", state.book.best_bid)
                _entries_this_window[0] += 1
                return


def _confirm_bounce_yes():
    """Confirm YES price is genuinely recovering, not just a tick blip."""
    history = list(state.token_price_history)
    if len(history) < MR_BOUNCE_CONFIRM_TICKS:
        return False

    recent = [p for _, p in history[-MR_BOUNCE_CONFIRM_TICKS:]]
    current = recent[-1]

    # 1. Find the recent low in the last 30 ticks
    extended = [p for _, p in history[-30:]] if len(history) >= 30 else [p for _, p in history]
    recent_low = min(extended)

    # 2. Current price must be at least MR_BOUNCE_MIN_RECOVERY cents above the low
    recovery = (current - recent_low) * 100
    if recovery < MR_BOUNCE_MIN_RECOVERY:
        return False

    # 3. Price must be trending UP over the confirmation window (not just one tick)
    first_half = sum(recent[:len(recent)//2]) / (len(recent)//2)
    second_half = sum(recent[len(recent)//2:]) / (len(recent) - len(recent)//2)
    if second_half <= first_half:
        return False  # second half not higher than first half — not a real bounce

    # 4. BTC must not be accelerating against us
    if state.binance_price and state.window_open and state.window_open > 0:
        delta = (state.binance_price - state.window_open) / state.window_open * 10000
        if delta < -8:  # BTC crashing hard — don't buy YES
            return False

    return True


def _confirm_bounce_no():
    """Confirm NO price is recovering (YES price is falling from a high)."""
    history = list(state.token_price_history)
    if len(history) < MR_BOUNCE_CONFIRM_TICKS:
        return False

    recent = [p for _, p in history[-MR_BOUNCE_CONFIRM_TICKS:]]
    current = recent[-1]

    # For NO bounce: YES price should be FALLING (meaning NO is recovering)
    # Recent high of YES
    extended = [p for _, p in history[-30:]] if len(history) >= 30 else [p for _, p in history]
    recent_high = max(extended)

    # YES must have dropped MR_BOUNCE_MIN_RECOVERY from its high
    drop_from_high = (recent_high - current) * 100
    if drop_from_high < MR_BOUNCE_MIN_RECOVERY:
        return False

    # YES must be trending DOWN (meaning NO is recovering)
    first_half = sum(recent[:len(recent)//2]) / (len(recent)//2)
    second_half = sum(recent[len(recent)//2:]) / (len(recent) - len(recent)//2)
    if second_half >= first_half:
        return False  # YES not falling — NO not recovering

    # BTC not pumping hard
    if state.binance_price and state.window_open and state.window_open > 0:
        delta = (state.binance_price - state.window_open) / state.window_open * 10000
        if delta > 8:
            return False

    return True


def _execute_entry(direction, entry_price):
    """Open a position."""
    book = state.book

    shares = min(int(BASE_TRADE_DOLLARS / entry_price), MAX_SHARES)
    if shares < 1:
        return

    if direction == "YES":
        result = walk_book(book.asks, shares)
    else:
        result = walk_book(book.bids, shares)

    if result is None:
        return

    fill_price, filled, levels = result
    fee = compute_fee(fill_price)

    if direction == "YES":
        cost = fill_price + fee
    else:
        cost = (1.0 - fill_price) + fee  # NO cost

    state.position = Position(
        direction=direction,
        entry_price=fill_price,
        entry_cost=cost,
        filled_size=filled,
        entry_time=time.time(),
        entry_fee=fee,
        btc_at_entry=state.binance_price or 0,
        window_start=state.window_start,
        high_since_entry=fill_price if direction == "YES" else (1.0 - fill_price),
        low_since_entry=fill_price if direction == "YES" else (1.0 - fill_price),
    )

    token_price = fill_price if direction == "YES" else (1.0 - fill_price)
    dc = G if direction == "YES" else R
    print("  {}^ BUY {} @ {:.1f}c x{} (${:.0f}) | T-{:.0f}s | dropped {:.0f}c from high{}".format(
        dc, direction, token_price * 100, filled, cost * filled,
        state.window_end - time.time(),
        (state.token_high - fill_price) * 100 if direction == "YES" else 0,
        RST))


def check_exit(now_s):
    """Check if we should close the position."""
    pos = state.position
    if pos is None:
        return

    if not state.book.bids or not state.book.asks:
        return

    time_remaining = state.window_end - time.time()
    hold_time = time.time() - pos.entry_time

    # Current token price
    if pos.direction == "YES":
        current_price = state.book.best_bid  # what we'd get selling
        pos.high_since_entry = max(pos.high_since_entry, current_price)
        pos.low_since_entry = min(pos.low_since_entry, current_price)
    else:
        current_price = 1.0 - state.book.best_ask  # NO sell price
        pos.high_since_entry = max(pos.high_since_entry, current_price)
        pos.low_since_entry = min(pos.low_since_entry, current_price)

    entry_token_price = pos.entry_price if pos.direction == "YES" else (1.0 - pos.entry_price)
    unrealized = (current_price - entry_token_price) * pos.filled_size
    change_cents = (current_price - entry_token_price) * 100

    # Exit reasons (priority order)
    exit_reason = None

    # 1. Forced exit before settlement
    if time_remaining <= MR_EXIT_BEFORE_SEC:
        exit_reason = "time_settlement"

    # 2. Max hold time exceeded
    elif hold_time >= MR_MAX_HOLD_SEC:
        exit_reason = "time_max_hold"

    # 3. Target profit hit
    elif change_cents >= MR_TARGET_CENTS:
        exit_reason = "target_profit"

    # 4. Stop loss hit
    elif change_cents <= -MR_STOP_LOSS_CENTS:
        exit_reason = "stop_loss"

    # 5. Trailing stop
    elif (pos.high_since_entry - current_price) * 100 >= MR_TRAILING_STOP_CENTS and change_cents > 0:
        exit_reason = "trailing_stop"

    if exit_reason:
        _execute_exit(exit_reason, current_price)


def _execute_exit(reason, current_price):
    """Close the position."""
    pos = state.position
    if pos is None:
        return

    book = state.book

    # Walk opposite side to exit
    if pos.direction == "YES":
        result = walk_book(book.bids, pos.filled_size)
    else:
        result = walk_book(book.asks, pos.filled_size)

    if result is None:
        # Can't exit — try again next tick
        return

    exit_price, filled_exit, levels = result
    exit_fee = compute_fee(exit_price)

    # PnL calculation
    if pos.direction == "YES":
        # Bought YES at entry, selling YES at exit
        pnl = (exit_price - pos.entry_price - pos.entry_fee - exit_fee) * filled_exit
    else:
        # Bought NO: paid (1-entry), selling NO: receive (1-exit)
        pnl = (pos.entry_price - exit_price - pos.entry_fee - exit_fee) * filled_exit

    pnl = round(pnl, 2)
    hold_sec = round(time.time() - pos.entry_time, 1)
    won = pnl > 0

    state.total_pnl += pnl
    state.bankroll += pnl
    if won:
        state.wins += 1
    else:
        state.losses += 1

    entry_token = pos.entry_price if pos.direction == "YES" else (1.0 - pos.entry_price)
    exit_token = exit_price if pos.direction == "YES" else (1.0 - exit_price)

    # Log trade — v2-compatible format so dashboard can read it
    total_fees = round((pos.entry_fee + exit_fee) * filled_exit, 2)
    trade = {
        "timestamp": time.time(),
        "window_start": pos.window_start,
        "architecture": "mean_revert",
        "combo": "MR_{}c_{}s".format(MR_TARGET_CENTS, MR_MAX_HOLD_SEC),
        "direction": pos.direction,
        "impulse_bps": round((exit_token - entry_token) * 100, 1),  # price change in cents
        "time_remaining": round(state.window_end - time.time(), 1),
        "best_bid": state.book.best_bid,
        "best_ask": state.book.best_ask,
        "mid": round(state.book.mid, 4),
        "spread": round(state.book.spread, 4),
        "fill_price": round(entry_token, 4),  # entry token price
        "levels_consumed": 0,
        "slippage": 0,
        "fee": round(pos.entry_fee + exit_fee, 4),
        "effective_entry_taker": round(entry_token + pos.entry_fee, 4),
        "effective_entry_maker": round(entry_token, 4),
        "filled_size": filled_exit,
        "notional": round(entry_token * filled_exit, 2),
        "total_fee": total_fees,
        "total_slippage": 0,
        "total_cost": round(entry_token * filled_exit + total_fees, 2),
        "btc_price": round(state.binance_price or 0, 2),
        "delta_bps": 0,
        "book_source": state.book.source,
        "book_age_ms": 0,
        "outcome": reason,  # exit reason as "outcome"
        "pnl_taker": pnl,
        "pnl_maker": pnl,
        "result": "WIN" if pnl > 0 else "LOSS",
        # MR-specific
        "exit_price": round(exit_token, 4),
        "hold_seconds": hold_sec,
        "exit_reason": reason,
    }
    state.trade_buffer.append(trade)
    state.completed_trades.append(trade)

    # Print
    pc = G if pnl > 0 else R
    rc = G if won else R
    print("  {}  EXIT {} @ {:.1f}c -> {:.1f}c | {} | ${:+.0f} | {:.0f}s hold | bank ${:.0f}{}".format(
        pc, pos.direction, entry_token * 100, exit_token * 100,
        reason, pnl, hold_sec, state.bankroll, RST))

    state.position = None


# ══════════════════════════════════════════════════════════════
# PERSISTENCE
# ══════════════════════════════════════════════════════════════

def flush_trades():
    if not state.trade_buffer:
        return
    rows = state.trade_buffer[:]
    state.trade_buffer.clear()
    exists = TRADE_CSV.exists()
    try:
        fd, tmp = tempfile.mkstemp(suffix=".csv", dir=TRADE_CSV.parent)
        with open(fd, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_COLS)
            if not exists:
                w.writeheader()
            w.writerows(rows)
        with open(tmp, "r") as src, open(TRADE_CSV, "a") as dst:
            dst.write(src.read())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    print("  Saved {} trades to {}".format(len(rows), TRADE_CSV))


def write_stats():
    n = state.wins + state.losses
    stats = {
        "instance": INSTANCE,
        "architecture": "mean_revert",
        "updated": time.time(),
        "trades": n,
        "wins": state.wins,
        "losses": state.losses,
        "wr": round(state.wins / n * 100, 1) if n > 0 else 0,
        "pnl_taker": round(state.total_pnl, 2),
        "pnl_maker": 0,
        "bankroll": round(state.bankroll, 2),
        "windows_settled": 0,
        "current_window": state.window_start,
        "window_end": state.window_end,
        "window_open": state.window_open,
        "window_active": state.window_active,
        "btc_price": state.binance_price,
        "book_bid": state.book.best_bid,
        "book_ask": state.book.best_ask,
        "book_spread": state.book.spread,
        "book_source": state.book.source,
        "position_open": state.position is not None,
        "errors": state.errors,
        "combos": {"MR_{}c_{}s".format(MR_TARGET_CENTS, MR_MAX_HOLD_SEC): {
            "trades": n, "wins": state.wins, "wr": round(state.wins / n * 100, 1) if n > 0 else 0,
            "pnl_taker": round(state.total_pnl, 2), "bankroll": round(state.bankroll, 2),
        }},
        "recent_trades": [t for t in state.completed_trades[-20:]],
    }
    tmp = str(STATS_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(stats, f)
    os.replace(tmp, str(STATS_PATH))


# ══════════════════════════════════════════════════════════════
# MAIN LOOPS
# ══════════════════════════════════════════════════════════════

_last_status = [0.0]


async def binance_ws_loop():
    while state.running:
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=30) as ws:
                state.errors["binance_ws"] = 0
                async for raw in ws:
                    if not state.running:
                        break
                    data = json.loads(raw)
                    price = float(data["p"])
                    ts_ms = int(data["T"])
                    state.binance_price = price
                    state.binance_ts = ts_ms

                    current_s = ts_ms // 1000
                    if current_s != state.last_recorded_second:
                        state.price_buffer.append((current_s, price))
                        state.last_recorded_second = current_s

                        # Track token prices
                        if state.book.mid > 0:
                            state.token_price_history.append((current_s, state.book.best_ask))
                            state.token_high = max(state.token_high, state.book.best_ask)
                            state.token_low = min(state.token_low, state.book.best_bid)

                        # Trading logic — every tick
                        check_entry(current_s)
                        check_exit(current_s)

                        # Status line
                        now_t = time.time()
                        if now_t - _last_status[0] >= 10 and state.window_active:
                            time_remaining = state.window_end - time.time()
                            pos_str = ""
                            if state.position:
                                p = state.position
                                tp = p.entry_price if p.direction == "YES" else (1.0 - p.entry_price)
                                cp = state.book.best_bid if p.direction == "YES" else (1.0 - state.book.best_ask)
                                ur = (cp - tp) * p.filled_size
                                pos_str = " | {}POS {} {:.0f}c->{:.0f}c ${:+.0f}{}".format(
                                    G if ur > 0 else R, p.direction, tp*100, cp*100, ur, RST)
                            print("  {}{}{}T-{:>3.0f}s{} | YES:{:.0f}c NO:{:.0f}c hi:{:.0f}c lo:{:.0f}c{}".format(
                                DIM, fmt_time(), RST, time_remaining, RST,
                                state.book.best_ask * 100, (1-state.book.best_bid)*100,
                                state.token_high * 100, state.token_low * 100,
                                pos_str))
                            _last_status[0] = now_t

                        # Periodic flush
                        if time.time() % 60 < 1:
                            flush_trades()
                        # Write stats every 3s for real-time dashboard
                        if time.time() % 3 < 1:
                            write_stats()

        except asyncio.CancelledError:
            return
        except Exception:
            state.errors["binance_ws"] += 1
            state.price_buffer.clear()
        if not state.running:
            break
        await asyncio.sleep(2)


async def window_manager():
    http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "mean-revert-bot/1.0"})
    try:
        while state.running:
            await asyncio.sleep(1)
            now = time.time()

            if state.window_active and state.window_end > 0 and now >= state.window_end:
                state.window_active = False

                # Force close any open position
                if state.position:
                    if state.book.bids and state.book.asks:
                        cp = state.book.best_bid if state.position.direction == "YES" else (1.0 - state.book.best_ask)
                        _execute_exit("window_end", cp)
                    else:
                        # Can't exit — treat as total loss
                        state.position = None

                # Setup next window
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

                # Price to beat
                if ptb:
                    state.window_open = ptb
                    state.window_open_source = "pm"
                else:
                    cl = await read_chainlink_btc()
                    state.window_open = cl or state.binance_price
                    state.window_open_source = "chainlink" if cl else "binance"

                if state.window_open and state.binance_price:
                    state.offset = state.binance_price - state.window_open

                # Reset per-window tracking
                state.token_price_history.clear()
                state.token_high = state.book.best_ask if state.book.best_ask > 0 else 0.5
                state.token_low = state.book.best_bid if state.book.best_bid > 0 else 0.5
                _entries_this_window[0] = 0

                ptb_str = "${:,.2f}".format(state.window_open) if state.window_open else "?"
                t0 = time.localtime(state.window_start)
                t1 = time.localtime(state.window_end)
                print("\n{}{}{}".format(Y, "=" * 60, RST))
                print("  {}MR WINDOW{} {}-{} ET  PTB: {}".format(
                    BOLD, RST,
                    time.strftime("%I:%M", t0), time.strftime("%I:%M %p", t1),
                    ptb_str))
                print("{}{}{}".format(Y, "=" * 60, RST))

                elapsed = time.time() - state.window_start
                if elapsed < 3:
                    await wait(3 - elapsed)

                state.window_active = True
                write_stats()  # immediately push new window
                continue

            if time.time() % 30 < 1:
                write_stats()

    except asyncio.CancelledError:
        pass
    finally:
        await http.aclose()


async def run():
    print("{}Mean Reversion Bot — Mid-Window Exit Trading{}".format(BOLD, RST))
    print("Architecture: mean_revert")
    print("Entry: {:.0f}-{:.0f}c tokens, drop >={:.0f}c from high".format(
        MR_ENTRY_MIN_PRICE * 100, MR_ENTRY_MAX_PRICE * 100, MR_MIN_DROP_CENTS))
    print("Exit: target +{:.0f}c, stop -{:.0f}c, max hold {}s, trailing {}c".format(
        MR_TARGET_CENTS, MR_STOP_LOSS_CENTS, MR_MAX_HOLD_SEC, MR_TRAILING_STOP_CENTS))
    print("Sizing: ${}/trade, max {} shares, bank ${:.0f}".format(
        BASE_TRADE_DOLLARS, MAX_SHARES, STARTING_BANKROLL))

    # Initial window setup
    now_i = int(time.time())
    state.window_start = now_i - (now_i % 300)
    state.window_end = state.window_start + 300

    print("\nConnecting to Binance WebSocket...")
    ws_task = asyncio.create_task(binance_ws_loop())

    for _ in range(100):
        if state.binance_price:
            break
        await asyncio.sleep(0.1)
    print("  BTC: ${:,.2f}".format(state.binance_price or 0))

    http = httpx.AsyncClient(timeout=10)
    slug = "btc-updown-5m-{}".format(state.window_start)
    tid, no_tid, ptb, _, _ = await fetch_event(http, slug)
    if tid:
        state.yes_token_id = tid
        state.no_token_id = no_tid
    await http.aclose()

    if ptb:
        state.window_open = ptb
    else:
        cl = await read_chainlink_btc()
        state.window_open = cl or state.binance_price

    if state.window_open and state.binance_price:
        state.offset = state.binance_price - state.window_open

    book_task = asyncio.create_task(pm_book_ws_loop())
    rest_task = asyncio.create_task(pm_book_rest_poll())

    for _ in range(50):
        if state.book.updated_at > 0:
            break
        await asyncio.sleep(0.1)
    if state.book.updated_at > 0:
        print("  PM book: {} | Bid:{:.0f} Ask:{:.0f} Spr:{:.0f}c".format(
            state.book.source, state.book.best_bid * 100, state.book.best_ask * 100,
            state.book.spread * 100))

    state.token_high = state.book.best_ask if state.book.best_ask > 0 else 0.5
    state.token_low = state.book.best_bid if state.book.best_bid > 0 else 0.5
    state.window_active = True

    window_task = asyncio.create_task(window_manager())

    try:
        await asyncio.gather(ws_task, book_task, rest_task, window_task)
    except asyncio.CancelledError:
        pass
    finally:
        for t in [ws_task, book_task, rest_task, window_task]:
            t.cancel()
        for t in [ws_task, book_task, rest_task]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # Final flush
    if state.position:
        print("\n  [WARN] Closing open position on shutdown")
        if state.book.bids and state.book.asks:
            cp = state.book.best_bid if state.position.direction == "YES" else (1.0 - state.book.best_ask)
            _execute_exit("shutdown", cp)

    flush_trades()
    write_stats()

    n = state.wins + state.losses
    print("\n{}MEAN REVERT RESULTS{}".format(BOLD, RST))
    print("  Trades: {} | Wins: {} | Losses: {} | WR: {:.1f}%".format(
        n, state.wins, state.losses, state.wins / n * 100 if n > 0 else 0))
    print("  PnL: ${:+,.0f} | Bank: ${:,.0f}".format(state.total_pnl, state.bankroll))


def main():
    parser = argparse.ArgumentParser(description="Mean Reversion Bot")
    parser.add_argument("--instance", default="default")
    args = parser.parse_args()

    load_config(args.instance)
    if args.instance != "default":
        set_instance(args.instance)
        print("Instance: {}".format(args.instance))

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
