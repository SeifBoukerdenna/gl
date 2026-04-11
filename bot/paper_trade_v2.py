"""
Paper Trading Bot v2 — Heuristic C with filters from 22h of live data.
Fixes: flat sizing, impulse cap, entry filters, dead zone, cooldown, skip tracking.

Usage: caffeinate -i python -u paper_trade_v2.py
       Ctrl+C to stop and see full analysis.
"""

import asyncio
import csv
import json
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# Ensure the project root is on sys.path (for 'bot.architectures' imports)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import httpx
import websockets

# ══════════════════════════════════════════════════════════════════
# CONFIG — all tunable parameters in one place
# ══════════════════════════════════════════════════════════════════

# Position sizing (flat dollar, no Kelly)
BASE_TRADE_DOLLARS = 100
MAX_SHARES = 500
MAX_RISK_PCT = 0.10
MIN_SHARES = 1
STARTING_BANKROLL = 1000

# Entry filters
MIN_ENTRY_PRICE = 0.20    # wider with flat sizing — max loss is $80 not $4760
MAX_ENTRY_PRICE = 0.80    # let the strategy trade, filters were too tight
DOWN_MIN_ENTRY = 0.25     # slight floor for DOWN
MAX_IMPULSE_BP = 25       # 30+bp: 0% win -$4,672. 20-30bp: 77% win, keep it
MAX_SPREAD = 0.03
MAX_SLIPPAGE = 0.05         # reject fills > 5c worse than best price
MAX_BOOK_AGE_MS = 500       # reject signals when book data is older than 500ms
ONE_TRADE_PER_WINDOW = True # only one trade per architecture per window (prevents self-competition)
ALLOWED_DIRECTIONS = None   # None = both, or ["YES"] or ["NO"] — direction filter
MIN_BOOK_AGE_MS = 0         # minimum book age (ms) — 0 = disabled, >0 = require PM book to be at least this stale
CHOP_GATE = False           # if True, block trades when regime is choppy (>60% alternation in last 10 windows)
CHOP_FILTER = True          # if True, block trades when BTC crossed strike both ways in current window
LOSS_COOLDOWN = True        # if True, pause 10min after 2 consecutive all-loss windows
EDGE_TABLE_FILTER = False   # if True, check edge table before trading (block low-edge setups)
SIGNAL_CHECK_MS = 200       # signal check interval in ms (200=default, 50=fast)
_edge_table = None          # loaded at startup if EDGE_TABLE_FILTER is True


def _load_edge_table():
    """Load the V2 hierarchical edge table from data/edge_table.json."""
    global _edge_table
    table_path = Path("data/edge_table.json")
    if not table_path.exists():
        print("  [EDGE TABLE] Not found at {}".format(table_path))
        return
    import json as _json
    with open(table_path) as f:
        data = _json.load(f)
    _edge_table = data
    version = data.get("version", 1)
    if version >= 2:
        n1 = len(data.get("L1", {}))
        n2 = len(data.get("L2", {}))
        n3 = len(data.get("L3", {}))
        print("  [EDGE TABLE] V2 loaded: L1={} L2={} L3={} cells".format(n1, n2, n3))
    else:
        n = len(data.get("cells", {}))
        print("  [EDGE TABLE] V1 loaded: {} cells".format(n))


def _edge_table_score(direction, entry_price, time_remaining):
    """Score a trade using the hierarchical edge table.
    Returns a sizing multiplier: 0.0 (block), 0.5 (weak), 1.0 (normal), 1.5 (strong).
    Returns 1.0 if no table loaded."""
    if not _edge_table or not state.window_open or state.window_open <= 0:
        return 1.0
    btc = state.binance_price
    if btc is None:
        return 1.0

    btc_corrected = btc - state.offset
    strike = state.window_open
    delta_bps = (btc_corrected - strike) / strike * 10000

    # Buckets
    abs_d = abs(delta_bps)
    if abs_d >= 12:
        d_bucket = "far_above" if delta_bps > 0 else "far_below"
    elif abs_d >= 5:
        d_bucket = "above" if delta_bps > 0 else "below"
    elif abs_d >= 2:
        d_bucket = "near_above" if delta_bps > 0 else "near_below"
    else:
        d_bucket = "at_strike"

    if time_remaining > 240: t_bucket = "240-300"
    elif time_remaining > 180: t_bucket = "180-240"
    elif time_remaining > 120: t_bucket = "120-180"
    elif time_remaining > 60: t_bucket = "60-120"
    else: t_bucket = "0-60"

    # Momentum
    mom_type = "flat"
    if len(state.price_buffer) >= 5:
        price_30ago = None
        target_ts = time.time() - 30
        for ts, px in state.price_buffer:
            if ts <= target_ts:
                price_30ago = px  # keep LATEST tick still ≥ 30s old
            else:
                break
        if price_30ago and price_30ago > 0:
            mom_bps = (btc - price_30ago) / price_30ago * 10000
            if delta_bps > 0:
                mom_type = "away" if mom_bps > 0.5 else "toward" if mom_bps < -0.5 else "flat"
            else:
                mom_type = "away" if mom_bps < -0.5 else "toward" if mom_bps > 0.5 else "flat"

    side = "above" if delta_bps > 0 else "below"

    # Hierarchical lookup: most specific level with >= 50 samples
    version = _edge_table.get("version", 1)
    if version < 2:
        # V1 fallback
        cells = _edge_table.get("cells", {})
        key = "{}|{}".format(d_bucket, t_bucket)
        cell = cells.get(key)
        if not cell or cell.get("count", 0) < 30:
            return 1.0
        wr = cell.get("ci_lower", 0.5)
    else:
        # V2: cascade L3 → L2 → L1
        k1 = "{}|{}".format(d_bucket, t_bucket)
        k2 = "{}|{}".format(k1, mom_type)
        k3 = "{}|{}".format(k2, side)

        L1 = _edge_table.get("L1", {})
        L2 = _edge_table.get("L2", {})
        L3 = _edge_table.get("L3", {})

        cell = None
        if k3 in L3 and L3[k3].get("count", 0) >= 50:
            cell = L3[k3]
        elif k2 in L2 and L2[k2].get("count", 0) >= 50:
            cell = L2[k2]
        elif k1 in L1 and L1[k1].get("count", 0) >= 50:
            cell = L1[k1]

        if cell is None:
            return 1.0
        wr = cell.get("wr", 0.5)

    # Table WR is for betting WITH the delta. Adjust for actual direction.
    table_direction = "YES" if delta_bps > 0 else "NO"
    if direction != table_direction:
        wr = 1.0 - wr

    # Breakeven
    fee = entry_price * (1 - entry_price) * 0.072
    if direction == "YES":
        breakeven = entry_price + fee
    else:
        breakeven = 1.0 - (entry_price - fee)

    # Edge = how much WR exceeds breakeven
    edge = wr - breakeven

    # Convert edge to sizing multiplier
    if edge >= 0.08:
        return 1.0   # normal edge: standard size
    elif edge >= 0.02:
        return 0.7   # weak edge: reduce size
    elif edge >= 0.0:
        return 0.5   # marginal: half size
    else:
        return 0.0   # negative edge: block
STALE_BOOK_SIZING = False   # dynamic sizing: scale up when PM book is stale (more lag = more edge)
MIN_BOOK_LEVELS = 3

