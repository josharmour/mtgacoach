"""Tests for the X-cost chooser bridge (submit_x + CastingTimeOption fields).

Verifies that GREBridge.submit_x sends the correct pipe command and
handles responses properly, and that get_pending_actions passes through
the CastingTimeOption_NumericInputRequest fields surfaced by the plugin.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from arenamcp.gre_bridge import GREBridge, GREBridgeError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockPipe:
    """A pipe mock that records writes and returns canned responses."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self._writes: list[bytes] = []
        self._responses: list[bytes] = []
        self._closed = threading.Event()
        if responses:
            for r in responses:
                self._responses.append(
                    (json.dumps(r, separators=(",", ":")) + "\n").encode("utf-8")
                )

    def write(self, data: bytes) -> int:
        if self._closed.is_set():
            raise OSError("Mock pipe closed")
        self._writes.append(data)
        return len(data)

    def flush(self) -> None:
        if self._closed.is_set():
            raise OSError("Mock pipe closed")

    def read(self, n: int) -> bytes:
        if self._closed.is_set():
            return b""
        if self._responses:
            return self._responses.pop(0)
        return b""

    def close(self) -> None:
        self._closed.set()


def _bridge_with_pipe(pipe: Any, *, timeout: float = 1.0) -> GREBridge:
    bridge = GREBridge.__new__(GREBridge)
    bridge._pipe_lock = threading.Lock()
    bridge._pipe_file = pipe
    bridge._connected = True
    bridge._server_socket = None
    bridge._client_socket = None
    bridge._last_ping_at = 0.0
    bridge._DEFAULT_READ_TIMEOUT_S = timeout
    return bridge


# ---------------------------------------------------------------------------
# submit_x — command shape
# ---------------------------------------------------------------------------


def test_submit_x_sends_correct_action():
    """submit_x must send {"action": "submit_x", "value": <int>}."""
    pipe = _MockPipe([{"ok": True, "submitted_type": "CastingTimeOption_X", "value": 3}])
    bridge = _bridge_with_pipe(pipe)
    result = bridge.submit_x(3)
    assert result is True
    # Decode the write buffer to check command shape
    written = json.loads(pipe._writes[0].decode("utf-8").strip())
    assert written == {"action": "submit_x", "value": 3}


def test_submit_x_returns_true_on_success():
    """submit_x returns True when the plugin responds ok."""
    pipe = _MockPipe([{"ok": True, "submitted_type": "CastingTimeOption_X", "value": 5}])
    bridge = _bridge_with_pipe(pipe)
    assert bridge.submit_x(5) is True


def test_submit_x_returns_false_on_plugin_error():
    """submit_x returns False when the plugin returns ok=False."""
    pipe = _MockPipe([{"ok": False, "error": "CastingTimeOptionRequest has no CastingTimeOption_NumericInputRequest child"}])
    bridge = _bridge_with_pipe(pipe)
    assert bridge.submit_x(3) is False


def test_submit_x_returns_false_on_disconnect():
    """submit_x returns False when the pipe is disconnected."""
    pipe = _MockPipe()
    pipe.close()
    bridge = _bridge_with_pipe(pipe)
    with pytest.raises(GREBridgeError):
        bridge._send_command({"action": "submit_x", "value": 0})


def test_submit_x_value_zero():
    """submit_x must handle X=0 (valid for e.g. Silkguard with no spare mana)."""
    pipe = _MockPipe([{"ok": True, "submitted_type": "CastingTimeOption_X", "value": 0}])
    bridge = _bridge_with_pipe(pipe)
    assert bridge.submit_x(0) is True
    written = json.loads(pipe._writes[0].decode("utf-8").strip())
    assert written["value"] == 0


def test_submit_x_value_max():
    """submit_x must handle large X values (e.g. X=20 with lots of mana)."""
    pipe = _MockPipe([{"ok": True, "submitted_type": "CastingTimeOption_X", "value": 20}])
    bridge = _bridge_with_pipe(pipe)
    assert bridge.submit_x(20) is True
    written = json.loads(pipe._writes[0].decode("utf-8").strip())
    assert written["value"] == 20


# ---------------------------------------------------------------------------
# CastingTimeOptionRequest numeric fields — get_pending_actions pass-through
# ---------------------------------------------------------------------------


def _casting_option_response(
    *,
    numeric_min: int = 0,
    numeric_max: int = 10,
    numeric_step: int = 1,
    numeric_input_type: str | None = "ChooseX",
    numeric_disallowed: list[int] | None = None,
    numeric_suggested: list[int] | None = None,
    numeric_disallow_even: bool = False,
    numeric_disallow_odd: bool = False,
    grp_id: int = 0,
) -> dict[str, Any]:
    """Build a plausible CastingTimeOptionRequest response with numeric fields."""
    resp: dict[str, Any] = {
        "ok": True,
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "can_cancel": True,
        "decision_context": {
            "type": "casting_time_options",
            "num_options": 4,
            "options": [
                {"actionType": "CastingTimeOption", "choiceKind": "done", "childIndex": 0, "label": "Done"},
            ],
        },
        "actions": [
            {"actionType": "CastingTimeOption", "choiceKind": "numeric_input", "label": "X = 0", "numericValue": 0},
            {"actionType": "CastingTimeOption", "choiceKind": "numeric_input", "label": "X = 2", "numericValue": 2},
            {"actionType": "CastingTimeOption", "choiceKind": "numeric_input", "label": "X = 4", "numericValue": 4},
            {"actionType": "CastingTimeOption", "choiceKind": "done", "label": "Done"},
        ],
        "numeric_min": numeric_min,
        "numeric_max": numeric_max,
        "numeric_step": numeric_step,
        "can_pass": True,
    }
    if numeric_input_type is not None:
        resp["numeric_input_type"] = numeric_input_type
    if numeric_disallowed:
        resp["numeric_disallowed"] = numeric_disallowed
    if numeric_suggested:
        resp["numeric_suggested"] = numeric_suggested
    if numeric_disallow_even:
        resp["numeric_disallow_even"] = True
    if numeric_disallow_odd:
        resp["numeric_disallow_odd"] = True
    if grp_id:
        resp["grp_id"] = grp_id
    return resp


