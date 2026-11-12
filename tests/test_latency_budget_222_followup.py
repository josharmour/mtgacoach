"""Remaining Coach-side latency after #222: declaration races and reform retries.

Historical signatures: #265/#277/#285/#286 wait for high to expire before
using low; #141/#223 repeatedly lose first plans while reforms occupy the lane.
These are deterministic reproductions, not live-service latency measurements.
"""

import json
import threading
import time
from unittest.mock import Mock

import pytest
from tests.test_game_plan import FakeBackend, _plan_json, _state

from arenamcp.action_planner import ActionPlanner, ActionType
from arenamcp.game_plan import GamePlanManager


def test_ready_low_declaration_cancels_high_before_its_budget_expires():
    planner = ActionPlanner(Mock(), timeout=2)
    high_started = threading.Event()
    high_cancelled = threading.Event()
    returned = threading.Event()
    result = []

    def complete(call_class, effort, *, cancel_event):
        if effort == "high":
            high_started.set()
            if cancel_event.wait(3):
                high_cancelled.set()
            raise TimeoutError("high exhausted its budget")
        assert high_started.wait(1)
        return "ready low answer"

    def run():
        try:
            result.append(planner._hedged_declaration(complete))
        finally:
            returned.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert returned.wait(0.8), "a ready declaration waited out the high-effort budget"
        assert result == ["ready low answer"]
        assert high_cancelled.wait(0.8), "losing model work must stop promptly"
    finally:
        worker.join(4)


@pytest.mark.parametrize("low_answer", ["not json", '{"actions":[{"action_type":"pass_priority"}]}'])
def test_fast_invalid_declaration_does_not_displace_the_valid_other_leg(monkeypatch, low_answer):
    checked_low = threading.Event()
    calls = []
    high_answer = json.dumps({"actions": [{"action_type": "declare_attackers", "attacker_names": []}]})

    class Backend:
        def complete(self, *args, **kwargs):
            calls.append(kwargs)
            if kwargs.get("reasoning_effort") == "high":
                assert checked_low.wait(2), "must inspect fast low before accepting it"
                return high_answer
            return low_answer

    planner = ActionPlanner(Backend(), timeout=2, land_drop_first=False)
    parse = planner._parse_response

    def checked_parse(answer, *args, **kwargs):
        try:
            return parse(answer, *args, **kwargs)
        finally:
            if answer == low_answer:
                checked_low.set()

    monkeypatch.setattr(planner, "_parse_response", checked_parse)
    monkeypatch.setattr("arenamcp.action_planner.declaration_effort", lambda *a: ("high", "danger"))
    plan = planner.plan_actions(
        _state(turn=3),
        "decision_required",
        ["Attack with: Spider", "Done"],
        {"type": "declare_attackers"},
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type is ActionType.DECLARE_ATTACKERS
    assert len(calls) == 2, "a valid hedge answer must avoid an unnecessary repair call"


def test_both_unusable_answers_are_retained_for_the_bounded_repair():
    planner = ActionPlanner(Mock(), timeout=2)
    assert (
        planner._hedged_declaration(
            lambda call_class, effort, **kwargs: f"unusable {effort}",
            usable=lambda answer: False,
        )
        == "unusable high"
    )


def test_both_failed_declarations_surface_the_high_error():
    planner = ActionPlanner(Mock(), timeout=2)

    def fail(call_class, effort, **kwargs):
        raise ValueError(f"failed {effort}")

    with pytest.raises(ValueError, match="failed high"):
        planner._hedged_declaration(fail)


def test_expired_declaration_deadline_does_not_launch_either_leg():
    planner = ActionPlanner(Mock(), timeout=20)
    complete = Mock()
    with pytest.raises(TimeoutError):
        planner._hedged_declaration(complete, deadline=time.monotonic() - 1)
    complete.assert_not_called()


def test_shorter_declaration_deadline_cancels_both_legs():
    planner = ActionPlanner(Mock(), timeout=20)
    stopped = {"high": threading.Event(), None: threading.Event()}

    def complete(call_class, effort, *, cancel_event):
        assert cancel_event.wait(2)
        stopped[effort].set()
        raise TimeoutError("cancelled")

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        planner._hedged_declaration(complete, deadline=started + 0.1)
    assert time.monotonic() - started < 1
    assert all(event.wait(1) for event in stopped.values())


@pytest.mark.parametrize("spent", [4.0, 7.0, 10.0, 11.0])
def test_strategy_json_retry_shares_the_original_reform_budget(monkeypatch, spent):
    clock = [100.0]
    budgets = []
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: clock[0])
    # This regression only tests admission/budgeting. Keep diagnostic writes
    # out of the user's runtime evidence directory.
    monkeypatch.setattr("arenamcp.game_plan.save_unusable_answer", lambda *a, **k: None)

    class Backend:
        def complete(self, *args, **kwargs):
            budgets.append(kwargs["request_timeout_s"])
            if len(budgets) == 1:
                clock[0] += spent
                return "malformed strategy"
            return _plan_json()

    manager = GamePlanManager(Backend(), timeout=10)
    plan = manager.maybe_reform(_state(turn=3))
    if spent <= 5:
        assert budgets == [10.0, 10.0 - spent]
        assert plan is not None
    else:
        assert budgets == [10.0], "insufficient remaining budget must not start another model request"
        assert plan is None


