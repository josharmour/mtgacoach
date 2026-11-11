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
    """Minimal stand-in for ProxyBackend: only the attributes the settings read."""

    def __init__(self, model="current", served=None):
        self.model = model
        self.last_served_model = served


def _efforts(backend):
    """Effort the table dictates per call class, from a 'high' request."""
    return {
        "decision.typed": effort_for_backend(backend, "decision.typed", "high"),
        "background.game_plan": effort_for_backend(backend, "background.game_plan", "high"),
        "background.deck_playbook": effort_for_backend(backend, "background.deck_playbook", "high"),
        "background.commentary": effort_for_backend(backend, "background.commentary", "high"),
        "probe": effort_for_backend(backend, "probe", "high"),
    }


GLM_BEFORE = {"decision.typed": "none", "background.game_plan": "low",
              "background.deck_playbook": "none", "background.commentary": "none",
              "probe": "none"}


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
