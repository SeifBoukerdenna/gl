"""
Architecture: piggyback (PM Book Dynamics Reader)

Thesis: PM's market makers see order flow we can't see. When they become confident
about the window outcome, their behavior changes in DETECTABLE ways:
  1. Spread compresses (MMs tighten when confident)
  2. Depth shifts (bid side thickens when MMs expect UP)
  3. Mid accelerates (midpoint moves rapidly toward the outcome)

Every other architecture reads BTC price and PREDICTS where PM will go.
This one reads PM's BOOK and sees where MMs are ALREADY positioning.
Then confirms with Binance that BTC agrees. Trades the MMs' direction.

Why this is different:
  - Doesn't use impulse detection (no lookback windows)
  - Doesn't use z-score or vol model
  - Doesn't use volume rate detection
  - PRIMARY signal is PM book dynamics, NOT BTC price
  - BTC is only used as CONFIRMATION, not as signal source
  - Regime-immune: MMs reflect current conditions regardless of regime

Why this might work when others failed:
  - book_fade traded AGAINST the book (contrarian) → lost money
  - piggyback trades WITH the book (follow smart money) → different thesis
  - MMs have access to order flow data we can't see → they ARE the signal
"""

import time
import math
from collections import deque

# ═══ Book tracking state ═══
_mid_history = deque(maxlen=60)       # (timestamp, mid) — last 60 seconds
_spread_history = deque(maxlen=60)    # (timestamp, spread)
_depth_history = deque(maxlen=30)     # (timestamp, bid_depth, ask_depth)
_last_signal_time = [0.0]
_last_status = [0.0]

# ═══ Parameters ═══
PB_MID_WINDOW = 10          # seconds to measure mid acceleration
PB_MID_MIN_MOVE = 0.03      # mid must move 3c+ in PB_MID_WINDOW seconds
PB_DEPTH_RATIO = 1.5        # winning side depth must be 1.5x+ the other
PB_SPREAD_MAX = 0.03        # spread must be tight (MMs confident)
PB_SPREAD_COMPRESSED = 0.02 # spread < 2c = very confident MMs
PB_BTC_CONFIRM_BPS = 1.0    # BTC must agree by at least 1bp
PB_MIN_ENTRY = 0.30
PB_MAX_ENTRY = 0.80
PB_COOLDOWN_SEC = 15
PB_MIN_TIME = 15
PB_MAX_TIME = 280
PB_MIN_SIGNALS = 2          # need at least 2 of 3 book signals to agree
PB_BASE_DOLLARS = 150
PB_STRONG_DOLLARS = 300     # when all 3 book signals + BTC confirm

COMBO_PARAMS = [
    {"name": "PB_2sig", "btc_threshold_bp": 0, "lookback_s": 0, "min_signals": 2},
    {"name": "PB_3sig", "btc_threshold_bp": 0, "lookback_s": 0, "min_signals": 3},
]
_combo_config = {p["name"]: p for p in COMBO_PARAMS}


def on_tick(state, price, ts):
    """Record PM book state every second."""
    book = state.book
    if book.mid > 0:
        now = time.time()
        _mid_history.append((now, book.mid))
        _spread_history.append((now, book.spread))

        # Compute depth = total shares at top 3 levels
        bid_depth = sum(qty for _, qty in book.bids[:3]) if book.bids else 0
        ask_depth = sum(qty for _, qty in book.asks[:3]) if book.asks else 0
        _depth_history.append((now, bid_depth, ask_depth))


def _get_mid_acceleration():
    """How fast is PM's midpoint moving? Returns (direction, move_cents) or None."""
    if len(_mid_history) < 5:
        return None
    now = time.time()
    recent = [(t, m) for t, m in _mid_history if now - t <= PB_MID_WINDOW]
    if len(recent) < 3:
        return None
    old_mid = recent[0][1]
    new_mid = recent[-1][1]
    move = new_mid - old_mid  # positive = mid going up = MMs think UP
    return ("YES" if move > 0 else "NO", abs(move))


