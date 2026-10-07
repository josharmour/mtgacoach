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


# ---------------------------------------------------------------------------
# Line-search fixtures (multi-turn planning spec, work package 2), extracted on
# 2026-10-06 with the recipe above (scratch scripts, never checked in) and from:
# - the match packet ~/.arenamcp/match_packets/packet_20261006_154509_d3d701e3-
#   7fee-480e-be9a-d3cf0ad23bbe.json (the T14 mode menu);
# - three bug reports ~/.arenamcp/bug_reports/bug_20261006_{135027,174855,
#   180436}.json, reduced through an explicit whitelist: match_id, event_id,
#   format_name, seats, turn, players, the zones' cards, zone counts and the deck
#   list. Never 'settings' (license and vision API keys) or opponent names. They
#   keep the Mac bridge's shape exactly as recorded: phase "Main1", step "None",
#   card_types without the "CardType_" prefix. tests/test_strategic_states.py
#   checks each one against a whitelisted copy of its report's game_state in
#   tests/fixtures/.
# ---------------------------------------------------------------------------

# Cards the states below add (oracle text collapsed as above; power/toughness as
# printed in MTGA's card database; per-instance changes live on the state rows).
CARDS.update(
    {
        "Academic Ascent": {
            "type_line": "Instant",
            "mana_cost": "{1}{W}",
            "oracle_text": "Target creature gets +2/+2 and gains flying until end of turn.\nEmpower Jace 2.",
            "card_types": ["CardType_Instant"],
        },
        "Break Under Pressure": {
            "type_line": "Instant",
            "mana_cost": "{2}{B}",
            "oracle_text": "Target opponent sacrifices a creature or planeswalker with the greatest mana "
            "value among creatures and planeswalkers they control. You gain 2 life.",
            "card_types": ["CardType_Instant"],
        },
        "Chandra's Emberling": {
            "type_line": "Creature — Gremlin Elemental",
            "mana_cost": "{2}{R}",
            "oracle_text": "Haste\nWhenever you cast a noncreature spell, put a +1/+1 counter on this "
            "creature.",
            "power": 2,
            "toughness": 2,
            "keywords": ["haste"],
            "card_types": ["CardType_Creature"],
        },
        "Cryotheory Adept": {
            "type_line": "Creature — Human Wizard",
            "mana_cost": "{1}{U}",
            "oracle_text": "Prowess\n{o3oU}, Exile this card from your graveyard: Tap target creature and "
            "put a stun counter on it. Activate only as a sorcery.",
            "power": 2,
            "toughness": 1,
            "card_types": ["CardType_Creature"],
        },
        "Dark Matter Manipulator": {
            "type_line": "Creature — Human Warlock",
            "mana_cost": "{B}",
            "oracle_text": "When this creature enters, mill three cards.\nThis creature gets +2/+0 for every "
            "seven cards in your graveyard.",
            "power": 1,
            "toughness": 2,
            "card_types": ["CardType_Creature"],
        },
        "Dedicated Commons": {
            "type_line": "Land",
            "oracle_text": "This land enters tapped unless you control a planeswalker.\n{oT}: Add {oR} or "
            "{oW}.",
            "card_types": ["CardType_Land"],
        },
        "Divining Duelist": {
            "type_line": "Creature — Merfolk Wizard",
            "mana_cost": "{2}{U}",
            "oracle_text": "Flash\nWhen this creature enters, choose one —\n•Tap target creature.\n•Untap "
            "target creature.\n•Draw a card, then discard a card.",
            "power": 3,
            "toughness": 2,
            "card_types": ["CardType_Creature"],
        },
        "Extended Absence": {
            "type_line": "Instant",
            "mana_cost": "{3}{B}",
            "oracle_text": "Exile target creature or planeswalker. Extended Absence deals 1 damage to each "
            "opponent and you gain 1 life.",
            "card_types": ["CardType_Instant"],
        },
        "Inspired Tethermage": {
            "type_line": "Creature — Elf Warrior",
            "mana_cost": "{2}{G}",
            "oracle_text": "Whenever you put one or more loyalty counters on a planeswalker, put a +1/+1 "
            "counter on this creature.\n{o6}: Empower Jace 2.",
            "power": 3,
            "toughness": 2,
            "card_types": ["CardType_Creature"],
        },
        "Jace": {
            "type_line": "Token Planeswalker — Jace",
            "oracle_text": "Surveil 1.\nDraw a card.",
            "card_types": ["CardType_Planeswalker"],
        },
        "Jiang Yanggu, Never Alone": {
            "type_line": "Legendary Creature — Human Druid",
            "mana_cost": "{3}{G}",
            "oracle_text": "When Jiang Yanggu enters, create Mowu, a legendary 3/3 green Dog creature "
            "token.\nAt the beginning of your end step, untap all tokens you control.",
            "power": 2,
            "toughness": 2,
            "card_types": ["CardType_Creature"],
        },
        "Keeper of the Quiet Hour": {
            "type_line": "Artifact Creature — Chimera",
            "mana_cost": "{3}",
            "oracle_text": "When this creature enters, empower Jace 2.",
            "power": 3,
            "toughness": 2,
            "card_types": ["CardType_Artifact", "CardType_Creature"],
        },
        "Marwyn, the Preserver": {
            "type_line": "Legendary Creature — Elf Druid",
            "mana_cost": "{1}{G}",
            "oracle_text": "Lands you control have hexproof.\n{o2}: Return target land card from your "
            "graveyard to your hand.",
            "power": 3,
            "toughness": 2,
            "card_types": ["CardType_Creature"],
        },
        "Mindseeker Oculus": {
            "type_line": "Creature — Homunculus",
            "mana_cost": "{2}{U}",
            "oracle_text": "When this creature enters, empower Jace 4.",
            "power": 2,
            "toughness": 1,
            "card_types": ["CardType_Creature"],
        },
        "Mowu": {
            "type_line": "Token Legendary Creature — Dog",
            "power": 3,
            "toughness": 3,
            "card_types": ["CardType_Creature"],
        },
        "Pia, Determined Rebuilder": {
            "type_line": "Legendary Creature — Human Artificer",
            "mana_cost": "{2}{R}",
            "oracle_text": "When Pia enters, create a 1/1 colorless Thopter artifact creature token with "
            "flying.\n{o5oR}: Target creature gets +X/+0 until end of turn, where X is the number of "
            "artifacts you control.",
            "power": 2,
            "toughness": 2,
            "card_types": ["CardType_Creature"],
        },
        "Plains": {
            "type_line": "Basic Land — Plains",
            "oracle_text": "({T}: Add {W}.)",
            "card_types": ["CardType_Land"],
        },
        "Predictive Preparations": {
            "type_line": "Sorcery",
            "mana_cost": "{1}{W}",
            "oracle_text": "Put a +1/+1 counter on each of one or two target creatures.\nFlashback {o3oW}",
            "card_types": ["CardType_Sorcery"],
        },
        "Proft, Sinister Mastermind": {
            "type_line": "Legendary Creature — Human Rogue",
            "mana_cost": "{2}{B}",
            "oracle_text": "Threshold — You can't cast this spell unless there are seven or more cards in "
            "your graveyard.\nMenace\n{oB}, Discard this card: Target creature gets -3/-1 until end of turn.",
            "power": 5,
            "toughness": 5,
            "keywords": ["menace"],
            "card_types": ["CardType_Creature"],
        },
        "Protege's Awakening": {
            "type_line": "Sorcery",
            "mana_cost": "{3}{U}",
            "oracle_text": "Empower Jace 6.\nDraw a card.",
            "card_types": ["CardType_Sorcery"],
        },
        "Recursive Recruitment": {
            "type_line": "Sorcery",
            "mana_cost": "{2}{U}{B}",
            "oracle_text": "Create two 2/2 colorless Wizard Soldier creature tokens named Cadet. If this "
            "spell was cast from a graveyard, put a +1/+1 counter on each of them for every three cards in "
            "your graveyard.\nFlashback {o6oUoB}",
            "card_types": ["CardType_Sorcery"],
        },
        "Room of Refuge": {
            "type_line": "Land",
            "oracle_text": "This land enters tapped. As it enters, choose a color.\n{oT}: Add one mana of "
            "the chosen color.\n{o5}, {oT}, Sacrifice this land: Put two +1/+1 counters on target creature. "
            "Activate only as a sorcery.",
            "card_types": ["CardType_Land"],
        },
        "Ruric Thar, Magecrusher": {
            "type_line": "Legendary Creature — Ogre Warrior",
            "mana_cost": "{5}{G}{G}",
            "oracle_text": "This spell can't be countered.\nReach\nVigilance\nTrample\nRuric Thar has "
            "hexproof as long as they haven't dealt combat damage yet.",
            "power": 7,
            "toughness": 7,
            "keywords": ["reach", "vigilance", "trample"],
            "card_types": ["CardType_Creature"],
        },
        "Seasoned Cryomancer": {
            "type_line": "Creature — Human Wizard",
            "mana_cost": "{1}{U}{U}",
            "oracle_text": "When this creature enters, draw two cards, then discard two cards. When you "
            "discard one or more nonland cards this way, tap up to that many target creatures and put a stun "
            "counter on each of them.\n{o3oUoU}, Exile this card from your graveyard: Draw two cards.",
            "power": 2,
            "toughness": 2,
            "card_types": ["CardType_Creature"],
        },
        "Semester Foreseer": {
            "type_line": "Creature — Human Wizard",
            "mana_cost": "{3}{U}",
            "oracle_text": "This creature enters prepared.\nWhen this creature enters, surveil 1.",
            "power": 3,
            "toughness": 4,
            "card_types": ["CardType_Creature"],
        },
        "Skilled Battlecarver": {
            "type_line": "Creature — Human Warrior",
            "mana_cost": "{1}{R}",
            "oracle_text": "During your turn, this creature has first strike.\n{o1oR}: This creature gets "
            "+1/+0 until end of turn.",
            "power": 2,
            "toughness": 1,
            "card_types": ["CardType_Creature"],
        },
        "Solarium Sentry": {
            "type_line": "Creature — Cat Soldier",
            "mana_cost": "{G}{W}",
            "oracle_text": "Whenever an opponent casts a spell with mana value 2 or less, you gain 2 life.",
            "power": 3,
            "toughness": 3,
            "card_types": ["CardType_Creature"],
        },
        "Sphinx's Approach": {
            "type_line": "Instant",
            "mana_cost": "{1}{U}{U}",
            "oracle_text": "Draw two cards. Then you may exile this spell and four cards named Sphinx's "
            "Approach from your graveyard. If you do, search your library for a Sphinx creature card, put it "
            "onto the battlefield, then shuffle.\nA deck can have any number of cards named Sphinx's "
            "Approach.",
            "card_types": ["CardType_Instant"],
        },
        "Surveillance Phantasm": {
            "type_line": "Creature — Bird Illusion",
            "mana_cost": "{1}{U}",
            "oracle_text": "Defender\nFlying\nVigilance\nAs long as you've scried or surveilled this turn, "
            "this creature can attack as though it didn't have defender.\n{o3oU}: Surveil 1.",
            "power": 2,
            "toughness": 3,
            "keywords": ["defender", "flying", "vigilance"],
            "card_types": ["CardType_Creature"],
        },
        "Swamp": {
            "type_line": "Basic Land — Swamp",
            "oracle_text": "({T}: Add {B}.)",
            "card_types": ["CardType_Land"],
        },
        "Tam's Resistance": {
            "type_line": "Sorcery",
            "mana_cost": "{1}{G/U}",
            "oracle_text": "Put a +1/+1 counter on up to one target creature. It gains vigilance until end "
            "of turn.\nEmpower Jace 4.",
            "card_types": ["CardType_Sorcery"],
        },
        "Tethermage's Advantage": {
            "type_line": "Instant",
            "mana_cost": "{G}",
            "oracle_text": "Target creature gets +2/+2 and gains reach until end of turn. Untap it.",
            "card_types": ["CardType_Instant"],
        },
        "Theoretical Necromancer": {
            "type_line": "Creature — Vampire Warlock",
            "mana_cost": "{2}{B}",
            "oracle_text": "{o3oB}, Exile this card from your graveyard: Return another target creature card "
            "from your graveyard to your hand.",
            "power": 4,
            "toughness": 1,
            "card_types": ["CardType_Creature"],
        },
        "Theorix Charm": {
            "type_line": "Instant",
            "mana_cost": "{U}{B}",
            "oracle_text": "Choose one —\n•Counter target noncreature spell unless its controller pays "
            "{o2}.\n•Target creature gets -2/-2 until end of turn.\n•Mill three cards, then draw a card.",
            "card_types": ["CardType_Instant"],
        },
        "Thopter": {
            "type_line": "Token Artifact Creature — Thopter",
            "oracle_text": "Flying",
            "power": 1,
            "toughness": 1,
            "keywords": ["flying"],
            "card_types": ["CardType_Artifact", "CardType_Creature"],
        },
        "Unflinching Hortimancer": {
            "type_line": "Creature — Human Cleric",
            "mana_cost": "{1}{W}",
            "oracle_text": "Ward {o1}\nWhenever you gain life, put a +1/+1 counter on this creature.",
            "power": 2,
            "toughness": 1,
            "card_types": ["CardType_Creature"],
        },
        "Void Extrapolator": {
            "type_line": "Creature — Aetherborn Warlock",
            "mana_cost": "{1}{B}",
            "oracle_text": "This creature enters prepared.\nThreshold — This creature gets +1/+1 as long as "
            "there are seven or more cards in your graveyard.",
            "power": 2,
            "toughness": 2,
            "card_types": ["CardType_Creature"],
        },
        "Yuriko, Hope from the Shadows": {
            "type_line": "Legendary Creature — Human Ninja",
            "mana_cost": "{U}",
            "oracle_text": "Flash\nWhen Yuriko enters, choose one —\n•Target creature gets -X/-0 until end "
            "of turn, where X is the number of cards in your graveyard.\n•Surveil 2.",
            "power": 1,
            "toughness": 1,
            "card_types": ["CardType_Creature"],
        },
    }
)

