"""Attackers that only feed an untapped blocker are held back.

bug_20261006_135027 (match 3da54de9, turn 6): the planner sent a 1/1 Fblthp,
Impossibly Lost at a 1-loyalty Jace token past an untapped 3/2 Keeper of the
Quiet Hour with both our lands tapped after Twinned Vision. Keeper blocked,
Fblthp died, Keeper and Jace survived. The board below is that declaration
window, taken from the report.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_strategy import combat_choice, losing_attackers, loyalty

FBLTHP_TEXT = (
    "When one or more of your opponents are dealt combat damage during your turn, draw two cards. "
    "If your library has no cards in it, you win the game. Fblthp's owner shuffles him into their "
    "library. (If you draw from an empty library this way, you still win the game.)"
)
RECIPIENTS = [
    {"type": "Player", "playerSystemSeatId": 1},
    {"type": "PlanesWalker", "planeswalkerInstanceId": 298},
]
MENU = ["Attack with: Fblthp, Impossibly Lost (1/1)", "Done (confirm attackers)"]


def land(identity, name, seat, tapped=True):
    return {
        "instance_id": identity,
        "name": name,
        "oracle_text": "({T}: Add {U}.)",
        "type_line": f"Basic Land — {name}",
        "card_types": ["Land"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": tapped,
    }


def creature(identity, name, seat, power, toughness, oracle="", **extra):
    return {
        "instance_id": identity,
        "name": name,
        "oracle_text": oracle,
        "type_line": "Creature",
        "card_types": ["Creature"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "power": power,
        "toughness": toughness,
        "is_tapped": False,
        **extra,
    }


def fblthp(identity=288, name="Fblthp, Impossibly Lost", **extra):
    return creature(identity, name, 2, 1, 1, FBLTHP_TEXT, **extra)


def keeper(identity=293, **extra):
    return creature(
        identity, "Keeper of the Quiet Hour", 1, 3, 2, "When this creature enters, empower Jace 2.", **extra
    )


def incident(attackers=None, blockers=None, *, opponent_life=20, must_attack=False):
    """The turn-6 DeclareAttackers window from bug_20261006_135027."""
    attackers = [fblthp()] if attackers is None else attackers
    blockers = [keeper()] if blockers is None else blockers
    jace = {
        "instance_id": 298,
        "name": "Jace",
        "oracle_text": "Surveil 1.\nDraw a card.",
        "type_line": "Token Planeswalker — Jace",
        "card_types": ["Planeswalker"],
        "owner_seat_id": 1,
        "controller_seat_id": 1,
        "object_kind": "TOKEN",
        "counters": {"Loyalty": 1},
        "loyalty": 0,  # stale bridge field; the live counter says 1
        "is_tapped": False,
    }
    battlefield = [
        land(281, "Forest", 1),
        land(283, "Mountain", 2),
        land(285, "Swamp", 1),
        land(287, "Island", 2),
        land(292, "Forest", 1),
        *attackers,
        *blockers,
        jace,
    ]
    ids = [card["instance_id"] for card in attackers]
    return {
        "match_id": "3da54de9-9820-4ae2-b49e-8f81c913c446",
        "turn": {"turn_number": 6, "active_player": 2, "priority_player": 2, "phase": "Combat"},
        "local_seat_id": 2,
        "players": [
            {"seat_id": 1, "life_total": opponent_life, "is_local": False, "mana_pool": {}},
            {"seat_id": 2, "life_total": 20, "is_local": True, "mana_pool": {}},
        ],
        "battlefield": battlefield,
        "hand": [
            {
                "instance_id": 242,
                "name": "Tam's Resistance",
                "oracle_text": "Put a +1/+1 counter on up to one target creature. Empower Jace 4.",
                "type_line": "Sorcery",
                "mana_cost": "{1}{G/U}",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
            creature(244, "Mindseeker Oculus", 2, 2, 1, "When this creature enters, empower Jace 4."),
        ],
        "graveyard": [
            {
                "instance_id": 305,
                "name": "Twinned Vision",
                "oracle_text": "Draw a card. If this spell wasn't cast from your hand, draw two cards "
                "instead.\nFlashback—{o1o(U/R)o(U/R)}, Discard a card.",
                "type_line": "Instant",
                "mana_cost": "{1}{U/R}",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            }
        ],
        "recent_events": [
            {
                "type": "zone_transfer",
                "card": "Twinned Vision",
                "instance_id": 305,
                "from_zone": 27,
                "to_zone": 37,
                "category": "Resolve",
                "turn": 6,
                "phase": "Phase_Main1",
            }
        ],
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": [card["name"] for card in attackers],
            "legal_attacker_ids": ids,
            "raw_attackers": [
                {
                    "attackerInstanceId": identity,
                    "mustAttack": must_attack,
                    "legalDamageRecipients": RECIPIENTS,
                }
                for identity in ids
            ],
        },
        "_bridge_request_type": "DeclareAttackers",
    }


# --- the incident ------------------------------------------------------------


def test_fblthp_into_untapped_keeper_at_jace_is_refused():
    losing = losing_attackers(incident(), [288])
    assert set(losing) == {288}
    assert "Keeper of the Quiet Hour 3/2" in losing[288]


class AttackBackend:
    """Returns the incident's planner answer."""

    def __init__(self):
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return json.dumps(
            {
                "actions": [
                    {
                        "action_type": "declare_attackers",
                        "attacker_names": ["Fblthp, Impossibly Lost"],
                        "attacker_targets": {"Fblthp, Impossibly Lost": "Jace [298]"},
                        "reasoning": "Remove the planeswalker",
                    }
                ],
                "overall_strategy": "Trade Fblthp into Jace to remove the planeswalker, then rebuild "
                "toward the Arni combo.",
            }
        )


