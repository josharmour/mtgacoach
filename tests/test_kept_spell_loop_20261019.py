"""A spell declined because its damage kills nothing is declined once per turn.

2026-10-19 11:13 (Mono-Red Burn vs Trostani, Three Whispers 7/7): Iroh's
Demonstration's "4 damage to target creature" mode was aimed at the only
opposing creature; _prefer_lethal_damage_target declined the target, which
cancels the cast, and the cast step recast it every ~6 s until Arena's turn
timer ran out. The second decline of the same spell in a turn keeps the pick.
"""

from __future__ import annotations

from types import SimpleNamespace

# After the mode choice the spell on the stack carries only the chosen mode's text.
IROH = "iroh's demonstration deals 4 damage to target creature."


def _state(turn: int) -> dict:
    return {
        "turn": {"turn_number": turn},
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": [
            {
                "instance_id": 299,
                "name": "Trostani, Three Whispers",
                "type_line": "Legendary Creature",
                "power": 7,
                "toughness": 7,
                "controller_seat_id": 1,
            }
        ],
    }


def _planner():
    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._decision_source_oracle = lambda decision, state: IROH
    planner._decision_source_instance = lambda state: 219
    return planner


def test_second_decline_in_the_same_turn_keeps_the_pick():
    from arenamcp.action_planner import DECLINE_DECISION

    planner = _planner()
    decision = SimpleNamespace(options=[SimpleNamespace(option_id="tgt:299")])
    assert planner._prefer_lethal_damage_target(decision, _state(8), ["tgt:299"]) == [DECLINE_DECISION]
    assert planner._prefer_lethal_damage_target(decision, _state(8), ["tgt:299"]) == ["tgt:299"]


def test_a_new_turn_declines_again():
    from arenamcp.action_planner import DECLINE_DECISION

    planner = _planner()
    decision = SimpleNamespace(options=[SimpleNamespace(option_id="tgt:299")])
    assert planner._prefer_lethal_damage_target(decision, _state(8), ["tgt:299"]) == [DECLINE_DECISION]
    assert planner._prefer_lethal_damage_target(decision, _state(10), ["tgt:299"]) == [DECLINE_DECISION]
