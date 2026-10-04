"""Engine resume prepares its HTTP client without synthetic inference."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.backends.proxy import ONLINE_BASE_URL, ProxyBackend


def install_client(monkeypatch, factory=None):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock())))
    constructor = factory or Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=constructor))
    return constructor, client


def test_engine_reload_skips_loopback_model_warmup_but_can_prepare_http(monkeypatch):
    monkeypatch.setenv("ARENAMCP_ENGINE_RELOAD", "1")
    constructor, client = install_client(monkeypatch)
    thread = Mock(side_effect=AssertionError("reload must not launch model warmup"))
    monkeypatch.setattr("arenamcp.backends.proxy.threading.Thread", thread)
    backend = ProxyBackend(base_url="http://localhost:8000/v1")
    constructor.assert_not_called()
    assert backend.prepare_client() is None
    backend.prepare_client()
    constructor.assert_called_once()
    client.chat.completions.create.assert_not_called()
    thread.assert_not_called()


def test_normal_cold_loopback_start_keeps_one_minimal_warmup(monkeypatch):
    monkeypatch.delenv("ARENAMCP_ENGINE_RELOAD", raising=False)
    constructor, client = install_client(monkeypatch)

    def immediate_thread(*, target, daemon):
        assert daemon is True
        return SimpleNamespace(start=target)

    monkeypatch.setattr("arenamcp.backends.proxy.threading.Thread", immediate_thread)
    backend = ProxyBackend(model="glm-5.3-flash", base_url="http://127.0.0.1:8000/v1")
    backend.prepare_client()
    constructor.assert_called_once()
    client.chat.completions.create.assert_called_once_with(
        model="glm-5.3-flash", messages=[{"role": "user", "content": "hi"}], max_tokens=1
    )


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