def test_long_failed_reform_cools_down_from_completion(monkeypatch, plan_threads):
    clock = [100.0]
    calls = []
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: clock[0])

    class Backend:
        def complete(self, *args, **kwargs):
            calls.append(kwargs)
            clock[0] += 20
            return "[BACKEND ERROR] LLM streaming time budget exhausted"

    manager = GamePlanManager(Backend(), timeout=20)
    state = _state(turn=3)
    assert manager.request_reform(state)
    plan_threads[-1].join(2)
    assert not manager._inflight
    assert not manager.request_reform(state), "long failure must not immediately occupy the lane again"
    assert len(calls) == 1
    clock[0] += manager._REFRESH_INTERVAL_S
    assert manager.request_reform(state)
    plan_threads[-1].join(2)
    assert len(calls) == 2


@pytest.mark.parametrize("model", ["current", "deepseek-v4.1-flash-local", "dsv4-local"])
@pytest.mark.parametrize("separate", [False, True])
def test_hosted_opening_and_routine_next_turn_plans_both_use_fast_calls(model, separate):
    # 650ae555: low effort still generated 12,439 reasoning characters and
    # no content in 45.3s, then the first plan was cancelled. Fast initial
    # requests must actually disable that hidden think, not just say "low".
    backend = FakeBackend([_plan_json(), _plan_json("updated line")])
    backend.model = model
    manager = GamePlanManager(backend, separate_strategy_planning=separate)
    first = manager.maybe_reform(_state(turn=3))
    options = backend.requests[0][2]
    assert first is not None and first.effort == "none"
    assert options["reasoning_effort"] == "none"
    assert 0 < options["request_timeout_s"] <= 20
    assert options["background"] is True
    next_state = _state(turn=4)
    next_state["turn"]["active_player"] = 2
    assert manager.maybe_reform(next_state, force=True) is not None
    refresh = backend.requests[1][2]
    assert refresh["reasoning_effort"] == "none"
    assert 0 < refresh["request_timeout_s"] <= 20


def test_first_hosted_plan_repair_keeps_fast_mode_and_shared_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: clock[0])

    class Backend(FakeBackend):
        model = "current"

        def complete(self, *args, **kwargs):
            clock[0] += 4
            return super().complete(*args, **kwargs)

    backend = Backend(["malformed", _plan_json()])
    manager = GamePlanManager(backend, timeout=10, separate_strategy_planning=True)
    plan = manager.maybe_reform(_state(turn=3))
    assert plan is not None and plan.effort == "none"
    assert [r[2]["reasoning_effort"] for r in backend.requests] == ["none", "none"]
    assert [r[2]["request_timeout_s"] for r in backend.requests] == [10, 6]


def test_fast_initial_plan_reaches_proxy_without_hidden_reasoning(monkeypatch):
    from arenamcp.backends.proxy import ProxyBackend

    backend = ProxyBackend(
        model="current", base_url="http://127.0.0.1:9/v1", api_key="test", enable_thinking=True
    )
    monkeypatch.setattr(backend, "_get_client", lambda: Mock())
    requests = []

    def complete_once(client, params, **kwargs):
        requests.append((params, kwargs["call"].budget_s))
        return _plan_json()

    monkeypatch.setattr(backend, "_complete_once", complete_once)
    manager = GamePlanManager(backend, separate_strategy_planning=True)
    assert manager.maybe_reform(_state(turn=3)) is not None
    assert requests[0][0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": False,
        "reasoning_effort": "none",
    }
    assert requests[0][1] <= 20
    assert manager.maybe_reform(_state(turn=4), force=True) is not None
    assert requests[1][0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }
    assert requests[1][1] > 20
    assert backend.enable_thinking is True  # per-request only
