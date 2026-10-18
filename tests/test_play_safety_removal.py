"""Fixed-damage removal: withheld when it kills nothing, pointed at a creature it kills.

Wrath of the Bloodmane (4 damage to a creature or planeswalker) into a board of
5/5 and 4/6 creatures kills nothing (play_safety.damage_removal_kills_nothing),
and its target goes to a creature the damage actually kills
(ActionPlanner._prefer_lethal_damage_target). 2026-10-05 Arena Direct: Wrath
was cast at a 5/5. Found during draft autoplay; the behaviour is in-match play.
"""

from __future__ import annotations

from types import SimpleNamespace

WRATH = {
    "name": "Wrath of the Bloodmane",
    "type_line": "Instant",
    "oracle_text": "This spell costs {1} less to cast if you control a legendary creature.\n"
    "Wrath of the Bloodmane deals 4 damage to target creature or planeswalker.",
}


def board(*creatures, walker=False):
    battlefield = [
        {
            "instance_id": iid,
            "name": name,
            "type_line": "Creature",
            "power": p,
            "toughness": t,
            "controller_seat_id": 1,
            **extra,
        }
        for iid, name, p, t, extra in creatures
    ]
    if walker:
        battlefield.append(
            {
                "instance_id": 900,
                "name": "Walker",
                "type_line": "Legendary Planeswalker",
                "controller_seat_id": 1,
            }
        )
    return {
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": battlefield,
    }


def test_fixed_damage_removal_is_withheld_when_nothing_dies():
    from arenamcp.play_safety import damage_removal_kills_nothing, fixed_damage_removal

    assert fixed_damage_removal(WRATH) == (4, True)
    big = board((275, "Uldaros", 5, 5, {}), (276, "Ruric", 4, 6, {}))
    assert "kills no opposing creature" in damage_removal_kills_nothing(WRATH, big)
    assert (
        damage_removal_kills_nothing(WRATH, board((275, "Uldaros", 5, 5, {}), (300, "Geist", 1, 2, {}))) == ""
    )
    assert damage_removal_kills_nothing(WRATH, board((275, "Uldaros", 5, 5, {"is_attacking": True}))) == ""
    assert damage_removal_kills_nothing(WRATH, board((275, "Uldaros", 5, 5, {}), walker=True)) == ""
    burn = {**WRATH, "oracle_text": "Deals 4 damage to any target."}
    assert damage_removal_kills_nothing(burn, big) == ""


def test_damage_removal_is_pointed_at_a_creature_it_kills():
    from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._decision_source_oracle = lambda decision, state: WRATH["oracle_text"].lower()
    state = board((275, "Uldaros", 5, 5, {}), (276, "Ruric", 4, 6, {}), (300, "Geist", 1, 2, {}))
    decision = SimpleNamespace(options=[SimpleNamespace(option_id=f"tgt:{i}") for i in (275, 276, 300)])
    assert planner._prefer_lethal_damage_target(decision, state, ["tgt:275"]) == ["tgt:300"]
    assert planner._prefer_lethal_damage_target(decision, state, ["tgt:300"]) == ["tgt:300"]
    no_kill = board((275, "Uldaros", 5, 5, {}), (276, "Ruric", 4, 6, {}))
    two = SimpleNamespace(options=[SimpleNamespace(option_id=f"tgt:{i}") for i in (275, 276)])
    assert planner._prefer_lethal_damage_target(two, no_kill, ["tgt:275"]) == [DECLINE_DECISION]
