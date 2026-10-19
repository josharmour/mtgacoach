"""A hung model server is detected fast and costs as few requests as possible (#30).

2026-10-19 09:33:52-09:34:23 (standalone.log): vLLM hung after a CUDA OOM in
one tensor-parallel worker. Both legs of a critical decision got a first token
at 5.1 s and then nothing; the socket read timeout fired at 14.5 s, past the
12 s budget, and the coaching loop stalled 22 s. Those stalls did not count
toward the circuit ("timeouts after the first token: slow but alive"), so it
took two more decisions ("no first token within 8.0s" x4) before it opened at
09:34:23: about 32 s and 6 requests into a hung server, while a background
game plan held a slot for 57 s.

The streaming tests talk to a local HTTP server through the real openai SDK.
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from tests.test_critical_decision_budget import LOW, _Legs, _run

from arenamcp import action_planner
from arenamcp.backends import proxy
from arenamcp.backends.health import CircuitBreaker
from arenamcp.backends.proxy import BackendCallCancelled, BackendError, ProxyBackend

MODEL = "glm-5.3-flash"
HANG_LIMIT_S = 8.0


def _sse(payload: dict) -> bytes:
    raw = f"data: {json.dumps(payload)}\n\n".encode()
    return f"{len(raw):x}\r\n".encode() + raw + b"\r\n"


def _delta(**delta) -> dict:
    return {
        "id": "x",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


class HungServer:
    """Each POST runs the next behavior.

    ("token_then_hang", delay_s): SSE headers, after delay_s one reasoning
    token, then nothing (09:33:57: vLLM hung mid-generation). "hang": SSE
    headers, then nothing (no first token). Anything else: an SSE answer
    with that text. A hang ends when the client disconnects (recorded).
    """

    def __init__(self, script):
        self.script = list(script)
        self.lock = threading.Lock()
        self.disconnects = 0
        self.hanging = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with server.lock:
                    behavior = server.script.pop(0) if server.script else "ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.flush()
                if isinstance(behavior, tuple):
                    time.sleep(behavior[1])
                    self.wfile.write(_sse(_delta(role="assistant", reasoning_content="Let")))
                    self.wfile.flush()
                    server._hang(self.connection)
                    return
                if behavior == "hang":
                    server._hang(self.connection)
                    return
                answer = _delta(content=behavior)
                answer["choices"][0]["finish_reason"] = "stop"
                self.wfile.write(_sse(answer))
                raw = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n0\r\n\r\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def _hang(self, conn) -> None:
        self.hanging.set()
        deadline = time.monotonic() + HANG_LIMIT_S
        while time.monotonic() < deadline:
            readable, _, _ = select.select([conn], [], [], 0.05)
            if readable:
                try:
                    data = conn.recv(1, socket.MSG_PEEK)
                except OSError:
                    data = b""
                if not data:
                    with self.lock:
                        self.disconnects += 1
                    return

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _backend(url: str) -> ProxyBackend:
    return ProxyBackend(model=MODEL, base_url=url, api_key="x")


@pytest.fixture(scope="module")
def _warm_sdk():
    """Load the openai SDK's lazy streaming imports before anything is timed (see test_proxy_cancellation)."""
    server = HungServer(["warm"])
    try:
        assert _backend(server.url).complete("s", "u", request_timeout_s=120, call_class="probe") == "warm"
    finally:
        server.close()


@pytest.fixture
def hung(_warm_sdk):
    servers = []

    def make(script):
        server = HungServer(script)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _timed(fn, *args, **kwargs):
    started = time.monotonic()
    try:
        return fn(*args, **kwargs), time.monotonic() - started
    except BackendError as error:
        return error, time.monotonic() - started


def test_a_stream_that_stops_after_its_first_token_fails_fast_and_counts(hung, monkeypatch):
    monkeypatch.setattr(proxy, "STREAM_STALL_S", 0.5, raising=False)
    server = hung([("token_then_hang", 0.1)])
    be = _backend(server.url)
    error, elapsed = _timed(
        be.complete,
        "s",
        "PENDING DECISION",
        request_timeout_s=12,
        first_token_timeout_s=8,
        call_class="decision.typed",
        raise_on_error=True,
    )
    assert isinstance(error, BackendError) and error.kind == "stream_stall", error
    assert elapsed < 4.0, "the stall is cut at STREAM_STALL_S (0.5 s), not at the 8 s read timeout"
    assert be.last_request_metrics["status"] == "stalled"
    snap = be.circuit_snapshot()
    assert snap["consecutive_failures"] == 1, "a stalled stream is a counted circuit failure"
    assert snap["degraded"] and be.degraded() and be.background_blocked()


def test_the_budget_is_a_wall_clock_deadline_after_the_first_token(hung, monkeypatch):
    """Field shape, scaled down: first token at ~2/3 of the first-token limit, then silence.

    Each socket read used to wait the first-token limit again (5.1 s + 8 s
    against a 12 s budget); now no read outlives the budget.
    """
    monkeypatch.setattr(proxy, "STREAM_STALL_S", 30.0, raising=False)  # only the budget can end it
    server = hung([("token_then_hang", 2.0)])
    be = _backend(server.url)
    error, elapsed = _timed(
        be.complete,
        "s",
        "u",
        request_timeout_s=3.0,
        first_token_timeout_s=2.9,
        call_class="decision.typed",
        raise_on_error=True,
    )
    assert isinstance(error, BackendError) and "budget exhausted" in str(error), error
    # The old reads ran to 2.0 + 2.9 s; the margin is for a loaded test machine.
    assert elapsed < 3.0 + 1.2, f"the 3 s budget ran {elapsed:.1f} s"
    # Tokens arrived: a budget that ran out is slow, not down.
    assert be.circuit_snapshot()["consecutive_failures"] == 0


