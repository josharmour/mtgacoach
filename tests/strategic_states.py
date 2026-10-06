"""Real board states from the 2026-10-06 FRA PremierDraft match (game 1).

Reconstructed by replaying MTGA's Player.log (match
d3d701e3-7fee-480e-be9a-d3cf0ad23bbe, lines 9997-15056) through arenamcp's own
LogParser + GameState and taking the planner snapshot at each of our
decision points; only the fields the strategic layer reads are kept. Oracle
text is the card database's text with Arena's duplicated formatting variants
collapsed. We were seat 1, on the draw; the autopilot's actual picks are noted
on each state (from ~/.arenamcp/standalone.log 15:41-15:45).

Tests import these instead of reading ~/.arenamcp or Player.log.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

MATCH_ID = "d3d701e3-7fee-480e-be9a-d3cf0ad23bbe"

CARDS: dict[str, dict[str, Any]] = {
    "Archive Arbiter": {
        "type_line": "Artifact Creature — Sphinx",
        "mana_cost": "{6}",
        "oracle_text": "Flying\nWhen this creature enters, choose one —\n•Destroy target noncreature, nonland "
        "permanent.\n•You gain 4 life.",
        "power": 4,
        "toughness": 4,
        "keywords": ["flying"],
        "card_types": ["CardType_Artifact", "CardType_Creature"],
    },
    "Blazing Crescendo": {
        "type_line": "Instant",
        "mana_cost": "{1}{R}",
        "oracle_text": "Target creature gets +3/+1 until end of turn. Exile the top card of your library. "
        "Until the end of your next turn, you may play that card.",
        "card_types": ["CardType_Instant"],
    },
    "Cadet": {
        "type_line": "Token Creature — Wizard Soldier",
        "power": 2,
        "toughness": 2,
        "card_types": ["CardType_Creature"],
    },
    "Countersculpt": {
        "type_line": "Instant",
        "mana_cost": "{U}{U}",
        "oracle_text": "As an additional cost to cast this spell, behold a Jace or pay {o1}.\nCounter target "
        "spell. Empower Jace 1.",
        "card_types": ["CardType_Instant"],
    },
    "Eye of Jace": {
        "type_line": "Artifact",
        "mana_cost": "{1}",
        "oracle_text": "At the beginning of your upkeep, surveil 1. Then if there are seven or more cards in "
        "your graveyard, sacrifice this artifact, it deals 2 damage to each opponent, and you gain 2 life.",
        "card_types": ["CardType_Artifact"],
    },
    "Fatehold Chronologist": {
        "type_line": "Creature — Bird Wizard",
        "mana_cost": "{1}{W/U}",
        "oracle_text": "Flying\nThis creature enters prepared.",
        "power": 1,
        "toughness": 2,
        "keywords": ["flying"],
        "card_types": ["CardType_Creature"],
    },
    "Forest": {
        "type_line": "Basic Land — Forest",
        "oracle_text": "({T}: Add {G}.)",
        "card_types": ["CardType_Land"],
    },
    "Fulminous Forte": {
        "type_line": "Instant",
        "mana_cost": "{2}{R}",
        "oracle_text": "Choose one —\n•Fulminous Forte deals 1 damage to each creature and planeswalker your "
        "opponents control.\n•Fulminous Forte deals 5 damage to target creature or planeswalker.",
        "card_types": ["CardType_Instant"],
    },
    "Geist of Saint Thalia": {
        "type_line": "Legendary Creature — Spirit Cleric",
        "mana_cost": "{1}{U}",
        "oracle_text": "Flying\nNoncreature spells you cast cost {o1} less to cast.",
        "power": 1,
        "toughness": 2,
        "keywords": ["flying"],
        "card_types": ["CardType_Creature"],
    },
    "Heartstring Puller": {
        "type_line": "Creature — Elf Sorcerer",
        "mana_cost": "{3}{R}",
        "oracle_text": "Trample\nWhen this creature enters, create a 2/2 colorless Wizard Soldier creature "
        "token named Cadet.",
        "power": 3,
        "toughness": 1,
        "keywords": ["trample"],
        "card_types": ["CardType_Creature"],
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
    "Murmuring Volume": {
        "type_line": "Artifact — Book",
        "mana_cost": "{3}",
        "oracle_text": "{oT}: Add one mana of any color.\n{o2}, {oT}, Discard a card: Draw a card.",
        "card_types": ["CardType_Artifact"],
    },
    "Paradox Shaper": {
        "type_line": "Creature — Octopus Wizard",
        "mana_cost": "{1}{U/B}",
        "oracle_text": "At the beginning of your upkeep, if this creature isn't prepared, it becomes "
        "prepared.\n{o2}: Put target card from your graveyard on the bottom of your library.",
        "power": 1,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
    "Splinter Twin": {
        "type_line": "Enchantment — Aura",
        "mana_cost": "{2}{R}{R}",
        "oracle_text": "Enchant creature\nEnchanted creature has \"{oT}: Create a token that's a copy of this "
        'creature, except it has haste. Exile that token at the beginning of the next end step."',
        "card_types": ["CardType_Enchantment"],
    },
    "Sureshot Sower": {
        "type_line": "Creature — Human Archer",
        "mana_cost": "{1}{G}",
        "oracle_text": "Reach\n{o3oG}, Discard this card: Destroy target creature with flying.",
        "power": 3,
        "toughness": 1,
        "keywords": ["reach"],
        "card_types": ["CardType_Creature"],
    },
    "Tetsuko Umezawa, Fugitive": {
        "type_line": "Legendary Creature — Human Rogue",
        "mana_cost": "{1}{U}",
        "oracle_text": "Creatures you control with power or toughness 1 or less can't be blocked.",
        "power": 1,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
    "Theorix Metamage": {
        "type_line": "Creature — Shade Wizard",
        "mana_cost": "{2}{U/B}",
        "oracle_text": "This creature enters prepared.\nThreshold — This creature gets +1/+0 and has flying as "
        "long as there are seven or more cards in your graveyard.",
        "power": 2,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
    "Twinned Vision": {
        "type_line": "Instant",
        "mana_cost": "{1}{U/R}",
        "oracle_text": "Draw a card. If this spell wasn't cast from your hand, draw two cards instead.\n"
        "Flashback—{o1o(U/R)o(U/R)}, Discard a card.",
        "card_types": ["CardType_Instant"],
    },
    "Undulating Witness": {
        "type_line": "Creature — Serpent",
        "mana_cost": "{4}{U}",
        "oracle_text": "Flying\n{o2}: This creature gets +1/-1 until end of turn.\nBasic landcycling {o2}",
        "power": 3,
        "toughness": 5,
        "keywords": ["flying"],
        "card_types": ["CardType_Creature"],
    },
    "Unsummon": {
        "type_line": "Instant",
        "mana_cost": "{U}",
        "oracle_text": "Return target creature to its owner's hand.",
        "card_types": ["CardType_Instant"],
    },
    # Game 2's library-out engine (still in the library during game 1).
    "Fblthp, Impossibly Lost": {
        "type_line": "Legendary Creature — Homunculus",
        "mana_cost": "{1}{U}",
        "oracle_text": "When one or more of your opponents are dealt combat damage during your turn, draw two "
        "cards. If your library has no cards in it, you win the game. Fblthp's owner shuffles him into their "
        "library.",
        "power": 1,
        "toughness": 1,
        "card_types": ["CardType_Creature"],
    },
}

# Our 40-card deck (ConnectResp deck list, grp ids).
DECK_CARDS = [106535] * 5 + [106529] * 11 + [
    106438, 106273, 106273, 106419, 106256, 106256, 106268, 106268, 106399, 106250, 106265, 106331,
    106397, 106397, 106272, 106411, 106412, 106466, 106416, 106459, 106518, 106350, 106259, 106458,
]  # fmt: skip

# The deck cards the plan-validation tests reference (grp id -> printed data).
DECK_CATALOG = {
    106458: {"name": "Fblthp, Impossibly Lost", "type_line": "Legendary Creature — Homunculus", "cmc": 2},
    106466: {"name": "Tetsuko Umezawa, Fugitive", "type_line": "Legendary Creature — Human Rogue", "cmc": 2},
    106459: {"name": "Geist of Saint Thalia", "type_line": "Legendary Creature — Spirit Cleric", "cmc": 2},
    106412: {"name": "Archive Arbiter", "type_line": "Artifact Creature — Sphinx", "cmc": 6},
    106419: {"name": "Murmuring Volume", "type_line": "Artifact — Book", "cmc": 3},
    106272: {"name": "Undulating Witness", "type_line": "Creature — Serpent", "cmc": 5},
    106397: {"name": "Theorix Metamage", "type_line": "Creature — Shade Wizard", "cmc": 3},
    106399: {"name": "Twinned Vision", "type_line": "Instant", "cmc": 2},
    106529: {"name": "Island", "type_line": "Basic Land — Island", "cmc": 0},
    106535: {"name": "Forest", "type_line": "Basic Land — Forest", "cmc": 0},
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


def state(
    *,
    turn: int,
    active: int,
    phase: str,
    step: str,
    life: dict[int, int],
    lands_played: dict[int, int],
    library: int | None,
    opponent_hand: int,
    battlefield: list[tuple],
    hand: list[tuple],
    graveyard: list[tuple],
    local: int = 1,
) -> dict[str, Any]:
    """Planner-shape snapshot (server.get_game_state) from compact rows."""
    opponent = 2 if local == 1 else 1
    return {
        "match_id": MATCH_ID,
        "local_seat_id": local,
        "opponent_seat_id": opponent,
        "turn": {
            "turn_number": turn,
            "active_player": active,
            "priority_player": active,
            "phase": phase,
            "step": step,
        },
        "players": [
            {
                "seat_id": seat,
                "life_total": life[seat],
                "is_local": seat == local,
                "lands_played": lands_played[seat],
            }
            for seat in (1, 2)
        ],
        "battlefield": [
            card(iid, name, ctrl, is_tapped=tapped, turn_entered_battlefield=entered)
            for iid, name, ctrl, tapped, entered in battlefield
        ],
        "hand": [card(iid, name, local) for iid, name in hand],
        "graveyard": [card(iid, name, owner) for iid, name, owner in graveyard],
        "zones": {
            "library_count": library if library is not None else "?",
            "library_count_source": "log_zone_membership" if library is not None else "unknown",
            "opponent_hand_count": opponent_hand,
        },
        "deck_cards": list(DECK_CARDS) if local == 1 else [],
    }


# Our 4th turn: Sureshot Sower traded with Geist; nothing on our board vs a
# 3/1 trampler and a 2/2. The autopilot cast Tetsuko (2 of 3 mana).
G1_T8 = state(
    turn=8, active=1, phase="Phase_Main1", step="",
    life={1: 20, 2: 20}, lands_played={1: 0, 2: 0}, library=28, opponent_hand=3,
    battlefield=[(238, "Heartstring Puller", 2, False, 7), (237, "Mountain", 2, True, 7), (233, "Forest", 1, False, 6), (224, "Eye of Jace", 2, False, 5), (220, "Island", 2, True, 5), (213, "Forest", 1, False, 4), (203, "Mountain", 2, True, 3), (201, "Island", 1, False, 2), (199, "Island", 2, True, 1), (244, "Cadet", 2, False, 7)],
    hand=[(247, "Tetsuko Umezawa, Fugitive"), (229, "Undulating Witness"), (217, "Murmuring Volume"), (200, "Archive Arbiter"), (125, "Theorix Metamage"), (120, "Countersculpt")],
    graveyard=[(246, "Sureshot Sower", 1), (218, "Twinned Vision", 1), (211, "Unsummon", 1), (245, "Geist of Saint Thalia", 2), (235, "Island", 2), (228, "Twinned Vision", 2)],
)  # fmt: skip

# Our 5th turn at 15 life (Tetsuko died). The autopilot played Forest and
# Theorix Metamage.
G1_T10 = state(
    turn=10, active=1, phase="Phase_Main1", step="",
    life={1: 15, 2: 20}, lands_played={1: 0, 2: 0}, library=27, opponent_hand=1,
    battlefield=[(260, "Paradox Shaper", 2, False, 9), (253, "Mountain", 2, True, 9), (238, "Heartstring Puller", 2, True, 7), (237, "Mountain", 2, True, 7), (233, "Forest", 1, False, 6), (224, "Eye of Jace", 2, False, 5), (220, "Island", 2, True, 5), (213, "Forest", 1, False, 4), (203, "Mountain", 2, True, 3), (201, "Island", 1, False, 2), (199, "Island", 2, True, 1), (244, "Cadet", 2, True, 7)],
    hand=[(263, "Forest"), (229, "Undulating Witness"), (217, "Murmuring Volume"), (200, "Archive Arbiter"), (125, "Theorix Metamage"), (120, "Countersculpt")],
    graveyard=[(259, "Tetsuko Umezawa, Fugitive", 1), (246, "Sureshot Sower", 1), (218, "Twinned Vision", 1), (211, "Unsummon", 1), (258, "Fulminous Forte", 2), (245, "Geist of Saint Thalia", 2), (235, "Island", 2), (228, "Twinned Vision", 2)],
)  # fmt: skip

# Our 6th turn at 11 life, no creatures vs four (7 power). The autopilot cast
# Murmuring Volume (mana rock), played Island, then landcycled Undulating
# Witness — the only creature it could have cast this turn.
G1_T12 = state(
    turn=12, active=1, phase="Phase_Main1", step="",
    life={1: 11, 2: 20}, lands_played={1: 0, 2: 0}, library=26, opponent_hand=1,
    battlefield=[(280, "Fatehold Chronologist", 2, False, 11), (264, "Forest", 1, False, 10), (260, "Paradox Shaper", 2, True, 9), (253, "Mountain", 2, False, 9), (238, "Heartstring Puller", 2, True, 7), (237, "Mountain", 2, True, 7), (233, "Forest", 1, False, 6), (224, "Eye of Jace", 2, False, 5), (220, "Island", 2, True, 5), (213, "Forest", 1, False, 4), (203, "Mountain", 2, True, 3), (201, "Island", 1, False, 2), (199, "Island", 2, True, 1), (244, "Cadet", 2, True, 7)],
    hand=[(284, "Island"), (229, "Undulating Witness"), (217, "Murmuring Volume"), (200, "Archive Arbiter"), (120, "Countersculpt")],
    graveyard=[(279, "Theorix Metamage", 1), (259, "Tetsuko Umezawa, Fugitive", 1), (246, "Sureshot Sower", 1), (218, "Twinned Vision", 1), (211, "Unsummon", 1), (278, "Blazing Crescendo", 2), (258, "Fulminous Forte", 2), (245, "Geist of Saint Thalia", 2), (235, "Island", 2), (228, "Twinned Vision", 2)],
)  # fmt: skip

# The same turn right after the autopilot's Murmuring Volume: three lands
# tapped, Island still in hand — Undulating Witness is no longer castable.
G1_T12_AFTER_VOLUME = state(
    turn=12, active=1, phase="Phase_Main1", step="",
    life={1: 11, 2: 20}, lands_played={1: 0, 2: 0}, library=26, opponent_hand=1,
    battlefield=[(285, "Murmuring Volume", 1, False, 12), (280, "Fatehold Chronologist", 2, False, 11), (264, "Forest", 1, False, 10), (260, "Paradox Shaper", 2, True, 9), (253, "Mountain", 2, False, 9), (238, "Heartstring Puller", 2, True, 7), (237, "Mountain", 2, True, 7), (233, "Forest", 1, True, 6), (224, "Eye of Jace", 2, False, 5), (220, "Island", 2, True, 5), (213, "Forest", 1, True, 4), (203, "Mountain", 2, True, 3), (201, "Island", 1, True, 2), (199, "Island", 2, True, 1), (244, "Cadet", 2, True, 7)],
    hand=[(284, "Island"), (229, "Undulating Witness"), (200, "Archive Arbiter"), (120, "Countersculpt")],
    graveyard=[(279, "Theorix Metamage", 1), (259, "Tetsuko Umezawa, Fugitive", 1), (246, "Sureshot Sower", 1), (218, "Twinned Vision", 1), (211, "Unsummon", 1), (278, "Blazing Crescendo", 2), (258, "Fulminous Forte", 2), (245, "Geist of Saint Thalia", 2), (235, "Island", 2), (228, "Twinned Vision", 2)],
)  # fmt: skip

# Our 7th turn at 4 life: five opposing creatures (9 power) and Splinter Twin.
# The autopilot cast Archive Arbiter and destroyed Splinter Twin; dead next turn.
G1_T14 = state(
    turn=14, active=1, phase="Phase_Main1", step="",
    life={1: 4, 2: 20}, lands_played={1: 0, 2: 0}, library=24, opponent_hand=1,
    battlefield=[(324, "Splinter Twin", 2, False, 13), (289, "Island", 1, False, 12), (285, "Murmuring Volume", 1, False, 12), (280, "Fatehold Chronologist", 2, True, 11), (264, "Forest", 1, False, 10), (260, "Paradox Shaper", 2, True, 9), (253, "Mountain", 2, True, 9), (238, "Heartstring Puller", 2, True, 7), (237, "Mountain", 2, True, 7), (233, "Forest", 1, False, 6), (224, "Eye of Jace", 2, False, 5), (220, "Island", 2, False, 5), (213, "Forest", 1, False, 4), (203, "Mountain", 2, True, 3), (201, "Island", 1, False, 2), (199, "Island", 2, True, 1), (244, "Cadet", 2, True, 7), (333, "Cadet", 2, False, 13)],
    hand=[(336, "Island"), (295, "Island"), (200, "Archive Arbiter"), (120, "Countersculpt")],
    graveyard=[(293, "Undulating Witness", 1), (279, "Theorix Metamage", 1), (259, "Tetsuko Umezawa, Fugitive", 1), (246, "Sureshot Sower", 1), (218, "Twinned Vision", 1), (211, "Unsummon", 1), (278, "Blazing Crescendo", 2), (258, "Fulminous Forte", 2), (245, "Geist of Saint Thalia", 2), (235, "Island", 2), (228, "Twinned Vision", 2)],
)  # fmt: skip

# The opponent's T15 main phase seen from THEIR seat (2): they swing past our
# Archive Arbiter for exactly lethal (4 life) — a real lethal-on-board state.
G1_T15_FROM_OPPONENT = state(
    turn=15, active=2, phase="Phase_Main1", step="",
    life={1: 4, 2: 20}, lands_played={1: 0, 2: 0}, library=None, opponent_hand=2,
    battlefield=[(347, "Island", 1, False, 14), (337, "Archive Arbiter", 1, False, 14), (289, "Island", 1, True, 12), (285, "Murmuring Volume", 1, True, 12), (280, "Fatehold Chronologist", 2, False, 11), (264, "Forest", 1, True, 10), (260, "Paradox Shaper", 2, False, 9), (253, "Mountain", 2, False, 9), (238, "Heartstring Puller", 2, False, 7), (237, "Mountain", 2, False, 7), (233, "Forest", 1, True, 6), (224, "Eye of Jace", 2, False, 5), (220, "Island", 2, False, 5), (213, "Forest", 1, True, 4), (203, "Mountain", 2, False, 3), (201, "Island", 1, True, 2), (199, "Island", 2, False, 1), (244, "Cadet", 2, True, 7), (333, "Cadet", 2, False, 13)],
    hand=[],
    graveyard=[(293, "Undulating Witness", 1), (279, "Theorix Metamage", 1), (259, "Tetsuko Umezawa, Fugitive", 1), (246, "Sureshot Sower", 1), (218, "Twinned Vision", 1), (211, "Unsummon", 1), (346, "Splinter Twin", 2), (278, "Blazing Crescendo", 2), (258, "Fulminous Forte", 2), (245, "Geist of Saint Thalia", 2), (235, "Island", 2), (228, "Twinned Vision", 2)],
    local=2,
)  # fmt: skip


def after_land_drop(source: dict[str, Any], instance_id: int) -> dict[str, Any]:
    """``source`` with the hand land ``instance_id`` played untapped this turn."""
    result = deepcopy(source)
    land = next(c for c in result["hand"] if c["instance_id"] == instance_id)
    result["hand"] = [c for c in result["hand"] if c["instance_id"] != instance_id]
    land.update(is_tapped=False, turn_entered_battlefield=result["turn"]["turn_number"])
    result["battlefield"].insert(0, land)
    for player in result["players"]:
        if player["is_local"]:
            player["lands_played"] = 1
    return result


def actions_decision(options: list[tuple[str, str, dict[str, Any] | None, bool | None]]) -> Any:
    """A typed ActionsAvailable PendingDecision from (option_id, label, meta, payable)."""
    from arenamcp.decisions import decision_from_dict

    return decision_from_dict(
        {
            "request_id": [1, 1],
            "request_type": "ActionsAvailable",
            "options": [
                {"option_id": oid, "label": label, "payable": payable, "meta": meta or {}}
                for oid, label, meta, payable in options
            ],
            "can_pass": True,
        }
    )


def cast(instance_id: int, grp_id: int = 0) -> dict[str, Any]:
    return {"actionType": "ActionType_Cast", "instanceId": instance_id, "grpId": grp_id}


def play(instance_id: int) -> dict[str, Any]:
    return {"actionType": "ActionType_Play", "instanceId": instance_id}


def activate(instance_id: int) -> dict[str, Any]:
    return {"actionType": "ActionType_Activate", "instanceId": instance_id}


# The T12 ActionsAvailable menu the autopilot saw (match packet decision 13).
G1_T12_MENU = [
    ("idx:1", "Cast Murmuring Volume", cast(217, 106419), True),
    (
        "idx:4",
        "Activate: Undulating Witness [from hand: Basic landcycling {2} — discard it to search for a basic land "
        "card and put it into your hand]",
        activate(229),
        True,
    ),
    ("idx:5", "Play land: Island", play(284), None),
    ("pass", "Pass", None, None),
]