# Time filters (from 1,274-trade paper data)
DEAD_ZONE_START = 90      # T-90-210s: 33-58% win, -$12,534 combined PnL
DEAD_ZONE_END = 210
WINDOW_BUFFER_START = 3
WINDOW_BUFFER_END = 5

# Cooldown
COOLDOWN_RANGE_BP = 50    # only trigger on extreme crashes, not normal vol
COOLDOWN_DURATION = 120   # 2 minutes, not full window

# Settlement
SETTLE_DELAY_INITIAL = 15
SETTLE_RETRY_DELAY = 5
SETTLE_MAX_RETRIES = 5

# Display
PRINT_STATUS_INTERVAL = 15
MAX_SKIP_PRINTS = 3
FLUSH_INTERVAL = 60

# Endpoints
GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_URL = "https://clob.polymarket.com"
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
POLYGON_RPC = "https://polygon-mainnet.g.alchemy.com/v2/EcRCiNAXJXzC22dzlU1gc"
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

# Output — each instance ALWAYS gets its own directory
INSTANCE = "default"
TRADE_CSV = Path("data/default/trades.csv")
SKIP_CSV = Path("data/default/skips.csv")


def set_instance(name):
    """Set instance name and update all file paths. Each instance gets its own dir."""
    global INSTANCE, TRADE_CSV, SKIP_CSV
    INSTANCE = name
    base = Path("data") / name
    base.mkdir(parents=True, exist_ok=True)
    TRADE_CSV = base / "trades.csv"
    SKIP_CSV = base / "skips.csv"
    out = Path("output") / name
    out.mkdir(parents=True, exist_ok=True)


def load_config(name):
    """Load config overrides from configs/{name}.json. Missing keys use defaults."""
    global _ARCH_SPEC
    config_path = Path("configs") / "{}.json".format(name)
    if not config_path.exists():
        config_path = Path("configs/default.json")
    if not config_path.exists():
        return

    import json as _json
    with open(config_path) as f:
        overrides = _json.load(f)

    g = globals()

    # Load architecture FIRST (determines combo params and extra globals)
    arch_name = overrides.pop("ARCHITECTURE", g.get("ARCHITECTURE", "impulse_lag"))
    g["ARCHITECTURE"] = arch_name

    from bot.architectures import load_architecture
    spec = load_architecture(arch_name)
    g["_ARCH_SPEC"] = spec

    # Merge architecture's extra_globals as defaults
    for key, val in (spec.get("extra_globals") or {}).items():
        if key not in g:
            g[key] = val

    # Apply config overrides (these take priority over architecture defaults)
    applied = []
    for key, val in overrides.items():
        if key.startswith("_"):
            continue
        if key in g:
            g[key] = val
            applied.append(key)

    # Load edge table if enabled
    if g.get("EDGE_TABLE_FILTER"):
        _load_edge_table()

    if applied:
        print("Config: {} ({} overrides, arch={})".format(config_path, len(applied), arch_name))
    else:
        print("Config: {} (arch={})".format(config_path, arch_name))

TRADE_COLS = [
    "timestamp", "window_start", "architecture", "combo", "direction", "impulse_bps",
    "time_remaining", "best_bid", "best_ask", "mid", "spread",
    "fill_price", "levels_consumed", "slippage", "fee",
    "effective_entry_taker", "effective_entry_maker", "filled_size",
    "notional", "total_fee", "total_slippage", "total_cost",
    "btc_price", "delta_bps", "book_source", "book_age_ms",
    "outcome", "pnl_taker", "pnl_maker", "result",
]
SKIP_COLS = [
    "timestamp", "window_start", "combo", "direction", "entry_price",
    "impulse_bps", "time_remaining", "reason", "outcome", "would_have_won",
]

COMBO_PARAMS = [
    {"name": "A_5bp_30s", "btc_threshold_bp": 5, "lookback_s": 30},
    {"name": "B_7bp_30s", "btc_threshold_bp": 7, "lookback_s": 30},
    {"name": "C_10bp_30s", "btc_threshold_bp": 10, "lookback_s": 30},
    {"name": "D_15bp_15s", "btc_threshold_bp": 15, "lookback_s": 15},
    {"name": "E_5bp_15s", "btc_threshold_bp": 5, "lookback_s": 15},
    {"name": "F_7bp_15s", "btc_threshold_bp": 7, "lookback_s": 15},
    {"name": "G_3bp_10s", "btc_threshold_bp": 3, "lookback_s": 10},
    {"name": "H_5bp_10s", "btc_threshold_bp": 5, "lookback_s": 10},
    {"name": "I_10bp_15s", "btc_threshold_bp": 10, "lookback_s": 15},
    {"name": "J_7bp_45s", "btc_threshold_bp": 7, "lookback_s": 45},
    {"name": "K_5bp_60s", "btc_threshold_bp": 5, "lookback_s": 60},
    {"name": "L_10bp_60s", "btc_threshold_bp": 10, "lookback_s": 60},
]

# If set in config, only these combos will be active (list of names)
# e.g. ["A_5bp_30s", "B_7bp_30s", "E_5bp_15s"]
ENABLED_COMBOS = None  # None = all combos active

# Architecture system — each session declares which strategy it uses
ARCHITECTURE = "impulse_lag"  # default, overridden by config
_ARCH_SPEC = None  # loaded at startup


# ══════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════

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
class Combo:
    name: str
    btc_threshold_bp: float
    lookback_s: int
    unsettled: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    total_pnl_taker: float = 0
    total_pnl_maker: float = 0
    bankroll: float = field(default_factory=lambda: STARTING_BANKROLL)
    tracking_only: bool = False
    restored_trade_count: int = 0  # trades restored from CSV (not in trades list)
    restored_win_count: int = 0

    @property
    def total_trades(self):
        return len(self.trades) + self.restored_trade_count

    def compute_position_size(self, entry_price, override_dollars=None):
        """Flat dollar sizing with hard caps. No Kelly."""
        if entry_price <= 0.01 or entry_price >= 0.99:
            return 0
        dollars = override_dollars if override_dollars is not None else BASE_TRADE_DOLLARS
        shares_by_dollars = dollars / entry_price
        shares_by_risk = (self.bankroll * MAX_RISK_PCT) / entry_price
        shares = min(shares_by_dollars, shares_by_risk, MAX_SHARES)
        return max(MIN_SHARES, round(shares)) if shares >= MIN_SHARES else 0

    def has_position_in_window(self, ws):
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
        self.price_buffer = deque(maxlen=120)
        self.last_recorded_second = 0
        # PM book
        self.book = BookState()
        self.yes_token_id = None
        self.no_token_id = None
        # Window
        self.window_start = 0
        self.window_end = 0
        self.window_open = None
        self.window_open_source = "?"
        self.offset = 35.0
        self.window_active = False
        self.pending_windows = []
        self.completed_windows = []
        self.chained_ptb = None
        # Cooldown
        self.cooldown_until = 0
        self.window_price_high = None
        self.window_price_low = None
        self.window_crossed_above = False  # BTC went above strike this window
        self.window_crossed_below = False  # BTC went below strike this window
        self.consecutive_all_loss_windows = 0
        # Skip tracking
        self.pending_skips = []
        self.skip_buffer = []
        self.skip_prints_this_window = 0
        self.recent_outcomes = deque(maxlen=10)  # last 10 window outcomes for regime detection
        self.architecture = ARCHITECTURE
        # Combos — use architecture combo_params if loaded, else default
        params = _ARCH_SPEC["combo_params"] if _ARCH_SPEC else COMBO_PARAMS
        self.combos = [Combo(**{k: v for k, v in p.items()
                                if k in ("name", "btc_threshold_bp", "lookback_s")})
                       for p in params]
        # Persistence
        self.trade_csv_buffer = []
        # Errors
        self.errors = {"binance_ws": 0, "pm_ws": 0, "pm_rest": 0, "api": 0}