def test_background_work_has_no_stall_limit_only_its_budget(hung, monkeypatch):
    """vLLM may preempt a priority-10 request and resume it later: that pause is not a hang."""
    monkeypatch.setattr(proxy, "STREAM_STALL_S", 0.3)
    server = hung([("token_then_hang", 0.05)])
    be = _backend(server.url)
    error, elapsed = _timed(
        be.complete,
        "s",
        "u",
        request_timeout_s=1.5,
        call_class="background.game_plan",
        background=True,
        raise_on_error=True,
    )
    assert isinstance(error, BackendError) and "budget exhausted" in str(error), error
    assert 1.3 < elapsed < 3.5, elapsed  # its budget, not the 0.3 s stall limit
    assert be.circuit_snapshot()["consecutive_failures"] == 0 and not be.degraded()


def test_a_hung_server_cancels_background_work_holding_a_slot(hung, monkeypatch):
    """09:33:27-09:34:24: a game-plan reform ran 57 s alongside the failing decisions."""
    server = hung(["hang", "hang"])
    plan_backend, decision_backend = _backend(server.url), _backend(server.url)
    result: dict = {}

    def plan():
        result["value"], result["elapsed"] = _timed(
            plan_backend.complete,
            "s",
            "u",
            call_class="background.game_plan",
            background=True,
            request_timeout_s=60,
            raise_on_error=True,
        )

    thread = threading.Thread(target=plan, daemon=True)
    thread.start()
    assert server.hanging.wait(5)
    error, elapsed = _timed(
        decision_backend.complete,
        "s",
        "u",
        request_timeout_s=12,
        first_token_timeout_s=0.5,
        call_class="decision.typed",
        raise_on_error=True,
    )
    assert isinstance(error, BackendError) and error.kind == "first_token_timeout", error
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(result["value"], BackendCallCancelled) and result["value"].reason == "degraded"
    assert result["elapsed"] < 5, "cancelled at the decision's failure, not after its 60 s budget"
    assert proxy._BACKGROUND_LANE.holder_class() is None
    # And no new background call starts while degraded.
    skipped = plan_backend.complete("s", "u", call_class="background.game_plan", background=True)
    assert proxy.is_backend_unavailable(skipped)


def test_breaker_degraded_only_on_saturation_and_until_a_success():
    breaker = CircuitBreaker("k", probe_thread=False)
    breaker.record_failure("connection reset")  # a Cloudflare blip
    assert not breaker.degraded() and not breaker.background_blocked()
    breaker.record_failure("no first token within 8.0s", saturation=True)
    assert breaker.degraded() and breaker.background_blocked() and breaker.available()
    breaker.record_success()
    assert not breaker.degraded() and not breaker.background_blocked()


def test_a_saturation_streak_goes_stale_like_any_other():
    now = [0.0]
    breaker = CircuitBreaker("k", clock=lambda: now[0], probe_thread=False)
    breaker.record_failure("stalled", saturation=True)
    assert breaker.degraded()
    now[0] += 61
    assert not breaker.degraded() and not breaker.background_blocked()


def test_vision_timeouts_in_an_outage_do_not_disable_screenshots():
    """2026-10-16 19:59:17-19:59:40: three "Request timed out." navigation calls latched _vision_dead."""
    from tests.proxy_fakes import APITimeoutError, make_backend

    be = make_backend([APITimeoutError("Request timed out.") for _ in range(4)])
    for _ in range(4):
        assert be.complete_with_image("s", "u", b"\x89PNG fake").startswith("[BACKEND ERROR]")
    assert not getattr(be, "_vision_dead", False)
    out = be.complete_with_image("s", "u", b"\x89PNG fake")
    assert "vision endpoint disabled" not in out and len(be._client.calls) == 5  # still asked


# --- the decision hedge


class _DegradedLegs(_Legs):
    def degraded(self):
        return True


def test_a_degraded_server_gets_one_low_effort_call_not_the_hedge(monkeypatch, caplog):
    monkeypatch.setattr(action_planner, "critical_decision", lambda *a, **k: "removal")
    backend = _DegradedLegs(low=(0.01, LOW))
    engine, bridge, _ = _run(backend)
    assert backend.calls == [None], "no high-effort leg into a server that is not generating"
    assert engine._planner._last_decision_reasoning.startswith("low:")
    bridge.submit_action_by_index.assert_called_once()
    assert "model server degraded; one low-effort call, no high-effort leg" in caplog.text


def test_a_healthy_server_still_hedges(monkeypatch):
    monkeypatch.setattr(action_planner, "critical_decision", lambda *a, **k: "removal")

    class _Healthy(_Legs):
        def degraded(self):
            return False

    backend = _Healthy()
    _run(backend)
    assert sorted(backend.calls, key=str) == sorted(["high", None], key=str)