# Real FRA cards (MTGA card database text, Arena's formatting variants kept) that the
# 2026-10-07 review's regression tests use.
# fmt: off
CARDS.update({
    "Surgical Precision": {
        "type_line": "Sorcery",
        "mana_cost": "{1}{W}",
        "oracle_text": "Choose one — \n•Destroy target creature with toughness 4 or greater. You gain 1 life. \n•You draw a card and gain 2 life.\nChoose one<nobr> —</nobr> \n•<indent=4%><indent=4%>Destroy target creature with toughness 4 or greater. You gain 1 life. </indent></indent>\n•<indent=4%><indent=4%>You draw a card and gain 2 life.</indent></indent>\nChoose one — \n•Destroy target creature with toughness 4 or greater. You gain 1 life. \n•You draw a card and gain 2 life.",
        "card_types": ["CardType_Sorcery"],
    },
    "Your Fate Ends Here": {
        "type_line": "Instant",
        "mana_cost": "{2}{W}",
        "oracle_text": "Destroy target creature or planeswalker with mana value 3 or greater. Surveil 1.",
        "card_types": ["CardType_Instant"],
    },
    "Vigorbloom Charm": {
        "type_line": "Instant",
        "mana_cost": "{G}{W}",
        "oracle_text": "Choose one — \n•Target permanent you control gains hexproof and indestructible until end of turn. \n•You draw a card and gain 3 life. \n•Put a +1/+1 counter on target creature you control. Then it fights target creature an opponent controls.\nChoose one<nobr> —</nobr> \n•<indent=4%><indent=4%>Target permanent you control gains hexproof and indestructible until end of turn. </indent></indent>\n•<indent=4%><indent=4%>You draw a card and gain 3 life. </indent></indent>\n•<indent=4%><indent=4%>Put a <nobr>+1/+1</nobr> counter on target creature you control. Then it fights target creature an opponent controls.</indent></indent>\nChoose one — \n•Target permanent you control gains hexproof and indestructible until end of turn. \n•You draw a card and gain 3 life. \n•Put a +1/+1 counter on target creature you control. Then it fights target creature an opponent controls.",
        "card_types": ["CardType_Instant"],
    },
    "Gideon's Memorial": {
        "type_line": "Legendary Artifact",
        "mana_cost": "{1}{W}",
        "oracle_text": "Creature tokens you control get +1/+0 and have vigilance.\nCreature tokens you control get <nobr>+1/+0</nobr> and have vigilance.\nCreature tokens you control get +1/+0 and have vigilance.\n{oT}: Add one mana of any color. Spend this mana only to cast a planeswalker spell.\n{o1oW}, Discard this card: It deals 4 damage to target attacking or blocking creature.",
        "card_types": ["CardType_Artifact"],
    },
    "Identity Echo": {
        "type_line": "Enchantment",
        "mana_cost": "{2}{R}",
        "oracle_text": "{o3oR}: Exile target creature or planeswalker you control. Reveal cards from the top of your library until you reveal a creature or planeswalker card. Put that card onto the battlefield and the rest on the bottom of your library in a random order. Activate only as a sorcery.",
        "card_types": ["CardType_Enchantment"],
    },
    "Way of the Warlord": {
        "type_line": "Legendary Enchantment",
        "mana_cost": "{2}{R}",
        "oracle_text": "When Way of the Warlord enters, empower Jace 5.\nPlaneswalkers you control have \"[-4]: This planeswalker deals 2 damage to up to one target creature or planeswalker and 2 damage to target player.\"",
        "card_types": ["CardType_Enchantment"],
    },
    "Silence the Echo": {
        "type_line": "Sorcery",
        "mana_cost": "{1}{B}",
        "oracle_text": "As an additional cost to cast this spell, sacrifice a creature or planeswalker or pay {o3}.\nDestroy target creature or planeswalker.",
        "card_types": ["CardType_Sorcery"],
    },
    "Vraska's Final Mercy": {
        "type_line": "Sorcery",
        "mana_cost": "{B}{B}",
        "oracle_text": "Choose one — \n•You lose 2 life. Destroy target creature or planeswalker. \n•You lose 2 life. Empower Jace 6.\nChoose one<nobr> —</nobr> \n•<indent=4%><indent=4%><indent=4%>You lose 2 life. Destroy target creature or planeswalker. </indent></indent></indent>\n•<indent=4%><indent=4%><indent=4%>You lose 2 life. Empower Jace 6.</indent></indent></indent>\nChoose one — \n•You lose 2 life. Destroy target creature or planeswalker. \n•You lose 2 life. Empower Jace 6.",
        "card_types": ["CardType_Sorcery"],
    },
    "Multiply by Zero": {
        "type_line": "Instant",
        "mana_cost": "{1}{B}",
        "oracle_text": "Target creature has base power and toughness 0/0 until end of turn.",
        "card_types": ["CardType_Instant"],
    },
    "Clash of Elements": {
        "type_line": "Instant",
        "mana_cost": "{1}{U}{R}",
        "oracle_text": "Choose target nonland permanent. Its owner may put it on top of their library. If they do, Clash of Elements deals 2 damage to them. If they didn't put the card on top of their library, they put it on the bottom.",
        "card_types": ["CardType_Instant"],
    },
    "Rise of the Deathbringer": {
        "type_line": "Instant",
        "mana_cost": "{4}{B}",
        "oracle_text": "Choose one — \n•Draw cards equal to the greatest power among creatures you control. You lose life equal to the number of cards drawn this way. \n•All creatures get -3/-3 until end of turn.\nChoose one<nobr> —</nobr> \n•<indent=4%><indent=4%><indent=4%>Draw cards equal to the greatest power among creatures you control. You lose life equal to the number of cards drawn this way. </indent></indent></indent>\n•<indent=4%><indent=4%><indent=4%>All creatures get <nobr>-3/-3</nobr> until end of turn.</indent></indent></indent>\nChoose one — \n•Draw cards equal to the greatest power among creatures you control. You lose life equal to the number of cards drawn this way. \n•All creatures get -3/-3 until end of turn.",
        "card_types": ["CardType_Instant"],
    },
    "Perfected Theory": {
        "type_line": "Instant",
        "mana_cost": "{U}",
        "oracle_text": "Choose one — \n•Target creature has base power and toughness 1/1 until end of turn. \n•Target creature has base power and toughness 4/5 until end of turn.\nChoose one<nobr> —</nobr> \n•<indent=4%><indent=4%>Target creature has base power and toughness 1/1 until end of turn. </indent></indent>\n•<indent=4%><indent=4%>Target creature has base power and toughness 4/5 until end of turn.</indent></indent>\nChoose one — \n•Target creature has base power and toughness 1/1 until end of turn. \n•Target creature has base power and toughness 4/5 until end of turn.",
        "card_types": ["CardType_Instant"],
    },
    "Flourishing Grapple": {
        "type_line": "Instant",
        "mana_cost": "{G}",
        "oracle_text": "Target creature or planeswalker an opponent controls that's red or white loses all abilities until end of turn. Target creature you control deals damage equal to its power to that permanent.",
        "card_types": ["CardType_Instant"],
    },
    "Heartwood Crafter": {
        "type_line": "Creature — Elf Artificer",
        "mana_cost": "{G}",
        "oracle_text": "This creature enters prepared.\n{oT}: Add {oC}. This mana can't be spent to cast spells from your hand.",
        "power": 1,
        "toughness": 1,
        "card_types": ["CardType_Creature"],
    },
    "Loot, the Anomaly": {
        "type_line": "Legendary Creature — Beast Horror",
        "mana_cost": "{2}{B}",
        "oracle_text": "If Loot's power is negative, he assigns combat damage as though his power were positive.\nThreshold — Sacrifice another creature or planeswalker: Loot gets -2/-0 until end of turn. Activate only if there are seven or more cards in your graveyard.\n<i>Threshold</i><nobr> —</nobr> Sacrifice another creature or planeswalker: Loot gets <nobr>-2/-0</nobr> until end of turn. Activate only if there are seven or more cards in your graveyard.\nThreshold — Sacrifice another creature or planeswalker: Loot gets -2/-0 until end of turn. Activate only if there are seven or more cards in your graveyard.",
        "power": -2,
        "toughness": 4,
        "card_types": ["CardType_Creature"],
    },
    "Avatar of Burgeoning Echoes": {
        "type_line": "Creature — Avatar",
        "mana_cost": "{G}{U}",
        "oracle_text": "Landfall — Whenever a land you control enters, empower Jace 2.\n<i>Landfall</i><nobr> —</nobr> Whenever a land you control enters, empower Jace 2.\nLandfall — Whenever a land you control enters, empower Jace 2.\nPlaneswalkers you control have \"[-10]: Put a +1/+1 counter on target creature for each land you control.\"\nPlaneswalkers you control have \"[-10]: Put a <nobr>+1/+1</nobr> counter on target creature for each land you control.\"\nPlaneswalkers you control have \"[-10]: Put a +1/+1 counter on target creature for each land you control.\"",
        "power": 2,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
    "Essence Burn": {
        "type_line": "Instant",
        "mana_cost": "{1}{R}",
        "oracle_text": "Essence Burn deals 5 damage to target black or green creature or planeswalker. If that permanent would die this turn, exile it instead.",
        "card_types": ["CardType_Instant"],
    },
    "Terminal Criticism": {
        "type_line": "Instant",
        "mana_cost": "{1}{B}",
        "oracle_text": "Destroy target creature or planeswalker that's blue or red. You gain 1 life.",
        "card_types": ["CardType_Instant"],
    },
})
# fmt: on

# What the Mac bridge publishes on top of CARDS (subtypes, colours, a
# battlefield land's colour production) and its own oracle text where the card
# database's differs (Fblthp keeps its reminder text).
BRIDGE_TRAITS: dict[str, dict[str, Any]] = {
    "Academic Ascent": {"colors": ["White"]},
    "Break Under Pressure": {"colors": ["Black"]},
    "Cadet": {"subtypes": ["Wizard", "Soldier"]},
    "Chandra's Emberling": {"subtypes": ["Gremlin", "Elemental"], "colors": ["Red"]},
    "Cryotheory Adept": {"subtypes": ["Human", "Wizard"], "colors": ["Blue"]},
    "Dark Matter Manipulator": {"subtypes": ["Human", "Warlock"], "colors": ["Black"]},
    "Dedicated Commons": {"color_production": ["White", "Red"]},
    "Divining Duelist": {"subtypes": ["Merfolk", "Wizard"], "colors": ["Blue"]},
    "Extended Absence": {"colors": ["Black"]},
    "Fblthp, Impossibly Lost": {"subtypes": ["Homunculus"], "colors": ["Blue"], "oracle_text": "When one or more of your opponents are dealt combat damage during your turn, draw two cards. If your library has no cards in it, you win the game. Fblthp's owner shuffles him into their library. (If you draw from an empty library this way, you still win the game.)"},
    "Forest": {"subtypes": ["Forest"], "color_production": ["Green"]},
    "Inspired Tethermage": {"subtypes": ["Elf", "Warrior"], "colors": ["Green"]},
    "Island": {"subtypes": ["Island"], "color_production": ["Blue"]},
    "Jace": {"subtypes": ["Jace"], "colors": ["Blue"]},
    "Jiang Yanggu, Never Alone": {"subtypes": ["Human", "Druid"], "colors": ["Green"]},
    "Keeper of the Quiet Hour": {"subtypes": ["Chimera"]},
    "Marwyn, the Preserver": {"subtypes": ["Elf", "Druid"], "colors": ["Green"]},
    "Mindseeker Oculus": {"subtypes": ["Homunculus"], "colors": ["Blue"]},
    "Mountain": {"subtypes": ["Mountain"], "color_production": ["Red"]},
    "Mowu": {"subtypes": ["Dog"], "colors": ["Green"]},
    "Pia, Determined Rebuilder": {"subtypes": ["Human", "Artificer"], "colors": ["Red"]},
    "Plains": {"subtypes": ["Plains"], "color_production": ["White"]},
    "Predictive Preparations": {"colors": ["White"]},
    "Proft, Sinister Mastermind": {"subtypes": ["Human", "Rogue"], "colors": ["Black"]},
    "Protege's Awakening": {"colors": ["Blue"]},
    "Recursive Recruitment": {"colors": ["Blue", "Black"]},
    "Ruric Thar, Magecrusher": {"subtypes": ["Ogre", "Warrior"], "colors": ["Green"]},
    "Seasoned Cryomancer": {"subtypes": ["Human", "Wizard"], "colors": ["Blue"]},
    "Semester Foreseer": {"subtypes": ["Human", "Wizard"], "colors": ["Blue"]},
    "Skilled Battlecarver": {"subtypes": ["Human", "Warrior"], "colors": ["Red"]},
    "Solarium Sentry": {"subtypes": ["Cat", "Soldier"], "colors": ["White", "Green"]},
    "Sphinx's Approach": {"colors": ["Blue"]},
    "Surveillance Phantasm": {"subtypes": ["Bird", "Illusion"], "colors": ["Blue"]},
    "Swamp": {"subtypes": ["Swamp"], "color_production": ["Black"]},
    "Tam's Resistance": {"colors": ["Blue", "Green"]},
    "Tethermage's Advantage": {"colors": ["Green"]},
    "Theoretical Necromancer": {"subtypes": ["Vampire", "Warlock"], "colors": ["Black"]},
    "Theorix Charm": {"colors": ["Blue", "Black"]},
    "Theorix Metamage": {"subtypes": ["Shade", "Wizard"], "colors": ["Blue", "Black"]},
    "Thopter": {"subtypes": ["Thopter"]},
    "Twinned Vision": {"colors": ["Blue", "Red"]},
    "Undulating Witness": {"subtypes": ["Serpent"], "colors": ["Blue"]},
    "Unflinching Hortimancer": {"subtypes": ["Human", "Cleric"], "colors": ["White"]},
    "Void Extrapolator": {"subtypes": ["Aetherborn", "Warlock"], "colors": ["Black"]},
    "Yuriko, Hope from the Shadows": {"subtypes": ["Human", "Ninja"], "colors": ["Blue"]},
}  # fmt: skip


def mac_phase(source: dict[str, Any]) -> dict[str, Any]:
    """``source`` with the Mac bridge's names: 'Phase_Main1' -> 'Main1', step '' -> 'None', 'Step_X' -> 'X'."""
    result = deepcopy(source)
    turn = result["turn"]
    turn["phase"] = str(turn.get("phase") or "").removeprefix("Phase_")
    turn["step"] = str(turn.get("step") or "").removeprefix("Step_") or "None"
    return result


def log_phase(source: dict[str, Any]) -> dict[str, Any]:
    """The inverse of ``mac_phase``: Player.log names ('Phase_Main1', step '' when there is none)."""
    result = deepcopy(source)
    turn = result["turn"]
    phase, step = str(turn.get("phase") or ""), str(turn.get("step") or "")
    turn["phase"] = phase if not phase or phase.startswith("Phase_") else f"Phase_{phase}"
    turn["step"] = "" if step in ("", "None") else step if step.startswith("Step_") else f"Step_{step}"
    return result


def amend(
    source: dict[str, Any], cards: dict[int, dict[str, Any]] | None = None, **fields: Any
) -> dict[str, Any]:
    """A copy of ``source`` with top-level ``fields`` replaced and ``cards`` merged into those instances."""
    result = deepcopy(source)
    result.update(deepcopy(fields))
    pending = deepcopy(cards or {})
    for zone in ("battlefield", "hand", "graveyard", "exile", "stack"):
        for entry in result.get(zone) or []:
            entry.update(pending.pop(entry["instance_id"], {}))
    if pending:
        raise KeyError(f"no such instances: {sorted(pending)}")
    return result


_COLORS = frozenset("WUBRG")


def after_cast(source: dict[str, Any], instance_id: int, stack_id: int | None = None) -> dict[str, Any]:
    """``source`` with hand card ``instance_id`` cast: on top of the stack as ``stack_id``, its cost paid.

    Taps our untapped mana sources in battlefield order, lands before mana
    rocks: each coloured pip takes the first source that makes one of its
    colours, then the generic part takes the next ones ({X} counts as 0).
    Raises ValueError when they can't pay.
    """
    from arenamcp.mulligan_policy import _land_colors

    result = deepcopy(source)
    spell = next(c for c in result["hand"] if c["instance_id"] == instance_id)
    result["hand"] = [c for c in result["hand"] if c["instance_id"] != instance_id]
    ours = [
        c
        for c in result["battlefield"]
        if c.get("controller_seat_id") == result["local_seat_id"] and not c.get("is_tapped")
    ]
    lands = [c for c in ours if "Land" in c.get("type_line", "")]
    rocks = [
        c
        for c in ours
        if not any(t in c.get("type_line", "") for t in ("Land", "Creature")) and _land_colors(c)
    ]
    sources = lands + rocks
    generic, pips = 0, []
    for symbol in (spell.get("mana_cost") or "").upper().replace("}", "").split("{")[1:]:
        colors = _COLORS.intersection(symbol.split("/"))
        if symbol.isdigit():
            generic += int(symbol)
        elif colors:
            pips.append(colors)
        elif symbol != "X":
            generic += 1
    for pip in pips:
        payer = next((c for c in sources if _land_colors(c) & pip), None)
        if payer is None:
            raise ValueError(f"no untapped source makes {'/'.join(sorted(pip))} for {spell['name']}")
        payer["is_tapped"] = True
        sources.remove(payer)
    if generic > len(sources):
        raise ValueError(f"{spell['name']} needs {generic} more mana than our untapped sources make")
    for payer in sources[:generic]:
        payer["is_tapped"] = True
    if stack_id is not None:
        spell["instance_id"] = stack_id
    result["stack"] = [spell, *(result.get("stack") or [])]
    return result


def modal_decision(
    options: list[tuple[str, str, dict[str, Any]]], request_id: tuple[int, int] = (1, 1)
) -> Any:
    """A typed CastingTimeOptions PendingDecision from (option_id, label, meta)."""
    from arenamcp.decisions import decision_from_dict

    return decision_from_dict(
        {
            "request_id": list(request_id),
            "request_type": "CastingTimeOptions",
            "options": [
                {"option_id": oid, "label": label, "payable": None, "meta": deepcopy(meta)}
                for oid, label, meta in options
            ],
            "can_pass": False,
        }
    )


def bridge_card(
    instance_id: int,
    grp_id: int,
    name: str,
    controller: int,
    zone: str,
    *,
    tapped: bool = False,
    entered: int = -1,
    **extra: Any,
) -> dict[str, Any]:
    """A card as a Mac bridge snapshot publishes it: CARDS plus BRIDGE_TRAITS, then ``extra``."""
    info = deepcopy(CARDS[name])
    traits = deepcopy(BRIDGE_TRAITS.get(name, {}))
    production = traits.pop("color_production", None)
    result = {
        "instance_id": instance_id,
        "grp_id": grp_id,
        "name": name,
        "owner_seat_id": controller,
        "controller_seat_id": controller,
        **info,
        **traits,
        "card_types": [kind.removeprefix("CardType_") for kind in info.get("card_types", [])],
        "object_kind": "TOKEN" if "Token" in info["type_line"] else "CARD",
        "is_tapped": tapped,
        "turn_entered_battlefield": entered,
    }
    if production and zone == "battlefield":
        result["color_production"] = production
    result.update(deepcopy(extra))
    return result


def bridge_state(
    *,
    match_id: str,
    event_id: str,
    format_name: str,
    turn: int,
    active: int,
    phase: str,
    step: str,
    local: int,
    life: dict[int, int],
    lands_played: dict[int, int],
    mulligans: dict[int, int],
    library: int,
    opponent_hand: int,
    battlefield: list[tuple],
    hand: list[tuple],
    graveyard: list[tuple],
    exile: list[tuple] | None = None,
    deck: list[int],
) -> dict[str, Any]:
    """Planner-shape snapshot as the Mac bridge publishes it, from compact rows.

    Rows: battlefield (instance_id, grp_id, name, controller, tapped, entered),
    hand (instance_id, grp_id, name), graveyard and exile (instance_id, grp_id,
    name, owner). A row may end with a dict of per-instance fields (counters,
    current power/toughness, ...). Cards off the battlefield keep the bridge's
    turn_entered_battlefield -1.
    """

    def cards(rows: list[tuple], zone: str) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            fields, extra = (row[:-1], row[-1]) if isinstance(row[-1], dict) else (row, {})
            if zone == "battlefield":
                iid, grp, name, controller, tapped, entered = fields
                result.append(
                    bridge_card(iid, grp, name, controller, zone, tapped=tapped, entered=entered, **extra)
                )
            else:
                iid, grp, name, *owner = fields
                result.append(bridge_card(iid, grp, name, owner[0] if owner else local, zone, **extra))
        return result

    return {
        "match_id": match_id,
        "event_id": event_id,
        "format_name": format_name,
        "local_seat_id": local,
        "opponent_seat_id": 2 if local == 1 else 1,
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
                "mulligan_count": mulligans[seat],
            }
            for seat in (1, 2)
        ],
        "battlefield": cards(battlefield, "battlefield"),
        "hand": cards(hand, "hand"),
        "graveyard": cards(graveyard, "graveyard"),
        "exile": cards(exile or [], "exile"),
        "stack": [],
        "command": [],
        "zones": {
            "library_count": library,
            "library_count_source": "bridge_total_card_count",
            "opponent_hand_count": opponent_hand,
        },
        "deck_cards": list(deck),
    }


