"""bug_20261006_180436 (FRA, T21-22): at 3 life facing a 3/3 flying Thopter.

~12 damage comes through our best blocks; the planner held every blocker
back and attacked with nothing, and on the opponent's T22 Main1 the game was
lost no matter what (the concede board). Shared by test_all_in, test_concede
and test_oops_emote.
"""

from __future__ import annotations

from typing import Any

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


def attack_state(our_life: int) -> dict:
    """Our T21 attack step on the board, at ``our_life``."""
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


# Our lands tapped during our turn 21, as in the report.
TAPPED = frozenset({255, 264, 279, 294, 305, 329})
PROFT_TEXT = (
    "Threshold — You can't cast this spell unless there are seven or more cards in your graveyard.\n"
    "Menace\n"
    "{oB}, Discard this card: Target creature gets -3/-1 until end of turn.\n"
    "{oB}, Discard this card: Target creature gets <nobr>-3/-1</nobr> until end of turn."
)
ISLAND = {
    "instance_id": 447,
    "name": "Island",
    "type_line": "Basic Land — Island",
    "oracle_text": "({T}: Add {U}.)",
    "mana_cost": "",
    "card_types": ["Land"],
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}
DARKNESS = {
    "instance_id": 900,
    "name": "Darkness",
    "type_line": "Instant",
    "oracle_text": "Prevent all combat damage that would be dealt this turn.",
    "mana_cost": "{oB}",
    "card_types": ["Instant"],
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}


def bug_state(
    life: int = 3,
    *,
    phase: str = "Phase_Main1",
    step: str = "",
    active: int = 2,
    hand: list[dict] | None = None,
    tapped: frozenset[int] = TAPPED,
    opp_life: int = 18,
    turn: int = 22,
) -> dict[str, Any]:
    battlefield = []
    for iid, name, ctrl, power, toughness, type_line, text in BOARD:
        battlefield.append(
            {
                "instance_id": iid,
                "name": name,
                "controller_seat_id": ctrl,
                "owner_seat_id": ctrl,
                "power": power,
                "toughness": toughness,
                "type_line": type_line,
                "oracle_text": PROFT_TEXT if name.startswith("Proft") else text,
                "keywords": ["flying"] if name == "Thopter" else [],
                "is_tapped": iid in tapped,
                "turn_entered_battlefield": 1,
            }
        )
    return {
        "match_id": "fra-3da54de9",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": turn,
            "active_player": active,
            "priority_player": active,
            "phase": phase,
            "step": step,
        },
        "players": [
            {"seat_id": 1, "life_total": life, "is_local": True, "status": "InGame", "mana_pool": {}},
            {"seat_id": 2, "life_total": opp_life, "is_local": False, "status": "InGame", "mana_pool": {}},
        ],
        "battlefield": battlefield,
        "hand": [dict(ISLAND)] if hand is None else hand,
        "graveyard": [],
        "exile": [],
        "stack": [],
        "action_history": [],
        "pending_decision": None,
        "controlled_turn": {"opponent_controlled_by_you": False, "you_controlled_by_opponent": False},
        "zones": {
            "library_count": 12,
            "library_count_source": "log_zone_membership",
            "opponent_hand_count": 1,
        },
    }