def test_planner_holds_back_the_incident_attack(caplog):
    state = incident()
    planner = ActionPlanner(AttackBackend(), timeout=1, land_drop_first=False)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        plan = planner.plan_actions(state, "decision_required", MENU, state["decision_context"])
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action_type == ActionType.DECLARE_ATTACKERS
    assert action.attacker_names == []
    assert action.attacker_instance_ids == []
    assert action.attacker_targets == {}
    assert plan.fallback_reason == "planner_losing_attack"
    assert "Fblthp" in plan.voice_advice and "die to a block" in plan.voice_advice
    assert "Losing-attack guard" in caplog.text
    assert "Fblthp, Impossibly Lost [288]" in caplog.text and "Jace [298]" in caplog.text


def test_guard_only_runs_at_the_declare_attackers_window():
    state = incident()
    plan = ActionPlan(
        actions=[GameAction(ActionType.DECLARE_ATTACKERS, attacker_names=["Fblthp, Impossibly Lost"])]
    )
    ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, {"type": "actions_available"})
    assert plan.actions[0].attacker_names == ["Fblthp, Impossibly Lost"]
    assert plan.fallback_reason == ""


def test_guard_keeps_the_rest_of_a_split_plan():
    state = incident([fblthp(), creature(400, "Wind Drake", 2, 2, 2, "Flying")])
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Fblthp, Impossibly Lost", "Wind Drake"],
                attacker_instance_ids=[288, 400],
                attacker_targets={"Fblthp, Impossibly Lost": "Jace [298]", "Wind Drake": "Opponent"},
            )
        ]
    )
    # The flyer cannot be answered, so the plan is pressure: keep both.
    ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_instance_ids == [288, 400]


# --- legitimate attacks are preserved -------------------------------------------


def test_lethal_alpha_strike_is_preserved():
    # Keeper can eat Fblthp OR chump the 5/5, not both: opponent at 5 dies.
    state = incident([fblthp(), creature(401, "Ogre", 2, 5, 5)], opponent_life=5)
    assert losing_attackers(state, [288, 401]) == {}


def test_multi_attacker_pressure_with_too_few_blockers_is_preserved():
    state = incident([fblthp(), fblthp(289, name="Other Homunculus")])
    assert losing_attackers(state, [288, 289]) == {}


def test_every_doomed_attacker_is_dropped_when_each_has_its_own_killer():
    state = incident(
        [fblthp(), fblthp(289, name="Other Homunculus")],
        [keeper(), keeper(294)],
    )
    assert set(losing_attackers(state, [288, 289])) == {288, 289}


def test_attack_trigger_is_preserved():
    looter = creature(
        402, "Looter", 2, 2, 1, "Whenever this creature attacks, draw a card, then discard a card."
    )
    assert losing_attackers(incident([looter]), [402]) == {}


def test_evasive_attacker_the_blocker_cannot_block_is_preserved():
    flyer = creature(403, "Bird", 2, 1, 1, "Flying")
    assert losing_attackers(incident([flyer]), [403]) == {}


@pytest.mark.parametrize(
    "attacker, must_attack",
    [
        (fblthp(), True),
        (creature(404, "Berserker", 2, 1, 1, "This creature attacks each combat if able."), False),
    ],
)
def test_forced_attacker_is_preserved(attacker, must_attack):
    state = incident([attacker], must_attack=must_attack)
    assert losing_attackers(state, [attacker["instance_id"]]) == {}