# The T14 mode choice the game was lost on (match packet decision 18, request
# [344, 455]): once Archive Arbiter resolved, its enters trigger (instance 344,
# abilityGrpId 208284) asked for a mode. The autopilot chose Mode 1 and destroyed
# Splinter Twin (standalone.log 15:44:38); at 4 life we died to their T15 attack
# (G1_T15_FROM_OPPONENT).
G1_T14_MODE_REQUEST_ID = (344, 455)
G1_T14_MODE_MENU = [
    (
        "idx:0",
        "Mode 1: Destroy target noncreature, nonland permanent.",
        {
            "actionType": "CastingTimeOption", "choiceKind": "modal", "requestClass": "CastingTimeOption_ModalRequest",
            "childIndex": 0, "label": "Mode 1", "optionIndex": 0, "grpId": 208283, "sourceId": 344,
            "abilityGrpId": 208284, "min": 1, "max": 1,
        },
    ),
    (
        "idx:1",
        "Mode 2: You gain 4 life.",
        {
            "actionType": "CastingTimeOption", "choiceKind": "modal", "requestClass": "CastingTimeOption_ModalRequest",
            "childIndex": 0, "label": "Mode 2", "optionIndex": 1, "grpId": 121504, "sourceId": 344,
            "abilityGrpId": 208284, "min": 1, "max": 1,
        },
    ),
]  # fmt: skip
G1_T14_MODE_CHOSEN = ["idx:0"]

