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


INTERVENTION = {
    "name": "Purphoros's Intervention",
    "type_line": "Sorcery",
    "oracle_text": "Choose one —\n• Create an X/1 red Elemental creature token with trample and haste. "
    "Sacrifice it at the beginning of the next end step.\n"
    "• Purphoros's Intervention deals twice X damage to target creature or planeswalker.",
}


def x_choice(source_id, value):
    return SimpleNamespace(
        source_label="Purphoros's Intervention",
        options=[
            SimpleNamespace(
                option_id=f"idx:{v}",
                meta={"choiceKind": "numeric_input", "numericValue": v, "sourceId": source_id},
            )
            for v in range(4)
        ],
    )


def test_x_damage_reads_the_chosen_x():
    from arenamcp.play_safety import x_damage_removal

    assert x_damage_removal(INTERVENTION, 1) == (2, True)
    assert x_damage_removal({"oracle_text": "Deals X damage to target creature."}, 3) == (3, False)
    where_x = {"oracle_text": "Deals X damage to target creature, where X is the number of cards in your hand."}
    assert x_damage_removal(where_x, 3) is None
    assert x_damage_removal(WRATH, 3) is None


def test_x_damage_removal_is_retargeted_at_the_chosen_x():
    """2026-10-18 21:42 (bug_20261018_214252): Purphoros's Intervention with X=1 (2 damage)
    was aimed at a 1/3 Sanguine Syphoner, which survived."""
    from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._decision_source_oracle = lambda decision, state: INTERVENTION["oracle_text"]
    planner._remember_chosen_x(x_choice(500, 1), ["idx:1"])
    state = {
        **board((283, "Sanguine Syphoner", 1, 3, {}), (284, "Leonin Warleader", 4, 4, {}), (285, "Squire", 1, 2, {})),
        "decision_context": {"source_id": 500},
    }
    targets = SimpleNamespace(
        source_label="Purphoros's Intervention",
        options=[SimpleNamespace(option_id=f"tgt:{i}") for i in (283, 284, 285)],
    )
    assert planner._prefer_lethal_damage_target(targets, state, ["tgt:283"]) == ["tgt:285"]
    no_kill = {**board((283, "Sanguine Syphoner", 1, 3, {}), (284, "Leonin Warleader", 4, 4, {})), "decision_context": {"source_id": 500}}
    two = SimpleNamespace(source_label="", options=[SimpleNamespace(option_id=f"tgt:{i}") for i in (283, 284)])
    assert planner._prefer_lethal_damage_target(two, no_kill, ["tgt:283"]) == [DECLINE_DECISION]
    # At X=2 (4 damage) the Warleader dies, so the model's pick stands.
    planner._remember_chosen_x(x_choice(500, 2), ["idx:2"])
    assert planner._prefer_lethal_damage_target(targets, state, ["tgt:284"]) == ["tgt:284"]


def test_an_x_from_another_spell_is_not_used():
    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._decision_source_oracle = lambda decision, state: INTERVENTION["oracle_text"]
    planner._remember_chosen_x(x_choice(500, 1), ["idx:1"])
    other = {**board((283, "Sanguine Syphoner", 1, 3, {}), (285, "Squire", 1, 2, {})), "decision_context": {"source_id": 777}}
    targets = SimpleNamespace(source_label="", options=[SimpleNamespace(option_id=f"tgt:{i}") for i in (283, 285)])
    assert planner._prefer_lethal_damage_target(targets, other, ["tgt:283"]) == ["tgt:283"]
