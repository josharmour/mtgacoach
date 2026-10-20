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

GameStateMessages carry the same timers (``timers``, each with its
``timerId``; ``players[].timerIds`` says whose) whenever one changes —
started, stopped, extended — so between TimerStateMessages the clock is still
in the log: a turn spent on a cast/cancel loop (2026-10-19 11:12:52-11:14:00)
sent no TimerStateMessage at all, but every GameStateMessage reported our
ActivePlayer timer running. gamestate merges both per timer
(:func:`seat_entry`); :func:`wrap_up_reason` reads our turn's rope from it.
"""

from __future__ import annotations

import time
from typing import Any

# Behaviors whose expiry costs us: the rope (Arena acts for us) or the game.
_DEADLINE_BEHAVIORS = frozenset({"TimerBehavior_TakeControl", "TimerBehavior_Timeout"})
# Real GreToClientEvent timestamps are Unix epoch milliseconds (2020-01-01..);
# synthetic test numbers ("0", "1000") or future clock skew fall back to now.
_MIN_EPOCH_S = 1_577_836_800.0
_MAX_FUTURE_SKEW_S = 24 * 3600.0


def event_wall_time(payload: dict[str, Any] | None, now: float | None = None) -> float:
    """Wall-clock seconds of a GreToClientEvent from its ``timestamp`` (ms), else now."""
    now = time.time() if now is None else now
    raw = (payload or {}).get("timestamp") if isinstance(payload, dict) else None
    try:
        stamp = float(raw) / 1000.0
    except (TypeError, ValueError):
        return now
    return stamp if _MIN_EPOCH_S <= stamp <= now + _MAX_FUTURE_SKEW_S else now


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
    return seat, seat_entry([(timer, received_at) for timer in timers], received_at)


# The rope: the active player's turn budget and the other player's response
# budget. Arena adds time to them as the player acts (61 s + 7 s per action
# seen 2026-10-19); their TakeControl expiry is a TimeoutMessage.
_ROPE_TYPES = ("TimerType_ActivePlayer", "TimerType_NonActivePlayer")


def seat_entry(timers: list[tuple], received_at: float) -> dict[str, Any]:
    """The ``timer_state`` entry for one seat from its latest report of each timer.

    Each timer comes as ``(timer, reported_at[, turn])`` (a GameStateMessage
    reports only the timers that changed); a running one has run on since, so
    every remaining time is as of ``received_at``. Besides the soonest running
    deadline (the legacy keys), ``rope_s`` / ``rope_running`` / ``rope_turn``
    give each rope timer's time left, paused or not, and the turn it was read
    in (a turn's first request comes before Arena resets the rope).
    """
    running, rope, rope_running, rope_turn = [], {}, {}, {}
    for report in timers:
        timer, at = report[0], report[1]
        turn = report[2] if len(report) > 2 else None
        if timer.get("behavior") not in _DEADLINE_BEHAVIORS:
            continue
        duration = _seconds(timer, "durationMs", "durationSec")
        if duration <= 0:
            continue
        remaining = duration - _seconds(timer, "elapsedMs", "elapsedSec")
        if timer.get("running"):
            remaining -= max(0.0, float(received_at) - float(at))
        remaining = max(0.0, remaining)
        kind = str(timer.get("type") or "")
        if kind in _ROPE_TYPES:
            rope[kind] = remaining
            rope_running[kind] = bool(timer.get("running"))
            rope_turn[kind] = turn
        if timer.get("running"):
            running.append(
                {"type": kind, "behavior": str(timer.get("behavior") or ""), "remaining_s": remaining}
            )
    soonest = min(running, key=lambda timer: timer["remaining_s"]) if running else None
    return {
        "time_remaining_ms": int(soonest["remaining_s"] * 1000) if soonest else 0,
        "timer_type": soonest["type"] if soonest else "",
        "behavior": soonest["behavior"] if soonest else "",
        "is_ticking": bool(running),
        "running": running,
        "rope_s": rope,
        "rope_running": rope_running,
        "rope_turn": rope_turn,
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


# Our turn's rope this low: stop starting optional plays (each costs a model
# call or several) and move on to the attack / the pass. The decision
# watchdog's CLOCK_MARGIN_S takes over at the same mark for an unanswered
# request.
WRAP_UP_REMAINING_S = 20.0


def wrap_up_reason(
    timer_state: dict[Any, Any] | None,
    seat: int | None,
    turn: int,
    active_player: int | None,
    *,
    now: float | None = None,
    threshold: float = WRAP_UP_REMAINING_S,
) -> str:
    """Why this turn of ours must wrap up now, or "".

    2026-10-19 (match 115944ce T8, #66): a cast/cancel loop spent the whole
    turn clock — Arena's TimeoutMessage for our seat at 11:14:00 used up a
    banked timeout, and 30 s later Arena's AI took our seat over, cancelled
    the pending cast and passed Main 1. The clock was in the log all along
    (GameStateMessage timers: ActivePlayer 75 s, 56 s elapsed at 11:13:41)
    but only a TimeoutMessage was ever logged.

    Wrap up once Arena has reported a timeout for our seat this turn, or when
    our turn's rope (read this turn) is down to ``threshold`` seconds.
    """
    if not timer_state or not seat or not turn:
        return ""
    entry = timer_state.get(seat)
    if entry is None:
        entry = timer_state.get(str(seat))
    if not isinstance(entry, dict):
        return ""
    if entry.get("timed_out_turn") == turn:
        return "Arena's turn timer already ran out on us this turn"
    if active_player != seat or (entry.get("rope_turn") or {}).get("TimerType_ActivePlayer") != turn:
        return ""
    rope = (entry.get("rope_s") or {}).get("TimerType_ActivePlayer")
    if not isinstance(rope, (int, float)):
        return ""
    if (entry.get("rope_running") or {}).get("TimerType_ActivePlayer"):
        now = time.time() if now is None else now
        received = entry.get("received_at")
        rope -= max(0.0, now - (now if received is None else float(received)))
    if rope > threshold:
        return ""
    return f"{max(0.0, rope):.0f}s left on our turn clock"
