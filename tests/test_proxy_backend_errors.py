"""R4: typed backend errors, bounded retry, served-model capture."""

from arenamcp.backends.proxy import (
    BackendError,
    ProxyBackend,
    _classify_api_error,
)

import pytest


class _FakeAPIError(Exception):
    def __init__(self, msg, status_code=None, body=None):
        super().__init__(msg)
        self.status_code = status_code
        self.body = body


class _Delta:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = None
        self.model_extra = None
        self.reasoning = None


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content, model=None):
        self.model = model
        self.choices = [_Choice(content)]


class _Completions:
    """Scriptable completions endpoint: pops one behavior per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def create(self, **params):
        self.calls += 1
        behavior = self.script.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _Client:
    def __init__(self, script):
        self.chat = type("chat", (), {})()
        self.chat.completions = _Completions(script)

    def with_options(self, **kw):
        return self


def _backend(script) -> ProxyBackend:
    be = ProxyBackend(model="nemotron-3-super", base_url="http://test.invalid/v1")
    be._client = _Client(script)
    return be


def test_classify_502_with_retry_after():
    err = _classify_api_error(
        _FakeAPIError("bad gateway", status_code=502, body={"retry_after": 60})
    )
    assert err.retryable is True
    assert err.retry_after_s == 60.0
    assert err.status_code == 502


def test_long_retry_after_skips_retry_and_raises_typed():
    boom = _FakeAPIError("bad gateway", status_code=502, body={"retry_after": 60})
    be = _backend([boom])
    with pytest.raises(BackendError) as exc:
        be.complete("sys", "user", 16, raise_on_error=True)
    assert exc.value.status_code == 502
    # retry_after=60 > 5s budget → exactly one attempt
    assert be._client.chat.completions.calls == 1


def test_retryable_error_gets_one_retry_then_succeeds():
    boom = _FakeAPIError("service unavailable", status_code=503)
    ok = _Chunk("advice text", model="deepseek-v4-flash")
    be = _backend([boom, iter([ok])])
    out = be.complete("sys", "user", 16, raise_on_error=True)
    assert out == "advice text"
    assert be._client.chat.completions.calls == 2


def test_sentinel_string_backcompat_without_raise():
    boom = _FakeAPIError("auth", status_code=401)
    be = _backend([boom])
    out = be.complete("sys", "user", 16)
    assert out.startswith("Error getting advice")
    # 401 is not retryable → one attempt
    assert be._client.chat.completions.calls == 1


def test_served_model_recorded_and_differs_from_alias():
    ok = _Chunk("hello", model="deepseek-v4-flash")
    be = _backend([iter([ok])])
    out = be.complete("sys", "user", 16)
    assert out == "hello"
    assert be.last_served_model == "deepseek-v4-flash"


def test_vision_circuit_breaker_disables_after_repeated_failures():
    # Live 2026-07-06: the vision watchdog burned a failing call pair every
    # ~40s all match (text-only gateway model + tunnel choking on MB
    # payloads). Three consecutive failures now open the circuit.
    boom = _FakeAPIError("connection reset")
    be = _backend([boom, boom, boom])
    for _ in range(3):
        out = be.complete_with_image("sys", "user", b"\x89PNG fake")
        assert out.startswith("Error getting vision analysis")
    assert be._vision_dead is True
    # Circuit open: no further backend calls.
    out = be.complete_with_image("sys", "user", b"\x89PNG fake")
    assert "disabled" in out
    assert be._client.chat.completions.calls == 3