# G1_T14 with Archive Arbiter cast and on the stack as the modal source 344, all
# six of our mana sources tapped (the same six Arena tapped: Player.log
# gameStateId 342). A model of a cast-time mode choice only: the real menu came
# from the resolved Arbiter's trigger (G1_T14_MODE_STATE).
G1_T14_ON_STACK = after_cast(G1_T14, 200, stack_id=344)

# The real planner snapshot at decision 18 (Player.log gameStateId 344,
# replayed): Arbiter is 337 on the battlefield, every land and Murmuring Volume
# is tapped, and the stack holds the trigger 344, named "Ability (ID: 208284)"
# and linked to its source only through parent_instance_id.
G1_T14_MODE_STATE = amend(
    state(
        turn=14, active=1, phase="Phase_Main1", step="",
        life={1: 4, 2: 20}, lands_played={1: 0, 2: 0}, library=24, opponent_hand=1,
        battlefield=[(337, "Archive Arbiter", 1, False, 14), (324, "Splinter Twin", 2, False, 13), (289, "Island", 1, True, 12), (285, "Murmuring Volume", 1, True, 12), (280, "Fatehold Chronologist", 2, True, 11), (264, "Forest", 1, True, 10), (260, "Paradox Shaper", 2, True, 9), (253, "Mountain", 2, True, 9), (238, "Heartstring Puller", 2, True, 7), (237, "Mountain", 2, True, 7), (233, "Forest", 1, True, 6), (224, "Eye of Jace", 2, False, 5), (220, "Island", 2, False, 5), (213, "Forest", 1, True, 4), (203, "Mountain", 2, True, 3), (201, "Island", 1, True, 2), (199, "Island", 2, True, 1), (244, "Cadet", 2, True, 7), (333, "Cadet", 2, False, 13)],
        hand=[(336, "Island"), (295, "Island"), (120, "Countersculpt")],
        graveyard=[(293, "Undulating Witness", 1), (279, "Theorix Metamage", 1), (259, "Tetsuko Umezawa, Fugitive", 1), (246, "Sureshot Sower", 1), (218, "Twinned Vision", 1), (211, "Unsummon", 1), (278, "Blazing Crescendo", 2), (258, "Fulminous Forte", 2), (245, "Geist of Saint Thalia", 2), (235, "Island", 2), (228, "Twinned Vision", 2)],
    ),
    stack=[{"instance_id": 344, "grp_id": 208284, "name": "Ability (ID: 208284)", "type_line": "Ability", "oracle_text": "When CARDNAME enters, choose one —\n•Destroy target noncreature, nonland permanent.\n•You gain 4 life.", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "ABILITY", "parent_instance_id": 337}],
)  # fmt: skip

