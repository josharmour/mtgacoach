"""Shared fakes for ProxyBackend tests: a scriptable OpenAI client and stream chunks.

The breaker, priority, lane and BackendHealth state these tests touch is reset
around every test by conftest's ``_isolate_llm_gates``.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace as NS

from arenamcp.backends.proxy import ProxyBackend

URL = "http://test.invalid/v1"
MODEL = "glm-5.3-flash"


class FakeAPIError(Exception):
    """An SDK-style API error: the proxy reads status_code and body."""

    def __init__(self, msg, status_code=None, body=None):
        super().__init__(msg)
        self.status_code = status_code
        self.body = body


class APIConnectionError(Exception):
    """Name matches the SDK's connection error, which the proxy keys on."""


class APITimeoutError(Exception):
    """Name matches the SDK's timeout error (a socket read timeout)."""


def chunk(text=None, *, reasoning=None, finish=None, model=MODEL):
    """One streamed chat-completion chunk (no choices when every field is None)."""
    has_choice = text is not None or reasoning is not None or finish is not None
    return NS(
        model=model,
        usage=None,
        choices=[NS(delta=NS(content=text, reasoning_content=reasoning), finish_reason=finish)]
        if has_choice
        else [],
    )


def ok_stream(text="ok", *, model=MODEL):
    return [chunk(text, finish="stop", model=model)]


# What a 1-token inference probe gets back (non-streamed).
PROBE_OK = NS(model=MODEL, usage=None, choices=[NS(message=NS(content=""), finish_reason="length")])

LITELLM_500 = (
    "Error code: 500 - OpenAIException - Connection error. No fallback model group found. "
    "LiteLLM Retried: 1 times"
)


def litellm_500():
    """A gateway 500 that LiteLLM already retried (the client must not retry it again)."""
    return FakeAPIError(LITELLM_500, status_code=500)


class FakeClient:
    """Scriptable OpenAI client: one behavior per chat.completions.create() call.

    A behavior is an exception (raised), a callable (its result is returned),
    a list (returned as a stream iterator) or any other object (returned as
    is). With the script exhausted every call streams "ok". ``calls`` records
    each call's params with ``extra_body`` copied, since the proxy may edit it
    before a retry; ``timeouts`` records each ``with_options(timeout=...)``.
    """

    def __init__(self, behaviors=()):
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []
        self.timeouts: list = []
        self.lock = threading.Lock()
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        self.timeouts.append(options.get("timeout"))
        return self

    def _create(self, **params):
        with self.lock:
            if "extra_body" in params:
                params = {**params, "extra_body": dict(params["extra_body"] or {})}
            self.calls.append(params)
            behavior = self.behaviors.pop(0) if self.behaviors else ok_stream()
        if isinstance(behavior, Exception):
            raise behavior
        if callable(behavior):
            return behavior()
        if isinstance(behavior, list):
            return iter(behavior)
        return behavior


def make_backend(behaviors=(), model=MODEL, url=URL) -> ProxyBackend:
    """A ProxyBackend whose HTTP client is a FakeClient running ``behaviors``."""
    be = ProxyBackend(model=model, base_url=url)
    be._client = FakeClient(behaviors)
    return be