state = State()


# ══════════════════════════════════════════════════════════════════
# ANSI COLORS
# ══════════════════════════════════════════════════════════════════
G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; C = "\033[36m"
M = "\033[35m"; W = "\033[97m"; DIM = "\033[2m"; BOLD = "\033[1m"; RST = "\033[0m"


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════
def fmt_time():
    return time.strftime("%H:%M:%S", time.localtime())


async def wait(seconds):
    end = time.time() + seconds
    while state.running and time.time() < end:
        await asyncio.sleep(min(0.5, max(0, end - time.time())))


def find_price_at(buf, target_s):
    best, best_dist = None, float("inf")
    for ts, px in buf:
        d = abs(ts - target_s)
        if d < best_dist:
            best_dist = d
            best = px
    return best if best_dist <= 2 else None


def walk_book(levels, size):
    remaining, total_cost, levels_hit = size, 0, 0
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


def log_skip(combo_name, direction, entry_price, impulse_bps, time_remaining, reason):
    """Record a filtered signal — skip CSV logging to prevent disk/memory bloat."""
    # Only print first few skips per window, don't buffer to CSV
    if state.skip_prints_this_window < MAX_SKIP_PRINTS:
        print("  {}[skip] {} {} @{:.0f}c | {}{}".format(
            DIM, combo_name, direction, (entry_price or 0) * 100, reason, RST))
        state.skip_prints_this_window += 1
    elif state.skip_prints_this_window == MAX_SKIP_PRINTS:
        print("  {}[skip] ... more filtered{}".format(DIM, RST))
        state.skip_prints_this_window += 1


def fmt_window_time(ws):
    import datetime
    start = datetime.datetime.fromtimestamp(ws)
    end = datetime.datetime.fromtimestamp(ws + 300)
    return "{}-{} ET".format(start.strftime("%I:%M"), end.strftime("%I:%M %p"))


# ══════════════════════════════════════════════════════════════════
# BINANCE WEBSOCKET
# ══════════════════════════════════════════════════════════════════
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

                    # Raw tick hook — fires on EVERY Binance message (for volume counting)
                    if _ARCH_SPEC and _ARCH_SPEC.get("on_raw_tick"):
                        _ARCH_SPEC["on_raw_tick"](state, price, ts_ms)

                    # Track window high/low for cooldown
                    if state.window_active:
                        if state.window_price_high is None or price > state.window_price_high:
                            state.window_price_high = price
                        if state.window_price_low is None or price < state.window_price_low:
                            state.window_price_low = price
                        # Track if BTC crossed above/below strike (for chop detection)
                        # Only count crosses that are >3bp from strike (ignore noise around strike)
                        if state.window_open and state.offset and state.window_open > 0:
                            corrected = price - state.offset
                            cross_margin = state.window_open * 0.0003  # 3bp
                            if corrected >= state.window_open + cross_margin:
                                state.window_crossed_above = True
                            if corrected <= state.window_open - cross_margin:
                                state.window_crossed_below = True

                    current_s = ts_ms // 1000
                    # Record price buffer once per second (for lookback calculations)
                    if current_s != state.last_recorded_second:
                        state.price_buffer.append((current_s, price))
                        state.last_recorded_second = current_s
                        # on_tick hooks only need once per second (vol tracking etc)
                        if _ARCH_SPEC and _ARCH_SPEC.get("on_tick"):
                            _ARCH_SPEC["on_tick"](state, price, current_s)

                    # Signal check interval (configurable: 200ms default, 50ms fast)
                    if ts_ms - getattr(state, '_last_signal_check_ms', 0) >= SIGNAL_CHECK_MS:
                        state._last_signal_check_ms = ts_ms
                        if _ARCH_SPEC:
                            _ARCH_SPEC["check_signals"](state, current_s)
                        else:
                            check_signals_sync(current_s)
        except asyncio.CancelledError:
            return
        except Exception:
            state.errors["binance_ws"] += 1
            if state.running:
                print("  [WARN] Binance WS reconnecting...")
                state.price_buffer.clear()
                await asyncio.sleep(2)


# ══════════════════════════════════════════════════════════════════
# POLYMARKET BOOK (WebSocket + REST fallback)
# ══════════════════════════════════════════════════════════════════
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
        # Also update depth from WS price_changes (complements REST poll)
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
        if not state.running:
            return
        token_id = state.yes_token_id
        try:
            async with websockets.connect(PM_WS_URL, ping_interval=20, close_timeout=5) as ws:
                await ws.send(json.dumps({"type": "subscribe", "channel": "book", "assets_ids": [token_id]}))
                ws_failures = 0
                async for raw in ws:
                    if not state.running:
                        return
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
        if state.yes_token_id != token_id:
            continue
        if ws_failures >= 3:
            print("  [WARN] PM WS failed {}x, REST fallback for 60s then retry".format(ws_failures))
            for _ in range(120):  # 60 seconds of REST polling
                if not state.running:
                    return
                await asyncio.sleep(0.5)
            ws_failures = 0  # Reset and retry WS
            continue
        await asyncio.sleep(1)


async def pm_book_rest_poll():
    """Always-running REST poll for full order book depth.
    This is the authoritative source for book.bids/asks depth levels.
    The WS feed updates best_bid/best_ask faster but doesn't maintain full depth."""
    async with httpx.AsyncClient(timeout=10) as client:
        while state.running:
            if state.yes_token_id:
                try:
                    resp = await client.get("{}/book".format(CLOB_URL), params={"token_id": state.yes_token_id})
                    resp.raise_for_status()
                    raw = resp.json()
                    bids = sorted([(float(b["price"]), float(b["size"])) for b in raw.get("bids", [])], key=lambda x: -x[0])
                    asks = sorted([(float(a["price"]), float(a["size"])) for a in raw.get("asks", [])], key=lambda x: x[0])
                    book = state.book
                    book.bids, book.asks = bids, asks
                    if bids and asks:
                        book.best_bid, book.best_ask = bids[0][0], asks[0][0]
                        book.mid = (book.best_bid + book.best_ask) / 2
                        book.spread = book.best_ask - book.best_bid
                    book.updated_at = time.time()
                except Exception as e:
                    state.errors["pm_rest"] += 1
                    if state.errors["pm_rest"] <= 3:
                        print("  [REST] Error: {}".format(e))
            await asyncio.sleep(0.5)  # poll every 500ms for fresh depth


# ══════════════════════════════════════════════════════════════════
# POLYMARKET REST (discovery + settlement)
# ══════════════════════════════════════════════════════════════════
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
        token_id = no_token = outcome = None
        markets = event.get("markets", [])
        if markets:
            m = markets[0]
            tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else (m.get("clobTokenIds") or [])
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


