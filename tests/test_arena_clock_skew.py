"""Windows bug_20261104_193402: clock skew blocked a commander return prompt."""

from unittest.mock import Mock

import pytest
from tests.test_arena_model_budget_327 import state_with_clock

from arenamcp.action_planner import ActionPlanner
from arenamcp.decision_clock import (
    decision_seconds_remaining,
    model_deadline,
    seconds_remaining,
    wrap_up_reason,
)
from arenamcp.decisions import build_pending_decision


@pytest.mark.parametrize("age", [3248, -3248, 301, -6, float("nan"), float("inf")])
@pytest.mark.parametrize("active", [1, 2])
def test_untrusted_clock_cannot_force_a_deadline_or_turn_wrap_up(age, active):
    state = state_with_clock(61, age=age, active=active)
    assert seconds_remaining(state["timer_state"], 1) is None
    assert decision_seconds_remaining(state) is None
    assert model_deadline(state) is None
    assert wrap_up_reason(state["timer_state"], 1, 4, active) == ""


@pytest.mark.parametrize("age,expected", [(10, 51), (65, -4), (-1, 61)])
def test_normal_countdown_and_real_expiry_still_apply(age, expected):
    state = state_with_clock(61, age=age)
    assert decision_seconds_remaining(state) == pytest.approx(expected, abs=0.1)
    assert bool(wrap_up_reason(state["timer_state"], 1, 4, 1)) == (expected <= 20)


def test_explicit_timeout_still_wraps_up_with_skewed_timestamp():
    state = state_with_clock(61, age=3248)
    state["timer_state"][1]["timed_out_turn"] = 4
    assert "already ran out" in wrap_up_reason(state["timer_state"], 1, 4, 1)


def test_old_paused_rope_cannot_force_wrap_up():
    state = state_with_clock(0, age=3248, running=False)
    assert wrap_up_reason(state["timer_state"], 1, 4, 1) == ""


def test_commander_return_reaches_model_despite_vm_clock_skew(monkeypatch):
    state = state_with_clock(61, age=3248)
    state["decision_context"] = {"type": "optional_action"}
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "OptionalAction",
            "game_state_id": 315,
            "msg_id": 470,
            "prompt": {"promptId": 144},
            "optional_mechanics": ["ZoneTransfer"],
            "optional_recipients": [540],
        },
        resolve_instance=lambda iid: "Leonardo, Big Brother",
    )
    backend = Mock()
    backend.complete.return_value = (
        '{"option_ids": ["optional:accept"], "reasoning": "Return the commander for future access."}'
    )
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    monkeypatch.setattr(planner, "_yield_background", lambda: True)
    assert planner.plan_decision_options(decision, state) == ["optional:accept"]
    backend.complete.assert_called_once()
