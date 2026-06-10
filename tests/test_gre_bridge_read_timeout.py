"""Regression test for the pipe read timeout in GREBridge._send_command.

Without a timeout, a hung BepInEx plugin (Unity main thread busy mid-
target-selection, scene transition, etc.) blocked the read forever
while holding _pipe_lock. That cascaded into autopilot lock contention
and a frozen UI requiring autopilot toggle-off + a "wait a while"
recovery period. See bug report 2026-05-01 (Optimistic Scavenger
select_target lockup).

This test simulates a hung plugin by feeding the bridge a pipe-file
mock whose read() blocks until the test cancels it. The bridge must
time out, force-disconnect, and raise — not block forever.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from arenamcp.gre_bridge import GREBridge, GREBridgeError


class _BlockingPipe:
    """Mimics a Win32 pipe file object that never returns from read."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self._writes: list[bytes] = []

    def write(self, data: bytes) -> int:
        if self._closed.is_set():
            raise OSError("Pipe closed")
        self._writes.append(data)
        return len(data)

    def flush(self) -> None:
        if self._closed.is_set():
            raise OSError("Pipe closed")

    def read(self, n: int) -> bytes:
        # Block until close. Once closed, return empty (EOF).
        self._closed.wait()
        return b""

    def close(self) -> None:
        self._closed.set()


def _bridge_with_pipe(pipe: Any, *, timeout: float = 0.3) -> GREBridge:
    bridge = GREBridge.__new__(GREBridge)
    bridge._pipe_lock = threading.Lock()
    bridge._pipe_file = pipe
    bridge._connected = True
    bridge._pipe_created = False
    bridge._server_pipe_handle = None
    bridge._pipe_handle = None
    bridge._pipe_fd = None
    bridge._last_ping_at = 0.0
    bridge._DEFAULT_READ_TIMEOUT_S = timeout
    return bridge


def test_send_command_times_out_on_hung_plugin():
    """When the plugin doesn't respond, the read must time out and raise."""
    pipe = _BlockingPipe()
    bridge = _bridge_with_pipe(pipe, timeout=0.25)

    start = time.monotonic()
    with pytest.raises(GREBridgeError) as excinfo:
        bridge._send_command({"action": "submit_targets", "instanceId": 42})
    elapsed = time.monotonic() - start

    # Should time out roughly at the configured 0.25s, not block indefinitely.
    assert elapsed < 15.0, f"timeout took {elapsed:.2f}s — looks like it hung"
    assert "timeout" in str(excinfo.value).lower()
    assert pipe._closed.is_set(), "pipe must be force-disconnected on timeout"
    assert bridge._connected is False


def test_send_command_does_not_hold_pipe_lock_after_timeout():
    """A second send after a timeout must not deadlock waiting for _pipe_lock."""
    pipe = _BlockingPipe()
    bridge = _bridge_with_pipe(pipe, timeout=0.2)

    with pytest.raises(GREBridgeError):
        bridge._send_command({"action": "ping"})

    # Lock must be released — try acquiring without blocking.
    acquired = bridge._pipe_lock.acquire(blocking=False)
    try:
        assert acquired, "_pipe_lock was still held after timeout — would freeze UI"
    finally:
        if acquired:
            bridge._pipe_lock.release()


def test_send_command_respects_explicit_timeout_param():
    """Caller can override the default with a per-call timeout."""
    pipe = _BlockingPipe()
    bridge = _bridge_with_pipe(pipe, timeout=10.0)  # default 10s

    start = time.monotonic()
    with pytest.raises(GREBridgeError):
        bridge._send_command({"action": "ping"}, timeout=0.15)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"per-call timeout was ignored ({elapsed:.2f}s)"


def test_send_command_succeeds_when_response_arrives_in_time():
    """Sanity check — non-hung path still works."""

    class _GoodPipe:
        def __init__(self) -> None:
            self._buf = b'{"ok":true,"version":"0.3.0"}\n'
            self._idx = 0
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> int:
            self.writes.append(data)
            return len(data)

        def flush(self) -> None:
            pass

        def read(self, n: int) -> bytes:
            if self._idx >= len(self._buf):
                return b""
            chunk = self._buf[self._idx : self._idx + n]
            self._idx += len(chunk)
            return chunk

        def close(self) -> None:
            pass

    pipe = _GoodPipe()
    bridge = _bridge_with_pipe(pipe, timeout=2.0)

    resp = bridge._send_command({"action": "ping"})
    assert resp == {"ok": True, "version": "0.3.0"}
    assert bridge._connected is True
