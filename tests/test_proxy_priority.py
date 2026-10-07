"""Client-side vLLM priority for background calls (WP12).

The shared GLM server runs ``--scheduling-policy priority`` (lower = sooner,
default 0). On 2026-10-06 the LiteLLM gateway was verified to forward the
field: priority 10 got HTTP 200, a string got HTTP 400 with vLLM's validation
error after 14 s. So background classes default to priority 10, only real
ints are ever sent, ARENAMCP_LLM_PRIORITY=0 turns it off, and a server that
rejects the field disables it for the session after one immediate retry.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace as NS

import pytest

from arenamcp.backend_health import BackendHealth
from arenamcp.backends import health, proxy
from arenamcp.backends.proxy import BackendError, ProxyBackend


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in ("ARENAMCP_LLM_PRIORITY", "ARENAMCP_LLM_CIRCUIT", "ARENAMCP_LLM_BACKGROUND_LANE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ProxyBackend, "_local_warmup", lambda self: None)
    monkeypatch.setattr(proxy, "_BACKGROUND_LANE", proxy._BackgroundLane())
    monkeypatch.setattr(proxy.time, "sleep", lambda s: None)
    health.reset_circuit_breakers()
    proxy.reset_priority_state()
    BackendHealth.reset_instance()
    yield
    health.reset_circuit_breakers()
    proxy.reset_priority_state()
    BackendHealth.reset_instance()


class FakeAPIError(Exception):
    def __init__(self, msg, status_code=None, body=None):
        super().__init__(msg)
        self.status_code = status_code
        self.body = body


def ok_stream(text="ok"):
    return [
        NS(
            model="glm-5.3-flash",
            usage=None,
            choices=[NS(delta=NS(content=text, reasoning_content=None), finish_reason="stop")],
        )
    ]


class FakeClient:
    def __init__(self, behaviors=()):
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **params):
        with self.lock:
            # Snapshot extra_body: the proxy may edit it before a retry.
            self.calls.append({**params, "extra_body": dict(params.get("extra_body") or {})})
            behavior = self.behaviors.pop(0) if self.behaviors else ok_stream()
        if isinstance(behavior, Exception):
            raise behavior
        return iter(behavior)


def make_backend(behaviors=(), model="glm-5.3-flash", url="http://test.invalid/v1") -> ProxyBackend:
    be = ProxyBackend(model=model, base_url=url)
    be._client = FakeClient(behaviors)
    return be


def sent_priority(be, index=-1):
    return be._client.calls[index]["extra_body"].get("priority")


PRIORITY_REJECTION = FakeAPIError(
    "Error code: 400 - litellm.BadRequestError: OpenAIException - Got priority 10 but "
    "Priority scheduling is not enabled.",
    status_code=400,
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"background": True},
        {"call_class": "background.game_plan"},
        {"call_class": "background.deck_playbook"},
        {"call_class": "background.win_plan"},
        {"call_class": "background.set_primer"},
        {"call_class": "background.postmatch"},
    ],
)
def test_background_work_defaults_to_priority_10(kwargs):
    be = make_backend()
    assert be.complete("s", "u", **kwargs) == "ok"
    assert sent_priority(be) == proxy.BACKGROUND_PRIORITY == 10
    assert be.last_request_metrics["priority"] == 10


@pytest.mark.parametrize("call_class", ["decision.typed", "decision.plan", "coach.advice", None])
def test_foreground_calls_send_no_priority_field(call_class):
    be = make_backend()
    be.complete("s", "u", call_class=call_class)
    assert "priority" not in be._client.calls[0]["extra_body"]
    assert be.last_request_metrics["priority"] is None


def test_explicit_int_priority_is_sent_as_given():
    be = make_backend()
    be.complete("s", "u", call_class="decision.typed", priority=-10)
    assert sent_priority(be) == -10


def test_explicit_priority_overrides_the_background_default():
    be = make_backend()
    be.complete("s", "u", call_class="background.game_plan", priority=5)
    assert sent_priority(be) == 5


def test_priority_zero_is_the_server_default_and_is_omitted():
    be = make_backend()
    be.complete("s", "u", call_class="background.game_plan", priority=0)
    assert "priority" not in be._client.calls[0]["extra_body"]


@pytest.mark.parametrize("bad", ["not-an-int", "10", 1.5, True, None])
def test_only_real_ints_are_ever_sent(bad):
    """A string cost 14 s and an HTTP 400 at the gateway (2026-10-06 probe)."""
    be = make_backend()
    be.complete("s", "u", call_class="decision.typed", priority=bad)
    assert "priority" not in be._client.calls[0]["extra_body"]
    be.complete("s", "u", call_class="background.game_plan", priority=bad)
    assert sent_priority(be) == 10  # falls back to the class default, an int


@pytest.mark.parametrize("value", ["0", "false", "off", "no", " OFF "])
def test_env_switch_disables_every_priority_field(monkeypatch, value):
    monkeypatch.setenv("ARENAMCP_LLM_PRIORITY", value)
    be = make_backend()
    be.complete("s", "u", call_class="background.game_plan")
    be.complete("s", "u", call_class="decision.typed", priority=-10)
    assert all("priority" not in call["extra_body"] for call in be._client.calls)


@pytest.mark.parametrize("value", ["1", "on", ""])
def test_env_switch_on_or_unset_keeps_priority(monkeypatch, value):
    monkeypatch.setenv("ARENAMCP_LLM_PRIORITY", value)
    be = make_backend()
    be.complete("s", "u", call_class="background.game_plan")
    assert sent_priority(be) == 10


@pytest.mark.parametrize("model", ["gemma-4-12b-it", "gemma4:e2b", "claude-sonnet-4", "gpt-5-mini"])
def test_priority_only_sent_on_the_glm_vllm_path(model):
    be = make_backend(model=model)
    be.complete("s", "u", call_class="background.game_plan", priority=None)
    be.complete("s", "u", call_class="decision.typed", priority=-10)
    assert all("priority" not in call["extra_body"] for call in be._client.calls)


def test_priority_rejection_disables_it_for_the_session_and_retries_at_once():
    be = make_backend([PRIORITY_REJECTION, ok_stream("plan")])
    assert be.complete("s", "u", call_class="background.game_plan", raise_on_error=True) == "plan"
    assert len(be._client.calls) == 2
    assert be._client.calls[0]["extra_body"]["priority"] == 10
    assert "priority" not in be._client.calls[1]["extra_body"]
    assert not proxy.priority_enabled()
    # Not a backend failure: the server answered.
    assert BackendHealth.instance().snapshot()["total_failures"] == 0
    assert be.circuit_snapshot()["consecutive_failures"] == 0

    other = make_backend()
    other.complete("s", "u", call_class="background.deck_playbook")
    assert "priority" not in other._client.calls[0]["extra_body"]

    proxy.reset_priority_state()
    assert proxy.priority_enabled()


def test_priority_rejection_does_not_use_up_the_transient_retry():
    be = make_backend(
        [PRIORITY_REJECTION, FakeAPIError("service unavailable", status_code=503), ok_stream("x")]
    )
    assert be.complete("s", "u", call_class="background.game_plan") == "x"
    assert len(be._client.calls) == 3


def test_unrelated_400_is_not_mistaken_for_a_priority_rejection():
    be = make_backend([FakeAPIError("invalid model name", status_code=400)])
    with pytest.raises(BackendError):
        be.complete("s", "u", call_class="background.game_plan", raise_on_error=True)
    assert len(be._client.calls) == 1
    assert proxy.priority_enabled()


def test_priority_400_without_priority_in_the_request_is_not_retried():
    be = make_backend([PRIORITY_REJECTION])
    with pytest.raises(BackendError):
        be.complete("s", "u", call_class="decision.typed", raise_on_error=True)
    assert len(be._client.calls) == 1
    assert proxy.priority_enabled()


def test_priority_does_not_change_temperature_or_chat_template_kwargs():
    be = make_backend()
    be.complete(
        "s",
        "u",
        3000,
        temperature=0.0,
        request_timeout_s=75,
        background=True,
        reasoning_effort="low",
    )
    params = be._client.calls[0]
    assert params["temperature"] == 0.0
    assert params["extra_body"]["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "low"}
    assert set(params["extra_body"]) == {"chat_template_kwargs", "priority"}
