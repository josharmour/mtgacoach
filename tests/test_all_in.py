"""All-in attacks when no defensive line survives (bug_20261006_180436).

At 3 life facing a 3/3 flying Thopter and ~12 damage through our best blocks,
the planner held every blocker back and attacked with nothing.
"""

from __future__ import annotations

from copy import deepcopy

from arenamcp.board_assessment import ROLE_AGGRESSOR, assess
from arenamcp.combat_strategy import losing_attackers
from arenamcp.game_plan import GamePlan, validate_plan

# (instance, name, controller, power, toughness, type line, rules text) from the report.
BOARD = [
    (255, "Island", 1, None, None, "Basic Land — Island", "({T}: Add {U}.)"),
    (262, "Plains", 2, None, None, "Basic Land — Plains", "({T}: Add {W}.)"),
    (264, "Island", 1, None, None, "Basic Land — Island", "({T}: Add {U}.)"),
    (269, "Forest", 2, None, None, "Basic Land — Forest", "({T}: Add {G}.)"),
    (279, "Swamp", 1, None, None, "Basic Land — Swamp", "({T}: Add {B}.)"),
    (281, "Mountain", 2, None, None, "Basic Land — Mountain", "({T}: Add {R}.)"),
    (287, "Thopter", 2, 3, 3, "Token Artifact Creature — Thopter", "Flying"),
    (294, "Island", 1, None, None, "Basic Land — Island", "({T}: Add {U}.)"),
    (296, "Forest", 2, None, None, "Basic Land — Forest", "({T}: Add {G}.)"),
    (
        297,
        "Chandra's Emberling",
        2,
        7,
        7,
        "Creature — Gremlin Elemental",
        "Haste\nWhenever you cast a noncreature spell, put a +1/+1 counter on this creature.\nWhenever you cast a noncreature spell, put a <nobr>+1/+1</nobr> counter on th",
    ),
    (305, "Swamp", 1, None, None, "Basic Land — Swamp", "({T}: Add {B}.)"),
    (307, "Forest", 2, None, None, "Basic Land — Forest", "({T}: Add {G}.)"),
    (
        321,
        "Semester Foreseer",
        1,
        3,
        4,
        "Creature — Human Wizard",
        "This creature enters prepared.\nWhen this creature enters, surveil 1.",
    ),
    (329, "Island", 1, None, None, "Basic Land — Island", "({T}: Add {U}.)"),
    (355, "Swamp", 1, None, None, "Basic Land — Swamp", "({T}: Add {B}.)"),
    (
        357,
        "Solarium Sentry",
        2,
        3,
        3,
        "Creature — Cat Soldier",
        "Whenever an opponent casts a spell with mana value 2 or less, you gain 2 life.",
    ),
    (
        360,
        "Inspired Tethermage",
        2,
        4,
        3,
        "Creature — Elf Warrior",
        "Whenever you put one or more loyalty counters on a planeswalker, put a +1/+1 counter on this creature.\nWhenever you put one or more loyalty counters on a planes",
    ),
    (
        378,
        "Proft, Sinister Mastermind",
        1,
        5,
        5,
        "Legendary Creature — Human Rogue",
        "Threshold — You can't cast this spell unless there are seven or more cards in your graveyard.\n<i>Threshold</i><nobr> —</nobr> You can't cast this spell unless t",
    ),
    (383, "Forest", 2, None, None, "Basic Land — Forest", "({T}: Add {G}.)"),
    (401, "Cadet", 1, 2, 2, "Token Creature — Wizard Soldier", ""),
    (403, "Island", 1, None, None, "Basic Land — Island", "({T}: Add {U}.)"),
    (
        412,
        "Jiang Yanggu, Never Alone",
        2,
        2,
        2,
        "Legendary Creature — Human Druid",
        "When Jiang Yanggu enters, create Mowu, a legendary 3/3 green Dog creature token.\nAt the beginning of your end step, untap all tokens you control.",
    ),
    (418, "Mowu", 2, 3, 3, "Token Legendary Creature — Dog", ""),
    (432, "Cadet", 1, 5, 5, "Token Creature — Wizard Soldier", ""),
    (433, "Cadet", 1, 5, 5, "Token Creature — Wizard Soldier", ""),
    (435, "Island", 1, None, None, "Basic Land — Island", "({T}: Add {U}.)"),
    (437, "Mountain", 2, None, None, "Basic Land — Mountain", "({T}: Add {R}.)"),
    (
        438,
        "Ruric Thar, Magecrusher",
        2,
        7,
        7,
        "Legendary Creature — Ogre Warrior",
        "This spell can't be countered.\nReach\nVigilance\nTrample\nRuric Thar has hexproof as long as they haven't dealt combat damage yet.",
    ),
    (
        457,
        "Dark Matter Manipulator",
        1,
        3,
        2,
        "Creature — Human Warlock",
        "When this creature enters, mill three cards.\nThis creature gets +2/+0 for every seven cards in your graveyard.\nThis creature gets <nobr>+2/+0</nobr> for every s",
    ),
    (464, "Swamp", 1, None, None, "Basic Land — Swamp", "({T}: Add {B}.)"),
    (466, "Plains", 2, None, None, "Basic Land — Plains", "({T}: Add {W}.)"),
]


