"""Cityscape Leveler must not mistake our Vorinclex for an opposing target."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision

EFFECT = (
    "When you cast this spell and whenever this creature attacks, destroy up to one target "
    "nonland permanent. Its controller creates a tapped Powerstone token."
)


def state():
    return {
        "local_seat_id": 2,
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1}],
        "battlefield": [
            {
                "instance_id": 611,
                "name": "Vorinclex",
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "power": 6,
            },
            {
                "instance_id": 450,
                "name": "Tifa Lockhart",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "power": 1,
            },
            {
                "instance_id": 712,
                "name": "Cityscape Leveler",
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "oracle_text": EFFECT,
            },
        ],
        "stack": [{"instance_id": 714, "name": "Leveler trigger", "parent_instance_id": 712}],
        "decision_context": {"type": "target_selection", "source_id": 714},
    }


def decision():
    return PendingDecision(
        (10, 1),
        "SelectTargets",
        (
            DecisionOption("tgt:611", "Target #611"),
            DecisionOption("tgt:450", "Target #450"),
        ),
    )


def planner(response):
    result = ActionPlanner.__new__(ActionPlanner)
    result._timeout = 1
    result._backend = Mock()
    result._backend.complete.return_value = json.dumps(response)
    return result


def test_reported_opponent_intent_cannot_destroy_own_vorinclex():
    p = planner(
        {
            "option_ids": ["tgt:611"],
            "target_controllers": {"tgt:611": "opponent"},
            "reasoning": "Destroy the opponent's Vorinclex.",
        }
    )
    assert p.plan_decision_options(decision(), state()) == [DECLINE_DECISION]
    prompt = p._backend.complete.call_args.args[1]
    assert "YOU ARE SEAT 2" in prompt
    assert "tgt:611: Vorinclex (YOURS)" in prompt
    assert '"controller_seat_id": 2, "owner_seat_id": 2' in prompt
    assert "tgt:450: Tifa Lockhart (opponent's)" in prompt
    assert EFFECT.lower() in prompt
    trace = p.get_last_decision_trace()
    assert trace["selected_ids"] == ["tgt:611"]
    assert trace["validated_ids"] == []
    assert trace["target_validation"] == "declined"
    assert trace["targets"][0]["controller_seat_id"] == 2
    assert p._backend.complete.call_count == 1


def test_old_response_without_controller_acknowledgment_uses_existing_safe_opponent_fallback():
    p = planner({"option_ids": ["tgt:611"], "reasoning": "Destroy the opponent's Vorinclex."})
    assert p.plan_decision_options(decision(), state()) == ["tgt:450"]
    assert p.get_decision_reasoning(["tgt:450"]) == ""


@pytest.mark.parametrize("missing", ["source", "controller", "local_seat"])
def test_missing_ground_truth_cannot_be_filled_by_model_ownership_claim(missing):
    gs = state()
    if missing == "source":
        gs["decision_context"]["source_id"] = 999
        gs["stack"] = [{"instance_id": 888, "oracle_text": "Target creature gains flying."}]
    elif missing == "controller":
        gs["battlefield"][0].pop("controller_seat_id")  # Owner remains us, control is not known.
    else:
        gs.pop("local_seat_id")
        gs["players"] = []
    p = planner({"option_ids": ["tgt:611"], "target_controllers": {"tgt:611": "opponent"}})
    assert p.plan_decision_options(decision(), gs) == [DECLINE_DECISION]


def test_explicit_self_removal_with_concrete_benefit_is_not_globally_forbidden():
    p = planner(
        {
            "option_ids": ["tgt:611"],
            "target_controllers": {"tgt:611": "self"},
            "unusual_target_reasons": {"tgt:611": "Destroy my creature to trigger its death ability."},
        }
    )
    assert p.plan_decision_options(decision(), state()) == ["tgt:611"]


def test_stolen_creature_uses_controller_instead_of_original_owner():
    gs = state()
    gs["battlefield"][0]["controller_seat_id"] = 1
    p = planner({"option_ids": ["tgt:611"], "target_controllers": {"tgt:611": "opponent"}})
    assert p.plan_decision_options(decision(), gs) == ["tgt:611"]


def test_known_mixed_effect_allows_explicit_per_id_control_for_both_sides():
    gs = state()
    gs["stack"][0]["oracle_text"] = "Target creature you control fights target creature an opponent controls."
    pending = PendingDecision((10, 1), "SelectTargets", decision().options, min_select=2, max_select=2)
    p = planner(
        {
            "option_ids": ["tgt:611", "tgt:450"],
            "target_controllers": {"tgt:450": "opponent", "tgt:611": "self"},
        }
    )
    assert p.plan_decision_options(pending, gs) == ["tgt:611", "tgt:450"]
    wrong = deepcopy(gs)
    wrong["battlefield"][0]["controller_seat_id"] = 1
    assert p.plan_decision_options(pending, wrong) == [DECLINE_DECISION]


def test_target_name_resolves_by_instance_when_bridge_omits_group_id():
    pending = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectTargets",
            "source_instance_id": 714,
            "target_candidates": [{"targetInstanceId": 611}],
        },
        resolve_name=lambda gid: "",
        resolve_instance=lambda iid: {611: "Vorinclex", 714: "Cityscape Leveler"}.get(iid, ""),
    )
    assert pending.source_label == "Cityscape Leveler"
    assert pending.options[0].label == "Vorinclex"
    assert pending.options[0].meta["instanceId"] == 611


def test_backend_error_cannot_buff_unknown_controller_based_only_on_ownership():
    gs = state()
    gs["stack"][0]["oracle_text"] = "Target creature gains flying until end of turn."
    gs["battlefield"][0].pop("controller_seat_id")
    pending = PendingDecision((10, 1), "SelectTargets", (decision().options[0],))
    p = planner({})
    p._backend.complete.side_effect = RuntimeError("offline")
    assert p.plan_decision_options(pending, gs) == [DECLINE_DECISION]
