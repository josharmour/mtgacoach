"""Firdoch Core and new Vehicles need an actual payoff for temporary animation."""

from collections import Counter
from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.coach import CoachEngine
from arenamcp.decisions import build_pending_decision
from arenamcp.play_safety import filter_play_options, unsafe_play_reason


def board(*, tapped=True):
    core = {
        "instance_id": 548,
        "name": "Firdoch Core",
        "type_line": "Kindred Artifact — Shapeshifter",
        "card_types": ["CardType_Artifact", "CardType_Kindred"],
        "oracle_text": "Changeling\n{T}: Add one mana of any color.\n"
        "{4}: This artifact becomes a 4/4 artifact creature until end of turn.",
        "owner_seat_id": 2,
        "controller_seat_id": 2,
        "is_tapped": tapped,
        "turn_entered_battlefield": 3,
    }
    action = {
        "actionType": "Activate",
        "instanceId": 548,
        "grpId": 98577,
        "abilityGrpId": 194115,
        "hasAutoTap": True,
        "autoTapActions": [{"instanceId": 566, "manaId": 0}],
        "manaCost": [{"color": '[ "Generic" ]', "count": 4}],
    }
    state = {
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "local_seat_id": 2,
        "turn": {"turn_number": 7, "active_player": 2, "phase": "Phase_Main1"},
        "battlefield": [
            core,
            {
                "instance_id": 576,
                "name": "Woodfall Primus",
                "type_line": "Creature",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "oracle_text": "Trample\nWhen this creature enters, destroy target noncreature permanent.\nPersist",
            },
        ],
        "stack": [
            {
                "name": "Badgermole Cub",
                "oracle_text": "When this creature enters, earthbend 1.\n"
                "Whenever you tap a creature for mana, add an additional {G}.",
            }
        ],
        "hand": [
            {
                "instance_id": 574,
                "name": "Disciple of Freyalise",
                "oracle_text": "When this creature enters, you may sacrifice another creature. If you do, draw cards.",
            }
        ],
        "_bridge_actions": [action],
        "_bridge_request_type": "ActionsAvailable",
    }
    return state, core, action


def decision(action):
    return build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "can_pass": True, "actions": [action]},
        resolve_name=lambda grp: "Firdoch Core",
    )


def test_tapped_core_cannot_be_sold_as_a_free_attacker_by_either_planner():
    state, core, action = board()
    assert "tapped" in unsafe_play_reason(state, core, "Activate", action)
    assert filter_play_options(decision(action), state).option_ids() == {"pass"}
    backend = Mock()
    backend.complete.return_value = '{"option_ids":["idx:0"],"reasoning":"Free attacker."}'
    planner = ActionPlanner(backend=backend)
    assert planner.plan_decision_options(decision(action), state) == ["pass"]
    assert "idx:0" not in backend.complete.call_args.args[1]
    assert planner._filter_legal_actions_for_planning(state, ["Activate: Firdoch Core [OK]", "Pass"]) == [
        "Pass"
    ]


def test_payment_that_taps_core_also_prevents_attacking():
    state, core, action = board(tapped=False)
    action["autoTapActions"] = [{"instanceId": 548, "manaId": 0}]
    assert "tapped" in unsafe_play_reason(state, core, "Activate", action)
    assert filter_play_options(decision(action), state).option_ids() == {"pass"}


def test_untapped_core_can_animate_even_with_old_stats_after_the_effect_expired():
    state, core, action = board(tapped=False)
    core.update(power=4, toughness=4)
    assert unsafe_play_reason(state, core, "Activate", action) == ""
    assert "idx:0" in filter_play_options(decision(action), state).option_ids()


def test_redundant_animation_uses_current_types_and_live_stats():
    state, core, action = board(tapped=False)
    core.update(card_types=["CardType_Artifact", "CardType_Creature"], power=4, toughness=4)
    assert "already a creature" in unsafe_play_reason(state, core, "Activate", action)
    core.update(power=1, toughness=1)
    assert unsafe_play_reason(state, core, "Activate", action) == ""


