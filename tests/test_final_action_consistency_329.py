"""Retained 1f24194a menu/reason, bounded state for final decision bookkeeping.

This is not a complete game replay or evidence that passing beats Hydra X=1.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, SecondLookError
from arenamcp.decisions import DecisionOption, PendingDecision


def retained():
    record = json.loads((Path(__file__).parent / "fixtures/live_329/1f24194a-hydra.json").read_text())
    raw = record["pending_decision"]
    decision = PendingDecision(
        **{
            **raw,
            "request_id": tuple(raw["request_id"]),
            "options": tuple(DecisionOption(**o) for o in raw["options"]),
        }
    )
    state = {
        "match_id": "1f24194a",
        "local_seat_id": 1,
        "turn": record["turn"],
        "players": [{"seat_id": 1, "is_local": True, "life_total": 25}, {"seat_id": 2, "life_total": 23}],
        "hand": [
            {
                "name": "Goldvein Hydra",
                "instance_id": 249,
                "grp_id": 90513,
                "type_line": "Creature — Hydra",
                "mana_cost": "{X}{G}",
                "controller_seat_id": 1,
            }
        ],
        "battlefield": [],
        "stack": [],
    }
    return decision, state, record["trace"]["reasoning"]


def test_final_boundary_aligns_ids_trace_cache_memory_and_intent(monkeypatch):
    d, s, reason = retained()
    planner = ActionPlanner(backend=Mock())

    def contradictory_answer(*args):
        planner._last_decision_reasoning = reason
        planner._last_decision_trace = {"selected_ids": ["idx:2"], "reasoning": reason}
        planner._last_llm_asked = True
        planner._last_decision_option_ids = ["idx:2"]
        return ["idx:2"]

    # This exercises the common final boundary, whether the internal answer
    # came from a model, cache, fallback or an action repair.
    monkeypatch.setattr(planner, "_plan_decision_options", contradictory_answer)
    assert planner.plan_decision_options(d, s) == ["pass"]
    trace = planner.get_last_decision_trace()
    final_reason = planner.get_decision_reasoning(["pass"])
    assert trace["selected_ids"] == planner._last_decision_option_ids == ["pass"]
    assert trace["reasoning"] == final_reason != reason
    assert trace["consistency_guard"]["original_ids"] == ["idx:2"]
    assert trace["consistency_guard"]["original_reasoning"] == reason
    assert planner._answer_cache["chosen"] == ["pass"]
    assert planner._answer_cache["reasoning"] == final_reason
    assert planner._cast_intent is None
    assert final_reason in planner.recent_decisions_text()
    assert "Casting Goldvein Hydra" not in planner.recent_decisions_text()
    assert planner._bound_decision_evidence[1]["trace"]["reasoning"] == final_reason


def test_reused_contradictory_answer_is_invalidated_before_next_window(monkeypatch):
    d, s, reason = retained()
    planner = ActionPlanner(backend=Mock())
    planner._last_llm_asked = True
    planner._cache_answer(d, s, ["idx:2"], reason)
    planner._last_llm_asked = False
    monkeypatch.setattr(planner, "_plan_decision_options", planner._reusable_answer)
    assert planner.plan_decision_options(d, s) == ["pass"]
    assert planner._answer_cache is None
    assert planner.get_last_decision_trace()["policy"] == "reused_answer"
    assert planner.get_last_decision_trace()["selected_ids"] == ["pass"]
    assert planner._cast_intent is None


@pytest.mark.parametrize(
    "chosen,reason",
    [
        (["pass"], "Pass to retain Hydra for a larger X."),
        (["idx:2"], "Cast Goldvein Hydra at X=1 now for an immediate body."),
    ],
)
def test_one_recheck_preserves_either_consistent_legal_alternative(chosen, reason):
    d, s, original = retained()
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    ask = Mock(return_value={"option_ids": chosen, "reasoning": reason})
    data, picks = planner._recheck_action_claim(
        d,
        s,
        {"reasoning": original},
        ["idx:2"],
        ask,
        lambda data: (data, data["option_ids"]),
        "prompt",
    )
    assert picks == chosen and data["reasoning"] == reason
    ask.assert_called_once()
    assert planner._last_decision_trace["action_claim_check"]["original_reasoning"] == original


def test_repeated_contradiction_uses_current_board_fallback():
    d, s, reason = retained()
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    ask = Mock(return_value={"option_ids": ["idx:2"], "reasoning": reason})
    with pytest.raises(SecondLookError, match="rationale unresolved"):
        planner._recheck_action_claim(
            d,
            s,
            {"reasoning": reason},
            ["idx:2"],
            ask,
            lambda data: (data, data["option_ids"]),
            "prompt",
        )
    assert planner._last_decision_trace["action_claim_check"]["fallback"] == "current_board"


@pytest.mark.parametrize("pick", ["pass", "idx:2"])
def test_typed_pipeline_rechecks_retained_reason_before_committing(monkeypatch, pick):
    d, s, original = retained()
    final_reason = "Pass to retain Hydra." if pick == "pass" else "Cast Goldvein Hydra for a body now."

    class Backend:
        def complete(self, system, user, *args, **kwargs):
            checked = "ACTION IDENTITY/INTENT CHECK:" in user
            return json.dumps(
                {
                    "option_ids": [pick] if checked else ["idx:2"],
                    "reasoning": final_reason if checked else original,
                }
            )

    planner = ActionPlanner(backend=Backend())
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *args: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *args, **kwargs: "")
    assert planner.plan_decision_options(d, s) == [pick]
    trace = planner.get_last_decision_trace()
    assert trace["action_claim_check"]["original_ids"] == ["idx:2"]
    assert trace["action_claim_check"]["original_reasoning"] == original
    assert trace["action_claim_check"]["final_ids"] == [pick]
    assert planner.get_decision_reasoning([pick]) == final_reason
    assert planner._answer_cache["chosen"] == [pick]
    assert final_reason in planner.recent_decisions_text()
