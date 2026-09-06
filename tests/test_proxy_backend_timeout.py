"""Regression tests for the LLM thread-leak fix.

The bug: ProxyBackend.complete() was called via a ThreadPoolExecutor with a
future-level timeout, but the underlying OpenAI client had no timeout (SDK
default is ~10 min). When the backend hung, the executor's future timed out
and shutdown(wait=False) abandoned the thread — but the thread itself was
still blocked on the socket. After enough hangs, threads piled up and the
process drifted. This test pins the contract: complete() forwards a hard
SDK-level deadline so the worker thread can actually exit.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from arenamcp.backends.proxy import ProxyBackend


def _make_backend_with_mock_client():
    backend = ProxyBackend(model="test-model", base_url="http://localhost:11434/v1")

    mock_client = MagicMock()
    mock_chunk = MagicMock()
    mock_chunk.choices = [MagicMock(delta=MagicMock(content="ok"))]
    mock_client.chat.completions.create.return_value = iter([mock_chunk])
    backend._client = mock_client
    return backend, mock_client


def test_complete_forwards_request_timeout_to_with_options():
    backend, mock_client = _make_backend_with_mock_client()

    backend.complete("sys", "user", request_timeout_s=7.5)

    mock_client.with_options.assert_called_once_with(timeout=7.5)


def test_complete_skips_with_options_when_timeout_omitted():
    backend, mock_client = _make_backend_with_mock_client()

    backend.complete("sys", "user")

    mock_client.with_options.assert_not_called()


def test_get_client_sets_finite_default_timeout():
    """Even without per-call request_timeout_s, the client must have a real ceiling.

    Without a finite default, the SDK falls back to ~10 minutes — long enough
    that a hung backend leaks the worker thread for what feels like forever.
    """
    backend = ProxyBackend(model="test-model", base_url="http://localhost:11434/v1")
    backend._client = None

    captured = {}

    def fake_openai_factory(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = type("FakeOpenAIModule", (), {"OpenAI": _FakeOpenAI})

    import sys

    sys.modules["openai"] = fake_module
    try:
        backend._get_client()
    finally:
        sys.modules.pop("openai", None)

    assert "timeout" in captured, "OpenAI client must be created with a finite timeout"
    assert isinstance(captured["timeout"], (int, float))
    assert 0 < captured["timeout"] <= 600, (
        f"client timeout={captured['timeout']!s} — must be a real ceiling, not the SDK's ~10-minute default"
    )
