"""R4: typed backend errors, bounded retry, served-model capture."""

import logging

import pytest
from tests.proxy_fakes import FakeAPIError, make_backend, ok_stream

from arenamcp.backend_health import BACKEND_ERROR_PREFIX, is_backend_error_text
from arenamcp.backends import proxy
from arenamcp.backends.proxy import BackendError, _classify_api_error


def test_classify_502_with_retry_after():
    err = _classify_api_error(FakeAPIError("bad gateway", status_code=502, body={"retry_after": 60}))
    assert err.retryable is True
    assert err.retry_after_s == 60.0
    assert err.status_code == 502


def test_long_retry_after_skips_retry_and_raises_typed():
    boom = FakeAPIError("bad gateway", status_code=502, body={"retry_after": 60})
    be = make_backend([boom])
    with pytest.raises(BackendError) as exc:
        be.complete("sys", "user", 16, raise_on_error=True)
    assert exc.value.status_code == 502
    # retry_after=60 > 5s budget → exactly one attempt
    assert len(be._client.calls) == 1


def test_failed_call_returns_a_tagged_detectable_sentinel_without_raising():
    """Callers that don't pass raise_on_error get text, never an exception."""
    be = make_backend([FakeAPIError("auth", status_code=401)])
    out = be.complete("sys", "user", 16)
    assert out.startswith(BACKEND_ERROR_PREFIX)
    assert is_backend_error_text(out)
    # 401 is not retryable → one attempt
    assert len(be._client.calls) == 1


def test_served_model_recorded_and_a_mismatch_warned_once(caplog):
    """2026-07-05: alias 'nemotron-3-super' silently ran an ollama 12B."""
    be = make_backend(
        [ok_stream("hello", model="glm-5.3-flash-0930"), ok_stream("again", model="glm-5.3-flash-0930")]
    )
    with caplog.at_level(logging.WARNING, logger=proxy.__name__):
        assert be.complete("sys", "user", 16) == "hello"
        assert be.complete("sys", "user", 16) == "again"
    assert be.last_served_model == "glm-5.3-flash-0930"
    warnings = [r.getMessage() for r in caplog.records if "!= configured alias" in r.getMessage()]
    assert warnings == [
        "[PROXY] served model 'glm-5.3-flash-0930' != configured alias 'glm-5.3-flash' — routing is gateway-side"
    ]