def _get_depth_signal():
    """Which side has more depth? Returns (direction, ratio) or None."""
    if len(_depth_history) < 3:
        return None
    now = time.time()
    recent = [(t, b, a) for t, b, a in _depth_history if now - t <= 10]
    if len(recent) < 2:
        return None

    avg_bid = sum(b for _, b, _ in recent) / len(recent)
    avg_ask = sum(a for _, _, a in recent) / len(recent)

    if avg_bid <= 0 or avg_ask <= 0:
        return None

    ratio = avg_bid / avg_ask
    if ratio >= PB_DEPTH_RATIO:
        return ("YES", ratio)  # more buyers = MMs expect UP
    elif (1 / ratio) >= PB_DEPTH_RATIO:
        return ("NO", 1 / ratio)  # more sellers = MMs expect DOWN
    return None


def _get_spread_signal():
    """Is spread compressed (MMs confident)? Returns True/False and current spread."""
    if len(_spread_history) < 3:
        return False, 0
    now = time.time()
    recent = [s for t, s in _spread_history if now - t <= 10]
    if not recent:
        return False, 0
    avg_spread = sum(recent) / len(recent)
    return avg_spread <= PB_SPREAD_MAX, avg_spread


def _get_btc_confirmation(state):
    """Does BTC price agree with the direction? Returns (direction, delta_bps) or None."""
    if not state.window_open or state.window_open <= 0:
        return None
    if state.binance_price is None:
        return None

    btc_corrected = state.binance_price - state.offset
    delta_bps = (btc_corrected - state.window_open) / state.window_open * 10000

    if abs(delta_bps) < PB_BTC_CONFIRM_BPS:
        return None  # BTC too close to strike, no confirmation

    return ("YES" if delta_bps > 0 else "NO", abs(delta_bps))


def check_signals(state, now_s):
    """Read PM book dynamics and trade the direction MMs are positioning for."""
    import bot.paper_trade_v2 as engine

    if not state.window_active:
        return
    if time.time() < state.cooldown_until:
        return
    if not state.book.bids or not state.book.asks:
        return
    if state.book.spread > getattr(engine, 'PB_SPREAD_MAX', PB_SPREAD_MAX):
        return

    time_remaining = state.window_end - time.time()
    if time_remaining < getattr(engine, 'PB_MIN_TIME', PB_MIN_TIME):
        return
    if time_remaining > getattr(engine, 'PB_MAX_TIME', PB_MAX_TIME):
        return

    now = time.time()
    if now - _last_signal_time[0] < getattr(engine, 'PB_COOLDOWN_SEC', PB_COOLDOWN_SEC):
        return

    book_age_ms = (now - state.book.updated_at) * 1000
    if book_age_ms > getattr(engine, 'MAX_BOOK_AGE_MS', 500):
        return

    # ── Read the 3 book signals ──
    mid_signal = _get_mid_acceleration()
    depth_signal = _get_depth_signal()
    spread_ok, spread_val = _get_spread_signal()
    btc_signal = _get_btc_confirmation(state)

    # Status line
    if now - _last_status[0] >= 15:
        mid_str = "{}({:.0f}c)".format(mid_signal[0], mid_signal[1]*100) if mid_signal else "---"
        depth_str = "{}({:.1f}x)".format(depth_signal[0], depth_signal[1]) if depth_signal else "---"
        btc_str = "{}({:.1f}bp)".format(btc_signal[0], btc_signal[1]) if btc_signal else "---"
        print("  {}{}{} {}T-{:>3.0f}s{} | mid:{} depth:{} spread:{:.0f}c{} btc:{}".format(
            engine.DIM, engine.fmt_time(), engine.RST, engine.DIM, time_remaining, engine.RST,
            mid_str, depth_str, spread_val*100,
            " TIGHT" if spread_ok else "", btc_str))
        _last_status[0] = now

    if not spread_ok:
        return  # MMs not confident (wide spread)

    # ── Count agreeing signals ──
    signals = {}
    if mid_signal and mid_signal[1] >= getattr(engine, 'PB_MID_MIN_MOVE', PB_MID_MIN_MOVE):
        signals['mid'] = mid_signal
    if depth_signal:
        signals['depth'] = depth_signal
    if btc_signal:
        signals['btc'] = btc_signal

    if not signals:
        return

    # Determine consensus direction
    direction_votes = {}
    for name, (d, strength) in signals.items():
        direction_votes[d] = direction_votes.get(d, 0) + 1

    if not direction_votes:
        return

    best_dir = max(direction_votes, key=direction_votes.get)
    vote_count = direction_votes[best_dir]

    for combo in state.combos:
        if combo.has_position_in_window(state.window_start):
            continue

        cfg = _combo_config.get(combo.name, {})
        min_sigs = cfg.get("min_signals", 2)

        if vote_count < min_sigs:
            continue

        direction = best_dir

        # Direction filter
        allowed = getattr(engine, 'ALLOWED_DIRECTIONS', None)
        if allowed and direction not in allowed:
            continue

        # Entry price
        entry = state.book.best_ask if direction == "YES" else state.book.best_bid
        min_entry = getattr(engine, 'PB_MIN_ENTRY', PB_MIN_ENTRY)
        max_entry = getattr(engine, 'PB_MAX_ENTRY', PB_MAX_ENTRY)
        if entry < min_entry or entry > max_entry:
            continue

        # Sizing: 3 signals = strong, 2 = base
        if vote_count >= 3:
            dollars = getattr(engine, 'PB_STRONG_DOLLARS', PB_STRONG_DOLLARS)
        else:
            dollars = getattr(engine, 'PB_BASE_DOLLARS', PB_BASE_DOLLARS)

        sig_str = " ".join("{}={}".format(k, d) for k, (d, _) in signals.items())
        print("  {}[PB] FIRE {} {} {}/{} signals [{}] spr={:.0f}c ${} T-{:.0f}s{}".format(
            engine.G if direction == "YES" else engine.R,
            combo.name, direction, vote_count, len(signals),
            sig_str, spread_val*100, dollars, time_remaining, engine.RST))

        _last_signal_time[0] = now
        engine.execute_paper_trade(combo, direction, vote_count, time_remaining, entry,
                                   override_dollars=dollars)
        break


