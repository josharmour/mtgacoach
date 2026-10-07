"""Cancelling a model call that is still waiting on the server, over a real socket.

2026-10-07 review: a priority-10 background request queued in vLLM sends no
chunk for a long time. Cancelling it (a newer game plan superseding it, or the
caller's cancel_event) only closed the openai Stream from another thread,
which did not wake the reader blocked in recv: the call kept the background
lane for its whole 60-120 s budget, the superseding call was dropped after its
30 s lane wait, and the queued server request was never aborted. A socket
shutdown wakes the reader at once and drops the connection.

These tests talk to a local HTTP server through the real openai SDK.
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arenamcp.backend_health import BackendHealth
from arenamcp.backends import health, proxy
from arenamcp.backends.proxy import BackendCallCancelled, ProxyBackend

MODEL = "glm-5.3-flash"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in ("ARENAMCP_LLM_PRIORITY", "ARENAMCP_LLM_CIRCUIT", "ARENAMCP_LLM_BACKGROUND_LANE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ProxyBackend, "_local_warmup", lambda self: None)
    monkeypatch.setattr(proxy, "_BACKGROUND_LANE", proxy._BackgroundLane())
    health.reset_circuit_breakers()
    proxy.reset_priority_state()
    BackendHealth.reset_instance()
    yield
    health.reset_circuit_breakers()
    proxy.reset_priority_state()
    BackendHealth.reset_instance()


class ScriptedServer:
    """Answers each POST with the next scripted behavior.

    "stall_headers": never sends response headers (queued before the gateway
    answers). "stall_stream": sends SSE headers, then nothing (queued in
    vLLM). Anything else: an SSE answer with that text. A stall ends when the
    client disconnects (recorded) or after 20 s.
    """

    def __init__(self, script):
        self.script = list(script)
        self.lock = threading.Lock()
        self.disconnected = threading.Event()
        self.requests = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with server.lock:
                    server.requests += 1
                    behavior = server.script.pop(0) if server.script else "ok"
                if behavior == "stall_headers":
                    server._wait_for_disconnect(self.connection)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.flush()
                if behavior == "stall_stream":
                    server._wait_for_disconnect(self.connection)
                    return
                payload = {
                    "id": "x",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": MODEL,
                    "choices": [{"index": 0, "delta": {"content": behavior}, "finish_reason": "stop"}],
                }
                for data in (f"data: {json.dumps(payload)}\n\n", "data: [DONE]\n\n"):
                    raw = data.encode()
                    self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def _wait_for_disconnect(self, conn) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            readable, _, _ = select.select([conn], [], [], 0.05)
            if readable:
                try:
                    data = conn.recv(1, socket.MSG_PEEK)
                except OSError:
                    data = b""
                if not data:
                    self.disconnected.set()
                    return

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def _warm_sdk():
    """Load the openai SDK's lazy streaming imports before anything is timed.

    On the dev Mac (repo on a network volume) the first import and the first
    streamed request can take 20+ s, which says nothing about cancellation.
    """
    server = ScriptedServer(["warm"])
    try:
        backend = _backend(server.url)
        assert backend.complete("s", "u", request_timeout_s=120, call_class="probe") == "warm"
    finally:
        server.close()


def _backend(url: str) -> ProxyBackend:
    original = ProxyBackend._local_warmup
    ProxyBackend._local_warmup = lambda self: None
    try:
        return ProxyBackend(model=MODEL, base_url=url, api_key="x")
    finally:
        ProxyBackend._local_warmup = original


@pytest.fixture
def server_factory(_warm_sdk):
    servers = []

    def make(script):
        server = ScriptedServer(script)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _start(fn, *args, **kwargs):
    result: dict = {}

    def run():
        started = time.monotonic()
        try:
            result["value"] = fn(*args, **kwargs)
        except Exception as e:  # surfaced to the test through result
            result["error"] = e
        result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def _wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.mark.parametrize("stall", ["stall_stream", "stall_headers"])
def test_cancel_event_aborts_a_call_still_waiting_for_the_server(server_factory, stall):
    server = server_factory([stall])
    be = _backend(server.url)
    cancel = threading.Event()
    thread, result = _start(
        be.complete,
        "s",
        "u",
        call_class="background.game_plan",
        request_timeout_s=30,
        cancel_event=cancel,
        raise_on_error=True,
    )
    assert _wait_for(lambda: server.requests == 1)
    time.sleep(0.2)
    cancel.set()
    thread.join(10)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), BackendCallCancelled), result
    assert result["error"].reason == "cancelled"
    assert result["elapsed"] < 5, "the blocked read must wake at once, not at its 30 s timeout"
    assert server.disconnected.wait(5), "the server must see the request dropped"
    assert be.last_request_metrics["status"] == "cancelled"
    assert BackendHealth.instance().snapshot()["total_failures"] == 0
    assert be.circuit_snapshot()["consecutive_failures"] == 0


def test_a_newer_same_class_call_takes_the_lane_from_a_queued_one(server_factory):
    server = server_factory(["stall_stream", "fresh plan"])
    old = _backend(server.url)
    new = _backend(server.url)
    thread, result = _start(
        old.complete,
        "s",
        "u",
        call_class="background.game_plan",
        request_timeout_s=60,
        raise_on_error=True,
    )
    assert _wait_for(lambda: server.requests == 1)
    time.sleep(0.2)
    started = time.monotonic()
    out = new.complete("s", "u", call_class="background.game_plan", request_timeout_s=60)
    assert out == "fresh plan"
    assert time.monotonic() - started < 5, "the newer call must not wait out the 30 s lane wait"
    thread.join(10)
    assert isinstance(result.get("error"), BackendCallCancelled), result
    assert result["error"].reason == "superseded"
    assert result["elapsed"] < 5
    assert server.disconnected.wait(5)
    assert proxy._BACKGROUND_LANE.holder_class() is None


def test_lane_passes_to_the_newer_call_even_if_the_old_thread_is_slow_to_unwind():
    lane = proxy._BackgroundLane()
    old = proxy._CallHandle("background.game_plan")
    assert lane.acquire(old, 0) == "acquired"
    # The old request thread is stuck (no socket to shut down): it has not
    # released yet when the newer call arrives.
    newer = proxy._CallHandle("background.game_plan")
    assert lane.acquire(newer, 0) == "acquired"
    assert old.cancelled() == "superseded"
    lane.release(old)  # the old thread finally unwinds: a no-op
    assert lane.holder_class() == "background.game_plan"
    other = proxy._CallHandle("background.deck_playbook")
    assert lane.acquire(other, 0) == "dropped", "the newer call still holds the lane"
    lane.release(newer)
    assert lane.holder_class() is None
