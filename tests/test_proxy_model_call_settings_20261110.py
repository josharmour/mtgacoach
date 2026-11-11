"""Thinking settings follow the model served, not the alias name (Fable Nov Q5).

Real cases behind these tests (evidence synced 2026-11-10,
/home/joshu/fable-nov-evidence/arenamcp/standalone.log):

- The proxy logs ``served_model`` on every call. Over the logged period the
  gateway served ``glm-5.3-flash`` (45,980 lines), echoed the alias back as
  ``current`` (51,148 lines), left it None before the first response
  (9,564 lines) and served OpenRouter test models ``or-gemma-4-26b`` /
  ``or-qwen3.7-flash`` (107 lines) - all while the configured alias stayed
  whatever the user picked. Settings keyed on the alias string therefore
  mislabelled every one of these calls.
- 2026-11-09 (Josh): GLM thinking was switched off because responses took very
  long and "model not available" errors were frequent; the account reached
  Mythic on these settings. This chunk must not change what GLM is sent:
  decisions ``none``, plan capped at ``low``, playbook/announcer/probe
  ``none``.
- Match eedf1ae3 (2026-11-09 23:24): an ``or-`` test model at ``low`` spent
  4,613 thinking tokens on a plan, 17 s median / 43 s worst, five of ten plan
  calls cancelled - test models keep the no-thinking row.
"""

import json
import time

from arenamcp.backends.proxy import _alias_effort
from arenamcp.model_call_settings import (
    GLM_ROW,
    effective_row,
    effort_for_backend,
    family_for_backend,
    family_from_alias,
    family_from_served,
)


class _Backend:
    """Minimal stand-in for ProxyBackend: only the attributes the settings read.

    ``served`` simulates a response the gateway already sent for this same
    alias: the double records it as provenance, like ProxyBackend
    ._note_served_model does. Pass ``noted=`` to simulate a response that
    arrived under a different (older) alias.
    """

    def __init__(self, model="current", served=None, noted=None):
        self.model = model
        self.last_served_model = served
        self.last_served_alias = model if (served and noted is None) else noted


def _efforts(backend):
    """Effort the table dictates per call class, from a 'high' request."""
    return {
        "decision.typed": effort_for_backend(backend, "decision.typed", "high"),
        "background.game_plan": effort_for_backend(backend, "background.game_plan", "high"),
        "background.deck_playbook": effort_for_backend(backend, "background.deck_playbook", "high"),
        "background.commentary": effort_for_backend(backend, "background.commentary", "high"),
        "probe": effort_for_backend(backend, "probe", "high"),
    }


GLM_BEFORE = {
    "decision.typed": "none",
    "background.game_plan": "low",
    "background.deck_playbook": "none",
    "background.commentary": "none",
    "probe": "none",
}


def test_glm_behind_current_alias_is_sent_exactly_today_settings():
    """GLM behind ``current`` keeps decisions none / plan low / rest none.

    Before this chunk the alias test in proxy._alias_effort produced exactly
    GLM_BEFORE for the ``current`` alias; Josh reached Mythic on these settings
    (2026-11-09), so the served-model table must reproduce them bit for bit -
    before the first response (served None), for the real served name
    (``glm-5.3-flash``) and for the echoed alias (``current``, 51,148 log
    lines).
    """
    for served in (None, "glm-5.3-flash", "current"):
        assert _efforts(_Backend("current", served)) == GLM_BEFORE, served


def test_alias_fallback_matches_the_old_alias_rule():
    """The pre-response alias fallback reproduces the old _alias_effort rule."""
    assert _alias_effort("current", "decision.typed", "low") == "none"
    assert _alias_effort("current", "background.game_plan", "high") == "low"
    assert _alias_effort("current", "background.game_plan", "medium") == "medium"
    assert _alias_effort("deepseek-v4", "decision.typed", "low") == "none"
    assert _alias_effort("dsv4-chat", "background.game_plan", "max") == "low"
    # A direct model name was never governed by the old rule and still is not.
    assert _alias_effort("glm-5.3-flash", "decision.typed", "low") == "low"
    assert _alias_effort("glm-5.3-flash", "background.game_plan", "high") == "high"