def test_death_trigger_is_preserved():
    sphinx = creature(
        405,
        "Sphinx Original",
        2,
        1,
        1,
        "When this creature dies, if it isn't a token, create a token that's a copy of it.",
    )
    assert losing_attackers(incident([sphinx]), [405]) == {}


def test_untapped_mana_with_an_instant_in_hand_is_preserved():
    state = incident()
    for card in state["battlefield"]:
        if card["instance_id"] in (283, 287):
            card["is_tapped"] = False
    state["hand"].append(
        {
            "instance_id": 351,
            "name": "Icy Reception",
            "oracle_text": "Choose one —\n•Target creature gets -5/-0 until end of turn.",
            "type_line": "Instant",
            "mana_cost": "{1}{U}",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
        }
    )
    assert losing_attackers(state, [288]) == {}


def test_one_open_land_cannot_pay_the_three_mana_flashback():
    state = incident()
    next(card for card in state["battlefield"] if card["instance_id"] == 287)["is_tapped"] = False
    assert set(losing_attackers(state, [288])) == {288}
    # With three open lands the flashback instant is castable: keep the attack.
    state["battlefield"].append(land(420, "Island", 2, tapped=False))
    state["battlefield"].append(land(421, "Island", 2, tapped=False))
    assert losing_attackers(state, [288]) == {}


def test_free_sacrifice_outlet_is_preserved():
    state = incident()
    state["battlefield"].append(
        {
            "instance_id": 410,
            "name": "Altar",
            "oracle_text": "Sacrifice a creature: Scry 1.",
            "type_line": "Artifact",
            "card_types": ["Artifact"],
            "owner_seat_id": 2,
            "controller_seat_id": 2,
            "is_tapped": False,
        }
    )
    assert losing_attackers(state, [288]) == {}


def test_attack_payoff_on_another_permanent_is_preserved():
    state = incident()
    state["battlefield"].append(
        creature(
            411, "Captain", 2, 2, 2, "Whenever a creature you control attacks, it gets +1/+0.", is_tapped=True
        )
    )
    assert losing_attackers(state, [288]) == {}


def test_spell_this_turn_that_mentions_blocking_is_preserved():
    state = incident()
    state["graveyard"].append(
        {
            "instance_id": 412,
            "name": "Hold Back",
            "oracle_text": "Target creature can't block this turn.",
            "type_line": "Sorcery",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
        }
    )
    state["recent_events"].append(
        {"type": "zone_transfer", "instance_id": 412, "category": "Resolve", "turn": 6}
    )
    assert losing_attackers(state, [288]) == {}


def test_trade_is_not_a_losing_attack():
    assert losing_attackers(incident([creature(406, "Bear", 2, 3, 2)]), [406]) == {}


def test_menace_with_one_blocker_is_preserved():
    sneak = creature(407, "Sneak", 2, 1, 1, "Menace")
    assert losing_attackers(incident([sneak]), [407]) == {}


def test_tapped_or_cant_block_creatures_are_not_blockers():
    assert losing_attackers(incident(blockers=[keeper(is_tapped=True)]), [288]) == {}
    wall = keeper(oracle_text="This creature can't block.")
    assert losing_attackers(incident(blockers=[wall]), [288]) == {}


def test_unknown_power_keeps_the_attack():
    assert losing_attackers(incident([{**fblthp(), "power": None}]), [288]) == {}


# --- planeswalker loyalty ------------------------------------------------------


def test_live_loyalty_counter_beats_a_stale_zero_field():
    jace = next(card for card in incident()["battlefield"] if card["instance_id"] == 298)
    assert loyalty(jace) == 1
    assert loyalty({"loyalty": 4}) == 4
    assert loyalty({"counters": {"Loyalty": 2}, "loyalty": 5}) == 2
    assert loyalty({"counters": {}}) is None


def test_unblocked_attack_now_removes_the_one_loyalty_jace():
    state = incident(blockers=[])
    choice = combat_choice(copy.deepcopy(state))
    assert choice.planeswalkers_removed == [298]
    assert choice.assignments[288]["planeswalkerInstanceId"] == 298


# --- bridge submission path (attacks that skip the planner) -------------------------


def bridge_pilot(state, pending_attackers):
    from unittest.mock import Mock

    from arenamcp.autopilot_bridge import _BridgeSubmitMixin

    pilot = _BridgeSubmitMixin()
    pilot._get_game_state = lambda: state
    pilot._find_instance_id = lambda name, battlefield, local: next(
        (card["instance_id"] for card in battlefield if card.get("name") == name), None
    )
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_class": "DeclareAttackerRequest",
        "attackers": pending_attackers,
    }
    pilot._gre_bridge.submit_attackers_raw.return_value = {"ok": True}
    pilot._attack_override = Mock(return_value=None)
    pilot._log_execution_path = Mock()
    return pilot