async def read_chainlink_btc():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(POLYGON_RPC, json={
                "jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": CHAINLINK_BTC_USD, "data": "0xfeaf968c"}, "latest"],
                "id": 1,
            })
            data = resp.json()["result"][2:]
            return int(data[64:128], 16) / 1e8
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# EXECUTION
# ══════════════════════════════════════════════════════════════════
def execute_paper_trade(combo, direction, impulse_bps, time_remaining, entry_price, override_dollars=None):
    book = state.book

    def _reject(reason):
        """Log rejected trade (only prints, no file I/O, no disk cost)."""
        print("  {}[REJECT] {} {} @{:.0f}c T-{:.0f}s — {}{}".format(
            DIM, combo.name, direction, entry_price * 100, time_remaining, reason, RST))

    # Direction filter — skip disallowed directions
    if ALLOWED_DIRECTIONS and direction not in ALLOWED_DIRECTIONS:
        _reject("direction {} blocked".format(direction))
        return

    # Chop gate — block momentum trades in real chop (>70% alternation, not just mixed)
    if CHOP_GATE and hasattr(state, 'recent_outcomes') and len(state.recent_outcomes) >= 5:
        outcomes = list(state.recent_outcomes)
        alts = sum(1 for i in range(1, len(outcomes)) if outcomes[i] != outcomes[i - 1])
        if alts / (len(outcomes) - 1) > 0.70:
            _reject("chop gate {:.0f}% alt".format(alts / (len(outcomes) - 1) * 100))
            return

    # Edge table scoring — continuous sizing multiplier from historical data
    if EDGE_TABLE_FILTER:
        _et_score = _edge_table_score(direction, entry_price, time_remaining)
        if _et_score <= 0.0:
            _reject("edge table score {:.2f}".format(_et_score))
            return
        if override_dollars is not None:
            override_dollars = int(override_dollars * _et_score)
        else:
            override_dollars = int(BASE_TRADE_DOLLARS * _et_score) if _et_score != 1.0 else None

    # Check book depth is available
    if not book.bids and not book.asks:
        _reject("no book depth")
        return

    # Book freshness gate — reject stale book data
    book_age_ms = (time.time() - book.updated_at) * 1000
    if book_age_ms > MAX_BOOK_AGE_MS:
        _reject("book too stale {:.0f}ms > {:.0f}ms".format(book_age_ms, MAX_BOOK_AGE_MS))
        return

    # Book staleness gate — only trade when PM hasn't fully repriced yet
    if MIN_BOOK_AGE_MS > 0 and book_age_ms < MIN_BOOK_AGE_MS:
        _reject("book too fresh {:.0f}ms < {:.0f}ms".format(book_age_ms, MIN_BOOK_AGE_MS))
        return

    # Chop filter — if BTC crossed the strike both ways (by 3bp+) AND we're past
    # the first 2 minutes, the window is choppy. Skip directional bets.
    # Early crosses are OK — BTC might still trend after initial noise.
    if CHOP_FILTER and state.window_crossed_above and state.window_crossed_below:
        elapsed = time.time() - state.window_start if state.window_start else 0
        if elapsed > 120:  # only block after 2 minutes into window
            _reject("chop filter (crossed both ways)")
            return

    # One trade per architecture per window — prevents self-competition on the same book
    if ONE_TRADE_PER_WINDOW and state.window_start:
        for c in state.combos:
            if c is not combo and c.has_position_in_window(state.window_start):
                _reject("one-trade-per-window ({} already traded)".format(c.name))
                return

    # NO trades use the YES bid as `entry_price`, but the actual cost per share
    # for a NO contract is (1 - YES_bid). Pass the correct cost-per-share into
    # the sizer so $200 of intended notional doesn't become $400-500 of actual
    # notional (a long-standing engine bug fixed 2026-04-10).
    cost_per_share = (1.0 - entry_price) if direction == "NO" else entry_price
    trade_size = combo.compute_position_size(cost_per_share, override_dollars)
    if trade_size <= 0:
        _reject("position size 0 (entry={:.2f} cost/sh={:.2f} dollars={})".format(
            entry_price, cost_per_share, override_dollars))
        return

    # Stale Book Sizing: scale position based on how stale PM's book is
    # Stale book = PM hasn't repriced yet = more lag edge = size up
    # Fresh book = PM already repriced = less edge = size down
    if STALE_BOOK_SIZING:
        if book_age_ms >= 300:
            trade_size = int(trade_size * 2.0)  # stale: double down
        elif book_age_ms >= 150:
            trade_size = int(trade_size * 1.5)  # moderate: 1.5x
        elif book_age_ms < 80:
            trade_size = int(trade_size * 0.5)  # fresh: half size
        trade_size = min(trade_size, MAX_SHARES)
        if trade_size <= 0:
            return

    if direction == "YES":
        result = walk_book(book.asks, trade_size)
    else:
        result = walk_book(book.bids, trade_size)

    if result is None:
        return

    fill_price, filled_size, levels_hit = result

    fee = fill_price * (1 - fill_price) * 0.072
    if direction == "YES":
        eff_taker = fill_price + fee
        slippage = fill_price - book.best_ask
    else:
        eff_taker = fill_price - fee
        slippage = book.best_bid - fill_price

    if abs(slippage) > MAX_SLIPPAGE:
        return

    maker_rebate = book.mid * (1 - book.mid) * 0.072 * 0.20
    eff_maker = book.mid - maker_rebate if direction == "YES" else book.mid + maker_rebate

    btc = state.binance_price or 0
    delta_bps = 0
    if state.window_open and state.window_open > 0:
        delta_bps = ((btc - state.offset) - state.window_open) / state.window_open * 10000

    # Total cost breakdown — use correct side (NO cost = 1-fill_price)
    if direction == "YES":
        notional = round(fill_price * filled_size, 2)
    else:
        notional = round((1.0 - fill_price) * filled_size, 2)
    total_fee = round(fee * filled_size, 2)              # total fee in dollars
    total_slip = round(abs(slippage) * filled_size, 2)   # total slippage cost in dollars
    total_cost = round(notional + total_fee + total_slip, 2)  # everything you spend

    trade = {
        "architecture": ARCHITECTURE,
        "combo": combo.name, "window_start": state.window_start,
        "timestamp": time.time(), "time_remaining": round(time_remaining, 1),
        "direction": direction, "impulse_bps": round(impulse_bps, 2),
        "best_bid": book.best_bid, "best_ask": book.best_ask,
        "mid": round(book.mid, 4), "spread": round(book.spread, 4),
        "fill_price": round(fill_price, 4), "levels_consumed": levels_hit,
        "slippage": round(slippage, 4), "fee": round(fee, 4),
        "effective_entry_taker": round(eff_taker, 4),
        "effective_entry_maker": round(eff_maker, 4),
        "filled_size": round(filled_size, 1),
        "notional": notional, "total_fee": total_fee,
        "total_slippage": total_slip, "total_cost": total_cost,
        "btc_price": round(btc, 2), "delta_bps": round(delta_bps, 2),
        "book_source": book.source,
        "book_age_ms": round((time.time() - book.updated_at) * 1000, 0),
        "outcome": None, "pnl_taker": None, "pnl_maker": None, "result": None,
    }
    combo.unsettled.append(trade)

    dc = G if direction == "YES" else R
    arrow = "^" if direction == "YES" else "v"
    dl = "BUY UP" if direction == "YES" else "BUY DOWN"
    print("  {}{} {} [{}]{} @ {}{:.1f}c{} x{:.0f} | cost ${:.0f} (fill ${:.0f} + fee ${:.1f} + slip ${:.1f}) "
          "| {:+.1f}bp/{}s | T-{:.0f}s".format(
              dc, arrow, dl, combo.name, RST,
              BOLD, fill_price * 100, RST, filled_size,
              total_cost, notional, total_fee, total_slip,
              impulse_bps, combo.lookback_s, time_remaining))


