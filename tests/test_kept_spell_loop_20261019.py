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


# --- #61: the -N/-N decline site loops the same way ---------------------------------

SHRINK = "target creature gets -3/-3 until end of turn."


def _shrink_planner():
    planner = _planner()
    planner._decision_source_oracle = lambda decision, state: SHRINK
    return planner


def test_a_shrink_spell_is_declined_once_per_turn():
    from arenamcp.action_planner import DECLINE_DECISION

    planner = _shrink_planner()
    decision = SimpleNamespace(options=[SimpleNamespace(option_id="tgt:299")])
    assert planner._prefer_lethal_shrink_target(decision, _state(8), ["tgt:299"]) == [DECLINE_DECISION]
    assert planner._prefer_lethal_shrink_target(decision, _state(8), ["tgt:299"]) == ["tgt:299"]
    assert planner._prefer_lethal_shrink_target(decision, _state(10), ["tgt:299"]) == [DECLINE_DECISION]


# --- #65: never give the spell back while its cast pumps an attacker ------------------

SLICKSHOT = (
    "Flying, haste\nWhenever you cast a noncreature spell, Slickshot Show-Off gets +2/+0 until end of turn.\n"
    "Plot {1}{R}"
)


def _our_turn(phase="Phase_Main1", step="", **slickshot):
    state = _state(8)
    state["turn"].update({"active_player": 2, "phase": phase, "step": step})
    state["battlefield"].append(
        {
            "instance_id": 292,
            "name": "Slickshot Show-Off",
            "type_line": "Creature — Bird Wizard",
            "oracle_text": SLICKSHOT,
            "power": 1,
            "toughness": 2,
            "controller_seat_id": 2,
            "turn_entered_battlefield": 4,
            **slickshot,
        }
    )
    return state


def test_a_cast_that_pumps_an_attacker_keeps_its_no_kill_target():
    decision = SimpleNamespace(options=[SimpleNamespace(option_id="tgt:299")])
    assert _planner()._prefer_lethal_damage_target(decision, _our_turn(), ["tgt:299"]) == ["tgt:299"]
    assert _shrink_planner()._prefer_lethal_shrink_target(decision, _our_turn(), ["tgt:299"]) == ["tgt:299"]
    # Attacking already, before blocks: the pump still lands.
    attacking = _our_turn("Phase_Combat", "Step_DeclareBlock", is_tapped=True, is_attacking=True)
    assert _planner()._prefer_lethal_damage_target(decision, attacking, ["tgt:299"]) == ["tgt:299"]


def test_no_waiting_attacker_no_exception():
    from arenamcp.action_planner import DECLINE_DECISION

    decision = SimpleNamespace(options=[SimpleNamespace(option_id="tgt:299")])
    for state in (
        _our_turn(is_tapped=True),  # it cannot attack any more
        _our_turn("Phase_Main2"),  # combat is over
        _our_turn(oracle_text="Flying\nWhenever you cast a creature spell, draw a card."),
    ):
        assert _planner()._prefer_lethal_damage_target(decision, state, ["tgt:299"]) == [DECLINE_DECISION]
    their_turn = _our_turn()
    their_turn["turn"]["active_player"] = 1
    assert _planner()._prefer_lethal_damage_target(decision, their_turn, ["tgt:299"]) == [DECLINE_DECISION]