@pytest.mark.parametrize(
    "payoff", ["untap", "sacrifice", "activation_trigger", "self_trigger", "combat_buff"]
)
def test_visible_noncombat_or_untap_payoffs_are_preserved(payoff):
    state, core, action = board()
    effects = {
        "untap": "Untap target artifact.",
        "sacrifice": "Sacrifice a creature: Draw two cards.",
        "activation_trigger": "Whenever you activate an ability of an artifact, draw a card.",
        "self_trigger": "Whenever this artifact becomes a creature, draw a card.",
        "combat_buff": "Creatures you control get +X/+X, where X is the number of creatures you control.",
    }
    if payoff == "self_trigger":
        core["oracle_text"] += "\n" + effects[payoff]
    else:
        state["stack"] = [{"oracle_text": effects[payoff]}]
    assert unsafe_play_reason(state, core, "Activate", action) == ""


def test_payable_sacrifice_spell_can_use_animated_body():
    state, core, action = board()
    state["_bridge_actions"].append({"actionType": "Cast", "instanceId": 574, "hasAutoTap": True})
    assert unsafe_play_reason(state, core, "Activate", action) == ""


def test_animation_with_other_effects_and_ambiguous_abilities_is_left_to_planner():
    state, core, action = board()
    core["oracle_text"] += " Untap it."
    assert unsafe_play_reason(state, core, "Activate", action) == ""
    state, core, action = board()
    core["oracle_text"] += "\n{1}: Draw a card."
    assert unsafe_play_reason(state, core, "Activate", action) == ""


@pytest.mark.parametrize("animated", [False, True])
def test_board_display_does_not_confuse_retained_stats_with_creature_status(animated):
    state, core, _ = board(tapped=False)
    core.update(power=4, toughness=4)
    if animated:
        core["card_types"].append("CardType_Creature")
    lines = CoachEngine.__new__(CoachEngine)._format_board_card(
        core,
        2,
        7,
        {},
        Counter({core["name"]: 1}),
        {},
        True,
        for_planner=True,
    )
    if animated:
        assert "Firdoch Core 4/4" in lines[0]
        assert "CREATURE NOW" in lines[0]
    else:
        assert "4/4" not in lines[0]
        assert "NOT A CREATURE" in lines[0]
    assert "becomes a 4/4" in "\n".join(lines[1:])


def test_activation_prompt_exposes_actual_cost_and_payment_taps():
    state, core, action = board(tapped=False)
    opt = decision(action).find("idx:0")
    assert opt.payable is True
    assert opt.meta["autoTapActions"] == action["autoTapActions"]
    backend = Mock()
    backend.complete.return_value = '{"option_ids":["idx:0"]}'
    planner = ActionPlanner(backend=backend)
    assert planner.plan_decision_options(decision(action), deepcopy(state)) == ["idx:0"]
    prompt = backend.complete.call_args.args[1]
    assert '"count": 4' in prompt
    assert '"source_tapped": false' in prompt
    assert '"payment_taps_source": false' in prompt


@pytest.mark.parametrize("animated", [False, True])
def test_combat_summary_includes_core_only_while_it_is_a_creature(animated):
    state, core, _ = board(tapped=False)
    core.update(power=4, toughness=4)
    if animated:
        core["card_types"].append("CardType_Creature")
    state["battlefield"] = [core]
    state["hand"] = []
    state["stack"] = []
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    attack_lines = "\n".join(line for line in context.splitlines() if line.startswith("Atk:"))
    assert ("Firdoch Core" in attack_lines) is animated