# ══════════════════════════════════════════════════════════════════
# SETTLEMENT
# ══════════════════════════════════════════════════════════════════
def settle_window(window_start, outcome, final_price=None, silent=False):
    if final_price is not None:
        state.chained_ptb = final_price

    # Track outcome for regime detection (chop vs trend)
    state.recent_outcomes.append(outcome)

    # Resolve pending skips for this window
    for skip in list(state.pending_skips):
        if skip["window_start"] == window_start and skip["outcome"] is None:
            skip["outcome"] = outcome
            skip["would_have_won"] = (
                (skip["direction"] == "YES" and outcome == "Up") or
                (skip["direction"] == "NO" and outcome == "Down")
            )
            state.skip_buffer.append(skip)
            state.pending_skips.remove(skip)

    traded_this_window = []
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
        else:
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
        # Cap trades list to prevent memory leak — keep last 50, move rest to counters
        if len(combo.trades) > 50:
            old = combo.trades.pop(0)
            combo.restored_trade_count += 1
            if old.get("result") == "WIN":
                combo.restored_win_count += 1
        combo.total_pnl_taker += pnl_tk
        combo.total_pnl_maker += pnl_mk
        if not combo.tracking_only:
            combo.bankroll += pnl_tk
        state.trade_csv_buffer.append(pos)
        traded_this_window.append((combo, pos))

    # Check cooldown triggers
    if state.window_price_high and state.window_price_low and state.window_open:
        rng = (state.window_price_high - state.window_price_low) / state.window_open * 10000
        if rng > COOLDOWN_RANGE_BP:
            state.cooldown_until = time.time() + COOLDOWN_DURATION
            if not silent:
                print("  {}[COOLDOWN] Range {:.0f}bp — skipping {}s{}".format(Y, rng, COOLDOWN_DURATION, RST))

    if LOSS_COOLDOWN and traded_this_window and all(p["result"] == "LOSS" for _, p in traded_this_window):
        state.consecutive_all_loss_windows += 1
        if state.consecutive_all_loss_windows >= 2:
            state.cooldown_until = time.time() + 600
            if not silent:
                print("  {}[COOLDOWN] {} all-loss windows — pausing 10min{}".format(
                    Y, state.consecutive_all_loss_windows, RST))
    else:
        state.consecutive_all_loss_windows = 0

    if silent:
        return

    # Print settlement
    oc = G if outcome == "Up" else R
    # Count filtered signals for this window
    window_skips = [s for s in state.skip_buffer if s["window_start"] == window_start]
    skip_wins = sum(1 for s in window_skips if s.get("would_have_won"))
    skip_losses = len(window_skips) - skip_wins

    print("\n  {}{}  RESULT: {}  {}{}\n".format(oc, BOLD, outcome.upper(), RST, "-" * 50))
    for c in state.combos:
        n = c.total_trades
        wins = sum(1 for t in c.trades if t.get("result") == "WIN") + c.restored_win_count
        wr = wins / n * 100 if n > 0 else 0
        last = c.trades[-1] if c.trades else None
        here = last and last["window_start"] == window_start
        if here:
            pnl = last["pnl_taker"]
            pc = G if pnl > 0 else R
            ri = G + "WIN " + RST if last["result"] == "WIN" else R + "LOSS" + RST
            bc = G if c.bankroll >= STARTING_BANKROLL else R
            print("  {}{:>12}{}  {} @{:.1f}c x{:.0f}  {}  {}{:+.2f}{}  {}|{}  Bank: {}${:.0f}{} ({}/{} {:.0f}%)".format(
                C, c.name, RST, last["direction"], last["fill_price"] * 100, last["filled_size"],
                ri, pc, pnl, RST, DIM, RST, bc, c.bankroll, RST, wins, n, wr))
        elif n > 0:
            bc = G if c.bankroll >= STARTING_BANKROLL else R
            print("  {}{:>12}{}  {}-- no trade --{}  {}|{}  Bank: {}${:.0f}{} ({}/{} {:.0f}%)".format(
                C, c.name, RST, DIM, RST, DIM, RST, bc, c.bankroll, RST, wins, n, wr))
        else:
            print("  {}{:>12}{}  {}-- no trade --{}  {}|{}  Bank: ${:.0f}".format(
                C, c.name, RST, DIM, RST, DIM, RST, c.bankroll))

    if window_skips:
        print("  {}Filtered: {} signals ({} would-win, {} would-lose){}".format(
            DIM, len(window_skips), skip_wins, skip_losses, RST))
    print()

    if len(state.completed_windows) > 0 and len(state.completed_windows) % 10 == 0:
        print_rolling_summary()


def print_rolling_summary():
    print("\n  {}{}  SCOREBOARD -- {} windows  {}{}".format(
        M, BOLD, len(state.completed_windows), RST, M + "=" * 50 + RST))
    print("  {}  {:>12} {:>6} {:>6} {:>10} {:>10} {:>5}{}".format(
        DIM, "Combo", "Trades", "Win%", "PnL(tk)", "PnL(mk)", "Slip", RST))
    for c in state.combos:
        if not c.trades:
            print("  {}{:>12}  -- no trades --{}".format(DIM, c.name, RST))
            continue
        n = c.total_trades
        wins = sum(1 for t in c.trades if t["result"] == "WIN")
        wr = wins / n * 100
        avg_slip = sum(abs(t["slippage"]) for t in c.trades) / n * 100
        tc = G if c.total_pnl_taker > 0 else R
        mc = G if c.total_pnl_maker > 0 else R
        wc = G if wr >= 55 else Y if wr >= 50 else R
        print("  {}{:>12}{}  {:>5}  {}{:>5.1f}%{}  {}{:>+9.2f}{}  {}{:>+9.2f}{}  {:.1f}c".format(
            C, c.name, RST, n, wc, wr, RST, tc, c.total_pnl_taker, RST, mc, c.total_pnl_maker, RST, avg_slip))
    print("  {}{}{}\n".format(M, "=" * 65, RST))


# ══════════════════════════════════════════════════════════════════
# CSV PERSISTENCE — atomic writes with temp file to prevent corruption
# ══════════════════════════════════════════════════════════════════
import tempfile
import shutil


