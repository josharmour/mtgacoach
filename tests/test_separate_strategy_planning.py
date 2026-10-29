"""Optional strategy/action separation keeps decisions independent of a running plan."""

import threading
from unittest.mock import Mock

from tests.test_game_plan import FakeBackend, _plan_json, _state
from tests.test_targeting_fallback_decline import _decision
from tests.test_targeting_fallback_decline import _state as target_state

from arenamcp.action_planner import ActionPlanner
from arenamcp.game_plan import BACKGROUND_PRIORITY, GamePlanManager


def test_critical_typed_decision_uses_one_low_call_and_completed_plan(monkeypatch):
    requests = []

    class Backend:
        def complete(self, system, user, max_tokens, **kwargs):
            requests.append((user, kwargs))
            return '{"option_ids":["tgt:812"],"reasoning":"Remove the opposing creature."}'

    planner = ActionPlanner(Backend(), separate_strategy_planning=True)
    planner.set_game_plan("Preserve removal for their flying threat.")
    cancel_plan = Mock()
    planner.set_background_yield(cancel_plan)
    monkeypatch.setattr("arenamcp.action_planner.critical_decision", lambda *a, **k: "within lethal reach")
    choice = planner._llm_decision_options(
        _decision([607, 812]), target_state(own_ids=(607,), their_ids=(812,))
    )
    assert choice == ["tgt:812"]
    assert len(requests) == 1
    assert requests[0][1]["reasoning_effort"] == "low"
    assert "Preserve removal" in requests[0][0]
    cancel_plan.assert_not_called()


def test_action_finishes_while_high_effort_strategy_is_inflight():
    entered, release, updated = threading.Event(), threading.Event(), threading.Event()
    requests = []

    class Backend:
        def complete(self, system, user, max_tokens, **kwargs):
            requests.append(kwargs)
            if kwargs.get("background"):
                entered.set()
                assert release.wait(5)
                return _plan_json()
            return '{"option_ids":["pass"]}'

    backend = Backend()
    manager = GamePlanManager(backend, separate_strategy_planning=True)
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    planner.set_background_yield(manager.yield_for_decision)
    try:
        assert manager.request_reform(_state(turn=3), on_updated=updated.set)
        assert entered.wait(3)
        assert manager.yield_for_decision() is False
        assert not manager._inflight_cancel.is_set()
        assert not manager.request_reform(_state(turn=3))  # no background backlog
        assert (
            planner._call_llm(
                "Return JSON",
                "Choose",
                100,
                timeout_s=2,
                call_class="decision.typed",
                reasoning_effort="high",
            )
            == '{"option_ids":["pass"]}'
        )
        assert not release.is_set()
        assert manager._inflight
        assert requests[0]["reasoning_effort"] == "high"
        assert requests[0]["priority"] == BACKGROUND_PRIORITY
        assert requests[1]["reasoning_effort"] == "low"
    finally:
        release.set()
        assert updated.wait(3)


def test_default_mode_keeps_initial_quick_plan():
    backend = FakeBackend([_plan_json()])
    manager = GamePlanManager(backend)
    assert manager.maybe_reform(_state(turn=3)) is not None
    assert backend.requests[0][2]["reasoning_effort"] == "low"


def test_critical_combat_does_not_launch_a_high_effort_hedge(monkeypatch):
    requests = []

    class Backend:
        def complete(self, *args, **kwargs):
            requests.append(kwargs)
            return '{"actions": [], "overall_strategy": "Hold."}'

    planner = ActionPlanner(Backend(), land_drop_first=False, separate_strategy_planning=True)
    monkeypatch.setattr("arenamcp.action_planner.declaration_effort", lambda *a: ("high", "possible lethal"))
    hedge = Mock(side_effect=AssertionError("must not race a second action call"))
    monkeypatch.setattr(planner, "_hedged_declaration", hedge)
    planner.plan_actions(
        {"turn": {"turn_number": 2}, "battlefield": [], "players": []},
        "decision_required",
        ["Attack with: Spider", "Done"],
        {"type": "declare_attackers"},
    )
    hedge.assert_not_called()
    assert requests
    assert all(request["reasoning_effort"] == "low" for request in requests)