def wagon_board():
    state, wagon, action = board(tapped=False)
    state["turn"]["turn_number"] = 9
    state["hand"] = []
    wagon.update(
        instance_id=602,
        name="Lumbering Worldwagon",
        type_line="Artifact — Vehicle",
        card_types=["CardType_Artifact"],
        power=5,
        toughness=4,
        turn_entered_battlefield=9,
        oracle_text="This Vehicle's power is equal to the number of lands you control.\n"
        "Whenever this Vehicle enters or attacks, you may search your library for a basic land card, "
        "put it onto the battlefield tapped, then shuffle.\nCrew 4",
    )
    state["battlefield"][1].update(
        instance_id=695,
        name="Thorn Mammoth",
        power=7,
        toughness=6,
        turn_entered_battlefield=9,
        oracle_text="Trample\nWhenever Thorn Mammoth or another creature you control enters, "
        "Thorn Mammoth fights up to one target creature you don't control.",
    )
    # Neither the Mammoth's fight nor removal aimed at the Mammoth gives
    # crewing an unrelated Vehicle a benefit.
    state["stack"] = [
        {"oracle_text": state["battlefield"][1]["oracle_text"], "targeting": [576]},
        {"oracle_text": "Exile target creature.", "targeting": [695]},
    ]
    action.clear()
    action.update(actionType="Activate", instanceId=602, grpId=94970, abilityGrpId=76611)
    return state, wagon, action


def test_new_worldwagon_cannot_be_crewed_for_an_impossible_attack_or_enter_trigger():
    state, wagon, action = wagon_board()
    assert "entered this turn" in unsafe_play_reason(state, wagon, "Activate", action)
    pending = build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "can_pass": True, "actions": [action]},
        resolve_name=lambda grp: wagon["name"],
    )
    assert filter_play_options(pending, state).option_ids() == {"pass"}
    backend = Mock()
    backend.complete.return_value = '{"option_ids":["idx:0"],"reasoning":"Crew to fetch a land."}'
    planner = ActionPlanner(backend=backend)
    assert planner.plan_decision_options(pending, state) == ["pass"]
    assert "idx:0" not in backend.complete.call_args.args[1]
    assert planner._filter_legal_actions_for_planning(state, ["Activate: Lumbering Worldwagon", "Pass"]) == [
        "Pass"
    ]


@pytest.mark.parametrize("case", ["older", "block", "printed_haste", "granted_haste", "global_haste"])
def test_useful_worldwagon_combat_activations_remain_available(case):
    state, wagon, action = wagon_board()
    if case == "older":
        wagon["turn_entered_battlefield"] = 7
    elif case == "block":
        state["turn"].update(active_player=1, phase="Phase_Combat", step="Step_DeclareAttack")
    elif case == "printed_haste":
        wagon["oracle_text"] += "\nHaste"
    elif case == "granted_haste":
        wagon["granted_abilities"] = ["Haste"]
    else:
        state["battlefield"].append({"owner_seat_id": 2, "oracle_text": "Creatures you control have haste."})
    assert unsafe_play_reason(state, wagon, "Activate", action) == ""


@pytest.mark.parametrize("trigger", ["crew", "tap", "becomes_creature", "creature_count", "power_mana"])
def test_crew_and_noncombat_payoffs_can_justify_animating_a_new_vehicle(trigger):
    state, wagon, action = wagon_board()
    if trigger == "crew":
        wagon["oracle_text"] += "\nWhenever this Vehicle becomes crewed, draw a card."
    elif trigger == "becomes_creature":
        wagon["oracle_text"] += "\nWhenever this Vehicle becomes a creature, draw a card."
    elif trigger == "power_mana":
        state["battlefield"].append(
            {
                "owner_seat_id": 2,
                "oracle_text": "{G}, {T}: Add X mana in any combination of colors, "
                "where X is the greatest power among creatures you control.",
            }
        )
    else:
        state["battlefield"].append(
            {
                "owner_seat_id": 2,
                "oracle_text": "Whenever this creature becomes tapped, draw a card."
                if trigger == "tap"
                else "Creatures you control get +1/+1 for each creature you control.",
            }
        )
    assert unsafe_play_reason(state, wagon, "Activate", action) == ""