def test_settings_follow_the_served_model_not_the_alias():
    """The served name decides the row; the alias is only the pre-response guess.

    standalone.log shows ``or-gemma-4-26b`` served while aliases stayed
    unchanged: a backend configured ``current`` but serving an OpenRouter test
    model must get the test-model row (plan ``none``, match eedf1ae3), and a
    backend configured ``or-gemma-4-26b`` but serving GLM must get the GLM row.
    """
    assert _efforts(_Backend("current", "or-gemma-4-26b"))["background.game_plan"] == "none"
    assert _efforts(_Backend("or-gemma-4-26b", "glm-5.3-flash"))["background.game_plan"] == "low"
    assert _efforts(_Backend("or-gemma-4-26b", "glm-5.3-flash"))["decision.typed"] == "none"


def test_deepseek_names_keep_the_deepseek_row():
    """DeepSeek names (alias or served) keep decisions none / plan capped low."""
    for backend in (_Backend("deepseek-v4"), _Backend("current", "dsv4-reasoning")):
        efforts = _efforts(backend)
        assert efforts["decision.typed"] == "none"
        assert efforts["background.game_plan"] == "low"
        assert efforts["background.deck_playbook"] == "none"


def test_unknown_served_model_left_as_requested_and_named_once(caplog):
    """An unknown served model gets no row: request untouched, one naming log line.

    Forcing thinking off on an uncalibrated model could silently degrade it;
    forcing it on repeats the DeepSeek-week hangs. Leaving the request as asked
    is the conservative row; the log line names the model so the gap is visible.
    """
    import logging

    backend = _Backend("current", "gemma4-local")
    with caplog.at_level(logging.INFO, logger="arenamcp.model_call_settings"):
        assert effort_for_backend(backend, "background.game_plan", "high") == "high"
        assert effort_for_backend(backend, "decision.typed", None) is None
        first = [r for r in caplog.records if "gemma4-local" in r.getMessage()]
        assert len(first) == 1
        effort_for_backend(backend, "decision.typed", "low")
        again = [r for r in caplog.records if "gemma4-local" in r.getMessage()]
        assert len(again) == 1  # named once per model, not once per call


def test_per_machine_override_rows(monkeypatch):
    """ARENAMCP_THINKING_ROWS overrides a row without a code change.

    Lets a test run pit one thinking setting against another (the hook H3's
    head-to-head runs need): a family override wins over the ``*`` override,
    which wins over the built-in row; junk entries are dropped with a warning.
    """
    monkeypatch.setenv(
        "ARENAMCP_THINKING_ROWS",
        json.dumps({"glm": {"plan": "high"}, "*": {"decision": "low"}}),
    )
    assert effective_row("glm")["plan"] == "high"
    assert effective_row("glm")["decision"] == "low"  # from "*"
    assert effective_row("deepseek")["decision"] == "low"  # "*" reaches every family
    assert effective_row("deepseek")["plan"] == "cap_low"  # built-in row kept
    backend = _Backend("current", "glm-5.3-flash")
    assert effort_for_backend(backend, "background.game_plan", "high") == "high"

    monkeypatch.setenv("ARENAMCP_THINKING_ROWS", "{not json")
    assert effective_row("glm")["plan"] == GLM_ROW["plan"]

    monkeypatch.setenv("ARENAMCP_THINKING_ROWS", json.dumps({"glm": {"nonsense": "high"}}))
    assert effective_row("glm") == dict(GLM_ROW)


def test_playbook_options_follow_the_served_model():
    """coach._playbook_call_options keys on the served family, not the alias.

    Before this chunk a backend configured with the literal name
    ``glm-5.3-flash`` got the non-hosted playbook settings even though GLM was
    what the gateway served; now any backend whose served model is a hosted
    family gets them, and one serving an unknown model does not.
    """
    from arenamcp.coach import _playbook_call_options

    hosted = _playbook_call_options(_Backend("glm-5.3-flash", "glm-5.3-flash"), discovery=True)
    assert hosted["reasoning_effort"] == "none"
    assert hosted["max_tokens"] == 1536

    alias_only = _playbook_call_options(_Backend("current"), discovery=True)
    assert alias_only["reasoning_effort"] == "none"  # pre-response guess: hosted alias

    test_model = _playbook_call_options(_Backend("current", "or-gemma-4-26b"), discovery=True)
    assert test_model["reasoning_effort"] == "none"

    unknown = _playbook_call_options(_Backend("current", "gemma4-local"), discovery=True)
    assert unknown["reasoning_effort"] == "high"  # non-hosted discovery settings