def _state(our_life: int) -> dict:
    battlefield = [
        {
            "instance_id": iid,
            "name": name,
            "controller_seat_id": ctrl,
            "owner_seat_id": ctrl,
            "power": power,
            "toughness": toughness,
            "type_line": type_line,
            "oracle_text": text,
            "is_tapped": False,
            "turn_entered_battlefield": 1,
        }
        for iid, name, ctrl, power, toughness, type_line, text in BOARD
    ]
    ours = [
        c["instance_id"] for c in battlefield if c["controller_seat_id"] == 1 and "Creature" in c["type_line"]
    ]
    return {
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": 21,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": 1, "life_total": our_life, "is_local": True, "lands_played": 1},
            {"seat_id": 2, "life_total": 18, "is_local": False, "lands_played": 1},
        ],
        "battlefield": battlefield,
        "hand": [],
        "graveyard": [],
        "zones": {
            "library_count": 12,
            "library_count_source": "log_zone_membership",
            "opponent_hand_count": 1,
        },
        "decision_context": {"legal_attacker_ids": ours},
    }


def test_dead_next_attack_whatever_we_do_means_all_in():
    assessment = assess(_state(3))
    assert assessment.all_in and assessment.role == ROLE_AGGRESSOR
    assert any(flag.startswith("ALL-IN") for flag in assessment.flags)


def test_a_healthy_life_total_is_not_all_in():
    assessment = assess(_state(20))
    assert not assessment.all_in


def test_plan_validator_forces_aggressor_when_all_in():
    state = _state(3)
    plan = GamePlan(role="control/stabilize", role_reason="survive the crackback and hold all blockers")
    validate_plan(plan, assess(state), state)
    assert plan.role == ROLE_AGGRESSOR
    assert any("all-in" in issue for issue in plan.issues)


def test_losing_attack_guard_steps_aside_when_all_in():
    state = _state(3)
    attackers = state["decision_context"]["legal_attacker_ids"]
    assert losing_attackers(deepcopy(state), attackers) == {}


def test_bridge_replaces_an_empty_attack_with_every_legal_attacker():
    from arenamcp.autopilot_bridge import _BridgeSubmitMixin

    state = _state(3)
    action = _BridgeSubmitMixin._all_in_attack_action(None, state, [])
    assert action is not None and len(action.attacker_names) == len(
        state["decision_context"]["legal_attacker_ids"]
    )
    assert _BridgeSubmitMixin._all_in_attack_action(None, _state(20), []) is None
