"""Arena's decision clock for our seat, read from Player.log timer messages.

Arena sends a ``GREMessageType_TimerStateMessage`` when a seat's timers start
running visibly (the rope), with the seat at message level and each timer as
``durationSec`` / ``elapsedSec`` / ``elapsedMs`` / ``running`` / ``behavior``::

    {"seatId": 1, "timers": [
        {"timerId": 3, "type": "TimerType_ActivePlayer", "durationSec": 140,
         "elapsedSec": 13, "running": true, "behavior": "TimerBehavior_TakeControl"},
        {"timerId": 6, "type": "TimerType_Inactivity", "durationSec": 150,
         "running": true, "behavior": "TimerBehavior_Timeout"}]}

``TakeControl`` expiry is the rope running out (a ``TimeoutMessage``, Arena
takes over); ``Timeout`` expiry loses the game (``ResultReason_Timeout``,
2026-10-18 18:38:20 after an unanswered Search). These messages are sparse —
15 in a whole game — and nothing says when a timer stops, so a reading only
describes the request that was pending when it arrived: callers pass
``not_before`` (when the request appeared) and older readings are ignored.

The wall-clock time a reading was made comes from the event's own
``timestamp`` (ms), so a log read late (startup catch-up) is not mistaken
for a fresh clock.
"""

from __future__ import annotations

import time
from typing import Any

# Behaviors whose expiry costs us: the rope (Arena acts for us) or the game.
_DEADLINE_BEHAVIORS = frozenset({"TimerBehavior_TakeControl", "TimerBehavior_Timeout"})
# An event timestamp further than this from now is not trusted as a wall clock.
_MAX_TIMESTAMP_SKEW_S = 24 * 3600.0


def event_wall_time(payload: dict[str, Any] | None, now: float | None = None) -> float:
    """Wall-clock seconds of a GreToClientEvent from its ``timestamp`` (ms), else now."""
    now = time.time() if now is None else now
    raw = (payload or {}).get("timestamp") if isinstance(payload, dict) else None
    try:
        stamp = float(raw) / 1000.0
    except (TypeError, ValueError):
        return now
    return stamp if abs(stamp - now) <= _MAX_TIMESTAMP_SKEW_S else now


def _seconds(timer: dict[str, Any], key_ms: str, key_s: str) -> float:
    if key_ms in timer:
        try:
            return float(timer[key_ms]) / 1000.0
        except (TypeError, ValueError):
            pass
    try:
        return float(timer.get(key_s) or 0)
    except (TypeError, ValueError):
        return 0.0


def timer_entry(timer_msg: dict[str, Any], received_at: float) -> tuple[int, dict[str, Any]] | None:
    """``(seat, entry)`` for a real-format timerStateMessage, or None for other shapes.

    ``entry`` keeps the legacy snapshot keys (``time_remaining_ms``,
    ``timer_type``, ``behavior``, ``is_ticking``) for the soonest running
    deadline, plus every running deadline and ``received_at``.
    """
    if not isinstance(timer_msg, dict):
        return None
    try:
        seat = int(timer_msg.get("seatId") or 0)
    except (TypeError, ValueError):
        return None
    timers = timer_msg.get("timers")
    if isinstance(timers, dict):
        timers = [timers]
    if not seat or not isinstance(timers, list):
        return None
    timers = [timer for timer in timers if isinstance(timer, dict)]
    if not any("durationSec" in timer for timer in timers):
        return None
    running = []
    for timer in timers:
        if not timer.get("running") or timer.get("behavior") not in _DEADLINE_BEHAVIORS:
            continue
        duration = _seconds(timer, "durationMs", "durationSec")
        if duration <= 0:
            continue
        running.append(
            {
                "type": str(timer.get("type") or ""),
                "behavior": str(timer.get("behavior") or ""),
                "remaining_s": max(0.0, duration - _seconds(timer, "elapsedMs", "elapsedSec")),
            }
        )
    soonest = min(running, key=lambda timer: timer["remaining_s"]) if running else None
    return seat, {
        "time_remaining_ms": int(soonest["remaining_s"] * 1000) if soonest else 0,
        "timer_type": soonest["type"] if soonest else "",
        "behavior": soonest["behavior"] if soonest else "",
        "is_ticking": bool(running),
        "running": running,
        "received_at": float(received_at),
    }


def timeout_entry(timeout_msg: dict[str, Any], received_at: float) -> tuple[int, dict[str, Any]] | None:
    """``(seat, entry)`` for a ``timeoutMessage``: that seat's rope just ran out."""
    if not isinstance(timeout_msg, dict):
        return None
    try:
        seat = int(timeout_msg.get("seatId") or 0)
    except (TypeError, ValueError):
        return None
    if not seat:
        return None
    timer = timeout_msg.get("timer") if isinstance(timeout_msg.get("timer"), dict) else {}
    return seat, {
        "time_remaining_ms": 0,
        "timer_type": str(timer.get("type") or ""),
        "behavior": str(timer.get("behavior") or ""),
        "is_ticking": True,
        "running": [],
        "timed_out": True,
        "received_at": float(received_at),
    }


def seconds_remaining(
    timer_state: dict[Any, Any] | None,
    seat: int | None,
    *,
    now: float | None = None,
    not_before: float | None = None,
) -> float | None:
    """Seconds left on our seat's soonest running deadline, or None when unknown.

    A reading made before ``not_before`` (wall clock) belongs to an earlier
    window and is ignored. Bridge-provided entries carry no ``received_at``
    and are read as current.
    """
    if not timer_state or not seat:
        return None
    entry = timer_state.get(seat)
    if entry is None:
        entry = timer_state.get(str(seat))
    if not isinstance(entry, dict) or not entry.get("is_ticking"):
        return None
    now = time.time() if now is None else now
    received = entry.get("received_at")
    received = now if received is None else float(received)
    if not_before is not None and received < not_before:
        return None
    try:
        remaining = float(entry.get("time_remaining_ms") or 0) / 1000.0
    except (TypeError, ValueError):
        return None
    return remaining - max(0.0, now - received)