# Our PremierDraft UB deck in matches bec097ac and 37bdbe2d (ConnectResp deck list).
UB_DECK_CARDS = [106531] * 6 + [106529] * 10 + [106433, 106396, 106279, 106279, 106263, 106263, 106268, 106283, 106399, 106399, 106265, 106252, 106397, 106471, 106480, 106298, 106255, 106387, 106299, 106416, 106269, 106281, 106264, 106259]  # fmt: skip
UB_EVENT = {"event_id": "PremierDraft_FRA_20260929", "format_name": "PremierDraft FRA 20260929"}

# Match 37bdbe2d (seat 1), their T10 DeclareBlock step: Player.log 17:59:38
# (gameStateId 222 and its DeclareBlockersReq; line 40409 of that day's log),
# replayed. Thopter (2/2 flier, one +1/+1 counter), Pia and Chandra's Emberling
# (4/4, two counters) attack our Surveillance Phantasm, Yuriko and Cryotheory
# Adept. They hold 3 cards with Forest, Forest and Mountain open (Forest and
# Plains tapped). The autopilot blocked Phantasm->Thopter and Adept->Pia
# (G3_T10_BLOCKS_CHOSEN; Player.log 17:59:45, standalone.log 374978-374983).
# They answered with Tethermage's Advantage on the Thopter (+2/+2, reach, untap;
# the Emberling took a counter): the Phantasm died to a 4/4, Adept and Pia
# traded, and 5 unblocked damage took us to 15.
G3_T10_BLOCKS = amend(
    state(
        turn=10, active=2, phase="Phase_Combat", step="Step_DeclareBlock",
        life={1: 20, 2: 19}, lands_played={1: 0, 2: 1}, library=27, opponent_hand=3,
        battlefield=[(307, "Forest", 2, False, 10), (305, "Swamp", 1, False, 9), (302, "Cryotheory Adept", 1, False, 9), (297, "Chandra's Emberling", 2, True, 8), (296, "Forest", 2, False, 8), (294, "Island", 1, False, 7), (282, "Pia, Determined Rebuilder", 2, True, 6), (281, "Mountain", 2, False, 6), (279, "Swamp", 1, False, 5), (269, "Forest", 2, True, 4), (265, "Surveillance Phantasm", 1, False, 3), (264, "Island", 1, True, 3), (262, "Plains", 2, True, 2), (256, "Yuriko, Hope from the Shadows", 1, False, 1), (255, "Island", 1, True, 1), (287, "Thopter", 2, True, 6)],
        hand=[(288, "Island"), (277, "Room of Refuge")],
        graveyard=[(292, "Theorix Charm", 1), (278, "Twinned Vision", 1), (260, "Break Under Pressure", 1), (312, "Predictive Preparations", 2), (293, "Marwyn, the Preserver", 2)],
    ),
    cards={
        297: {"power": 4, "toughness": 4, "counters": {"unknown": 2}, "is_attacking": True, "attack_target_id": 1},
        282: {"is_attacking": True, "attack_target_id": 1},
        287: {"power": 2, "toughness": 2, "counters": {"unknown": 1}, "is_attacking": True, "attack_target_id": 1},
    },
    match_id="37bdbe2d-eef6-4508-a1db-07fe49f15dd1", **UB_EVENT, deck_cards=list(UB_DECK_CARDS),
    decision_context={"type": "declare_blockers", "legal_blockers": ["Yuriko, Hope from the Shadows", "Surveillance Phantasm", "Cryotheory Adept"], "legal_blocker_ids": [256, 265, 302], "raw_blockers": [{"blockerInstanceId": 256, "attackerInstanceIds": [282, 297], "maxAttackers": 1}, {"blockerInstanceId": 265, "attackerInstanceIds": [287, 282, 297], "maxAttackers": 1}, {"blockerInstanceId": 302, "attackerInstanceIds": [282, 297], "maxAttackers": 1}], "attacker_ids": [282, 287, 297], "attackers": ["Pia, Determined Rebuilder", "Thopter", "Chandra's Emberling"]},
)  # fmt: skip
# blocker -> attacker
G3_T10_BLOCKS_CHOSEN = {265: 287, 302: 282}

# Our TradDraft deck in match 3da54de9 (ConnectResp deck list).
TRAD_DECK_CARDS = [106533] * 8 + [106529] * 9 + [106488, 106394, 106361, 106361, 106273, 106417, 106256, 106268, 106399, 106265, 106265] + [106272] * 4 + [106327, 106255] + [106416] * 3 + [106267, 106259, 106458]  # fmt: skip

# bug_20261006_135027 (TradDraft, match 3da54de9, seat 2 after two mulligans):
# the opponent's T7 Main1. Our only lands are a tapped Mountain and Island with
# none in hand, but both Undulating Witnesses can basic-landcycle for {2}: the
# landcycling case.
BUG_135027 = bridge_state(
    match_id="3da54de9-9820-4ae2-b49e-8f81c913c446", event_id="TradDraft_FRA_20260929", format_name="TradDraft FRA 20260929",
    turn=7, active=1, phase="Main1", step="None", local=2,
    life={1: 20, 2: 20}, lands_played={1: 0, 2: 0}, mulligans={1: 0, 2: 2},
    library=31, opponent_hand=6,
    battlefield=[(281, 74121, "Forest", 1, False, 1), (283, 106533, "Mountain", 2, True, 2), (285, 74159, "Swamp", 1, False, 3), (287, 106529, "Island", 2, True, 4), (292, 74121, "Forest", 1, False, 5), (293, 106416, "Keeper of the Quiet Hour", 1, False, 5), (298, 106555, "Jace", 1, False, 5, {"counters": {"Loyalty": 1}, "loyalty": 0, "parent_instance_id": 297})],
    hand=[(240, 106272, "Undulating Witness"), (242, 106394, "Tam's Resistance"), (244, 106259, "Mindseeker Oculus"), (282, 106416, "Keeper of the Quiet Hour"), (304, 106272, "Undulating Witness")],
    graveyard=[(305, 106399, "Twinned Vision", 2), (306, 106458, "Fblthp, Impossibly Lost", 2)],
    deck=TRAD_DECK_CARDS,
)  # fmt: skip

# bug_20261006_174855 (PremierDraft, match bec097ac, seat 2): the opponent's T9
# Main1, 14 life to their 20. Seasoned Cryomancer entered on our T8 (its stun
# trigger went unused: standalone.log 17:48:19-41); Room of Refuge, which enters
# tapped, and the flash Divining Duelist are in hand.
BUG_174855 = bridge_state(
    match_id="bec097ac-88cf-4cf7-af1f-759403be5224", event_id="PremierDraft_FRA_20260929", format_name="PremierDraft FRA 20260929",
    turn=9, active=1, phase="Main1", step="None", local=2,
    life={1: 20, 2: 14}, lands_played={1: 0, 2: 0}, mulligans={1: 0, 2: 0},
    library=24, opponent_hand=4,
    battlefield=[(221, 106420, "Dedicated Commons", 1, False, 1), (223, 106531, "Swamp", 2, True, 2), (225, 101034, "Plains", 1, False, 3), (226, 106248, "Unflinching Hortimancer", 1, False, 3), (230, 106529, "Island", 2, True, 4), (236, 101037, "Mountain", 1, False, 5), (242, 106555, "Jace", 1, False, 5, {"counters": {"Loyalty": 2}, "loyalty": 2, "parent_instance_id": 241}), (251, 106529, "Island", 2, True, 6), (255, 101037, "Mountain", 1, False, 7), (263, 106264, "Seasoned Cryomancer", 2, False, 8, {"summoning_sickness": True}), (276, 106529, "Island", 2, False, 8)],
    hand=[(181, 106263, "Protege's Awakening"), (183, 106255, "Divining Duelist"), (186, 106259, "Mindseeker Oculus"), (244, 106433, "Room of Refuge"), (269, 106397, "Theorix Metamage")],
    graveyard=[(247, 106283, "Extended Absence", 2), (248, 106279, "Break Under Pressure", 2), (249, 106263, "Protege's Awakening", 2), (252, 106299, "Void Extrapolator", 2), (271, 106298, "Theoretical Necromancer", 2), (272, 106268, "Sphinx's Approach", 2), (253, 106416, "Keeper of the Quiet Hour", 1), (259, 106226, "Academic Ascent", 1), (261, 106325, "Skilled Battlecarver", 1)],
    deck=UB_DECK_CARDS,
)  # fmt: skip

