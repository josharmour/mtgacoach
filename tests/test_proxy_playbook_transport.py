"""Q3 rework (owner-approved proxy scope, 2026-11-11): integration coverage at
the real ProxyBackend boundary for the playbook call options.

These tests drive ``ProxyBackend.complete`` itself — only ``_complete_once`` is
stubbed, so the budget policy, the effort aliasing and ``_build_params`` all
run for real — and record what actually goes over the wire for the idle
(thinking) and in-match (compact) playbook discovery passes, exactly as
``coach._playbook_call_options`` + ``accepted_call_kwargs`` pass them.

The idle discovery pass must reach the wire as thinking=True/effort=high with a
300 s budget: proxy.py honours the caller for ``background.deck_playbook`` when
it explicitly asks for thinking, and lets that class's timeout raise the 120 s
background default up to the 300 s cap. The in-match control must stay
none/1536/60, and every other call class keeps the alias policy and the old
budget rule.
"""

import json
import threading

from arenamcp.backends.proxy import BACKGROUND_PRIORITY, ProxyBackend


def _capture_complete_once(backend):
    captured = {}

    def fake_complete_once(client, params, *, request_started=None, deadline=None, attempt=1, call=None):
        captured["params"] = params
        captured["call"] = call
        return json.dumps({"ok": True})

    backend._complete_once = fake_complete_once
    return captured


def _chat_template_kwargs(params):
    return ((params.get("extra_body") or {}).get("chat_template_kwargs")) or {}


def _run_pass(options, call_class="background.deck_playbook", *, idle=True, model="current"):
    """Drive one real complete() with the labels coach.py attaches."""
    backend = ProxyBackend(model=model, base_url="http://127.0.0.1:1")
    captured = _capture_complete_once(backend)
    cancel = threading.Event()
    backend.complete(
        "system",
        "user",
        temperature=0.0,
        background=True,
        idle_playbook=idle,
        call_class=call_class,
        priority=BACKGROUND_PRIORITY,
        cancel_event=cancel,
        **options,
    )
    return captured["params"], captured["call"]


def test_idle_playbook_discovery_pass_reaches_the_wire_as_thinking_high_300s():
    """The caller's idle request (thinking/high/12000/300s) is honoured."""
    from arenamcp.coach import _playbook_call_options

    options = _playbook_call_options(ProxyBackend(model="current"), discovery=True, outside_match=True)
    assert options == {
        "enable_thinking": True,
        "reasoning_effort": "high",
        "max_tokens": 12000,
        "request_timeout_s": 300.0,
    }

    params, call = _run_pass(options)
    assert _chat_template_kwargs(params) == {"thinking": True, "reasoning_effort": "high"}
    assert params["max_tokens"] == 12000
    assert call.budget_s == 300.0
    assert call.call_class == "background.deck_playbook"
    assert call.background is True


def test_in_match_playbook_discovery_pass_transport_is_compact():
    """The in-match control must stay none/1536/60 at the same boundary."""
    from arenamcp.coach import _playbook_call_options

    options = _playbook_call_options(ProxyBackend(model="current"), discovery=True, outside_match=False)
    assert options == {
        "enable_thinking": False,
        "reasoning_effort": "none",
        "max_tokens": 1536,
        "request_timeout_s": 60.0,
    }

    params, call = _run_pass(options)
    assert _chat_template_kwargs(params) == {"thinking": False, "reasoning_effort": "none"}
    assert params["max_tokens"] == 1536
    assert call.budget_s == 60.0


def test_idle_playbook_compile_pass_keeps_the_alias_policy_and_120s_budget():
    """Only the thinking discovery pass is exempt; the compile pass is not.

    The compile pass passes enable_thinking=False, so the alias policy still
    decides (none) and its 120 s request cannot raise the background default.
    """
    from arenamcp.coach import _playbook_call_options

    options = _playbook_call_options(ProxyBackend(model="current"), discovery=False, outside_match=True)
    assert options["enable_thinking"] is False
    assert options["request_timeout_s"] == 120.0

    params, call = _run_pass(options)
    assert _chat_template_kwargs(params) == {"thinking": False, "reasoning_effort": "none"}
    assert call.budget_s == 120.0


def test_playbook_budget_cap_bounds_the_caller_timeout():
    """Above the cap the timeout is clamped to 300 s; below the default it wins."""
    _, call_low = _run_pass({"enable_thinking": True, "reasoning_effort": "high", "request_timeout_s": 90.0})
    assert call_low.budget_s == 90.0

    params_high, call_high = _run_pass(
        {"enable_thinking": True, "reasoning_effort": "high", "request_timeout_s": 1000.0}
    )
    assert call_high.budget_s == 300.0
    assert _chat_template_kwargs(params_high) == {"thinking": True, "reasoning_effort": "high"}


def test_other_background_classes_keep_the_alias_policy_and_120s_cap():
    """The exemption is keyed on background.deck_playbook only."""
    from arenamcp.backends.proxy import _alias_effort

    # Sanity: the alias policy still applies to a sibling class.
    assert _alias_effort("current", "background.game_plan", "high") == "low"

    params, call = _run_pass(
        {"enable_thinking": True, "reasoning_effort": "high", "request_timeout_s": 300.0},
        call_class="background.game_plan",
    )
    assert _chat_template_kwargs(params)["reasoning_effort"] == "low"
    assert call.budget_s == 120.0


def test_live_custom_model_keeps_tactical_ceiling():
    _, call = _run_pass({"request_timeout_s": 300}, idle=False, model="local-qwen")
    assert call.budget_s == 120


def test_idle_preserves_machine_and_outside_model_policy(monkeypatch):
    opts = {"enable_thinking": True, "reasoning_effort": "high", "request_timeout_s": 300}
    params, _ = _run_pass(opts, model="or-example")
    assert _chat_template_kwargs(params).get("thinking") is not True
    monkeypatch.setenv("ARENAMCP_THINKING_ROWS", '{"*":{"playbook":"cap_low"}}')
    params, _ = _run_pass(opts)
    assert _chat_template_kwargs(params)["reasoning_effort"] == "low"
