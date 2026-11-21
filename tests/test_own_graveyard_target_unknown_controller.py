"""2026-11-20 Wary Zone Guard: a land Player.log never placed in our graveyard.

Arena summarized the fetch land's sacrifice, so the land card the Survival
trigger offered was missing from our state. The trigger can only target our
own graveyard, so the pick is ours rather than of unknown control.
"""

import json
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision

WARY_ZONE_GUARD = (
    "This creature enters tapped.\n<i>Survival</i> \u2014 At the beginning of your second main phase, "
    "if this creature is tapped, return up to one target land card from your graveyard to the "
    "battlefield. This creature perpetually gets +1/+1."
)


def state(oracle=WARY_ZONE_GUARD):
    return {
        "local_seat_id": 2,
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1}],
        "battlefield": [
            {
                "instance_id": 774,
                "name": "Wary Zone Guard",
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "oracle_text": oracle,
            }
        ],
        # The sacrificed Wooded Foothills (783) is absent: its zone change was summarized.
        "graveyard": [
            {"instance_id": 549, "name": "Lightning Bolt", "owner_seat_id": 2, "controller_seat_id": 2}
        ],
        "stack": [{"instance_id": 979, "name": "Wary Zone Guard ability", "parent_instance_id": 774}],
        "decision_context": {"type": "target_selection", "source_id": 979},
    }


def decision():
    return PendingDecision(
        (296, 428),
        "SelectTargets",
        (DecisionOption("tgt:783", "Wooded Foothills"),),
        min_select=0,
        max_select=1,
        source_label="Wary Zone Guard",
    )


def planner(response):
    result = ActionPlanner.__new__(ActionPlanner)
    result._timeout = 1
    result._backend = Mock()
    result._backend.complete.return_value = json.dumps(response)
    return result


@pytest.mark.parametrize("controllers", [{}, {"tgt:783": "self"}])
def test_own_graveyard_land_return_is_submitted_when_log_missed_the_card(controllers):
    p = planner(
        {
            "option_ids": ["tgt:783"],
            "target_controllers": controllers,
            "reasoning": "Free land return ramps toward the lethal clock.",
        }
    )
    assert p.plan_decision_options(decision(), state()) == ["tgt:783"]


def test_opponent_intent_still_declines_own_graveyard_target():
    p = planner({"option_ids": ["tgt:783"], "target_controllers": {"tgt:783": "opponent"}})
    assert p.plan_decision_options(decision(), state()) == [DECLINE_DECISION]


def test_any_graveyard_target_with_unknown_card_still_declines():
    oracle = "When this creature enters, exile target card from a graveyard."
    p = planner({"option_ids": ["tgt:783"], "target_controllers": {"tgt:783": "opponent"}})
    assert p.plan_decision_options(decision(), state(oracle)) == [DECLINE_DECISION]


def test_mixed_targets_do_not_assume_our_control():
    oracle = "Exile target creature. Return up to one target creature card from your graveyard to your hand."
    p = planner({"option_ids": ["tgt:783"], "target_controllers": {"tgt:783": "self"}})
    assert p.plan_decision_options(decision(), state(oracle)) == [DECLINE_DECISION]


def test_mixed_targets_in_same_sentence_do_not_assume_our_control():
    oracle = "Return target creature card from your graveyard to the battlefield, and it fights target creature an opponent controls."
    p = planner({"option_ids": ["tgt:783"], "target_controllers": {"tgt:783": "self"}})
    assert p.plan_decision_options(decision(), state(oracle)) == [DECLINE_DECISION]


def test_battlefield_object_with_unknown_controller_still_declines():
    gs = state()
    gs["battlefield"].append({"instance_id": 783, "name": "Wooded Foothills", "owner_seat_id": 2})
    p = planner({"option_ids": ["tgt:783"], "target_controllers": {"tgt:783": "self"}})
    assert p.plan_decision_options(decision(), gs) == [DECLINE_DECISION]