def on_window_start(state):
    _last_status[0] = 0.0
    _last_signal_time[0] = 0.0
    _mid_history.clear()
    _spread_history.clear()
    _depth_history.clear()


ARCH_SPEC = {
    "name": "piggyback",
    "combo_params": COMBO_PARAMS,
    "check_signals": check_signals,
    "extra_globals": {
        "PB_MID_WINDOW": PB_MID_WINDOW,
        "PB_MID_MIN_MOVE": PB_MID_MIN_MOVE,
        "PB_DEPTH_RATIO": PB_DEPTH_RATIO,
        "PB_SPREAD_MAX": PB_SPREAD_MAX,
        "PB_SPREAD_COMPRESSED": PB_SPREAD_COMPRESSED,
        "PB_BTC_CONFIRM_BPS": PB_BTC_CONFIRM_BPS,
        "PB_MIN_ENTRY": PB_MIN_ENTRY,
        "PB_MAX_ENTRY": PB_MAX_ENTRY,
        "PB_COOLDOWN_SEC": PB_COOLDOWN_SEC,
        "PB_MIN_TIME": PB_MIN_TIME,
        "PB_MAX_TIME": PB_MAX_TIME,
        "PB_MIN_SIGNALS": PB_MIN_SIGNALS,
        "PB_BASE_DOLLARS": PB_BASE_DOLLARS,
        "PB_STRONG_DOLLARS": PB_STRONG_DOLLARS,
        "ALLOWED_DIRECTIONS": None,
        "MIN_ENTRY_PRICE": 0.01, "MAX_ENTRY_PRICE": 0.99,
        "DEAD_ZONE_START": 0, "DEAD_ZONE_END": 0,
        "MIN_SHARES": 1,
    },
    "on_window_start": on_window_start,
    "on_window_end": None,
    "on_tick": on_tick,
}