# bug_20261006_180436 (PremierDraft, match 37bdbe2d, seat 1): the opponent's
# T22 Main1 at 3 life to their 18. We have no flier or reach, so their 3/3
# flying Thopter alone is lethal, and their pending attack kills us
# ("OPPONENT HAS LETHAL ON BOARD (30 through our best blocks vs 3 life)",
# standalone.log 18:04:48).
BUG_180436 = bridge_state(
    match_id="37bdbe2d-eef6-4508-a1db-07fe49f15dd1", event_id="PremierDraft_FRA_20260929", format_name="PremierDraft FRA 20260929",
    turn=22, active=2, phase="Main1", step="None", local=1,
    life={1: 3, 2: 18}, lands_played={1: 0, 2: 0}, mulligans={1: 0, 2: 0},
    library=10, opponent_hand=1,
    battlefield=[(255, 106529, "Island", 1, True, 1), (262, 96181, "Plains", 2, False, 2), (264, 106529, "Island", 1, True, 3), (269, 96191, "Forest", 2, False, 4), (279, 106531, "Swamp", 1, True, 5), (281, 96189, "Mountain", 2, False, 6), (287, 106564, "Thopter", 2, False, 6, {"counters": {"P1P1": 2}, "parent_instance_id": 286, "power": 3, "toughness": 3}), (294, 106529, "Island", 1, True, 7), (296, 96191, "Forest", 2, False, 8), (297, 106306, "Chandra's Emberling", 2, False, 8, {"counters": {"P1P1": 5}, "power": 7, "toughness": 7}), (305, 106531, "Swamp", 1, True, 9), (307, 96191, "Forest", 2, False, 10), (321, 106265, "Semester Foreseer", 1, False, 11), (329, 106529, "Island", 1, True, 11), (355, 106531, "Swamp", 1, False, 13), (357, 106388, "Solarium Sentry", 2, False, 14), (360, 106344, "Inspired Tethermage", 2, False, 14, {"counters": {"P1P1": 1}, "power": 4, "toughness": 3}), (378, 106480, "Proft, Sinister Mastermind", 1, False, 15), (383, 96191, "Forest", 2, False, 16), (401, 106551, "Cadet", 1, False, 17, {"parent_instance_id": 395}), (403, 106529, "Island", 1, False, 17, {"parent_instance_id": 319}), (412, 106506, "Jiang Yanggu, Never Alone", 2, False, 18), (418, 106560, "Mowu", 2, False, 18, {"parent_instance_id": 417}), (432, 106551, "Cadet", 1, False, 19, {"counters": {"P1P1": 3}, "parent_instance_id": 423, "power": 5, "toughness": 5}), (433, 106551, "Cadet", 1, False, 19, {"counters": {"P1P1": 3}, "parent_instance_id": 423, "power": 5, "toughness": 5}), (435, 106529, "Island", 1, False, 19, {"parent_instance_id": 419}), (437, 96189, "Mountain", 2, False, 20), (438, 106510, "Ruric Thar, Magecrusher", 2, False, 20, {"keywords": ["reach", "vigilance", "trample", "hexproof"]}), (457, 106281, "Dark Matter Manipulator", 1, False, 21, {"power": 3, "summoning_sickness": True}), (464, 106531, "Swamp", 1, False, 21), (466, 96181, "Plains", 2, False, 22)],
    hand=[(447, 106529, "Island")],
    graveyard=[(260, 106279, "Break Under Pressure", 1), (292, 106396, "Theorix Charm", 1), (317, 106269, "Surveillance Phantasm", 1), (328, 106263, "Protege's Awakening", 1), (338, 106471, "Yuriko, Hope from the Shadows", 1), (348, 106433, "Room of Refuge", 1), (353, 106531, "Swamp", 1), (374, 106416, "Keeper of the Quiet Hour", 1), (375, 106279, "Break Under Pressure", 1), (461, 106397, "Theorix Metamage", 1), (462, 106283, "Extended Absence", 1), (463, 106529, "Island", 1), (293, 106508, "Marwyn, the Preserver", 2), (316, 106352, "Tethermage's Advantage", 2), (318, 106495, "Pia, Determined Rebuilder", 2)],
    exile=[(337, 106241, "Predictive Preparations", 2), (351, 106399, "Twinned Vision", 1), (409, 106252, "Cryotheory Adept", 1), (434, 106387, "Recursive Recruitment", 1), (454, 106264, "Seasoned Cryomancer", 1)],
    deck=UB_DECK_CARDS,
)  # fmt: skip