def test_get_pending_actions_passes_numeric_fields():
    """get_pending_actions for CastingTimeOptionRequest must include numeric fields."""
    response = _casting_option_response(
        numeric_min=0, numeric_max=7, numeric_step=1,
        numeric_disallowed=[1, 3, 5],
        grp_id=12345,
    )
    pipe = _MockPipe([response])
    bridge = _bridge_with_pipe(pipe)

    pending = bridge.get_pending_actions()
    assert pending is not None
    assert pending["has_pending"] is True
    assert pending["numeric_min"] == 0
    assert pending["numeric_max"] == 7
    assert pending["numeric_step"] == 1
    assert pending["numeric_disallowed"] == [1, 3, 5]
    assert pending["grp_id"] == 12345


def test_get_pending_actions_numeric_fields_absent_older_plugin():
    """Older plugin builds without numeric fields must not break get_pending_actions."""
    response: dict[str, Any] = {
        "ok": True,
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "can_cancel": True,
        "decision_context": {
            "type": "casting_time_options",
            "num_options": 1,
            "options": [
                {"actionType": "CastingTimeOption", "choiceKind": "done", "childIndex": 0, "label": "Done"},
            ],
        },
        "actions": [
            {"actionType": "CastingTimeOption", "choiceKind": "done", "label": "Done"},
        ],
        "can_pass": True,
    }
    pipe = _MockPipe([response])
    bridge = _bridge_with_pipe(pipe)

    pending = bridge.get_pending_actions()
    assert pending is not None
    assert pending["has_pending"] is True
    # Numeric fields should be absent without error
    assert "numeric_min" not in pending
    assert "numeric_max" not in pending
    assert "numeric_step" not in pending
    assert "numeric_input_type" not in pending
    assert "numeric_suggested" not in pending


def test_get_pending_actions_disallow_even():
    """Numeric fields must include disallow_even flag when set."""
    response = _casting_option_response(
        numeric_min=0, numeric_max=10,
        numeric_disallow_even=True,
    )
    pipe = _MockPipe([response])
    bridge = _bridge_with_pipe(pipe)

    pending = bridge.get_pending_actions()
    assert pending is not None
    assert pending.get("numeric_disallow_even") is True


def test_get_pending_actions_disallow_odd():
    """Numeric fields must include disallow_odd flag when set."""
    response = _casting_option_response(
        numeric_min=1, numeric_max=9,
        numeric_disallow_odd=True,
    )
    pipe = _MockPipe([response])
    bridge = _bridge_with_pipe(pipe)

    pending = bridge.get_pending_actions()
    assert pending is not None
    assert pending.get("numeric_disallow_odd") is True


# ---------------------------------------------------------------------------
# Pipe protocol boundary tests
# ---------------------------------------------------------------------------


def test_submit_x_via_send_safe():
    """submit_x sent through _send_safe must round-trip correctly."""
    pipe = _MockPipe([{"ok": True, "submitted_type": "CastingTimeOption_X", "value": 7}])
    bridge = _bridge_with_pipe(pipe)

    resp = bridge._send_safe({"action": "submit_x", "value": 7})
    assert resp["ok"] is True
    assert resp["submitted_type"] == "CastingTimeOption_X"
    assert resp["value"] == 7

    # Verify the command that was written
    written = json.loads(pipe._writes[0].decode("utf-8").strip())
    assert written["action"] == "submit_x"
    assert written["value"] == 7


def test_get_pending_actions_casting_option_has_actions():
    """CastingTimeOptionRequest response must include the 'actions' list."""
    response = _casting_option_response()
    pipe = _MockPipe([response])
    bridge = _bridge_with_pipe(pipe)

    pending = bridge.get_pending_actions()
    assert pending is not None
    assert "actions" in pending
    assert len(pending["actions"]) > 0
    # Each action should have the CastingTimeOption actionType
    for action in pending["actions"]:
        assert action.get("actionType") == "CastingTimeOption"


def test_get_pending_actions_numeric_input_type():
    """The plugin surfaces the child's InputType (e.g. 'ChooseX') as a string."""
    response = _casting_option_response(numeric_input_type="ChooseX")
    pipe = _MockPipe([response])
    bridge = _bridge_with_pipe(pipe)

    pending = bridge.get_pending_actions()
    assert pending is not None
    assert pending["numeric_input_type"] == "ChooseX"


def test_get_pending_actions_numeric_suggested():
    """SuggestedValues from the plugin must pass through as numeric_suggested."""
    response = _casting_option_response(
        numeric_min=0, numeric_max=12, numeric_suggested=[4, 6],
    )
    pipe = _MockPipe([response])
    bridge = _bridge_with_pipe(pipe)

    pending = bridge.get_pending_actions()
    assert pending is not None
    assert pending["numeric_suggested"] == [4, 6]


def test_submit_x_clamped_response_still_ok():
    """Plugin may clamp an out-of-range value and report clamped=True.

    submit_x must treat a clamped-but-ok response as success — the cast
    went through with a sane X rather than wedging the client (#390).
    """
    pipe = _MockPipe([{
        "ok": True,
        "submitted_type": "CastingTimeOption_X",
        "value": 10,
        "clamped": True,
    }])
    bridge = _bridge_with_pipe(pipe)
    assert bridge.submit_x(999) is True
