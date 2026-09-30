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
