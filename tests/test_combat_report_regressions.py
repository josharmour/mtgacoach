"""Observed September 29 combat mistakes, without private match data."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.coach import CoachEngine
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.combat_solver import _resolve_attacker
from arenamcp.combat_strategy import combat_choice, unproductive_attackers
from arenamcp.gamestate import GameState
from arenamcp.rules_engine import RulesEngine


def creature(identity, name, power, toughness, text="", owner=1):
    return {
        "instance_id": identity,
        "name": name,
        "power": power,
        "toughness": toughness,
        "oracle_text": text,
        "type_line": "Creature",
        "owner_seat_id": owner,
        "controller_seat_id": owner,
        "turn_entered_battlefield": 1,
    }


def board():
    birds = creature(10, "Birds of Paradise", 0, 1, "Flying\n{T}: Add one mana of any color.")
    tyrant = creature(20, "Vaultborn Tyrant", 6, 6, "Trample")
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 25},
            {"seat_id": 2, "is_local": False, "life_total": 25},
        ],
        "turn": {"turn_number": 9, "active_player": 1, "step": "Step_DeclareAttack"},
        "battlefield": [birds, tyrant],
        "hand": [],
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": [birds["name"], tyrant["name"]],
            "raw_attackers": [
                {
                    "attackerInstanceId": identity,
                    "legalDamageRecipients": [
                        {"type": "Player", "playerSystemSeatId": 2},
                    ],
                }
                for identity in (10, 20)
            ],
        },
    }


def test_explicit_protobuf_zero_keeps_birds_zero_damage_warning():
    game = GameState()
    game._update_game_object({"instanceId": 10, "power": {}, "toughness": {"value": 1}})
    game._update_game_object({"instanceId": 10, "isTapped": False})
    assert game.game_objects[10].power == 0
    assert game.game_objects[10].toughness == 1
    game._update_game_object({"instanceId": 11})
    assert game.game_objects[11].power is None
    state = board()
    state["battlefield"][0]["power"] = game.game_objects[10].power
    assert any("Birds of Paradise (0/1) [0 POWER" in label for label in RulesEngine.get_legal_actions(state))


@pytest.mark.parametrize("stat", ["power", "toughness"])
def test_present_zero_wrapper_replaces_previous_stat(stat):
    game = GameState()
    game._update_game_object({"instanceId": 10, stat: {"value": 3}})
    game._update_game_object({"instanceId": 10, stat: {}})
    assert getattr(game.game_objects[10], stat) == 0


@pytest.mark.parametrize(
    "text,keyword",
    [
        (
            "Sacrifice this creature: Another target creature you control gains indestructible until end of turn.",
            "indestructible",
        ),
        ("When this creature enters, create a 4/4 Dragon creature token with flying.", "flying"),
        ("This creature has flying as long as you control an artifact.", "flying"),
        ("Reach (This creature can block creatures with flying.)", "flying"),
        ("Creatures you control have flying, vigilance, and trample.", "vigilance"),
    ],
)
def test_granted_conditional_and_reminder_keywords_are_not_inherent(text, keyword):
    card = creature(30, "Ability source", 1, 1, text)
    assert not has_combat_keyword(card, keyword)
    assert keyword.upper().replace(" ", "-") not in CoachEngine.__new__(CoachEngine)._combat_keyword_flags(
        card
    )


def test_printed_keywords_still_apply_with_reminder_text_and_lists():
    card = creature(
        30,
        "Flyer",
        2,
        2,
        "Flying (This creature can't be blocked except by creatures with flying or reach.)\nVigilance, trample",
    )
    assert all(has_combat_keyword(card, keyword) for keyword in ["flying", "vigilance", "trample"])
    assert not has_combat_keyword(card, "reach")


def test_halfling_does_not_treat_selfless_savior_as_indestructible():
    state = board()
    halfling = creature(10, "Delighted Halfling", 1, 2, "{T}: Add {C}.")
    savior = creature(
        30,
        "Selfless Savior",
        1,
        1,
        "Sacrifice this creature: Another target creature you control gains indestructible until end of turn.",
        owner=2,
    )
    state["battlefield"] = [halfling, state["battlefield"][0] | {"instance_id": 40}, savior]
    state["decision_context"]["raw_attackers"] = state["decision_context"]["raw_attackers"][:1]
    outcome = _resolve_attacker(halfling, [savior])
    assert not outcome.attacker_died
    assert outcome.blockers_died == [savior]
    choice = combat_choice(state)
    assert set(choice.assignments) == {10}
    assert choice.player_damage == 1
    assert choice.crackback == 1


def test_equal_damage_attack_preserves_birds_for_mana_and_defense():
    choice = combat_choice(board())
    assert set(choice.assignments) == {20}
    assert choice.player_damage == 6


def test_only_birds_does_not_attack_for_zero():
    state = board()
    state["battlefield"] = state["battlefield"][:1]
    state["decision_context"]["raw_attackers"] = state["decision_context"]["raw_attackers"][:1]
    assert combat_choice(state).assignments == {}


@pytest.mark.parametrize(
    "exception", ["mandatory", "own_trigger", "raid", "pump", "pump_ability", "toughness", "unknown_power"]
)
def test_zero_power_guard_preserves_real_payoffs_and_unknowns(exception):
    state = board()
    assert unproductive_attackers(state) == {10}
    if exception == "mandatory":
        state["decision_context"]["raw_attackers"][0]["mustAttack"] = True
    elif exception == "own_trigger":
        state["battlefield"].append(
            creature(
                30,
                "Winota",
                4,
                4,
                "Whenever a non-Human creature you control attacks, look at the top six cards.",
            )
        )
    elif exception == "raid":
        state["hand"] = [
            {"oracle_text": "Raid — When this creature enters, if you attacked this turn, draw a card."}
        ]
    elif exception == "pump_ability":
        state["battlefield"].append(
            creature(30, "Combat mentor", 2, 2, "{G}: Target creature gets +1/+1 until end of turn.")
        )
    elif exception == "pump":
        state["hand"] = [
            {"type_line": "Instant", "oracle_text": "Target creature gets +3/+3 until end of turn."}
        ]
    elif exception == "toughness":
        state["battlefield"].append(
            creature(
                30,
                "Doran",
                0,
                5,
                "Each creature assigns combat damage equal to its toughness rather than its power.",
                owner=2,
            )
        )
    else:
        state["battlefield"][0]["power"] = None
    assert unproductive_attackers(state) == set()


@pytest.mark.parametrize("method", ["_try_bridge_declare_attackers", "_try_gre_bridge_attackers"])
def test_bridge_omits_useless_birds_and_returns_exact_submitted_attack(method):
    state = board()
    state["battlefield"].append(
        creature(
            30,
            "Opposing Winota",
            4,
            4,
            "Whenever a non-Human creature you control attacks, look at six cards.",
            owner=2,
        )
    )
    pilot = _BridgeSubmitMixin()
    pilot._get_game_state = lambda: deepcopy(state)
    pilot._find_instance_id = lambda name, cards, local: next(
        c["instance_id"] for c in cards if c["name"] == name
    )
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_class": "DeclareAttackersRequest",
        "attackers": state["decision_context"]["raw_attackers"],
    }
    pilot._gre_bridge.submit_attackers_raw.return_value = {"ok": True}
    pilot._gre_bridge.submit_attackers.return_value = True
    pilot._log_execution_path = Mock()
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Birds of Paradise", "Vaultborn Tyrant"],
        attacker_targets={"Birds of Paradise": "Opponent", "Vaultborn Tyrant": "Opponent"},
    )
    result = getattr(pilot, method)(action)
    assert result.success
    submit = (
        pilot._gre_bridge.submit_attackers_raw
        if method == "_try_bridge_declare_attackers"
        else pilot._gre_bridge.submit_attackers
    )
    assert [entry["attackerInstanceId"] for entry in submit.call_args.args[0]] == [20]
    spoken = ActionPlan(actions=[result.submitted_action]).spoken_actions()
    assert "Vaultborn Tyrant" in spoken
    assert "Birds" not in spoken
