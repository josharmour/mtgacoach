"""GREBridge.submit_x: the pipe command for an X cost, and fail-closed results.

(get_pending_actions returns the plugin's dict unchanged, so the numeric
fields need no bridge-side test; tests/test_x_chooser_plumbing covers how
they are used.)
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from arenamcp.gre_bridge import GREBridge

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
                self._responses.append((json.dumps(r, separators=(",", ":")) + "\n").encode("utf-8"))

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
# submit_x
# ---------------------------------------------------------------------------

OK = {"ok": True, "submitted_type": "CastingTimeOption_X"}


@pytest.mark.parametrize(
    ("value", "reply"),
    [
        (0, {**OK, "value": 0}),  # X=0 is valid (Silkguard with no spare mana)
        (3, {**OK, "value": 3}),
        (20, {**OK, "value": 20}),
        # The plugin clamps an out-of-range value and says so: the cast went
        # through with a sane X instead of wedging the client (#390).
        (999, {**OK, "value": 10, "clamped": True}),
    ],
)
def test_submit_x_sends_the_value_and_reports_success(value, reply):
    pipe = _MockPipe([reply])
    bridge = _bridge_with_pipe(pipe)
    assert bridge.submit_x(value) is True
    assert json.loads(pipe._writes[0].decode("utf-8").strip()) == {"action": "submit_x", "value": value}


@pytest.mark.parametrize("failure", ["plugin_error", "disconnected"])
def test_submit_x_fails_closed(failure):
    if failure == "plugin_error":
        pipe = _MockPipe(
            [
                {
                    "ok": False,
                    "error": "CastingTimeOptionRequest has no CastingTimeOption_NumericInputRequest child",
                }
            ]
        )
    else:
        pipe = _MockPipe()
        pipe.close()
    bridge = _bridge_with_pipe(pipe)
    bridge.connect = lambda: False  # no reconnect
    assert bridge.submit_x(3) is False
