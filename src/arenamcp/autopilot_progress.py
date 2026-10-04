"""Detect repeated submissions to an unchanged interactive Arena decision."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

_VOLATILE_KEYS = {
    "gamestateid",
    "msgid",
    "messageid",
    "requestid",
    "timestamp",
    "receivedat",
    "polltime",
}
_CHOICE_TYPES = {
    "selecttargets",
    "targetselection",
    "selectn",
    "search",
    "searchlibrary",
    "group",
    "scry",
    "numeric",
    "numericinput",
    "selectx",
    "selectxvalue",
    "castingtimeoptions",
    "modalchoice",
    "order",
    "ordertriggers",
    "selectfromgroups",
    "selectngroup",
    "searchfromgroups",
    "selectreplacement",
    "selectcounters",
    "distribution",
    "assigndamage",
    "ordercombatdamage",
    "declareattackers",
    "declareblockers",
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def decision_kind(poll: dict[str, Any]) -> str:
    value = str(poll.get("request_type") or poll.get("request_class") or "")
    kind = _key(re.sub(r"(?:Request|Req)$", "", value))
    return {
        "declareattacker": "declareattackers",
        "declareblocker": "declareblockers",
        "castingtimeoption": "castingtimeoptions",
        "orderblockers": "order",
    }.get(kind, kind)


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _stable(v) for k, v in value.items() if _key(str(k)) not in _VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def decision_semantics(poll: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """Keep choices, selected slots/counts, numeric bounds and source identity.

    GRE transport IDs are deliberately omitted. A real change in selection,
    board, source, turn or match starts a new progress window.
    """
    kind = decision_kind(poll)
    if poll.get("has_pending") is False or kind not in _CHOICE_TYPES:
        return None
    choice = _stable(poll)
    choice.pop("request_class", None)
    choice["request_type"] = kind
    board = {}
    for zone in ("battlefield", "hand", "stack", "graveyard", "exile", "command"):
        if zone in state:
            board[zone] = [
                {
                    k: card.get(k)
                    for k in (
                        "instance_id",
                        "grp_id",
                        "controller_seat_id",
                        "is_tapped",
                        "power",
                        "toughness",
                        "counters",
                    )
                }
                for card in state.get(zone) or []
                if isinstance(card, dict)
            ]
    return _stable(
        {
            "match_id": state.get("match_id"),
            "turn": state.get("turn") or state.get("turn_info"),
            "decision_context": state.get("decision_context"),
            "board": board,
            "choice": choice,
        }
    )


class DecisionProgressGuard:
    """Only actual mutating bridge calls count; observation never adds attempts."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._signature = ""
            self._window: dict[str, Any] = {}
            self._first_attempt: float | None = None
            self._last_attempt = 0.0
            self._attempts = 0
            self._last_command = ""
            self._last_arguments: Any = None
            self.blocked = False

    def observe(self, poll: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            window = decision_semantics(poll, state)
            signature = json.dumps(window, sort_keys=True, separators=(",", ":")) if window else ""
            if signature != self._signature:
                self.reset()
                self._signature = signature
                self._window = window or {}
            now = self._clock()
            if (
                signature
                and not self.blocked
                and self._attempts >= 3
                and self._first_attempt is not None
                and now - self._first_attempt >= 8.0
                and now - self._last_attempt >= 0.75
            ):
                self.blocked = True
                return self.snapshot()
            return None

    def note_attempt(self, command: str, arguments: Any) -> None:
        with self._lock:
            if not self._signature or self.blocked:
                return
            now = self._clock()
            if self._first_attempt is None:
                self._first_attempt = now
            self._last_attempt = now
            self._attempts += 1
            self._last_command = command
            self._last_arguments = _stable(arguments)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "attempts": self._attempts,
                "elapsed_s": round(self._clock() - self._first_attempt, 2)
                if self._first_attempt is not None
                else 0,
                "blocked": self.blocked,
                "signature": hashlib.sha256(self._signature.encode()).hexdigest() if self._signature else "",
                "last_command": self._last_command,
                "last_arguments": self._last_arguments,
                "window": self._window,
            }


class ProgressBridge:
    """Local forwarding view; never changes the engine's shared GRE bridge."""

    def __init__(self, bridge: Any, before_submit: Callable, *, poll: dict | None = None):
        self._bridge = bridge
        self._before_submit = before_submit
        self._poll = poll

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._bridge, name)
        if name == "get_pending_actions":

            def get_pending(*args, **kwargs):
                result = method(*args, **kwargs)
                if isinstance(result, dict):
                    self._poll = result
                return result

            return get_pending
        if name.startswith("submit_") or name in {"auto_respond", "cancel_action"}:

            def submit(*args, **kwargs):
                if not self._before_submit(self._poll, name, args, kwargs):
                    return False
                result = method(*args, **kwargs)
                return result

            return submit
        return method