def _append_csv(path, fieldnames, rows):
    """Append rows to CSV atomically. No file locking needed — write to temp then append."""
    if not rows:
        return
    exists = path.exists()
    # Write to temp file first
    fd, tmp = tempfile.mkstemp(suffix=".csv", dir=path.parent)
    try:
        with open(fd, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerows(rows)
        # Append temp content to main file
        with open(tmp, "r") as src, open(path, "a") as dst:
            dst.write(src.read())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def flush_csvs():
    if state.trade_csv_buffer:
        _append_csv(TRADE_CSV, TRADE_COLS, state.trade_csv_buffer)
        state.trade_csv_buffer.clear()
    if state.skip_buffer:
        _append_csv(SKIP_CSV, SKIP_COLS, state.skip_buffer)
        state.skip_buffer.clear()

    # Also write a live stats JSON for the dashboard to read without parsing CSV
    write_session_stats()


def write_session_stats():
    """Write a rich stats.json for the dashboard. Updated every flush (~60s)."""
    import json as _json
    stats_path = TRADE_CSV.parent / "stats.json"

    combos = {}
    for c in state.combos:
        n = c.total_trades
        wins = sum(1 for t in c.trades if t.get("result") == "WIN") + c.restored_win_count
        combos[c.name] = {
            "trades": n, "wins": wins,
            "wr": round(wins / n * 100, 1) if n > 0 else 0,
            "pnl_taker": round(c.total_pnl_taker, 2),
            "pnl_maker": round(c.total_pnl_maker, 2),
            "bankroll": round(c.bankroll, 2),
        }

    total_trades = sum(v["trades"] for v in combos.values())
    total_wins = sum(v["wins"] for v in combos.values())

    # Recent trades (last 20)
    all_trades = []
    for c in state.combos:
        all_trades.extend(c.trades)
    all_trades.sort(key=lambda t: t.get("timestamp", 0))
    recent = []
    for t in all_trades[-20:]:
        recent.append({
            "combo": t.get("combo"), "direction": t.get("direction"),
            "fill_price": t.get("fill_price"), "filled_size": t.get("filled_size"),
            "impulse_bps": t.get("impulse_bps"), "time_remaining": t.get("time_remaining"),
            "result": t.get("result"), "pnl_taker": t.get("pnl_taker"),
            "timestamp": t.get("timestamp"), "outcome": t.get("outcome"),
            "window_start": t.get("window_start"),
            "notional": t.get("notional"), "total_fee": t.get("total_fee"),
            "total_slippage": t.get("total_slippage"), "total_cost": t.get("total_cost"),
            "fee": t.get("fee"), "slippage": t.get("slippage"),
        })

    # Window history (last 20)
    window_history = []
    for w in state.completed_windows[-20:]:
        ws = w.get("window_start", 0)
        window_history.append({
            "window_start": ws, "outcome": w.get("outcome"),
        })

    stats = {
        "instance": INSTANCE,
        "architecture": ARCHITECTURE,
        "updated": time.time(),
        "started": state.completed_windows[0]["window_start"] if state.completed_windows else 0,
        "trades": total_trades,
        "wins": total_wins,
        "wr": round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0,
        "pnl_taker": round(sum(v["pnl_taker"] for v in combos.values()), 2),
        "pnl_maker": round(sum(v["pnl_maker"] for v in combos.values()), 2),
        "windows_settled": len(state.completed_windows),
        "current_window": state.window_start,
        "window_open": state.window_open,
        "window_open_source": state.window_open_source,
        "window_end": state.window_end,
        "window_active": state.window_active,
        "btc_price": state.binance_price,
        "book_bid": state.book.best_bid,
        "book_ask": state.book.best_ask,
        "book_spread": state.book.spread,
        "book_source": state.book.source,
        "cooldown_active": time.time() < state.cooldown_until,
        "errors": state.errors,
        "combos": combos,
        "recent_trades": recent,
        "recent_windows": window_history,
    }

    tmp = str(stats_path) + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(stats, f)
    os.replace(tmp, stats_path)


# ══════════════════════════════════════════════════════════════════
# SIGNAL CHECK — called on every Binance tick (1/sec)
# ══════════════════════════════════════════════════════════════════
_last_status = [0.0]


def check_signals_sync(now_s):
    if not state.window_active:
        return

    # F5: Cooldown check
    if time.time() < state.cooldown_until:
        return

    if not state.book.bids or not state.book.asks:
        return
    if len(state.book.bids) < MIN_BOOK_LEVELS or len(state.book.asks) < MIN_BOOK_LEVELS:
        return

    # F6: Spread check
    if state.book.spread > MAX_SPREAD:
        return

    if len(state.price_buffer) < 5:
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < WINDOW_BUFFER_END or time_remaining > (300 - WINDOW_BUFFER_START):
        return

    # Status line
    now_t = time.time()
    if now_t - _last_status[0] >= PRINT_STATUS_INTERVAL:
        btc = state.binance_price or 0
        corrected = btc - state.offset
        delta = 0
        if state.window_open and state.window_open > 0:
            delta = (corrected - state.window_open) / state.window_open * 10000
        pm_up = state.book.best_ask * 100
        pm_down = (1 - state.book.best_bid) * 100
        dc = G if delta > 0 else R if delta < 0 else RST
        print("  {}{}{} {}T-{:>3.0f}s{} | BTC ${:,.2f} {}{:+.1f}bp{} | {}Up:{:.0f}c{} {}Down:{:.0f}c{} Spr:{:.0f}c".format(
            DIM, fmt_time(), RST, DIM, time_remaining, RST, corrected, dc, delta, RST,
            G, pm_up, RST, R, pm_down, RST, state.book.spread * 100))
        _last_status[0] = now_t

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
        if abs(impulse_bps) < combo.btc_threshold_bp:
            continue

        direction = "YES" if impulse_bps > 0 else "NO"
        entry_price = state.book.best_ask if direction == "YES" else state.book.best_bid

        # F1: Impulse cap
        if abs(impulse_bps) > MAX_IMPULSE_BP:
            log_skip(combo.name, direction, entry_price, impulse_bps, time_remaining,
                     "impulse {:.0f}bp > {}bp".format(abs(impulse_bps), MAX_IMPULSE_BP))
            continue

        # F2: Entry price range (YES price — configs specify YES price bounds)
        if entry_price < MIN_ENTRY_PRICE or entry_price > MAX_ENTRY_PRICE:
            log_skip(combo.name, direction, entry_price, impulse_bps, time_remaining,
                     "YES price {:.0f}c outside {:.0f}-{:.0f}c".format(
                         entry_price * 100, MIN_ENTRY_PRICE * 100, MAX_ENTRY_PRICE * 100))
            continue

        # F4: Dead zone
        if DEAD_ZONE_START <= time_remaining <= DEAD_ZONE_END:
            log_skip(combo.name, direction, entry_price, impulse_bps, time_remaining,
                     "dead zone T-{:.0f}s".format(time_remaining))
            continue

        # ALL FILTERS PASSED
        execute_paper_trade(combo, direction, impulse_bps, time_remaining, entry_price)


# ══════════════════════════════════════════════════════════════════
# WINDOW MANAGER
# ══════════════════════════════════════════════════════════════════
async def window_manager():
    http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "paper-trade-v2/1.0"})
    try:
        while state.running:
            await asyncio.sleep(1)
            now = time.time()

            if state.window_active and state.window_end > 0 and now >= state.window_end:
                state.window_active = False
                if _ARCH_SPEC and _ARCH_SPEC.get("on_window_end"):
                    _ARCH_SPEC["on_window_end"](state)
                prev_ws = state.window_start

                # INSTANT settlement from book mid — no waiting
                pm_mid_estimate = None
                if state.book.mid > 0:
                    pm_mid_estimate = "Up" if state.book.mid > 0.50 else "Down"

                if pm_mid_estimate:
                    settle_window(prev_ws, pm_mid_estimate)
                    state.completed_windows.append({"window_start": prev_ws, "outcome": pm_mid_estimate})
                    print("  {}[SETTLED] {} via book mid (resolver will verify){}".format(C, pm_mid_estimate, RST))
                else:
                    print("  {}[WARN] No book mid — queued for resolver{}".format(Y, RST))

                # Queue for background verification
                state.pending_windows.append(prev_ws)

                # Reset window tracking
                state.window_price_high = None
                state.window_price_low = None
                state.window_crossed_above = False
                state.window_crossed_below = False
                state.skip_prints_this_window = 0

                # Setup next window IMMEDIATELY — don't wait for settlement API
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

                # Get priceToBeat
                if ptb:
                    state.window_open = ptb
                    state.window_open_source = "pm"
                else:
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

                # Print window header
                time_range = fmt_window_time(state.window_start)
                ptb_str = "{}${:,.2f}{}".format(W, state.window_open, RST) if state.window_open else "{}pending{}".format(Y, RST)
                print("\n{}{}{}\n  {}WINDOW{}  {}{}{}  Price To Beat: {}\n{}{}{}".format(
                    Y, "=" * 70, RST, BOLD, RST, M, time_range, RST, ptb_str, Y, "=" * 70, RST))

                elapsed = time.time() - state.window_start
                if elapsed < WINDOW_BUFFER_START:
                    await wait(WINDOW_BUFFER_START - elapsed)

                state.window_active = True
                if _ARCH_SPEC and _ARCH_SPEC.get("on_window_start"):
                    _ARCH_SPEC["on_window_start"](state)
                write_session_stats()  # immediately push new window to stats.json
                continue

            if time.time() % FLUSH_INTERVAL < 1:
                flush_csvs()
            # Write stats every 3s for real-time dashboard
            if time.time() % 3 < 1:
                write_session_stats()
    finally:
        await http.aclose()