def test_bridge_refuses_the_incident_attack_at_jace(caplog):
    state = incident()
    pilot = bridge_pilot(state, state["decision_context"]["raw_attackers"])
    action = GameAction(
        ActionType.DECLARE_ATTACKERS,
        attacker_names=["Fblthp, Impossibly Lost"],
        attacker_targets={"Fblthp, Impossibly Lost": "Jace [298]"},
    )
    with caplog.at_level(logging.WARNING, logger="arenamcp.autopilot_bridge"):
        result = pilot._try_bridge_declare_attackers(action)
    assert result.success
    pilot._gre_bridge.submit_attackers_raw.assert_called_once_with([])
    assert result.submitted_action.attacker_names == []
    assert "Losing-attack guard (bridge)" in caplog.text and "Jace [298]" in caplog.text


def test_bridge_guards_the_declare_every_legal_attacker_path():
    # The auto-confirm path declares every legal attacker with no recipients.
    state = incident()
    face_only = [{"attackerInstanceId": 288, "mustAttack": False, "legalDamageRecipients": [RECIPIENTS[0]]}]
    state["decision_context"]["raw_attackers"] = face_only
    pilot = bridge_pilot(state, face_only)
    action = GameAction(ActionType.DECLARE_ATTACKERS, attacker_names=["Fblthp, Impossibly Lost"])
    assert pilot._try_bridge_declare_attackers(action).success
    pilot._gre_bridge.submit_attackers_raw.assert_called_once_with([])


def test_bridge_keeps_a_forced_attacker_from_the_live_request():
    state = incident()  # the log-side state does not know it must attack
    forced = [{**entry, "mustAttack": True} for entry in state["decision_context"]["raw_attackers"]]
    pilot = bridge_pilot(state, forced)
    action = GameAction(
        ActionType.DECLARE_ATTACKERS,
        attacker_names=["Fblthp, Impossibly Lost"],
        attacker_targets={"Fblthp, Impossibly Lost": "Opponent"},
    )
    assert pilot._try_bridge_declare_attackers(action).success
    entries = pilot._gre_bridge.submit_attackers_raw.call_args.args[0]
    assert [entry["attackerInstanceId"] for entry in entries] == [288]


def test_bridge_keeps_an_attack_the_blocker_cannot_answer():
    state = incident([creature(403, "Bird", 2, 1, 1, "Flying")])
    pilot = bridge_pilot(state, state["decision_context"]["raw_attackers"])
    action = GameAction(
        ActionType.DECLARE_ATTACKERS, attacker_names=["Bird"], attacker_targets={"Bird": "Jace [298]"}
    )
    assert pilot._try_bridge_declare_attackers(action).success
    entries = pilot._gre_bridge.submit_attackers_raw.call_args.args[0]
    assert entries[0]["damageRecipient"]["planeswalkerInstanceId"] == 298


# --- macOS bridge snapshot loyalty -------------------------------------------------------


def test_mac_snapshot_reports_the_loyalty_counter_not_the_boxed_zero():
    from arenamcp.mac_game_state import _card_entry

    def enum(name):
        return {"e": name, "v": 0}

    def listing(*values):
        return {
            "$c": "System.Collections.Generic.List<X>",
            "$h": 900,
            "$n": len(values),
            "$items": list(values),
        }

    zero = {"$c": "System.Nullable`1[System.UInt32]", "$struct": True, "hasValue": True, "value": 0}
    counter = {"$c": "GreClient.Rules.CounterData", "$struct": True, "type_": enum("Loyalty"), "count_": 1}
    node = {
        "$c": "GreClient.Rules.MtgCardInstance",
        "$h": 298,
        "instanceId": 298,
        "baseGrpId": 106555,
        "objectType_": enum("Token"),
        "cardTypes_": listing(enum("Planeswalker")),
        "loyalty": zero,
        "counterDatas_": listing(counter),
    }
    entry = _card_entry(node)
    assert entry["loyalty"] == 1 and entry["counters"] == {"Loyalty": 1}
    # Without a counter, a planeswalker's zero is unknown, not a free kill.
    assert "loyalty" not in _card_entry({**node, "counterDatas_": listing()})
    with_value = {**zero, "value": 4}
    assert _card_entry({**node, "loyalty": with_value, "counterDatas_": listing()})["loyalty"] == 4
