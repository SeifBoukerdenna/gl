"""
VPS WebSocket Relay Server — streams all session stats to connected clients.
Runs on the VPS on port 8765. Dashboard connects as a WebSocket client.

Reads all stats.json files every second, detects changes, broadcasts diffs.
Also monitors trade CSVs for new trade alerts.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import websockets

DATA_DIR = Path("/opt/polymarket-bot/data")
PORT = 8765
SCAN_INTERVAL = 0.5  # seconds between scans (faster updates)

# Track last known state per session
_last_state = {}
_last_trade_counts = {}
_clients = set()


def scan_sessions():
    """Read all stats.json and trade CSVs, return current state."""
    sessions = {}
    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        stats_path = d / "stats.json"
        if stats_path.exists():
            try:
                with open(stats_path) as f:
                    data = json.load(f)
                data["_name"] = d.name

                # Count CSV trades
                for fname in ["trades.csv", "mr_trades.csv"]:
                    csv_path = d / fname
                    if csv_path.exists():
                        try:
                            with open(csv_path) as f:
                                lines = sum(1 for _ in f) - 1  # minus header
                            data["_csv_trades"] = max(lines, 0)
                        except Exception:
                            pass
                        break

                sessions[d.name] = data
            except Exception:
                pass
    return sessions


def compute_diff(old, new):
    """Find which sessions changed."""
    changed = {}
    for name, data in new.items():
        if name not in old:
            changed[name] = data  # new session
        else:
            # Check if updated timestamp changed
            if data.get("updated", 0) != old[name].get("updated", 0):
                changed[name] = data
            # Check if CSV trade count changed
            elif data.get("_csv_trades", 0) != old[name].get("_csv_trades", 0):
                changed[name] = data
    # Check for removed sessions
    removed = [n for n in old if n not in new]
    return changed, removed


async def broadcast(message):
    """Send message to all connected clients."""
    if len(_clients) == 0:
        return
    msg = json.dumps(message)
    dead = set()
    for ws in list(_clients):
        try:
            await ws.send(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _clients.discard(ws)


async def handler(websocket):
    """Handle a new client connection."""
    _clients.add(websocket)
    client_addr = websocket.remote_address
    print("[relay] Client connected: {}".format(client_addr))

    try:
        # Send full state on connect
        current = scan_sessions()
        await websocket.send(json.dumps({
            "type": "full_state",
            "sessions": current,
            "timestamp": time.time(),
        }))

        # Keep connection alive, handle pings
        async for msg in websocket:
            # Client can send "ping" or requests
            try:
                data = json.loads(msg)
                if data.get("type") == "ping":
                    await websocket.send(json.dumps({"type": "pong", "t": time.time()}))
                elif data.get("type") == "get_trades":
                    # Send recent trades for a session
                    name = data.get("session")
                    trades = _read_recent_trades(name)
                    await websocket.send(json.dumps({
                        "type": "trades",
                        "session": name,
                        "trades": trades,
                    }))
            except Exception:
                pass

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(websocket)
        print("[relay] Client disconnected: {}".format(client_addr))


def _read_recent_trades(name, limit=200):
    """Read recent trades from a session's CSV."""
    trades = []
    for fname in ["trades.csv", "mr_trades.csv"]:
        csv_path = DATA_DIR / name / fname
        if csv_path.exists():
            try:
                import csv
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    all_trades = list(reader)
                trades = all_trades[-limit:]
            except Exception:
                pass
            break
    return trades


async def scanner_loop():
    """Periodically scan for changes and broadcast."""
    global _last_state
    while True:
        try:
            current = scan_sessions()
            changed, removed = compute_diff(_last_state, current)

            if changed or removed:
                msg = {
                    "type": "update",
                    "changed": changed,
                    "removed": removed,
                    "timestamp": time.time(),
                }
                await broadcast(msg)

            _last_state = current
        except Exception as e:
            print("[relay] Scanner error: {}".format(e))

        await asyncio.sleep(SCAN_INTERVAL)


async def main():
    print("[relay] Starting WebSocket relay on port {}".format(PORT))
    print("[relay] Monitoring: {}".format(DATA_DIR))

    # Start scanner
    scanner_task = asyncio.create_task(scanner_loop())

    # Start WebSocket server
    async with websockets.serve(handler, "0.0.0.0", PORT, ping_interval=30, ping_timeout=10):
        print("[relay] Listening on ws://0.0.0.0:{}".format(PORT))
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
