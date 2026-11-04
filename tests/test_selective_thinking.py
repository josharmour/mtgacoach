"""Opt-in transport policy, real planner validation, bounded races and admission."""

import concurrent.futures
import json
import threading
import time
from types import SimpleNamespace

import pytest
from tests.test_lethal_self_damage_guard import position

from arenamcp import selective_thinking as policy
from arenamcp.action_planner import ActionPlanner
from arenamcp.backends.proxy import ProxyBackend
from arenamcp.decisions import DecisionOption, PendingDecision


@pytest.fixture(autouse=True)
def isolated_admission(monkeypatch):
    monkeypatch.setattr(policy, "_THINKER", threading.BoundedSemaphore(1))


@pytest.mark.parametrize(
    "enabled,marked,model,kind,bounded",
    [
        (True, True, "current", "decision.typed", True),
        (True, True, "deepseek-v4.1-flash-local", "decision.typed", True),
        (False, True, "current", "decision.typed", False),
        (True, False, "current", "decision.typed", False),
        (True, True, "current", "background.game_plan", False),
        (True, True, "current", "decision.fast", False),
        (True, True, "glm-5.3-flash", "decision.typed", False),
    ],
)
def test_actual_transport_requires_opt_in_typed_deepseek_leg(
    monkeypatch, enabled, marked, model, kind, bounded
):
    monkeypatch.setenv("ARENAMCP_SELECTIVE_THINKING", str(int(enabled)))
    seen = []
    monkeypatch.setattr(
        ProxyBackend, "_complete_once", lambda self, client, params, **kw: seen.append(params) or "ok"
    )
    be = ProxyBackend(model=model, base_url="http://127.0.0.1:9/v1", api_key="test")
    be.complete(
        "system", "user", call_class=kind, selective_thinking=marked, reasoning_effort="low", max_tokens=2048
    )
    extra = seen[0]["extra_body"]
    assert (extra.get("thinking_token_budget") == 256) is bounded
    assert seen[0]["max_tokens"] == 2048
    if bounded:
        assert extra["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": 1}
    elif model == "current" and kind.startswith("decision."):
        assert extra["chat_template_kwargs"] == {"thinking": False, "reasoning_effort": "none"}
    elif kind == "background.game_plan" or model.startswith("glm"):
        assert extra["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "low"}


def parsed(text):
    if text == "invalid":
        raise ValueError("unsafe answer")
    return {"reasoning": text}, [text]


def test_fast_valid_answer_cancels_thinker_and_returns_without_grace():
    thinking_started = threading.Event()
    thinking_cancelled = threading.Event()

    def ask(message, *, effort, budget, cancel_event):
        if effort:
            thinking_started.set()
            if cancel_event.wait(2):
                thinking_cancelled.set()
            raise TimeoutError()
        assert thinking_started.wait(1)
        return "fast"

    start = time.monotonic()
    trace = {}
    assert policy.race(ask, parsed, "same", deadline=start + 1, trace=trace)[1] == ["fast"]
    assert time.monotonic() - start < 0.5
    assert thinking_cancelled.wait(0.5)
    assert trace["selected_leg"] == "fast"


def test_rejected_fast_allows_valid_thinker_without_restarting_deadline():
    fast_finished = threading.Event()
    budgets = []

    def ask(message, *, effort, budget, cancel_event):
        budgets.append(budget)
        if effort:
            assert fast_finished.wait(1)
            return "thinking"
        return "invalid"

    def parse(text):
        try:
            return parsed(text)
        finally:
            if text == "invalid":
                fast_finished.set()

    trace = {}
    assert policy.race(ask, parse, "same", deadline=time.monotonic() + 1, trace=trace)[1] == ["thinking"]
    assert trace["replaced_rejected_fast"]
    assert all(0 < b <= 1 for b in budgets)


def test_cancelled_stalled_thinker_retains_slot_until_worker_exits():
    release = threading.Event()
    started = threading.Event()
    exited = threading.Event()

    def stalled(message, *, effort, budget, cancel_event):
        if effort:
            started.set()
            release.wait(2)
            exited.set()
        else:
            assert started.wait(1)
        return "first"

    try:
        policy.race(stalled, parsed, "same", deadline=time.monotonic() + 1, trace={})
        calls = []
        trace = {}
        policy.race(
            lambda m, **kw: calls.append(kw) or "next",
            parsed,
            "same",
            deadline=time.monotonic() + 1,
            trace=trace,
        )
        assert len(calls) == 1 and calls[0]["effort"] is None
        assert trace["admitted"] is False
    finally:
        release.set()
        assert exited.wait(1)


@pytest.mark.parametrize("superseded", [False, True])
def test_expired_or_superseded_decision_launches_nothing(superseded):
    cancel = threading.Event()
    if superseded:
        cancel.set()

    def ask(*a, **kw):
        pytest.fail("expired decision launched a request")

    with pytest.raises((TimeoutError, concurrent.futures.CancelledError)):
        policy.race(
            ask,
            parsed,
            "same",
            deadline=time.monotonic() + (1 if superseded else -1),
            trace={},
            cancel_event=cancel,
        )


def test_running_deadline_cancels_both_requests_without_waiting_for_transport():
    events = []

    def ask(message, *, effort, budget, cancel_event):
        events.append(cancel_event)
        cancel_event.wait(1)
        raise TimeoutError()

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        policy.race(ask, parsed, "same", deadline=start + 0.1, trace={})
    assert time.monotonic() - start < 0.5
    assert len(events) == 2 and all(e.is_set() for e in events)


@pytest.mark.parametrize("separate", [False, True])
def test_real_planner_rejects_fast_self_harm_and_marks_only_thinker(monkeypatch, separate):
    monkeypatch.setenv("ARENAMCP_SELECTIVE_THINKING", "1")
    fast_finished = threading.Event()
    calls = []

    class Backend:
        model = "current"

        def complete(self, system, user, *args, **kw):
            calls.append((system, user, kw))
            if kw.get("selective_thinking"):
                assert fast_finished.wait(1)
                time.sleep(0.02)
                return json.dumps(
                    {
                        "option_ids": ["tgt:1"],
                        "reasoning": "Deal 2 damage to opponent.",
                        "target_controllers": {"tgt:1": "opponent"},
                    }
                )
            fast_finished.set()
            return json.dumps(
                {
                    "option_ids": ["tgt:287"],
                    "reasoning": "Pump saves Slickshot from Shock.",
                    "target_controllers": {"tgt:287": "self"},
                    "unusual_target_reasons": {"tgt:287": "Pump saves Slickshot."},
                }
            )

    planner = ActionPlanner(Backend(), timeout=12, separate_strategy_planning=separate)
    monkeypatch.setattr(planner, "_yield_background", lambda: True)
    state = position()
    state["turn"] = {"turn_number": 4, "active_player": 2, "priority_player": 2, "phase": "Phase_Main1"}
    for p in state["players"]:
        p["life_total"] = 20
    decision = PendingDecision(
        (10, 11),
        "SelectTargets",
        (DecisionOption("tgt:287", "Slickshot"), DecisionOption("tgt:1", "Opponent")),
        source_label="Burst Lightning",
    )
    assert planner._llm_decision_options(decision, state) == ["tgt:1"]
    trace = planner._last_decision_trace["selective_thinking"]
    assert trace["selected_leg"] == "thinking" and trace["replaced_rejected_fast"]
    assert len(calls) == 2
    assert calls[0][:2] == calls[1][:2]
    assert sum(bool(c[2].get("selective_thinking")) for c in calls) == 1
    assert all(c[2]["request_timeout_s"] <= 12 for c in calls)


def test_routine_choices_do_not_qualify():
    for kind in ("Mulligan", "SelectN", "CastingTimeOptions"):
        assert not policy.selection_reason(SimpleNamespace(request_type=kind), {}, "critical")
    d = PendingDecision((1, 2), "ActionsAvailable", (DecisionOption("idx:0", "Pass"),))
    assert not policy.selection_reason(d, position(), "")


@pytest.mark.parametrize(
    "suppression", ["degraded", "background_busy", "opponent_turn", "short_clock", "disabled"]
)
def test_real_planner_skips_extra_leg_when_ineligible(monkeypatch, suppression):
    monkeypatch.setenv("ARENAMCP_SELECTIVE_THINKING", "0" if suppression == "disabled" else "1")
    calls = []

    class Backend:
        model = "current"

        def complete(self, *args, **kwargs):
            calls.append(kwargs)
            return '{"option_ids":["tgt:1"],"reasoning":"Deal 2 damage to opponent.","target_controllers":{"tgt:1":"opponent"}}'

    planner = ActionPlanner(Backend(), timeout=12, separate_strategy_planning=True)
    monkeypatch.setattr(planner, "_yield_background", lambda: suppression != "background_busy")
    monkeypatch.setattr("arenamcp.action_planner.llm_degraded", lambda b: suppression == "degraded")
    if suppression == "short_clock":
        monkeypatch.setattr("arenamcp.decision_clock.model_deadline", lambda state: time.monotonic() + 5)
    state = position()
    state["turn"] = {
        "turn_number": 4,
        "active_player": 1 if suppression == "opponent_turn" else 2,
        "priority_player": 2,
        "phase": "Phase_Main1",
    }
    for p in state["players"]:
        p["life_total"] = 20
    d = PendingDecision(
        (10, 11),
        "SelectTargets",
        (DecisionOption("tgt:287", "Slickshot"), DecisionOption("tgt:1", "Opponent")),
        source_label="Burst Lightning",
    )
    assert planner._llm_decision_options(d, state) == ["tgt:1"]
    assert calls and not any(c.get("selective_thinking") for c in calls)


def test_request_change_cancels_both_running_legs():
    external = threading.Event()
    both_started = threading.Barrier(3)
    events = []

    def ask(message, *, effort, budget, cancel_event):
        events.append(cancel_event)
        both_started.wait(timeout=1)
        external.set()
        cancel_event.wait(1)
        raise TimeoutError()

    result = []

    def run():
        try:
            policy.race(ask, parsed, "same", deadline=time.monotonic() + 2, trace={}, cancel_event=external)
        except concurrent.futures.CancelledError:
            result.append("cancelled")

    worker = threading.Thread(target=run)
    worker.start()
    both_started.wait(timeout=1)
    worker.join(0.5)
    assert result == ["cancelled"]
    assert all(e.is_set() for e in events)