def test_selective_thinking_follows_the_family():
    """The selective-thinking gate admits GLM and DeepSeek families, not or-* models."""
    import os

    from arenamcp import selective_thinking

    os.environ["ARENAMCP_SELECTIVE_THINKING"] = "1"
    try:
        assert selective_thinking.enabled("current")
        assert selective_thinking.enabled("glm-5.3-flash")
        assert selective_thinking.enabled("deepseek-v4")
        assert not selective_thinking.enabled("or-gemma-4-26b")
        assert not selective_thinking.enabled("gemma4-local")
        assert not selective_thinking.enabled(None)
    finally:
        os.environ.pop("ARENAMCP_SELECTIVE_THINKING", None)
    assert not selective_thinking.enabled("current")


def test_family_classification_and_session_cache():
    """Family lookup covers every name seen in served_model logs; cached per session."""
    assert family_from_served("glm-5.3-flash") == "glm"
    assert family_from_served("or-gemma-4-26b") == "openrouter-test"
    assert family_from_served("dsv4-reasoning") == "deepseek"
    assert family_from_served("gemma4-local") == ""
    # The gateway sometimes echoes the alias back as the served name.
    assert family_from_alias("current") == "glm"

    backend = _Backend("current", None)
    assert family_for_backend(backend) == "glm"  # alias guess before any response
    backend.last_served_model = None
    assert family_for_backend(backend) == "glm"  # cached: no reclassification


def test_family_cache_rebinds_when_the_alias_switches():
    """An explicit model switch rebinds the cached family immediately.

    standalone.set_backend and the coach's backend switch mutate the live
    ProxyBackend instance; the previously served name was noted under the old
    alias and must not classify the new one in either direction.
    """
    backend = _Backend("current", "glm-5.3-flash")
    assert family_for_backend(backend) == "glm"
    backend.model = "or-gemma-4-26b"
    assert family_for_backend(backend) == "openrouter-test"  # stale served name not trusted
    backend.model = "current"
    assert family_for_backend(backend) == "glm"


def test_switch_to_test_model_rebinds_settings_before_the_next_transport_call():
    """current → or-gemma-4-26b: the first call after the switch is sent as a test model.

    Real sequence (standalone.set_backend mutates the live backend): GLM is
    served under ``current``, then the user switches the same instance to an
    OpenRouter test model. The stale served name must not keep plan calls at
    ``low`` - a fresh or-gemma backend sends reasoning disabled (match
    eedf1ae3: test models never think), so this one must too.
    """
    from tests.proxy_fakes import make_backend, ok_stream

    backend = make_backend(
        [ok_stream("ok", model="glm-5.3-flash"), ok_stream("ok", model="or-gemma-4-26b")],
        model="current",
    )
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }

    backend.model = "or-gemma-4-26b"
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[1]["extra_body"]["reasoning"] == {"enabled": False}


def test_switch_back_to_current_restores_glm_settings_on_the_first_call():
    """or-gemma-4-26b → current: the first call after the switch is sent as GLM again."""
    from tests.proxy_fakes import make_backend, ok_stream

    backend = make_backend(
        [ok_stream("ok", model="or-gemma-4-26b"), ok_stream("ok", model="glm-5.3-flash")],
        model="or-gemma-4-26b",
    )
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[0]["extra_body"]["reasoning"] == {"enabled": False}

    backend.model = "current"
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[1]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }


def test_unknown_family_override_reaches_the_wire(monkeypatch):
    """Overrides reach unknown served families; without one, high survives.

    The conservative unknown row leaves the request as asked, but a wildcard
    override must still pin it (head-to-head runs need to force an unknown
    model's row); both sides are asserted on the actual transport payload.
    """
    from tests.proxy_fakes import make_backend

    def noted_backend():
        backend = make_backend(model="current")
        backend.last_served_model = "gemma4-local"
        backend.last_served_alias = "current"
        return backend

    monkeypatch.setenv("ARENAMCP_THINKING_ROWS", json.dumps({"*": {"plan": "none"}}))
    pinned = noted_backend()
    pinned.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert pinned._client.calls[0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": False,
        "reasoning_effort": "none",
    }

    monkeypatch.delenv("ARENAMCP_THINKING_ROWS")
    untouched = noted_backend()
    untouched.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert untouched._client.calls[0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "high",
    }


