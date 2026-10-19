"""A target that only dies to the burn the reasoning plans next is not declined.

2026-10-19 12:31:22 (ledger #103): Iroh's Demonstration's "4 damage to target
creature" at a 4/5 Hearth Elemental, reasoning "Mode 2 puts 4 damage on a Hearth
Elemental so Lightning Strike finishes it". The 4 alone does not kill, so the
target was declined and the cast cancelled; the Strike then hit an undamaged
Elemental. 4 + 3 kills a 4/5.
"""

from __future__ import annotations

from types import SimpleNamespace

IROH = "iroh's demonstration deals 4 damage to target creature."


def _state(hand: list[dict]) -> dict:
    return {
        "turn": {"turn_number": 15},
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": [
            {
                "instance_id": 401,
                "name": "Hearth Elemental",
                "type_line": "Creature — Elemental",
                "power": 4,
                "toughness": 5,
                "controller_seat_id": 1,
            }
        ],
        "hand": hand,
    }


STRIKE = {
    "instance_id": 330,
    "name": "Lightning Strike",
    "type_line": "Instant",
    "mana_cost": "{1}{R}",
    "oracle_text": "Lightning Strike deals 3 damage to any target.",
}


def _planner(reasoning: str):
    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._decision_source_oracle = lambda decision, state: IROH
    planner._decision_source_instance = lambda state: 219
    planner._last_decision_reasoning = reasoning
    return planner


def _decision():
    return SimpleNamespace(source_label="Iroh's Demonstration", options=[SimpleNamespace(option_id="tgt:401")])


def test_the_named_follow_up_burn_makes_it_a_kill():
    planner = _planner("Mode 2 puts 4 damage on a Hearth Elemental so Lightning Strike finishes it.")
    assert planner._prefer_lethal_damage_target(_decision(), _state([STRIKE]), ["tgt:401"]) == ["tgt:401"]


def test_without_the_follow_up_in_hand_it_is_still_declined():
    from arenamcp.action_planner import DECLINE_DECISION

    planner = _planner("Mode 2 puts 4 damage on a Hearth Elemental so Lightning Strike finishes it.")
    assert planner._prefer_lethal_damage_target(_decision(), _state([]), ["tgt:401"]) == [DECLINE_DECISION]


def test_a_two_damage_follow_up_kills_a_four_five():
    shock = dict(STRIKE, name="Shock", oracle_text="Shock deals 2 damage to any target.", mana_cost="{R}")
    planner = _planner("4 damage now, then Shock finishes the Elemental.")
    assert planner._prefer_lethal_damage_target(_decision(), _state([shock]), ["tgt:401"]) == ["tgt:401"]


def test_a_named_card_that_is_not_burn_does_not_count():
    from arenamcp.action_planner import DECLINE_DECISION

    effigy = {
        "instance_id": 331,
        "name": "Fleeting Effigy",
        "type_line": "Creature — Spirit",
        "mana_cost": "{R}",
        "oracle_text": "Haste. At the beginning of your end step, return this creature to its owner's hand.",
    }
    planner = _planner("4 damage now, then Fleeting Effigy attacks to finish the Elemental.")
    assert planner._prefer_lethal_damage_target(_decision(), _state([effigy]), ["tgt:401"]) == [DECLINE_DECISION]
