"""Automation readiness, independent of saved Autoplay/Auto Queue preferences."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def native_arena_identity(pid: int) -> str | None:
    """Verify a local Arena PID and its birth time without touching the app.

    A PID alone can be reused after exit. The executable plus start time binds
    the handshake to that process lifetime. Failure to inspect is not readiness.
    """
    if sys.platform != "darwin" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,lstart=,comm="],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    candidates = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and fields[1].endswith("/MTGA.app/Contents/MacOS/MTGA"):
            candidates.append((int(fields[0]), fields[1]))
    # Multiple tracked Steam PIDs do not prove a restart. Check actual Arena
    # executables, and fail closed if ownership is ambiguous instead of guessing.
    if len(candidates) != 1 or candidates[0][0] != pid:
        return None
    return candidates[0][1]


def automation_blocker(bridge: Any) -> str | None:
    """Use live readiness, never a possibly stale snapshot's connected flag."""
    check = getattr(bridge, "automation_blocker", None)
    if callable(check):
        reason = check()
        if reason is None or isinstance(reason, str):
            return reason
    # Other execution backends/test doubles expose the ordinary connection contract.
    connected = getattr(bridge, "connected", False)
    if callable(connected):
        connected = connected()
    if not connected:
        return "Bridge disconnected — automation paused; reconnect Arena through Coach after the match."
    return None


def incomplete_hand_blocker(state: dict) -> str | None:
    if state.get("hand_complete") is False:
        # Play/draw is chosen before the opening hand exists. Waiting for cards
        # here would deadlock opening play; mulligans still require the full hand.
        request = state.get("_bridge_request_type") or state.get("bridge_request_type")
        if request in {"ChooseStartingPlayer", "ChooseStartingPlayerReq", "ChooseStartingPlayerRequest"}:
            return None
        context = state.get("decision_context") or {}
        if not request and isinstance(context, dict) and context.get("type") == "choose_starting_player":
            return None
        return "Waiting for a complete hand refresh — known cards are only a partial view."
    return None