def test_malformed_override_values_are_rejected_safely(monkeypatch):
    """A non-string policy (JSON list/object) is dropped with a warning, not a TypeError.

    Reported 2026-11-11: ARENAMCP_THINKING_ROWS='{"glm":{"plan":[]}}' crashed
    _overrides with TypeError: unhashable type: 'list'.
    """
    from tests.proxy_fakes import make_backend

    monkeypatch.setenv("ARENAMCP_THINKING_ROWS", json.dumps({"glm": {"plan": []}}))
    assert effective_row("glm") == dict(GLM_ROW)

    monkeypatch.setenv(
        "ARENAMCP_THINKING_ROWS",
        json.dumps({"glm": {"plan": {"depth": 2}}, "*": ["none"]}),
    )
    assert effective_row("glm") == dict(GLM_ROW)

    # The transport still works end to end with junk in the environment.
    backend = make_backend(model="current")
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }


# ── Provenance binds to the requesting alias, not the config at response time ──
#
# Dotty review 2026-11-11 (on 28486fe): _note_served_model stamped
# last_served_alias from the mutable self.model when the RESPONSE arrived.
# standalone.set_backend switches the live instance's model while a request
# can still be in flight, so a response that predates the switch was labelled
# with the new alias and the next call mixed rows across models.


def _switch_mid_stream(backend, new_model):
    """A stream behavior that flips the configured alias before answering."""
    from tests.proxy_fakes import ok_stream

    def behavior():
        backend.model = new_model
        return ok_stream("ok", model="glm-5.3-flash")

    return behavior


def test_in_flight_switch_streamed_response_records_the_requesting_alias():
    """current → or-gemma mid-flight: the glm response stays provance 'current'.

    Real sequence (standalone.set_backend mutates the live instance): a plan
    request goes out under ``current``, the operator switches the same
    instance to an OpenRouter test model, and only then the glm response
    arrives. The response answered a request made under ``current``, so its
    provenance must say ``current`` - and the first call under the new alias
    must be sent exactly like a fresh or-gemma backend (match eedf1ae3: test
    models never think), not with GLM's capped plan effort.
    """
    from tests.proxy_fakes import make_backend, ok_stream

    backend = make_backend(model="current")
    backend._client.behaviors = [
        _switch_mid_stream(backend, "or-gemma-4-26b"),
        ok_stream("ok", model="or-gemma-4-26b"),
    ]
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend.last_served_alias == "current"
    assert backend.last_served_model == "glm-5.3-flash"

    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[1]["extra_body"]["reasoning"] == {"enabled": False}


def test_in_flight_switch_reverse_direction_records_the_requesting_alias():
    """or-gemma → current mid-flight: the or-gemma response keeps its own alias."""
    from tests.proxy_fakes import make_backend, ok_stream

    backend = make_backend(model="or-gemma-4-26b")

    def behavior():
        backend.model = "current"
        return ok_stream("ok", model="or-gemma-4-26b")

    backend._client.behaviors = [behavior, ok_stream("ok", model="glm-5.3-flash")]
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend.last_served_alias == "or-gemma-4-26b"
    assert backend.last_served_model == "or-gemma-4-26b"

    # The first call after switching back is sent as GLM again.
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[1]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }


def test_in_flight_switch_non_streamed_response_records_the_requesting_alias():
    """Same binding on the non-streaming fallback path (stream attempt failed)."""
    from types import SimpleNamespace as NS

    from tests.proxy_fakes import make_backend

    backend = make_backend(model="current")

    def switch_then_answer():
        backend.model = "or-gemma-4-26b"
        return NS(
            model="glm-5.3-flash",
            usage=None,
            choices=[NS(message=NS(content="ok", reasoning_content=None), finish_reason="stop")],
        )

    # A plain ValueError is non-retryable with no status: the proxy falls back
    # to one non-streamed create() instead of retrying the stream.
    backend._client.behaviors = [ValueError("no stream here"), switch_then_answer]
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert len(backend._client.calls) == 2  # stream attempt + non-streamed retry
    assert backend.last_served_alias == "current"
    assert backend.last_served_model == "glm-5.3-flash"

    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[2]["extra_body"]["reasoning"] == {"enabled": False}


