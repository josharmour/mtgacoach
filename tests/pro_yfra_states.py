"""Boards from a pro player's won PremierDraft_YFRA games (Player.log, 2026-10-18).

The GW Emrakul / Garruk / lifegain deck; we are seat 2 in every board. Card
texts are the card database's Oracle text. Each board is the decision point
the pro faced, reduced to the permanents and cards that matter for it:

- ``M1_T11``: match 1 turn 11, Garruk's -3 already made the Beast. The pro
  cast Something Worth Saving (gain 1) before Germinate Recruits (one Cadet).
- ``M1_T15``: match 1 turn 15, before Seed Suture (+1/+1 counter, gain 1)
  made the Beast 5/5 and cost-reduced Ghalta to {3}{G}... cast next.
- ``M1_T7``: match 1 turn 7, Garruk in hand with Heartwood Crafter's mana
  ("can't be spent to cast spells from your hand") among the sources.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

CARDS: dict[str, dict[str, Any]] = {
    "Forest": {
        "type_line": "Basic Land — Forest",
        "oracle_text": "({T}: Add {G}.)",
        "card_types": ["CardType_Land"],
    },
    "Plains": {
        "type_line": "Basic Land — Plains",
        "oracle_text": "({T}: Add {W}.)",
        "card_types": ["CardType_Land"],
    },
    "Island": {
        "type_line": "Basic Land — Island",
        "oracle_text": "({T}: Add {U}.)",
        "card_types": ["CardType_Land"],
    },
    "Mountain": {
        "type_line": "Basic Land — Mountain",
        "oracle_text": "({T}: Add {R}.)",
        "card_types": ["CardType_Land"],
    },
    "Heartwood Crafter": {
        "type_line": "Creature — Elf Artificer",
        "mana_cost": "{G}",
        "oracle_text": "This creature enters prepared. (While it's prepared, you may cast a copy of its "
        "spell. Doing so unprepares it.)\n{T}: Add {C}. This mana can't be spent to cast spells from your "
        "hand.",
        "power": 1,
        "toughness": 1,
        "card_types": ["CardType_Creature"],
    },
    "Heartwood": {
        "type_line": "Token Artifact — Heartwood",
        "oracle_text": "{T}: Add {R} or {G}.",
        "card_types": ["CardType_Artifact"],
    },
    "Garruk, Curse Breaker": {
        "type_line": "Legendary Planeswalker — Garruk",
        "mana_cost": "{3}{G}{G}",
        "oracle_text": "Whenever a creature you control with power 4 or greater enters, draw a card.\n"
        "[+2]: Untap up to two target lands.\n[−3]: Create a 4/4 green Beast creature token with trample.\n"
        "[−4]: Until your next turn, whenever one or more creatures attack one of your opponents, those "
        "creatures get +2/+2 and gain trample until end of turn.",
        "card_types": ["CardType_Planeswalker"],
    },
    "Beast": {
        "type_line": "Token Creature — Beast",
        "oracle_text": "Trample",
        "power": 4,
        "toughness": 4,
        "keywords": ["trample"],
        "card_types": ["CardType_Creature"],
    },
    "Something Worth Saving": {
        "type_line": "Instant",
        "mana_cost": "{1}{G}",
        "oracle_text": "Mill four cards. You may put a permanent card from among them into your hand. You "
        "gain 1 life.",
        "card_types": ["CardType_Instant"],
    },
    "Germinate Recruits": {
        "type_line": "Instant",
        "mana_cost": "{2}{W}",
        "oracle_text": "Create X 2/2 colorless Wizard Soldier creature tokens named Cadet, where X is the "
        "amount of life you gained this turn.",
        "card_types": ["CardType_Instant"],
    },
    "Flourishing Forces": {
        "type_line": "Sorcery",
        "mana_cost": "{G}{W}{W}",
        "oracle_text": "Create a 2/2 colorless Wizard Soldier creature token named Cadet. For every 3 life "
        "you gained this game, create an additional one of those tokens.",
        "card_types": ["CardType_Sorcery"],
    },
    "Ghalta the Unstoppable": {
        "type_line": "Legendary Creature — Elder Dinosaur",
        "mana_cost": "{8}{G}",
        "oracle_text": "This spell costs {X} less to cast, where X is the greatest power among creatures "
        "you control.\nTrample\nOther creatures you control have trample.",
        "power": 8,
        "toughness": 8,
        "keywords": ["trample"],
        "card_types": ["CardType_Creature"],
    },
    "Emergency Phytomedic": {
        "type_line": "Creature — Dryad Cleric",
        "mana_cost": "{G/W}",
        "oracle_text": "This creature enters prepared. (While it's prepared, you may cast a copy of its "
        "spell. Doing so unprepares it.)",
        "power": 1,
        "toughness": 1,
        "card_types": ["CardType_Creature"],
    },
    "Seed Suture": {
        "type_line": "Sorcery",
        "mana_cost": "{G/W}",
        "oracle_text": "Put a +1/+1 counter on target creature. You gain 1 life.",
        "card_types": ["CardType_Sorcery"],
    },
    "Loot, the Nexus": {
        "type_line": "Legendary Creature — Beast Noble",
        "mana_cost": "{2}{G}",
        "oracle_text": "{T}: Choose a color. Add one mana of that color for each different power among "
        "creatures you control.",
        "power": 1,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
    "Refute Destiny": {
        "type_line": "Instant",
        "mana_cost": "{1}{W}",
        "oracle_text": "Exile target creature or planeswalker an opponent controls with mana value 3 or "
        "greater.",
        "card_types": ["CardType_Instant"],
    },
    "Edgar, Moonlit Sovereign": {
        "type_line": "Legendary Creature — Werewolf Noble",
        "mana_cost": "{3}{G}{G}",
        "oracle_text": "Flash\nAt the beginning of your end step, if you didn't cast a spell this turn, put "
        "two +1/+1 counters on Edgar.\n{4}{G}: Put a +1/+1 counter on each creature you control with a "
        "+1/+1 counter on it.",
        "power": 3,
        "toughness": 3,
        "keywords": ["flash"],
        "card_types": ["CardType_Creature"],
    },
    "Saheeli, Jewel of Avishkar": {
        "type_line": "Legendary Creature — Human Artificer",
        "mana_cost": "{2}{U}{R}",
        "oracle_text": "",
        "power": 2,
        "toughness": 4,
        "card_types": ["CardType_Creature"],
    },
    "Traxos, Academy Guardian": {
        "type_line": "Legendary Artifact Creature — Construct",
        "mana_cost": "{4}",
        "oracle_text": "",
        "power": 2,
        "toughness": 6,
        "card_types": ["CardType_Artifact", "CardType_Creature"],
    },
    "Thopter": {
        "type_line": "Token Artifact Creature — Thopter",
        "oracle_text": "Flying",
        "power": 1,
        "toughness": 1,
        "keywords": ["flying"],
        "card_types": ["CardType_Artifact", "CardType_Creature"],
    },
    "Cryotheory Adept": {
        "type_line": "Creature — Human Wizard",
        "mana_cost": "{2}{U}",
        "oracle_text": "",
        "power": 3,
        "toughness": 2,
        "card_types": ["CardType_Creature"],
    },
}


def card(instance_id: int, name: str, controller: int, **extra: Any) -> dict[str, Any]:
    info = deepcopy(CARDS[name])
    result = {
        "instance_id": instance_id,
        "name": name,
        "owner_seat_id": controller,
        "controller_seat_id": controller,
        **info,
    }
    if "Token" in info["type_line"]:
        result["object_kind"] = "TOKEN"
    result.update(extra)
    return result


def board(
    *,
    turn: int,
    phase: str,
    life: dict[int, int],
    battlefield: list[tuple],
    hand: list[tuple],
    lands_played: int = 0,
    gained_turn: int = 0,
    gained_game: int = 0,
    active: int = 2,
    step: str = "",
    exile: list[tuple] = (),
) -> dict[str, Any]:
    """A planner-shape snapshot; we are seat 2. Battlefield rows: (iid, name, seat, tapped, entered)."""
    return {
        "local_seat_id": 2,
        "opponent_seat_id": 1,
        "turn": {"turn_number": turn, "active_player": active, "priority_player": active, "phase": phase,
                 "step": step},
        "players": [
            {"seat_id": 1, "life_total": life[1], "is_local": False, "lands_played": 0},
            {"seat_id": 2, "life_total": life[2], "is_local": True, "lands_played": lands_played,
             "life_gained_this_turn": gained_turn, "life_gained_this_game": gained_game},
        ],
        "battlefield": [
            card(iid, name, seat, is_tapped=tapped, turn_entered_battlefield=entered)
            for iid, name, seat, tapped, entered in battlefield
        ],
        "hand": [card(iid, name, 2) for iid, name in hand],
        "exile": [card(iid, name, 2) for iid, name in exile],
        "graveyard": [],
        "stack": [],
        "zones": {"library_count": 20, "library_count_source": "log_zone_membership",
                  "opponent_hand_count": 3},
    }  # fmt: skip


_OPP_M1 = [
    (204, "Mountain", 1, False, 2), (214, "Island", 1, False, 4), (224, "Island", 1, False, 6),
    (243, "Mountain", 1, False, 8), (244, "Saheeli, Jewel of Avishkar", 1, False, 8),
    (263, "Thopter", 1, False, 10), (266, "Traxos, Academy Guardian", 1, False, 10),
]  # fmt: skip

M1_T11 = board(
    turn=11, phase="Phase_Main1", life={1: 17, 2: 19}, lands_played=1,
    battlefield=[
        (199, "Forest", 2, False, 1), (206, "Plains", 2, False, 3), (236, "Plains", 2, False, 7),
        (250, "Plains", 2, False, 9), (200, "Heartwood Crafter", 2, False, 1), (211, "Heartwood", 2, False, 3),
        (251, "Garruk, Curse Breaker", 2, False, 9), (276, "Beast", 2, False, 11), *_OPP_M1,
    ],
    hand=[(278, "Something Worth Saving"), (164, "Germinate Recruits")],
)  # fmt: skip

# Match 1 turn 15 after the land drop. Seed Suture was Emergency Phytomedic's
# prepared copy, cast from exile; it stands in our hand here (the line search
# plans hand casts). The pro: Phytomedic, Seed Suture on the Beast (5/5), then
# Ghalta for {3}{G} - six mana from five lands and the Heartwood token.
M1_T15 = board(
    turn=15, phase="Phase_Main1", life={1: 19, 2: 7}, lands_played=1,
    battlefield=[
        (199, "Forest", 2, False, 1), (206, "Plains", 2, False, 3), (236, "Plains", 2, False, 7),
        (250, "Plains", 2, False, 9), (333, "Forest", 2, False, 15), (200, "Heartwood Crafter", 2, False, 1),
        (211, "Heartwood", 2, False, 3), (317, "Beast", 2, False, 13),
        (204, "Mountain", 1, False, 2), (214, "Island", 1, False, 4), (224, "Island", 1, False, 6),
        (243, "Mountain", 1, False, 8), (263, "Thopter", 1, False, 10), (266, "Traxos, Academy Guardian", 1, False, 10),
    ],
    hand=[(274, "Emergency Phytomedic"), (336, "Seed Suture"), (286, "Ghalta the Unstoppable")],
)  # fmt: skip

# Match 1 turn 7 after the land drop: three lands, the Heartwood token and
# Heartwood Crafter. Garruk ({3}{G}{G}) needs the Crafter's {C}, which "can't be
# spent to cast spells from your hand": the pro started the cast and cancelled.
M1_T7 = board(
    turn=7, phase="Phase_Main1", life={1: 20, 2: 17}, lands_played=1,
    battlefield=[
        (199, "Forest", 2, False, 1), (206, "Plains", 2, False, 3), (236, "Plains", 2, False, 7),
        (200, "Heartwood Crafter", 2, False, 1), (211, "Heartwood", 2, False, 3),
        (204, "Mountain", 1, False, 2), (214, "Island", 1, False, 4), (224, "Island", 1, False, 6),
        (215, "Cryotheory Adept", 1, False, 4),
    ],
    hand=[(159, "Garruk, Curse Breaker"), (163, "Refute Destiny")],
)  # fmt: skip