def test_redundant_crew_is_filtered_but_ambiguous_activations_are_left_to_planner():
    state, wagon, action = wagon_board()
    wagon["turn_entered_battlefield"] = 7
    wagon["card_types"].append("CardType_Creature")
    assert "already a creature" in unsafe_play_reason(state, wagon, "Activate", action)
    wagon["oracle_text"] += "\n{1}: Scry 1."
    assert unsafe_play_reason(state, wagon, "Activate", action) == ""


def test_new_core_also_needs_haste_but_unknown_entry_turn_does_not_establish_sickness():
    state, core, action = board(tapped=False)
    core["turn_entered_battlefield"] = 7
    assert "entered this turn" in unsafe_play_reason(state, core, "Activate", action)
    core["turn_entered_battlefield"] = -1
    assert unsafe_play_reason(state, core, "Activate", action) == ""


def test_new_vehicle_board_display_explains_attack_restriction_before_crewing():
    state, wagon, _ = wagon_board()
    lines = CoachEngine.__new__(CoachEngine)._format_board_card(
        wagon, 2, 9, {}, Counter({wagon["name"]: 1}), {}, True, for_planner=True
    )
    assert "5/4 when crewed" in lines[0]
    assert "ENTERED THIS TURN — needs haste to attack if animated" in lines[0]
    assert "CREATURE NOW" not in lines[0]


def mutavault_board():
    state, land, action = board()
    land.update(
        name="Mutavault",
        type_line="Land",
        card_types=["Land"],
        oracle_text="{oT}: Add {oC}.\n{o1}: This land becomes a 2/2 creature with all creature types "
        "until end of turn. It's still a land.",
        power=2,
        toughness=2,
    )
    state["stack"] = []
    state["hand"] = [
        {
            "instance_id": 700,
            "name": "The Great Henge",
            "oracle_text": "This spell costs {X} less to cast, where X is the greatest power among creatures you control.",
        }
    ]
    return state, land, action


@pytest.mark.parametrize("case", ["tapped", "payment_taps", "new_land", "postcombat"])
def test_mutavault_cannot_be_animated_for_a_future_henge_discount(case):
    state, land, action = mutavault_board()
    if case != "tapped":
        land["is_tapped"] = False
    if case == "payment_taps":
        action["autoTapActions"] = [{"instanceId": land["instance_id"]}]
    elif case == "new_land":
        land["turn_entered_battlefield"] = state["turn"]["turn_number"]
    elif case == "postcombat":
        state["turn"]["phase"] = "Phase_Main2"
    pending = decision(action)
    assert unsafe_play_reason(state, land, "Activate", action)
    assert filter_play_options(pending, state).option_ids() == {"pass"}


@pytest.mark.parametrize("kind", ["Creature", "CardType_Creature"])
def test_mutavault_redundant_animation_recognizes_both_type_encodings(kind):
    state, land, action = mutavault_board()
    land.update(is_tapped=False, card_types=["Land", kind])
    assert "already a creature" in unsafe_play_reason(state, land, "Activate", action)


@pytest.mark.parametrize("producer_state", ["ready", "tapped", "summoning_sick", "payment_taps"])
def test_mutavault_tribal_mana_payoff_requires_a_ready_producer(producer_state):
    state, land, action = mutavault_board()
    state["battlefield"].append(
        {
            "instance_id": 775,
            "name": "The Notary Hobbits",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
            "card_types": ["Creature"],
            "subtypes": ["Halfling", "Advisor"],
            "is_tapped": producer_state == "tapped",
            "turn_entered_battlefield": 7 if producer_state == "summoning_sick" else 5,
            "oracle_text": "{oT}: Add {oC} for each Halfling you control.",
        }
    )
    if producer_state == "payment_taps":
        action["autoTapActions"] = [{"instanceId": 775}]
    reason = unsafe_play_reason(state, land, "Activate", action)
    assert bool(reason) is (producer_state != "ready")


def test_untapped_mutavault_can_still_animate_for_combat():
    state, land, action = mutavault_board()
    land["is_tapped"] = False
    assert unsafe_play_reason(state, land, "Activate", action) == ""