def test_out_of_order_replies_each_stamp_their_own_request_alias():
    """Two in-flight requests under different aliases stamp provenance per reply.

      request A under ``current`` ── still in flight ──────────► glm reply
      switch to or-gemma; request B ──► or-gemma reply arrives FIRST

    Each reply records the alias its own request was made under; whichever
    lands last wins, and a stale pairing that no longer matches the configured
    alias degrades safely to the alias guess - never to the other model's row.
    """
    import threading

    from tests.proxy_fakes import make_backend, ok_stream

    backend = make_backend(model="current")
    release_first = threading.Event()

    def slow_current_answer():
        release_first.wait(5)
        return ok_stream("ok", model="glm-5.3-flash")

    backend._client.behaviors = [slow_current_answer, ok_stream("ok", model="or-gemma-4-26b")]

    errors: list[str] = []

    def first_call():
        try:
            backend.complete("s", "u", call_class="probe", reasoning_effort=None)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(repr(exc))

    thread = threading.Thread(target=first_call)
    thread.start()
    for _ in range(500):
        if len(backend._client.calls) >= 1:
            break
        time.sleep(0.01)
    assert len(backend._client.calls) == 1  # A's request is on the wire

    # B goes out under the new alias and its reply arrives first.
    backend.model = "or-gemma-4-26b"
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend.last_served_alias == "or-gemma-4-26b"
    assert backend.last_served_model == "or-gemma-4-26b"

    release_first.set()
    thread.join(10)
    assert not errors and not thread.is_alive()
    # A's late reply stamped its own requesting alias...
    assert backend.last_served_alias == "current"
    assert backend.last_served_model == "glm-5.3-flash"
    # ...which no longer matches the configured alias, so the next call falls
    # back to the alias guess - exactly what a fresh or-gemma backend sends.
    backend.complete("s", "u", background=True, call_class="background.game_plan", reasoning_effort="high")
    assert backend._client.calls[2]["extra_body"]["reasoning"] == {"enabled": False}


# ── The selective-thinking gate reads provenance too ────────────────────────


def test_selective_gate_after_switch_to_test_model_drops_bounded_thinking(monkeypatch):
    """current/GLM → or-gemma switch: no bounded thinking leaks onto the test model.

    Dotty review 2026-11-11: with ARENAMCP_SELECTIVE_THINKING=1 the gate read
    last_served_model raw, so after switching to an OpenRouter test model it
    still classified GLM and sent reasoning effort low plus a 256-token budget
    on top of the test-model row - while a fresh or-gemma decision sends
    thinking disabled (match eedf1ae3).
    """
    monkeypatch.setenv("ARENAMCP_SELECTIVE_THINKING", "1")
    from tests.proxy_fakes import make_backend, ok_stream

    backend = make_backend(
        [ok_stream("ok", model="glm-5.3-flash"), ok_stream("ok", model="or-gemma-4-26b")],
        model="current",
    )
    backend.complete("s", "u", call_class="decision.typed", reasoning_effort="high", selective_thinking=True)
    assert backend._client.calls[0]["extra_body"]["thinking_token_budget"] == 256

    backend.model = "or-gemma-4-26b"
    backend.complete("s", "u", call_class="decision.typed", reasoning_effort="high", selective_thinking=True)
    body = backend._client.calls[1]["extra_body"]
    assert body.get("thinking_token_budget") is None
    assert body["reasoning"] == {"enabled": False}


def test_selective_gate_after_switch_back_to_current_restores_bounded_thinking(monkeypatch):
    """or-gemma → current switch: bounded GLM thinking is not suppressed."""
    monkeypatch.setenv("ARENAMCP_SELECTIVE_THINKING", "1")
    from tests.proxy_fakes import make_backend, ok_stream

    backend = make_backend(
        [ok_stream("ok", model="or-gemma-4-26b"), ok_stream("ok", model="glm-5.3-flash")],
        model="or-gemma-4-26b",
    )
    backend.complete("s", "u", call_class="decision.typed", reasoning_effort="high", selective_thinking=True)
    assert backend._client.calls[0]["extra_body"].get("thinking_token_budget") is None

    backend.model = "current"
    backend.complete("s", "u", call_class="decision.typed", reasoning_effort="high", selective_thinking=True)
    body = backend._client.calls[1]["extra_body"]
    assert body["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": 1}
    assert body["thinking_token_budget"] == 256