# ══════════════════════════════════════════════════════════════════
# BACKGROUND RESOLVER
# ══════════════════════════════════════════════════════════════════
async def outcome_resolver():
    http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "paper-trade-v2/1.0"})
    try:
        while state.running:
            await wait(30)
            if not state.running:
                break
            resolved_ws = {w["window_start"] for w in state.completed_windows}
            now_i = int(time.time())
            unresolved = [ws for ws in state.pending_windows if ws not in resolved_ws and (now_i - ws) > 300]
            for ws in unresolved:
                if not state.running:
                    break
                slug = "btc-updown-5m-{}".format(ws)
                _, _, fp, _, outcome = await fetch_event(http, slug)
                if outcome:
                    # Check if we already settled with book mid — verify
                    already = next((w for w in state.completed_windows if w["window_start"] == ws), None)
                    if already and already["outcome"] != outcome:
                        print("  {}[CORRECTED] {} was {} now {} — re-settling{}".format(Y, ws, already["outcome"], outcome, RST))
                        # Re-settle: reverse the old PnL and apply the correct one
                        for combo in state.combos:
                            for t in combo.trades:
                                if t.get("window_start") == ws and t.get("outcome") == already["outcome"]:
                                    old_pnl = t["pnl_taker"]
                                    # Reverse old PnL
                                    combo.total_pnl_taker -= old_pnl
                                    combo.total_pnl_maker -= t["pnl_maker"]
                                    combo.bankroll -= old_pnl
                                    # Recompute with correct outcome
                                    size = t["filled_size"]
                                    if t["direction"] == "YES":
                                        if outcome == "Up":
                                            new_pnl = (1.0 - t["effective_entry_taker"]) * size
                                            t["result"] = "WIN"
                                        else:
                                            new_pnl = -t["effective_entry_taker"] * size
                                            t["result"] = "LOSS"
                                    else:
                                        if outcome == "Down":
                                            new_pnl = t["effective_entry_taker"] * size
                                            t["result"] = "WIN"
                                        else:
                                            new_pnl = -(1.0 - t["effective_entry_taker"]) * size
                                            t["result"] = "LOSS"
                                    t["pnl_taker"] = round(new_pnl, 2)
                                    t["outcome"] = outcome
                                    combo.total_pnl_taker += new_pnl
                                    combo.bankroll += new_pnl
                                    print("    {} {} {} -> {} PnL ${:.0f} -> ${:.0f}".format(
                                        combo.name, t["direction"], already["outcome"], outcome, old_pnl, new_pnl))
                        already["outcome"] = outcome
                    elif not already:
                        settle_window(ws, outcome, final_price=fp, silent=True)
                        state.completed_windows.append({"window_start": ws, "outcome": outcome})
                await wait(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        await http.aclose()


async def resolve_all_pending():
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


# ══════════════════════════════════════════════════════════════════
# FINAL ANALYSIS
# ══════════════════════════════════════════════════════════════════
def run_analysis():
    all_trades = []
    for c in state.combos:
        all_trades.extend(c.trades)
    settled = [t for t in all_trades if t.get("outcome")]
    hours = (max(t["timestamp"] for t in settled) - min(t["timestamp"] for t in settled)) / 3600 if settled else 0

    print("\n\n" + "=" * 70)
    print("FINAL PAPER TRADING v2 RESULTS")
    print("Runtime: {:.1f}h | Windows: {} | Settled: {}".format(
        hours, len(state.completed_windows), len({t["window_start"] for t in settled})))
    print("=" * 70)

    print("\n  {:>12} | {:>6} | {:>5} | {:>10} | {:>10} | {:>5} | {:>5} | {:>6}".format(
        "Combo", "Trades", "Win%", "PnL(tk)", "PnL(mk)", "Slip", "Fee", "Entry"))
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
        print("  {:>12} | {:>6} | {:>4.1f}% | ${:>9.2f} | ${:>9.2f} | {:.1f}c | {:.1f}c | ${:.2f}".format(
            c.name, n, wr, c.total_pnl_taker, c.total_pnl_maker, avg_slip, avg_fee, avg_entry))

    # Filter effectiveness
    all_skips = state.skip_buffer
    if all_skips:
        print("\n=== FILTER EFFECTIVENESS ===")
        total_signals = len(settled) + len(all_skips)
        print("  Signals detected: {} | Passed: {} | Filtered: {}".format(
            total_signals, len(settled), len(all_skips)))
        print("\n  {:>30} {:>7} {:>10} {:>10}".format("Reason", "Blocked", "Would-Win%", "Est.Saved"))
        reasons = {}
        for s in all_skips:
            r = s["reason"].split(" ")[0]  # first word as category
            if r not in reasons:
                reasons[r] = {"count": 0, "wins": 0, "losses": 0}
            reasons[r]["count"] += 1
            if s.get("would_have_won"):
                reasons[r]["wins"] += 1
            elif s.get("would_have_won") is not None:
                reasons[r]["losses"] += 1
        for reason, data in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
            total = data["wins"] + data["losses"]
            win_pct = data["wins"] / total * 100 if total > 0 else 0
            print("  {:>30} {:>7} {:>9.1f}%".format(reason, data["count"], win_pct))

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
        daily = avg_tk * n / max(1, hours) * 24
        print("  Best combo: {}".format(c.name))
        print("  Win rate: {:.1f}% | Taker: ${:.2f}/trade | Maker: ${:.2f}/trade".format(wr, avg_tk, avg_mk))
        print("  Projected daily (taker): ${:.0f}".format(daily))
        if avg_tk > 0:
            print("\n  -> GO LIVE (taker)")
        elif avg_mk > 0:
            print("\n  -> GO LIVE (maker only)")
        else:
            print("\n  -> STOP — execution costs eat the edge")
    else:
        print("  Not enough trades.")
    print("=" * 70)


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════
async def run():
    print("{}Paper Trading Bot v2 — Heuristic C{}".format(BOLD, RST))
    print("Architecture: {}".format(ARCHITECTURE))
    print("Filters: actual cost {:.0f}-{:.0f}c, impulse <{}bp, dead zone T-{}-{}s, spread <{:.0f}c".format(
        MIN_ENTRY_PRICE * 100, MAX_ENTRY_PRICE * 100, MAX_IMPULSE_BP,
        DEAD_ZONE_START, DEAD_ZONE_END, MAX_SPREAD * 100))
    if ENABLED_COMBOS:
        print("Enabled combos: {}".format(", ".join(ENABLED_COMBOS)))
    else:
        print("Combos: {} active".format(len(state.combos)))
    print("Sizing: ${}/trade, max {} shares, {}% risk cap".format(
        BASE_TRADE_DOLLARS, MAX_SHARES, int(MAX_RISK_PCT * 100)))
    print("Combos: {}".format(", ".join(c.name for c in state.combos)))
    print()

    # Ensure output dirs
    TRADE_CSV.parent.mkdir(parents=True, exist_ok=True)

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

    cl_price = await read_chainlink_btc()
    if ptb:
        state.window_open = ptb
        state.window_open_source = "pm"
    elif cl_price:
        state.window_open = cl_price
        state.window_open_source = "chainlink"
    if state.window_open and state.binance_price:
        state.offset = state.binance_price - state.window_open
    if cl_price:
        print("  Chainlink BTC/USD: {}${:,.2f}{}".format(G, cl_price, RST))

    time_range = fmt_window_time(state.window_start)
    ptb_str = "${:,.2f}".format(state.window_open) if state.window_open else "?"
    print("\n{}{}{}".format(Y, "=" * 70, RST))
    print("  {}WINDOW{}  {}{}{}  Price To Beat: {}{}{}".format(BOLD, RST, M, time_range, RST, W, ptb_str, RST))
    print("{}{}{}".format(Y, "=" * 70, RST))

    book_task = asyncio.create_task(pm_book_ws_loop())
    rest_poll_task = asyncio.create_task(pm_book_rest_poll())  # always run REST for full book depth
    for _ in range(50):
        if state.book.updated_at > 0:
            break
        await asyncio.sleep(0.1)
    if state.book.updated_at > 0:
        print("  PM book: {} | Bid:{:.0f} Ask:{:.0f} Spr:{:.0f}c".format(
            state.book.source, state.book.best_bid * 100, state.book.best_ask * 100, state.book.spread * 100))

    state.window_active = True
    resolver_task = asyncio.create_task(outcome_resolver())
    window_task = asyncio.create_task(window_manager())

    try:
        await asyncio.gather(ws_task, book_task, rest_poll_task, window_task)
    except asyncio.CancelledError:
        pass
    finally:
        for t in [resolver_task, book_task, rest_poll_task, ws_task, window_task]:
            t.cancel()
        for t in [resolver_task, book_task, rest_poll_task, ws_task]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        print("\n\nShutting down...")
        await resolve_all_pending()
        flush_csvs()
        n_trades = sum(c.total_trades for c in state.combos)
        n_skips = len(state.skip_buffer) + len(state.pending_skips)
        print("Saved {} trades to {}".format(n_trades, TRADE_CSV))
        print("Saved {} skips to {}".format(n_skips, SKIP_CSV))
        print("Errors: {}".format(state.errors))
        run_analysis()


def main():
    # Fix __main__ vs bot.paper_trade_v2 module duplication.
    # When run as a script, this module is loaded as __main__.
    # Architectures import bot.paper_trade_v2, creating a SECOND copy with separate globals.
    # Register __main__ as bot.paper_trade_v2 so both names share the same state.
    import sys as _sys
    _sys.modules["bot.paper_trade_v2"] = _sys.modules[__name__]

    import argparse
    parser = argparse.ArgumentParser(description="Polymarket Paper Trading Bot v2")
    parser.add_argument("--instance", default="default", help="Instance name for multi-run support")
    args = parser.parse_args()

    # Load config FIRST (overrides constants), then set instance (sets paths)
    load_config(args.instance)
    if args.instance != "default":
        set_instance(args.instance)
        print("Instance: {}".format(args.instance))

    # Ensure architecture is loaded
    global _ARCH_SPEC
    if _ARCH_SPEC is None:
        from bot.architectures import load_architecture
        _ARCH_SPEC = load_architecture(ARCHITECTURE)

    # VERIFY config applied correctly
    print("Verified: MIN_ENTRY={:.2f} MAX_ENTRY={:.2f} _ARCH_SPEC={}".format(
        MIN_ENTRY_PRICE, MAX_ENTRY_PRICE, _ARCH_SPEC["name"] if _ARCH_SPEC else "None"))

    # Rebuild combos from architecture spec + apply ENABLED_COMBOS filter
    params = _ARCH_SPEC["combo_params"] if _ARCH_SPEC else COMBO_PARAMS
    state.combos = [Combo(**{k: v for k, v in p.items()
                             if k in ("name", "btc_threshold_bp", "lookback_s")})
                    for p in params]
    if ENABLED_COMBOS:
        state.combos = [c for c in state.combos if c.name in ENABLED_COMBOS]
    state.architecture = ARCHITECTURE

    # Restore trade history from CSV so stats.json survives restarts
    csv_path = Path("data") / args.instance / "trades.csv"
    if csv_path.exists():
        try:
            import csv as _csv
            combo_map = {c.name: c for c in state.combos}
            restored = 0
            with open(csv_path) as f:
                for row in _csv.DictReader(f):
                    result = (row.get("result") or "").strip()
                    if result not in ("WIN", "LOSS"):
                        continue
                    combo_name = (row.get("combo") or "").strip()
                    combo = combo_map.get(combo_name)
                    if not combo:
                        continue
                    trade = {k: row.get(k) for k in row}
                    # Convert numeric fields
                    for k in ("pnl_taker", "pnl_maker", "fill_price", "filled_size",
                              "fee", "slippage", "total_cost", "total_fee", "total_slippage",
                              "notional", "timestamp", "time_remaining", "impulse_bps",
                              "best_bid", "best_ask", "mid", "spread", "btc_price", "delta_bps"):
                        if k in trade and trade[k]:
                            try:
                                trade[k] = float(trade[k])
                            except (ValueError, TypeError):
                                pass
                    combo.trades.append(trade)
                    pnl = float(row.get("pnl_taker", 0) or 0)
                    combo.total_pnl_taker += pnl
                    combo.total_pnl_maker += float(row.get("pnl_maker", 0) or 0)
                    combo.bankroll += pnl
                    restored += 1
            # Restore completed_windows from unique window_start values
            seen_windows = {}
            with open(csv_path) as f2:
                for row in _csv.DictReader(f2):
                    ws = row.get("window_start")
                    result = (row.get("result") or "").strip()
                    if ws and result in ("WIN", "LOSS"):
                        outcome = row.get("outcome") or ("Up" if result == "WIN" else "Down")
                        seen_windows[ws] = outcome
            for ws, outcome in sorted(seen_windows.items()):
                state.completed_windows.append({"window_start": float(ws), "outcome": outcome})
            # Cap completed_windows to prevent memory bloat (keep last 500)
            if len(state.completed_windows) > 500:
                state.completed_windows = state.completed_windows[-500:]
            # Move restored trade count to counter and free the objects from memory
            for c in state.combos:
                c.restored_trade_count = len(c.trades)
                c.restored_win_count = sum(1 for t in c.trades if t.get("result") == "WIN")
                c.trades = []
            if restored:
                print("Restored {} trades, {} windows from CSV".format(restored, len(seen_windows)))
        except Exception as e:
            print("CSV restore failed: {}".format(e))

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
