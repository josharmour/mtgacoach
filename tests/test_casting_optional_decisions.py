from unittest.mock import Mock

import pytest

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner, ActionType
from arenamcp.decisions import build_pending_decision, submit_option
from arenamcp.request_tracker import decision_fingerprint
from arenamcp.target_effects import target_effect_is_harmful


def test_casting_options_keep_actual_modes_and_identity():
    poll = {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "game_state_id": 42,
        "msg_id": 5,
        "actions": [
            {
                "actionType": "CastingTimeOption",
                "choiceKind": "modal",
                "optionIndex": index,
                "childIndex": 0,
                "grpId": 100 + index,
                "label": f"Mode {index + 1}",
            }
            for index in range(2)
        ],
    }
    decision = build_pending_decision(poll)
    assert decision.request_type == "CastingTimeOptions"
    bridge = Mock()
    assert submit_option(bridge, decision, ["idx:1"])
    assert bridge.submit_action_by_index.call_args.args == (1,)
    expected = bridge.submit_action_by_index.call_args.kwargs["expected"]
    assert expected["optionIndex"] == 1
    assert expected["gameStateId"] == 42
    assert expected["childIndex"] == 0


def test_cast_normally_is_not_a_spell_name():
    planner = ActionPlanner.__new__(ActionPlanner)
    assert planner._legal_action_to_action("Cast normally").action_type == ActionType.CASTING_OPTIONS


def test_optional_choice_is_request_bound_and_never_blindly_accepted():
    poll = {"has_pending": True, "request_type": "OptionalAction", "game_state_id": 42, "msg_id": 5}
    decision = build_pending_decision(poll)
    bridge = Mock()
    assert submit_option(bridge, decision, ["optional:accept"])
    bridge.submit_optional.assert_called_once_with(True, expected_request_id=(42, 5))
    assert ActionPlanner.deterministic_option_pick(decision) == [DECLINE_DECISION]
    newer = build_pending_decision({**poll, "msg_id": 6})
    assert decision_fingerprint(newer) != decision_fingerprint(decision)


def test_optional_choice_carries_the_real_source_and_mechanics():
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "OptionalAction",
            "source_instance_id": 42,
            "optional_mechanics": ["Draw"],
            "optional_recipients": [1],
        },
        resolve_instance=lambda identity: "Card draw trigger" if identity == 42 else "",
    )
    assert decision.source_label == "Card draw trigger"
    assert decision.options[0].meta == {"sourceId": 42, "mechanics": ["Draw"], "recipients": [1]}


@pytest.mark.parametrize(
    "rules",
    [
        "Target creature you control deals damage equal to its power to target creature an opponent controls.",
        "Target planeswalker you control deals damage equal to its loyalty to target creature or planeswalker an opponent controls.",
        "Target creature you control fights target creature you don't control.",
    ],
)
def test_two_sided_removal_does_not_reject_its_friendly_source(rules):
    assert target_effect_is_harmful(rules) is None
    assert target_effect_is_harmful("Destroy target creature.") is True


def titan_modes_poll():
    """Report 20261003_234921: choose two; unavailable removal leaves three modes."""
    return {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "game_state_id": 222,
        "msg_id": 310,
        "actions": [
            {
                "actionType": "CastingTimeOption",
                "choiceKind": "modal",
                "childIndex": 0,
                "optionIndex": index,
                "grpId": 149502 + index,
                "min": 2,
                "max": 2,
                "label": label,
            }
            for index, label in enumerate(
                ("Gain 5 life", "Create a 4/4 Rhino", "Put a shield counter on a creature")
            )
        ],
    }


def test_titan_preserves_both_model_choices_and_submits_them_together():
    import json

    poll = titan_modes_poll()
    decision = build_pending_decision(poll)
    assert decision.min_select == decision.max_select == 2
    backend = Mock()
    backend.complete.return_value = json.dumps(
        {"option_ids": ["idx:1", "idx:2"], "reasoning": "Make a Rhino and protect Titan with a shield."}
    )
    planner = ActionPlanner(backend)
    selected = planner.plan_decision_options(decision, {})
    assert selected == ["idx:1", "idx:2"]
    assert "at least 2 and at most 2" in backend.complete.call_args.args[1]
    assert "Rhino" in planner.get_decision_reasoning(selected)
    bridge = Mock()
    assert submit_option(bridge, decision, selected)
    bridge.submit_action_by_index.assert_not_called()
    bridge.submit_casting_options.assert_called_once_with(
        [1, 2],
        expected=[{**poll["actions"][index], "gameStateId": 222, "msgId": 310} for index in (1, 2)],
    )


@pytest.mark.parametrize(
    "ids", [["idx:1"], ["idx:0", "idx:1", "idx:2"], ["idx:1", "idx:1"], ["idx:1", "invalid"]]
)
def test_incomplete_or_invalid_modes_never_become_a_partial_submission(ids):
    decision = build_pending_decision(titan_modes_poll())
    backend = Mock()
    planner = ActionPlanner(backend)
    planner._llm_decision_options = Mock(return_value=ids)
    assert planner.plan_decision_options(decision, {}) == [DECLINE_DECISION]
    bridge = Mock()
    assert not submit_option(bridge, decision, ids)
    assert bridge.mock_calls == []


def test_cannot_mix_done_or_other_child_with_selected_modes():
    poll = titan_modes_poll()
    poll["actions"].append(
        {"actionType": "CastingTimeOption", "choiceKind": "done", "childIndex": 1, "label": "Done"}
    )
    decision = build_pending_decision(poll)
    assert decision.selection_is_valid(["idx:3"])
    assert decision.selection_is_valid(["idx:1", "idx:2"])
    assert not decision.selection_is_valid(["idx:1", "idx:3"])
    assert not decision.selection_is_valid(["idx:1"])
