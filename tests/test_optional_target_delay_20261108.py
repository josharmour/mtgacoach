"""Wary Zone Guard: a legal empty selection must not wait for the watchdog."""

from unittest.mock import Mock

import pytest
from tests.test_priority_after_cast_206 import _cast_poll, _engine
from tests.test_reanimation_targets import _state

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.autopilot import AutopilotState
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot


def decision(minimum=0):
    return PendingDecision(
        (120, 170),
        "SelectTargets",
        (DecisionOption("tgt:460", "Target #460"),),
        min_select=minimum,
        max_select=1,
        can_cancel=False,
        source_label="Wary Zone Guard ability",
        slots=(TargetSlot(1, minimum, 1, 0, (460,)),),
    )


def planner(answer):
    result = ActionPlanner.__new__(ActionPlanner)
    result._llm_decision_options = Mock(return_value=answer)
    result.targeting_fallback_choice = Mock(return_value=[DECLINE_DECISION])
    return result


def test_explicit_optional_empty_answer_is_not_a_failed_model_answer():
    subject = planner([])
    assert subject.plan_decision_options(decision(), _state()) == []
    subject.targeting_fallback_choice.assert_not_called()
    assert subject._last_decision_trace["target_validation"] == "no_targets"


@pytest.mark.parametrize("answer,minimum", [([], 1), (["tgt:999"], 0)])
def test_invalid_or_required_empty_answers_still_use_guarded_fallback(answer, minimum):
    subject = planner(answer)
    assert subject.plan_decision_options(decision(minimum), _state()) == [DECLINE_DECISION]
    subject.targeting_fallback_choice.assert_called_once()


def test_successful_outer_cast_does_not_clear_followup_manual_pause():
    engine = _engine([_cast_poll(), _cast_poll()], [["idx:0"]])

    def pause(*args):
        engine._state = AutopilotState.PAUSED

    engine._surface_post_cast_priority = pause
    assert engine._try_typed_decision_path({}, "decision_required") is True
    assert engine.state == AutopilotState.PAUSED


def test_optional_empty_answer_is_submitted_without_manual_handoff():
    poll = {
        "has_pending": True,
        "request_type": "SelectTargets",
        "game_state_id": 120,
        "msg_id": 170,
        "can_cancel": False,
        "target_candidates": [{"targetInstanceId": 460}],
        "target_selections": [
            {"targetIdx": 1, "minTargets": 0, "maxTargets": 1, "targets": [{"targetInstanceId": 460}]}
        ],
    }
    engine = _engine([poll, poll], [[]])
    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._gre_bridge.submit_targets.assert_called_once_with([])
    assert engine.state != AutopilotState.PAUSED
