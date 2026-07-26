"""Smoke test for Phase 1 of the bot-battle eval harness.

Issues start_bot_battle, then polls bot_battle_status until the test
reports completion. Logs per-strategy request counts so we can confirm
our BridgeStrategy was hot-swapped in (both sides should have non-zero
counts on success).

Architecture note:
  Python OWNS the named-pipe server (\\\\.\\pipe\\mtgacoach_bridge_v2).
  The BepInEx plugin is the client. So this script only works on Windows
  (ctypes.WinDLL('kernel32') is required to create the named pipe), and
  no other process can own the pipe at the same time. Stop the desktop
  app / standalone coach before running.

Prereqs:
  1. Windows shell (PowerShell or cmd) — NOT WSL.
  2. MTGA running, logged in, at the home screen (NOT in a match).
  3. Updated MtgaCoachBridge.dll deployed to MTGA\\BepInEx\\plugins\\.
  4. No desktop app / standalone coach running (would already own the pipe).

Usage:
  python -m tools.eval.bot_battle.smoke_test --sets EOE --matches 1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arenamcp.gre_bridge import GREBridge  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--sets", default="EOE", help="Comma-separated set codes for random deck (e.g. 'EOE,TDM')")
    p.add_argument("--matches", type=int, default=1)
    p.add_argument("--max-wait-sec", type=int, default=600)
    p.add_argument("--poll-sec", type=int, default=5)
    args = p.parse_args()

    bridge = GREBridge()
    # Reset cooldown so we can poll quickly for the plugin client.
    bridge._reconnect_cooldown = 0.5
    print("creating pipe server, waiting for plugin to connect...")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if bridge.connect():
            break
        time.sleep(0.5)
    else:
        print("ERROR: plugin never connected to our pipe server within 60s.", file=sys.stderr)
        sys.exit(2)
    print("plugin connected. ping...")
    if not bridge.ping():
        print("ERROR: ping failed.", file=sys.stderr)
        sys.exit(2)
    print("ping OK.")

    cmd = {
        "action": "start_bot_battle",
        "sets": args.sets,
        "matches": args.matches,
    }
    resp = bridge._send_safe(cmd, timeout=15.0)
    print(f"start_bot_battle response: {json.dumps(resp, indent=2)}")
    if not resp or not resp.get("ok"):
        print("ERROR: start_bot_battle failed", file=sys.stderr)
        sys.exit(3)

    waited = 0
    last_status = None
    while waited < args.max_wait_sec:
        time.sleep(args.poll_sec)
        waited += args.poll_sec
        status = bridge._send_safe({"action": "bot_battle_status"}, timeout=5.0) or {}
        if status != last_status:
            print(f"[t+{waited:4d}s] {json.dumps(status)}")
            last_status = status
        if status.get("matches_completed", 0) >= args.matches:
            print(f"DONE: {args.matches} match(es) completed.")
            return
        if status.get("last_error"):
            print(f"ABORT: plugin reports error: {status['last_error']}", file=sys.stderr)
            sys.exit(4)

    print(f"TIMEOUT: matches did not complete within {args.max_wait_sec}s", file=sys.stderr)
    sys.exit(5)


if __name__ == "__main__":
    main()
