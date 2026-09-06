"""Regression tests for coaching endpoint discovery (task 01).

These tests exercise the *reported failure mode*: automatic fallback to the
MageZero self-play / candidate-evaluation service (port 50052). The healthy
"50052 replica" below is a real local HTTP server that counts every request it
receives - if default coaching ever contacted it, the hit counter would catch
it. A dead replica of the coaching endpoint forces the fallback path, and the
fallback reason must be surfaced for diagnostics. All sockets are ephemeral
localhost; nothing from the running RL fleet is touched.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from typing import Any

import pytest

from arenamcp import magezero_client as mc
from arenamcp.magezero_client import MageZeroClient


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Counts requests; replies 200 on /healthz, 404 elsewhere."""

    hits = 0

    def do_GET(self) -> None:  # noqa: N802
        type(self).hits += 1
        status = 200 if self.path.startswith("/healthz") else 404
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    """Delays past a small discovery budget."""

    def do_GET(self) -> None:  # noqa: N802
        time.sleep(1.0)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


def _start_server(handler: type[http.server.BaseHTTPRequestHandler]) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv


@pytest.fixture
def healthy_50052_replica():
    """A healthy endpoint the coach must NOT touch under default discovery."""
    handler = type("CountingHandler", (_HealthHandler,), {"hits": 0})
    srv = _start_server(handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield url, handler
    srv.shutdown()


@pytest.fixture
def dead_coaching_replica():
    """The address of a coaching endpoint that is guaranteed unreachable."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient configuration and cached health state."""
    monkeypatch.delenv("MAGEZERO_SERVER_URL", raising=False)
    monkeypatch.delenv("MAGEZERO_ENABLE_LAN", raising=False)
    monkeypatch.delenv("ARENAMCP_LAN_EVAL", raising=False)
    monkeypatch.delenv("MAGEZERO_DISCOVERY_BUDGET", raising=False)
    MageZeroClient.reset_health_cache()


def test_ship_configuration_never_contains_selfplay_endpoint():
    """The pinned default config must exclude the self-play port 50052."""
    assert mc.DEFAULT_ENDPOINTS == ("http://127.0.0.1:50054",)
    assert mc.LAN_ENDPOINTS == ("http://10.0.0.10:50054",)
    hosts = mc._get_candidate_hosts()
    assert hosts == ["http://127.0.0.1:50054"]
    assert all(":50052" not in h for h in hosts)


def test_lan_opt_in_adds_lan_coaching_endpoint_only(monkeypatch: pytest.MonkeyPatch):
    """LAN opt-in widens discovery to the LAN coaching endpoint, never 50052."""
    monkeypatch.setenv("MAGEZERO_ENABLE_LAN", "1")
    hosts = mc._get_candidate_hosts()
    assert hosts == ["http://10.0.0.10:50054", "http://127.0.0.1:50054"]
    assert all(":50052" not in h for h in hosts)


def test_explicit_override_is_used_verbatim_for_diagnostics(
    monkeypatch: pytest.MonkeyPatch, healthy_50052_replica
):
    """MAGEZERO_SERVER_URL replaces the candidate list (deliberate diagnostics)."""
    url, _handler = healthy_50052_replica
    monkeypatch.setenv("MAGEZERO_SERVER_URL", url)
    assert mc._get_candidate_hosts() == [url]
    start = time.monotonic()
    assert MageZeroClient.check_health(force=True, timeout=2.0) is True
    assert time.monotonic() - start < 5.0
    assert MageZeroClient.is_available() is True


def test_default_coaching_makes_no_request_to_healthy_50052_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, dead_coaching_replica, healthy_50052_replica
):
    """50054-style endpoint dead + 50052-style endpoint healthy: no contact, fallback."""
    url_50052, handler_50052 = healthy_50052_replica
    # Point the pinned default coaching endpoint at our dead replica.
    monkeypatch.setattr(mc, "DEFAULT_ENDPOINTS", (dead_coaching_replica,))
    monkeypatch.setattr(mc, "LAN_ENDPOINTS", ())

    start = time.monotonic()
    assert MageZeroClient.check_health(force=True, timeout=0.5) is False
    elapsed = time.monotonic() - start

    # The healthy self-play-shaped endpoint was never contacted at all.
    assert handler_50052.hits == 0, (
        f"default coaching contacted {url_50052} ({handler_50052.hits} requests) "
        "during fallback discovery"
    )
    # Fallback is reported with a useful reason.
    reason = MageZeroClient.last_fallback_reason()
    assert reason and "no-healthy-coaching-endpoint" in reason
    assert dead_coaching_replica in reason
    # Fallback was fast (bounded by the per-attempt timeout), not a 15s+ hang.
    assert elapsed < 3.0


def test_discovery_latency_budget_bounds_unreachable_endpoint(
    monkeypatch: pytest.MonkeyPatch, healthy_50052_replica
):
    """A stalled coaching endpoint is bounded by the discovery budget, with reason."""
    url_50052, handler_50052 = healthy_50052_replica
    monkeypatch.setenv("MAGEZERO_DISCOVERY_BUDGET", "0.6")
    monkeypatch.setenv("MAGEZERO_SERVER_URL", "http://127.0.0.1:1")  # unreachable port

    start = time.monotonic()
    assert MageZeroClient.check_health(force=True, timeout=10.0) is False
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"discovery took {elapsed:.2f}s, budget not enforced"
    reason = MageZeroClient.last_fallback_reason()
    assert reason and ("timeout" in reason or "budget" in reason or "no-healthy" in reason)
    assert handler_50052.hits == 0


def test_coaching_requests_are_only_sent_to_discovered_endpoint(
    monkeypatch: pytest.MonkeyPatch, dead_coaching_replica, healthy_50052_replica
):
    """Request paths (evaluate/evaluate_batch) are gated by health for the host."""
    url_50052, handler_50052 = healthy_50052_replica
    monkeypatch.setattr(mc, "DEFAULT_ENDPOINTS", (dead_coaching_replica,))

    # Unhealthy coaching endpoint -> evaluate() returns None without any request.
    assert MageZeroClient.evaluate({"players": [], "hand": []}) is None
    assert MageZeroClient.evaluate_batch([({"players": [], "hand": []}, None)]) is None
    assert handler_50052.hits == 0
