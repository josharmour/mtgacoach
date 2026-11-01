"""Replay #348's exact source/ability IDs without suppressing useful repeat activations."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from tests.test_self_animation_safety import decision, mutavault_board

from arenamcp.action_planner import ActionPlanner
from arenamcp.gamestate import GameState
from arenamcp.play_safety import filter_play_options, unsafe_play_reason

FIXTURE = json.loads((Path(__file__).parent / "fixtures/pending_mutavault_348.json").read_text())
FINALE = "Creatures you control get +X/+X and gain haste until end of turn."


def position(count=1):
    state, land, _ = mutavault_board()
    land.update(instance_id=506, is_tapped=False)
    state.update(match_id=FIXTURE["match_id"], hand=[], battlefield=[land])
    state["turn"]["turn_number"] = 11
    action = deepcopy(FIXTURE["choices"][min(count, 2)]["action"])
    state["_bridge_actions"] = [action]
    game = GameState()
    game.set_local_seat_id(2)
    game.update_from_message(
        {
            "type": "GameStateType_Full",
            "gameStateId": 279,
            "zones": [{"zoneId": 27, "type": "ZoneType_Stack", "objectInstanceIds": [540]}],
            "gameObjects": [
                {
                    "instanceId": 540,
                    "grpId": 69611,
                    "zoneId": 27,
                    "type": "GameObjectType_Card",
                    "ownerSeatId": 2,
                    "controllerSeatId": 2,
                }
            ],
        }
    )
    for message in FIXTURE["messages"][:count]:
        game.update_from_message(deepcopy(message))
    state["stack"] = [obj.to_dict() for obj in game.stack]
    state["stack"][-1]["oracle_text"] = FINALE
    return state, land, action, game


@pytest.mark.parametrize("count", [1, 2, 3])
def test_exact_pending_animation_is_not_another_body(count):
    state, land, action, _ = position(count)
    assert state["stack"][0]["parent_instance_id"] == 506
    assert state["stack"][0]["grp_id"] == 15429
    assert "already pending" in unsafe_play_reason(state, land, "Activate", action)
    assert filter_play_options(decision(action), state).option_ids() == {"pass"}


def test_first_activation_for_pending_finale_remains_available():
    state, land, action, _ = position(0)
    assert unsafe_play_reason(state, land, "Activate", action) == ""


def test_model_cannot_repick_pending_animation_from_unfiltered_menu():
    state, _, action, _ = position()
    backend = Mock()
    backend.complete.return_value = '{"option_ids":["pass"],"reasoning":"Let animation resolve."}'
    planner = ActionPlanner(backend)
    assert planner.plan_decision_options(decision(action), state) == ["pass"]
    assert "idx:0" not in backend.complete.call_args.args[1]


def test_ready_answer_cannot_reintroduce_animation_after_it_reaches_stack():
    state, _, action, _ = position(0)
    backend = Mock()
    backend.complete.side_effect = [
        '{"option_ids":["idx:0"],"reasoning":"Animate before Finale resolves."}',
        '{"option_ids":["pass"],"reasoning":"Let the pending animation resolve."}',
    ]
    planner = ActionPlanner(backend)
    offered = decision(action)
    assert planner.plan_decision_options(offered, state) == ["idx:0"]
    pending, _, _, _ = position()
    assert planner.plan_decision_options(offered, pending) == ["pass"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("parent_instance_id", 999),
        ("grp_id", 1152),
        ("object_kind", "CARD"),
        ("controller_seat_id", 1),
        ("parent_instance_id", None),
    ],
)
def test_unknown_or_different_pending_identity_does_not_suppress(field, value):
    state, land, action, _ = position()
    state["stack"][0][field] = value
    assert unsafe_play_reason(state, land, "Activate", action) == ""


def test_intervening_counter_or_response_can_justify_another_animation():
    state, land, action, _ = position()
    state["stack"].insert(
        0, {"instance_id": 900, "targeting": [545], "oracle_text": "Counter target ability."}
    )
    assert unsafe_play_reason(state, land, "Activate", action) == ""


@pytest.mark.parametrize("reason", ["cancelled", "rejected", "expired"])
def test_removed_pending_ability_does_not_leave_a_sticky_repeat_ban(reason):
    state, land, action, game = position()
    assert filter_play_options(decision(action), state).option_ids() == {"pass"}
    game.update_from_message(
        {"type": "GameStateType_Diff", "gameStateId": 283, "diffDeletedInstanceIds": [545]}
    )
    state["stack"] = [dict(obj.to_dict(), oracle_text=FINALE) for obj in game.stack]
    state["recent_events"] = [{"type": reason, "source_id": 506, "ability_id": 15429}]
    assert unsafe_play_reason(state, land, "Activate", action) == ""


@pytest.mark.parametrize(
    "effect",
    [
        "This land gets +1/+1 until end of turn.",
        "This land becomes a 2/2 creature until end of turn. Draw a card.",
    ],
)
def test_additive_or_additional_effects_are_not_deduplicated(effect):
    state, land, action, _ = position()
    action["ability_text"] = "{1}: " + effect
    assert unsafe_play_reason(state, land, "Activate", action) == ""


@pytest.mark.parametrize("resolved", [False, True])
def test_activation_trigger_payoff_remains_a_model_decision(resolved):
    state, land, action, _ = position()
    if resolved:
        land.update(card_types=["Land", "Creature"], power=2, toughness=2)
        state["stack"].pop(0)
    state["battlefield"].append(
        {
            "instance_id": 800,
            "controller_seat_id": 2,
            "oracle_text": "Whenever you activate an ability, you gain 1 life.",
        }
    )
    assert unsafe_play_reason(state, land, "Activate", action) == ""


@pytest.mark.parametrize("case", ["nonmana_cost", "payment_trigger"])
def test_distinct_cost_or_payment_benefits_are_not_blanket_suppressed(case):
    state, land, action, _ = position()
    if case == "nonmana_cost":
        land["oracle_text"] = land["oracle_text"].replace("{o1}:", "{o1}, Sacrifice a Food:")
    else:
        action["autoTapActions"] = [{"instanceId": 800}]
        state["battlefield"].append(
            {
                "instance_id": 800,
                "oracle_text": "Whenever you tap a creature for mana, add an additional {G}.",
            }
        )
    assert unsafe_play_reason(state, land, "Activate", action) == ""


def test_resolved_animation_does_not_become_useful_again_because_finale_is_pending():
    state, land, action, _ = position(0)
    land.update(card_types=["Land", "Creature"], power=2, toughness=2)
    assert "already a creature" in unsafe_play_reason(state, land, "Activate", action)
