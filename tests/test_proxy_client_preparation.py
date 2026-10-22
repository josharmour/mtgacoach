"""The coach's HTTP client: built once, without synthetic inference, aimed at the gateway."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.backends.proxy import ONLINE_BASE_URL, ProxyBackend
from arenamcp.client_metadata import get_client_headers
from arenamcp.coach import create_backend


def install_client(monkeypatch, factory=None):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock())))
    constructor = factory or Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=constructor))
    return constructor, client


@pytest.mark.parametrize("url", ["http://10.0.0.100:8444/v1", ONLINE_BASE_URL])
def test_prepare_remote_client_never_sends_inference_or_marks_health_ready(monkeypatch, url):
    monkeypatch.delenv("ARENAMCP_ENGINE_RELOAD", raising=False)
    constructor, client = install_client(monkeypatch)
    health = Mock()
    monkeypatch.setattr("arenamcp.backends.proxy.BackendHealth.instance", health)
    backend = ProxyBackend(base_url=url)
    backend.prepare_client()
    constructor.assert_called_once()
    client.chat.completions.create.assert_not_called()
    health.assert_not_called()


def test_concurrent_preparation_and_request_initialization_share_one_client(monkeypatch):
    monkeypatch.setenv("ARENAMCP_ENGINE_RELOAD", "1")
    entered = threading.Event()
    release = threading.Event()
    client = object()
    constructions = []

    def factory(**kwargs):
        constructions.append(kwargs)
        entered.set()
        assert release.wait(3), "test did not release client constructor"
        return client

    install_client(monkeypatch, factory)
    backend = ProxyBackend(base_url="http://localhost:8000/v1")
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(backend.prepare_client)
        try:
            assert entered.wait(3), "client construction did not start"
            callers = [pool.submit(backend._get_client) for _ in range(3)]
        finally:
            release.set()
        assert first.result(timeout=3) is None
        assert all(call.result(timeout=3) is client for call in callers)
    assert len(constructions) == 1


def test_failed_preparation_can_be_retried_without_marking_model_healthy(monkeypatch):
    constructor, client = install_client(monkeypatch)
    constructor.side_effect = [RuntimeError("invalid local config"), client]
    backend = ProxyBackend(base_url="http://test.invalid/v1")
    with pytest.raises(RuntimeError, match="invalid local config"):
        backend.prepare_client()
    assert backend._client is None
    backend.prepare_client()
    assert backend._client is client
    client.chat.completions.create.assert_not_called()


def test_online_backend_authenticates_to_the_gateway_with_the_license_key(monkeypatch):
    """2026-07-16: an empty license key meant 435 silent 401s in one evening.

    The desktop's only factory call is create_backend("online"): it must aim
    the SDK at api.mtgacoach.com with the saved key, the client headers and
    no SDK-level retries.
    """
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: {"license_key": "sk-customer-key"})
    constructor, client = install_client(monkeypatch)
    backend = create_backend("online")
    assert backend.model == "glm-5.3-flash"
    backend.prepare_client()
    constructor.assert_called_once()
    kwargs = constructor.call_args.kwargs
    assert kwargs["base_url"] == ONLINE_BASE_URL
    assert kwargs["api_key"] == "sk-customer-key"
    assert kwargs["default_headers"] == get_client_headers()
    assert kwargs["default_headers"]["User-Agent"]
    assert kwargs["max_retries"] == 0
    client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("mode", ["local", "auto", "ollama"])
def test_only_the_online_mode_exists(mode):
    with pytest.raises(ValueError, match="online-only"):
        create_backend(mode)


def test_gateway_url_override_from_settings_and_env(monkeypatch):
    """gateway_url in settings.json (or ARENAMCP_GATEWAY_URL) overrides ONLINE_BASE_URL while keeping online headers."""
    monkeypatch.delenv("ARENAMCP_GATEWAY_URL", raising=False)
    monkeypatch.setattr(
        "arenamcp.settings.get_settings",
        lambda: {"license_key": "sk-lan-key", "gateway_url": "http://10.0.0.10:8444"},
    )
    constructor, _client = install_client(monkeypatch)
    backend = create_backend("online")
    assert backend._base_url == "http://10.0.0.10:8444/v1"
    backend.prepare_client()
    kwargs = constructor.call_args.kwargs
    assert kwargs["base_url"] == "http://10.0.0.10:8444/v1"
    assert kwargs["api_key"] == "sk-lan-key"
    assert kwargs["default_headers"] == get_client_headers()

    monkeypatch.setenv("ARENAMCP_GATEWAY_URL", "http://10.0.0.100:8444/v1/")
    backend2 = create_backend("online")
    assert backend2._base_url == "http://10.0.0.100:8444/v1"


def test_public_gateway_canary_alerts_when_api_mtgacoach_goes_down_and_recovers(monkeypatch):
    """When gateway_url points at a LAN address, _check_public_gateway_once alerts on public api.mtgacoach.com outages."""
    from arenamcp.standalone import StandaloneCoach

    coach = StandaloneCoach.__new__(StandaloneCoach)
    logs: list[str] = []
    statuses: list[str] = []
    coach.ui = SimpleNamespace(log=logs.append, status=statuses.append)
    backend = ProxyBackend.create_online(
        license_key="sk-lan-key",
        base_url="http://10.0.0.10:8444/v1",
    )

    outcomes = iter(
        [
            (True, "api.mtgacoach.com OK (90ms)"),
            (False, "api.mtgacoach.com unreachable after 6000ms: timed out"),
            (False, "api.mtgacoach.com unreachable after 6000ms: timed out"),
            (True, "api.mtgacoach.com OK (85ms)"),
        ]
    )
    monkeypatch.setattr(
        "arenamcp.backends.proxy.probe_public_gateway",
        lambda license_key="", timeout=6.0: next(outcomes),
    )

    # 1. Initial OK -> quiet in UI log
    assert coach._check_public_gateway_once(backend) is True
    assert logs == []

    # 2. Public gateway goes DOWN -> red alert in UI log and status bar
    assert coach._check_public_gateway_once(backend) is False
    assert len(logs) == 1 and "Public api.mtgacoach.com is DOWN" in logs[0]
    assert len(statuses) == 1 and "api.mtgacoach.com DOWN" in statuses[0]

    # 3. Still DOWN on next check -> does not spam duplicate DOWN alerts
    assert coach._check_public_gateway_once(backend) is False
    assert len(logs) == 1

    # 4. Public gateway recovers -> green recovery notice in UI log
    assert coach._check_public_gateway_once(backend) is True
    assert len(logs) == 2 and "Public api.mtgacoach.com recovered" in logs[1]