# Two of the slowest real boards in ~/.arenamcp/bug_reports (2026-10-07 latency review),
# field-whitelisted: only what the strategic layer reads (no player names, settings or keys).
# fmt: off
# bug_20260928_212608: turn 16 (Phase_Beginning/Step_Draw).
SLOW_212608: dict[str, Any] = {
    "match_id": "b8fe5e15-0dfe-41c1-a91e-f515b3346758", "local_seat_id": 1, "opponent_seat_id": 2,
    "turn": {"turn_number": 16, "active_player": 1, "priority_player": 1, "phase": "Phase_Beginning", "step": "Step_Draw"},
    "players": [{"seat_id": 1, "life_total": 14, "is_local": True, "lands_played": 0}, {"seat_id": 2, "life_total": 27, "is_local": False, "lands_played": 0}],
    "battlefield": [
        {"instance_id": 1224, "grp_id": 71909, "name": "Vito, Thorn of the Dusk Rose", "type_line": "Legendary Creature — Vampire Cleric", "mana_cost": "{2}{B}", "oracle_text": "Whenever you gain life, target opponent loses that much life.\n{o3oBoB}: Creatures you control gain lifelink until end of turn.", "power": 1, "toughness": 3, "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 15, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Vampire", "Cleric"]},
        {"instance_id": 1223, "grp_id": 70762, "name": "Island", "type_line": "Basic Land — Island", "oracle_text": "({T}: Add {U}.)", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 15, "object_kind": "CARD", "color_production": ["2"], "card_types": ["CardType_Land"], "subtypes": ["Island"]},
        {"instance_id": 1133, "grp_id": 97444, "name": "Badgermole Cub", "type_line": "Creature — Badger Mole", "mana_cost": "{1}{G}", "oracle_text": "When this creature enters, earthbend 1.\nWhenever you tap a creature for mana, add an additional {oG}.", "power": 2, "toughness": 2, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 14, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Badger", "Mole"]},
        {"instance_id": 1127, "grp_id": 54163, "name": "Elvish Mystic", "type_line": "Creature — Elf Druid", "mana_cost": "{G}", "oracle_text": "{oT}: Add {oG}.", "power": 1, "toughness": 1, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 14, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Elf", "Druid"]},
        {"instance_id": 1108, "grp_id": 69150, "name": "Smothering Tithe", "type_line": "Enchantment", "mana_cost": "{3}{W}", "oracle_text": "Whenever an opponent draws a card, that player may pay {o2}. If the player doesn't, you create a Treasure token.", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 13, "object_kind": "CARD", "card_types": ["CardType_Enchantment"]},
        {"instance_id": 1107, "grp_id": 70763, "name": "Swamp", "type_line": "Basic Land — Swamp", "oracle_text": "({T}: Add {B}.)", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 13, "object_kind": "CARD", "color_production": ["3"], "card_types": ["CardType_Land"], "subtypes": ["Swamp"]},
        {"instance_id": 1013, "grp_id": 93819, "name": "Loot, Exuberant Explorer", "type_line": "Legendary Creature — Beast Noble", "mana_cost": "{2}{G}", "oracle_text": "You may play an additional land on each of your turns.\n{o4oGoG}, {oT}: Look at the top six cards of your library. You may reveal a creature card with mana value less than or equal to the number of lands you control from among them and put it onto the battlefield. Put the rest on the bottom in a random order.", "power": 1, "toughness": 4, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 12, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Beast", "Noble"]},
        {"instance_id": 1004, "grp_id": 103511, "name": "The Notary Hobbits", "type_line": "Legendary Creature — Halfling Advisor", "mana_cost": "{3}{G}{G}", "oracle_text": "When The Notary Hobbits enter, if they're not a token, create two tokens that are copies of them, except the tokens aren't legendary.\n{oT}: Add {oC} for each Halfling you control.", "power": 1, "toughness": 1, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 12, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Halfling", "Advisor"]},
        {"instance_id": 1001, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 12, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 995, "grp_id": 80363, "name": "Queza, Augur of Agonies", "type_line": "Legendary Creature — Octopus Advisor", "mana_cost": "{1}{W}{U}{B}", "oracle_text": "Whenever you draw a card, target opponent loses 1 life and you gain 1 life.", "power": 3, "toughness": 4, "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 11, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Octopus", "Advisor"]},
        {"instance_id": 994, "grp_id": 70762, "name": "Island", "type_line": "Basic Land — Island", "oracle_text": "({T}: Add {U}.)", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 11, "object_kind": "CARD", "color_production": ["2"], "card_types": ["CardType_Land"], "subtypes": ["Island"]},
        {"instance_id": 885, "grp_id": 73426, "name": "Turntimber, Serpentine Wood", "type_line": "Land", "oracle_text": "As this land enters, you may pay 3 life. If you don't, it enters tapped.\n{oT}: Add {oG}.", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 10, "object_kind": "CARD", "card_types": ["CardType_Land"]},
        {"instance_id": 875, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 10, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 864, "grp_id": 87050, "name": "Rest in Peace", "type_line": "Enchantment", "mana_cost": "{1}{W}", "oracle_text": "When this enchantment enters, exile all graveyards.\nIf a card or token would be put into a graveyard from anywhere, exile it instead.", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 9, "object_kind": "CARD", "card_types": ["CardType_Enchantment"]},
        {"instance_id": 863, "grp_id": 70761, "name": "Plains", "type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 9, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Plains"]},
        {"instance_id": 776, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 8, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 683, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 8, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 676, "grp_id": 96766, "name": "Icetill Explorer", "type_line": "Creature — Insect Scout", "mana_cost": "{2}{G}{G}", "oracle_text": "You may play an additional land on each of your turns.\nYou may play lands from your graveyard.\nLandfall — Whenever a land you control enters, mill a card.\n<i>Landfall</i><nobr> —</nobr> Whenever a land you control enters, mill a card.\nLandfall — Whenever a land you control enters, mill a card.", "power": 2, "toughness": 4, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 8, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Insect", "Scout"]},
        {"instance_id": 671, "grp_id": 70165, "name": "Hushbringer", "type_line": "Creature — Faerie", "mana_cost": "{1}{W}", "oracle_text": "Flying\nLifelink\nCreatures entering or dying don't cause abilities to trigger.", "power": 1, "toughness": 2, "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 7, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Faerie"]},
        {"instance_id": 670, "grp_id": 70762, "name": "Island", "type_line": "Basic Land — Island", "oracle_text": "({T}: Add {U}.)", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 7, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Island"]},
        {"instance_id": 665, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 6, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 657, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 6, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 654, "grp_id": 75964, "name": "Esper Sentinel", "type_line": "Artifact Creature — Human Soldier", "mana_cost": "{W}", "oracle_text": "Whenever an opponent casts their first noncreature spell each turn, draw a card unless that player pays {oX}, where X is this creature's power.", "power": 1, "toughness": 1, "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 5, "object_kind": "CARD", "card_types": ["CardType_Artifact", "CardType_Creature"], "subtypes": ["Human", "Soldier"]},
        {"instance_id": 653, "grp_id": 70763, "name": "Swamp", "type_line": "Basic Land — Swamp", "oracle_text": "({T}: Add {B}.)", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 5, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Swamp"]},
        {"instance_id": 551, "grp_id": 91002, "name": "Fanatic of Rhonas", "type_line": "Creature — Snake Druid", "mana_cost": "{1}{G}", "oracle_text": "{oT}: Add {oG}.\nFerocious — {oT}: Add {oGoGoGoG}. Activate only if you control a creature with power 4 or greater.\n<i>Ferocious</i><nobr> —</nobr> {oT}: Add {oGoGoGoG}. Activate only if you control a creature with power 4 or greater.\nFerocious — {oT}: Add {oGoGoGoG}. Activate only if you control a creature with power 4 or greater.\nEternalize {o2oGoG}", "power": 1, "toughness": 4, "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": True, "turn_entered_battlefield": 4, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Snake", "Druid"]},
        {"instance_id": 550, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 4, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 547, "grp_id": 87047, "name": "Land Tax", "type_line": "Enchantment", "mana_cost": "{W}", "oracle_text": "At the beginning of your upkeep, if an opponent controls more lands than you, you may search your library for up to three basic land cards, reveal them, put them into your hand, then shuffle.", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 3, "object_kind": "CARD", "card_types": ["CardType_Enchantment"]},
        {"instance_id": 448, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 2, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 446, "grp_id": 70761, "name": "Plains", "type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 1, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Plains"]},
    ],
    "hand": [
        {"instance_id": 1230, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": -1, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 674, "grp_id": 90681, "name": "Vaultborn Tyrant", "type_line": "Creature — Dinosaur", "mana_cost": "{5}{G}{G}", "oracle_text": "Trample\nWhenever this creature or another creature you control with power 4 or greater enters, you gain 3 life and draw a card.\nWhen this creature dies, if it's not a token, create a token that's a copy of it, except it's an artifact in addition to its other types.", "power": 6, "toughness": 6, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": -1, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Dinosaur"]},
        {"instance_id": 549, "grp_id": 51281, "name": "Emrakul, the Aeons Torn", "type_line": "Legendary Creature — Eldrazi", "mana_cost": "{15}", "oracle_text": "This spell can't be countered.\nWhen you cast this spell, take an extra turn after this one.\nFlying\nProtection from spells that are one or more colors\nAnnihilator 6\nWhen Emrakul is put into a graveyard from anywhere, its owner shuffles their graveyard into their library.", "power": 15, "toughness": 15, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": -1, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Eldrazi"]},
        {"instance_id": 250, "grp_id": 82718, "name": "Cityscape Leveler", "type_line": "Artifact Creature — Construct", "mana_cost": "{8}", "oracle_text": "Trample\nWhen you cast this spell and whenever this creature attacks, destroy up to one target nonland permanent. Its controller creates a tapped Powerstone token.\nUnearth {o8}", "power": 8, "toughness": 8, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": -1, "object_kind": "CARD", "card_types": ["CardType_Artifact", "CardType_Creature"], "subtypes": ["Construct"]},
        {"instance_id": 248, "grp_id": 85121, "name": "Surrak and Goreclaw", "type_line": "Legendary Creature — Human Bear", "mana_cost": "{4}{G}{G}", "oracle_text": "Trample\nOther creatures you control have trample.\nWhenever another nontoken creature you control enters, put a +1/+1 counter on it. It gains haste until end of turn.\nWhenever another nontoken creature you control enters, put a <nobr>+1/+1</nobr> counter on it. It gains haste until end of turn.\nWhenever another nontoken creature you control enters, put a +1/+1 counter on it. It gains haste until end of turn.", "power": 6, "toughness": 5, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": -1, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Human", "Bear"]},
    ],
    "stack": [
        {"instance_id": 1231, "grp_id": 121936, "name": "Ability (ID: 121936)", "type_line": "Ability", "oracle_text": "Whenever an opponent draws a card, that player may pay {o2}. If the player doesn't, you create a Treasure token.", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": -1, "object_kind": "ABILITY", "parent_instance_id": 1108},
    ],
    "graveyard": [
    ],
    "exile": [
        {"instance_id": 1228, "grp_id": 70761, "name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 2, "controller_seat_id": 2, "object_kind": "CARD"},
        {"instance_id": 1229, "grp_id": 70763, "name": "Swamp", "type_line": "Basic Land — Swamp", "owner_seat_id": 2, "controller_seat_id": 2, "object_kind": "CARD"},
        {"instance_id": 1113, "grp_id": 70761, "name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 2, "controller_seat_id": 2, "object_kind": "CARD"},
        {"instance_id": 1114, "grp_id": 70763, "name": "Swamp", "type_line": "Basic Land — Swamp", "owner_seat_id": 2, "controller_seat_id": 2, "object_kind": "CARD"},
        {"instance_id": 1003, "grp_id": 79706, "name": "Boseiju, Who Endures", "type_line": "Legendary Land", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 894, "grp_id": 78345, "name": "Fateful Absence", "type_line": "Instant", "owner_seat_id": 2, "controller_seat_id": 2, "object_kind": "CARD"},
        {"instance_id": 888, "grp_id": 70308, "name": "The Great Henge", "type_line": "Legendary Artifact", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 877, "grp_id": 105082, "name": "Shang-Chi, Master of Kung Fu", "type_line": "Legendary Creature — Human Warrior Hero", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 868, "grp_id": 6947, "name": "Enlightened Tutor", "type_line": "Instant", "owner_seat_id": 2, "controller_seat_id": 2, "object_kind": "CARD"},
        {"instance_id": 873, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 872, "grp_id": 91087, "name": "Wooded Foothills", "type_line": "Land", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 871, "grp_id": 84850, "name": "Delighted Halfling", "type_line": "Creature — Halfling Citizen", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 870, "grp_id": 86606, "name": "Emrakul, the Promised End", "type_line": "Legendary Creature — Eldrazi", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 869, "grp_id": 79961, "name": "Settle the Wilds", "type_line": "Sorcery", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
    ],
    "zones": {"library_count": 0, "opponent_hand_count": 0},
}
# bug_20260929_202111: turn 10 (Phase_Combat/Step_DeclareAttack).
SLOW_202111: dict[str, Any] = {
    "match_id": "1fc52b12-f4f6-4070-9fff-8d5d952c8d3e", "local_seat_id": 1, "opponent_seat_id": 2,
    "turn": {"turn_number": 10, "active_player": 2, "priority_player": 2, "phase": "Phase_Combat", "step": "Step_DeclareAttack"},
    "players": [{"seat_id": 1, "life_total": 22, "is_local": True, "lands_played": 0}, {"seat_id": 2, "life_total": 23, "is_local": False, "lands_played": 1}],
    "battlefield": [
        {"instance_id": 532, "grp_id": 93797, "name": "Dragon Trainer", "type_line": "Creature — Human", "mana_cost": "{3}{R}{R}", "oracle_text": "When this creature enters, create a 4/4 red Dragon creature token with flying.", "power": 1, "toughness": 1, "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 10, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Human"]},
        {"instance_id": 531, "grp_id": 96179, "name": "Plains", "type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 10, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Plains"]},
        {"instance_id": 528, "grp_id": 91002, "name": "Fanatic of Rhonas", "type_line": "Creature — Snake Druid", "mana_cost": "{1}{G}", "oracle_text": "{oT}: Add {oG}.\nFerocious — {oT}: Add {oGoGoGoG}. Activate only if you control a creature with power 4 or greater.\n<i>Ferocious</i><nobr> —</nobr> {oT}: Add {oGoGoGoG}. Activate only if you control a creature with power 4 or greater.\nFerocious — {oT}: Add {oGoGoGoG}. Activate only if you control a creature with power 4 or greater.\nEternalize {o2oGoG}", "power": 1, "toughness": 4, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 9, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Snake", "Druid"]},
        {"instance_id": 519, "grp_id": 103511, "name": "The Notary Hobbits", "type_line": "Legendary Creature — Halfling Advisor", "mana_cost": "{3}{G}{G}", "oracle_text": "When The Notary Hobbits enter, if they're not a token, create two tokens that are copies of them, except the tokens aren't legendary.\n{oT}: Add {oC} for each Halfling you control.", "power": 1, "toughness": 1, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 9, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Halfling", "Advisor"]},
        {"instance_id": 518, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": True, "turn_entered_battlefield": 9, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 508, "grp_id": 79262, "name": "Thalia's Lieutenant", "type_line": "Creature — Human Soldier", "mana_cost": "{1}{W}", "oracle_text": "When this creature enters, put a +1/+1 counter on each other Human you control.\nWhen this creature enters, put a <nobr>+1/+1</nobr> counter on each other Human you control.\nWhen this creature enters, put a +1/+1 counter on each other Human you control.\nWhenever another Human you control enters, put a +1/+1 counter on this creature.\nWhenever another Human you control enters, put a <nobr>+1/+1</nobr> counter on this creature.\nWhenever another Human you control enters, put a +1/+1 counter on this creature.", "power": 2, "toughness": 2, "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 8, "object_kind": "CARD", "counters": {"unknown": 1}, "card_types": ["CardType_Creature"], "subtypes": ["Human", "Soldier"]},
        {"instance_id": 502, "grp_id": 82509, "name": "Siege Veteran", "type_line": "Creature — Human Soldier", "mana_cost": "{2}{W}", "oracle_text": "At the beginning of combat on your turn, put a +1/+1 counter on target creature you control.\nAt the beginning of combat on your turn, put a <nobr>+1/+1</nobr> counter on target creature you control.\nAt the beginning of combat on your turn, put a +1/+1 counter on target creature you control.\nWhenever another nontoken Soldier you control dies, create a 1/1 colorless Soldier artifact creature token.", "power": 3, "toughness": 3, "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 8, "object_kind": "CARD", "counters": {"unknown": 1}, "card_types": ["CardType_Creature"], "subtypes": ["Human", "Soldier"]},
        {"instance_id": 494, "grp_id": 96248, "name": "Winota, Joiner of Forces", "type_line": "Legendary Creature — Human Warrior", "mana_cost": "{2}{R}{W}", "oracle_text": "Whenever a non-Human creature you control attacks, look at the top six cards of your library. You may put a Human creature card from among them onto the battlefield tapped and attacking. It gains indestructible until end of turn. Put the rest of the cards on the bottom of your library in a random order.\nWhenever a <nobr>non-Human</nobr> creature you control attacks, look at the top six cards of your library. You may put a Human creature card from among them onto the battlefield tapped and attacking. It gains indestructible until end of turn. Put the rest of the cards on the bottom of your library in a random order.\nWhenever a non-Human creature you control attacks, look at the top six cards of your library. You may put a Human creature card from among them onto the battlefield tapped and attacking. It gains indestructible until end of turn. Put the rest of the cards on the bottom of your library in a random order.", "power": 6, "toughness": 6, "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 8, "object_kind": "CARD", "counters": {"unknown": 2}, "card_types": ["CardType_Creature"], "subtypes": ["Human", "Warrior"]},
        {"instance_id": 493, "grp_id": 96188, "name": "Mountain", "type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 8, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Mountain"]},
        {"instance_id": 491, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": True, "turn_entered_battlefield": 7, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 481, "grp_id": 90681, "name": "Vaultborn Tyrant", "type_line": "Creature — Dinosaur", "mana_cost": "{5}{G}{G}", "oracle_text": "Trample\nWhenever this creature or another creature you control with power 4 or greater enters, you gain 3 life and draw a card.\nWhen this creature dies, if it's not a token, create a token that's a copy of it, except it's an artifact in addition to its other types.", "power": 6, "toughness": 6, "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": True, "turn_entered_battlefield": 7, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Dinosaur"]},
        {"instance_id": 476, "grp_id": 24529, "name": "Coldsteel Heart", "type_line": "Snow Artifact", "mana_cost": "{2}", "oracle_text": "This artifact enters tapped.\nAs this artifact enters, choose a color.\n{oT}: Add one mana of the chosen color.", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 6, "object_kind": "CARD", "card_types": ["CardType_Artifact"]},
        {"instance_id": 475, "grp_id": 96188, "name": "Mountain", "type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 6, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Mountain"]},
        {"instance_id": 473, "grp_id": 75553, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": True, "turn_entered_battlefield": 5, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Forest"]},
        {"instance_id": 455, "grp_id": 92302, "name": "Arabella, Abandoned Doll", "type_line": "Legendary Artifact Creature — Toy", "mana_cost": "{R}{W}", "oracle_text": "Whenever Arabella attacks, it deals X damage to each opponent and you gain X life, where X is the number of creatures you control with power 2 or less.", "power": 1, "toughness": 3, "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "is_attacking": True, "turn_entered_battlefield": 4, "object_kind": "CARD", "card_types": ["CardType_Artifact", "CardType_Creature"], "subtypes": ["Toy"]},
        {"instance_id": 454, "grp_id": 96188, "name": "Mountain", "type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 4, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Mountain"]},
        {"instance_id": 451, "grp_id": 91888, "name": "Birds of Paradise", "type_line": "Creature — Bird", "mana_cost": "{G}", "oracle_text": "Flying\n{oT}: Add one mana of any color.", "toughness": 1, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 3, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Bird"]},
        {"instance_id": 450, "grp_id": 70387, "name": "Castle Garenbrig", "type_line": "Land", "oracle_text": "This land enters tapped unless you control a Forest.\n{oT}: Add {oG}.\n{o2oGoG}, {oT}: Add six {oG}. Spend this mana only to cast creature spells or activate abilities of creatures.", "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": True, "turn_entered_battlefield": 3, "object_kind": "CARD", "card_types": ["CardType_Land"]},
        {"instance_id": 446, "grp_id": 96179, "name": "Plains", "type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": True, "turn_entered_battlefield": 2, "object_kind": "CARD", "card_types": ["CardType_Land"], "subtypes": ["Plains"]},
        {"instance_id": 443, "grp_id": 84850, "name": "Delighted Halfling", "type_line": "Creature — Halfling Citizen", "mana_cost": "{G}", "oracle_text": "{oT}: Add {oC}.\n{oT}: Add one mana of any color. Spend this mana only to cast a legendary spell, and that spell can't be countered.", "power": 1, "toughness": 2, "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": True, "turn_entered_battlefield": 1, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Halfling", "Citizen"]},
        {"instance_id": 442, "grp_id": 79706, "name": "Boseiju, Who Endures", "type_line": "Legendary Land", "oracle_text": "{oT}: Add {oG}.\nChannel — {o1oG}, Discard this card: Destroy target artifact, enchantment, or nonbasic land an opponent controls. That player may search their library for a land card with a basic land type, put it onto the battlefield, then shuffle. This ability costs {o1} less to activate for each legendary creature you control.\n<i>Channel</i><nobr> —</nobr> {o1oG}, Discard this card: Destroy target artifact, enchantment, or nonbasic land an opponent controls. That player may search their library for a land card with a basic land type, put it onto the battlefield, then shuffle. This ability costs {o1} less to activate for each legendary creature you control.\nChannel — {o1oG}, Discard this card: Destroy target artifact, enchantment, or nonbasic land an opponent controls. That player may search their library for a land card with a basic land type, put it onto the battlefield, then shuffle. This ability costs {o1} less to activate for each legendary creature you control.", "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": True, "turn_entered_battlefield": 1, "object_kind": "CARD", "card_types": ["CardType_Land"]},
        {"instance_id": 526, "grp_id": 103511, "name": "The Notary Hobbits", "type_line": "Legendary Creature — Halfling Advisor", "mana_cost": "{3}{G}{G}", "oracle_text": "When The Notary Hobbits enter, if they're not a token, create two tokens that are copies of them, except the tokens aren't legendary.\n{oT}: Add {oC} for each Halfling you control.", "power": 1, "toughness": 1, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 9, "object_kind": "TOKEN", "card_types": ["CardType_Creature"], "subtypes": ["Halfling", "Advisor"], "parent_instance_id": 525},
        {"instance_id": 527, "grp_id": 103511, "name": "The Notary Hobbits", "type_line": "Legendary Creature — Halfling Advisor", "mana_cost": "{3}{G}{G}", "oracle_text": "When The Notary Hobbits enter, if they're not a token, create two tokens that are copies of them, except the tokens aren't legendary.\n{oT}: Add {oC} for each Halfling you control.", "power": 1, "toughness": 1, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": 9, "object_kind": "TOKEN", "card_types": ["CardType_Creature"], "subtypes": ["Halfling", "Advisor"], "parent_instance_id": 525},
        {"instance_id": 540, "grp_id": 94171, "name": "Dragon", "type_line": "Token Creature — Dragon", "oracle_text": "Flying", "power": 4, "toughness": 4, "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": 10, "object_kind": "TOKEN", "card_types": ["CardType_Creature"], "subtypes": ["Dragon"], "parent_instance_id": 539},
    ],
    "hand": [
        {"instance_id": 458, "grp_id": 61645, "name": "Ulamog, the Ceaseless Hunger", "type_line": "Legendary Creature — Eldrazi", "mana_cost": "{10}", "oracle_text": "When you cast this spell, exile two target permanents.\nIndestructible\nWhenever Ulamog attacks, defending player exiles the top twenty cards of their library.", "power": 10, "toughness": 10, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": -1, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Eldrazi"]},
        {"instance_id": 249, "grp_id": 29589, "name": "Woodfall Primus", "type_line": "Creature — Treefolk Shaman", "mana_cost": "{5}{G}{G}{G}", "oracle_text": "Trample\nWhen this creature enters, destroy target noncreature permanent.\nPersist", "power": 6, "toughness": 6, "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": -1, "object_kind": "CARD", "card_types": ["CardType_Creature"], "subtypes": ["Treefolk", "Shaman"]},
        {"instance_id": 248, "grp_id": 73425, "name": "Turntimber Symbiosis", "type_line": "Sorcery", "mana_cost": "{4}{G}{G}{G}", "oracle_text": "Look at the top seven cards of your library. You may put a creature card from among them onto the battlefield. If that card has mana value 3 or less, it enters with three additional +1/+1 counters on it. Put the rest on the bottom of your library in a random order.\nLook at the top seven cards of your library. You may put a creature card from among them onto the battlefield. If that card has mana value 3 or less, it enters with three additional <nobr>+1/+1</nobr> counters on it. Put the rest on the bottom of your library in a random order.\nLook at the top seven cards of your library. You may put a creature card from among them onto the battlefield. If that card has mana value 3 or less, it enters with three additional +1/+1 counters on it. Put the rest on the bottom of your library in a random order.", "owner_seat_id": 1, "controller_seat_id": 1, "turn_entered_battlefield": -1, "object_kind": "CARD", "card_types": ["CardType_Sorcery"]},
    ],
    "stack": [
        {"instance_id": 541, "grp_id": 138356, "name": "Ability (ID: 138356)", "type_line": "Ability", "oracle_text": "At the beginning of combat on your turn, put a +1/+1 counter on target creature you control.", "owner_seat_id": 2, "controller_seat_id": 2, "turn_entered_battlefield": -1, "object_kind": "ABILITY", "parent_instance_id": 502},
    ],
    "graveyard": [
        {"instance_id": 472, "grp_id": 91011, "name": "Malevolent Rumble", "type_line": "Sorcery", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 469, "grp_id": 97474, "name": "Shared Roots", "type_line": "Sorcery — Lesson", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 470, "grp_id": 59613, "name": "Rofellos, Llanowar Emissary", "type_line": "Legendary Creature — Elf Druid", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 468, "grp_id": 91087, "name": "Wooded Foothills", "type_line": "Land", "owner_seat_id": 1, "controller_seat_id": 1, "object_kind": "CARD"},
        {"instance_id": 516, "grp_id": 71818, "name": "Selfless Savior", "type_line": "Creature — Dog", "owner_seat_id": 2, "controller_seat_id": 2, "object_kind": "CARD"},
    ],
    "exile": [
    ],
    "zones": {"library_count": 0, "opponent_hand_count": 0},
}
# fmt: on
