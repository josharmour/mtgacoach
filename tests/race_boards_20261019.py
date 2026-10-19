"""Boards from the 2026-10-19 Mono-Red Burn ladder run (autopilot-improvements ledger #10, #11, #19, #27).

Card text is Scryfall's (``~/.arenamcp/cache/scryfall/default_cards.json``); the
boards are the games' own, read from Player.log and the coach log:

- match 41753907 (vs G/W auras + lifegain), T7 09:02:26: we are at 16 with
  three Mountains and Emberheart Challenger; they are at 10 with a tapped 4/4
  flying trample lifelink Dawnwing Marshal (Unflinching Courage). Hand:
  Fleeting Effigy, Slickshot Show-Off, Lightning Strike, Burst Lightning. The
  best line raced ("lethal on T9") blind to the 4 life a turn the Marshal
  gains, and Strike + Burst (5 damage) on the Marshal was never a move.
- the same game, T11 09:04:09: we are at 6, they are at 14 with a tapped 5/5
  Marshal and a fresh 2/2 Good-Fortune Unicorn; Lightning Strike + Shock kill
  the Marshal, nothing else stops it.
- match 92c09c7a (vs mono-G landfall), T10: Sazh's Chocobo grows +1/+1 per land
  they play; the line projected a static 7/8.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

LOCAL, OPPONENT = 1, 2

CARDS: dict[str, dict[str, Any]] = {
    "Mountain": {
        "type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)", "card_types": ["CardType_Land"],
    },
    "Plains": {
        "type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)", "card_types": ["CardType_Land"],
    },
    "Forest": {
        "type_line": "Basic Land — Forest", "oracle_text": "({T}: Add {G}.)", "card_types": ["CardType_Land"],
    },
    "Emberheart Challenger": {
        "type_line": "Creature — Mouse Warrior", "mana_cost": "{1}{R}", "power": 2, "toughness": 2,
        "keywords": ["haste"], "card_types": ["CardType_Creature"],
        "oracle_text": "Haste\nProwess (Whenever you cast a noncreature spell, this creature gets +1/+1 until end "
        "of turn.)\nValiant — Whenever this creature becomes the target of a spell or ability you control for "
        "the first time each turn, exile the top card of your library. Until end of turn, you may play that card.",
    },
    "Fleeting Effigy": {
        "type_line": "Creature — Elemental", "mana_cost": "{R}", "power": 2, "toughness": 2,
        "keywords": ["haste"], "card_types": ["CardType_Creature"],
        "oracle_text": "Haste\nAt the beginning of your end step, return this creature to its owner's hand. "
        "(Return it only if it's on the battlefield.)\n{2}{R}: This creature gets +2/+0 until end of turn.",
    },
    "Slickshot Show-Off": {
        "type_line": "Creature — Bird Wizard", "mana_cost": "{1}{R}", "power": 1, "toughness": 2,
        "keywords": ["flying", "haste"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end "
        "of turn.\nPlot {1}{R} (You may pay {1}{R} and exile this card from your hand. Cast it as a sorcery on a "
        "later turn without paying its mana cost. Plot only as a sorcery.)",
    },
    "Hired Claw": {
        "type_line": "Creature — Lizard Mercenary", "mana_cost": "{R}", "power": 1, "toughness": 2,
        "card_types": ["CardType_Creature"], "subtypes": ["Lizard", "Mercenary"],
        "oracle_text": "Whenever you attack with one or more Lizards, this creature deals 1 damage to target "
        "opponent.\n{1}{R}: Put a +1/+1 counter on this creature. Activate only if an opponent lost life this "
        "turn and only once each turn.",
    },
    "Lightning Strike": {
        "type_line": "Instant", "mana_cost": "{1}{R}", "card_types": ["CardType_Instant"],
        "oracle_text": "Lightning Strike deals 3 damage to any target.",
    },
    "Burst Lightning": {
        "type_line": "Instant", "mana_cost": "{R}", "card_types": ["CardType_Instant"],
        "oracle_text": "Kicker {4} (You may pay an additional {4} as you cast this spell.)\nBurst Lightning deals "
        "2 damage to any target. If this spell was kicked, it deals 4 damage instead.",
    },
    "Shock": {
        "type_line": "Instant", "mana_cost": "{R}", "card_types": ["CardType_Instant"],
        "oracle_text": "Shock deals 2 damage to any target.",
    },
    "Boltwave": {
        "type_line": "Sorcery", "mana_cost": "{R}", "card_types": ["CardType_Sorcery"],
        "oracle_text": "Boltwave deals 3 damage to each opponent.",
    },
    "Tectonic Hazard": {
        "type_line": "Sorcery", "mana_cost": "{R}", "card_types": ["CardType_Sorcery"],
        "oracle_text": "Tectonic Hazard deals 1 damage to each opponent and each creature they control.",
    },
    "Dawnwing Marshal": {
        "type_line": "Creature — Cat Soldier", "mana_cost": "{1}{W}", "power": 2, "toughness": 2,
        "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying\n{4}{W}: Creatures you control get +1/+1 until end of turn.",
    },
    "Unflinching Courage": {
        "type_line": "Enchantment — Aura", "mana_cost": "{1}{G}{W}", "card_types": ["CardType_Enchantment"],
        "oracle_text": "Enchant creature\nEnchanted creature gets +2/+2 and has trample and lifelink. (Damage "
        "dealt by the creature also causes its controller to gain that much life.)",
    },
    "Good-Fortune Unicorn": {
        "type_line": "Creature — Horse", "mana_cost": "{1}{G}{W}", "power": 2, "toughness": 2,
        "card_types": ["CardType_Creature"],
        "oracle_text": "Whenever another creature you control enters, put a +1/+1 counter on that creature.",
    },
    "Elenda, Saint of Dusk": {
        "type_line": "Legendary Creature — Vampire Knight", "mana_cost": "{2}{W}{B}", "power": 4, "toughness": 4,
        "keywords": ["lifelink"], "card_types": ["CardType_Creature"],
        "oracle_text": "Lifelink, hexproof from instants\nAs long as your life total is greater than your "
        "starting life total, Elenda gets +1/+1 and has menace. Elenda gets an additional +5/+5 as long as your "
        "life total is at least 10 greater than your starting life total.",
    },
    "Sazh's Chocobo": {
        "type_line": "Creature — Bird", "mana_cost": "{G}", "power": 0, "toughness": 1,
        "card_types": ["CardType_Creature"],
        # Arena's text repeats the line in its markup variant.
        "oracle_text": "Landfall — Whenever a land you control enters, put a +1/+1 counter on this creature.\n"
        "<i>Landfall</i><nobr> —</nobr> Whenever a land you control enters, put a +1/+1 counter on this creature.",
    },
    "Steppe Lynx": {
        "type_line": "Creature — Cat", "mana_cost": "{W}", "power": 0, "toughness": 1,
        "card_types": ["CardType_Creature"],
        "oracle_text": "Landfall — Whenever a land you control enters, this creature gets +2/+2 until end of turn.",
    },
    "Icetill Explorer": {
        "type_line": "Creature — Insect Scout", "mana_cost": "{2}{G}{G}", "power": 2, "toughness": 4,
        "card_types": ["CardType_Creature"],
        "oracle_text": "You may play an additional land on each of your turns.\nYou may play lands from your "
        "graveyard.\nLandfall — Whenever a land you control enters, mill a card.",
    },
    "Kaito, Cunning Infiltrator": {
        "type_line": "Legendary Planeswalker — Kaito", "mana_cost": "{1}{U}{U}", "loyalty": "3",
        "card_types": ["CardType_Planeswalker"],
        "oracle_text": "Whenever a creature you control deals combat damage to a player, put a loyalty counter on "
        "Kaito.\n+1: Up to one target creature you control can't be blocked this turn. Whenever that creature "
        "deals damage this turn, return it to its owner's hand.\n−2: Draw a card. Then discard a card unless you "
        "attacked this turn.\n−9: You get an emblem with \"Whenever a player casts a spell, you create a 1/1 blue "
        "Ninja creature token.\"",
    },
    "Ajani Resolute": {
        "type_line": "Legendary Planeswalker — Ajani", "mana_cost": "{2}{W}{W}", "loyalty": "5",
        "card_types": ["CardType_Planeswalker"],
        "oracle_text": "+1: Put a +1/+1 counter on up to one target creature. It gains vigilance until end of "
        "turn.\n−2: Return target creature card with mana value 2 or less from your graveyard to the "
        "battlefield.\n−6: Creatures you control get +4/+4 until end of turn.",
    },
    "Youthful Valkyrie": {
        "type_line": "Creature — Angel Warrior", "mana_cost": "{1}{W}", "power": 1, "toughness": 3,
        "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying\nWhenever another Angel you control enters, put a +1/+1 counter on this creature.",
    },
    "Grizzly Bears": {
        "type_line": "Creature — Bear", "mana_cost": "{1}{G}", "power": 2, "toughness": 2,
        "card_types": ["CardType_Creature"], "oracle_text": "",
    },
    "Hill Giant": {
        "type_line": "Creature — Giant", "mana_cost": "{3}{R}", "power": 3, "toughness": 3,
        "card_types": ["CardType_Creature"], "oracle_text": "",
    },
}  # fmt: skip


def card(instance_id: int, name: str, controller: int, **extra: Any) -> dict[str, Any]:
    result = {
        "instance_id": instance_id,
        "name": name,
        "owner_seat_id": controller,
        "controller_seat_id": controller,
        **deepcopy(CARDS[name]),
    }
    result.update(extra)
    return result


def board(
    *,
    turn: int,
    life: int,
    their_life: int,
    ours: list[tuple],
    theirs: list[tuple],
    hand: list[tuple],
    lands_played: int = 1,
    opponent_hand: int = 5,
    active: int = LOCAL,
    phase: str = "Phase_Main1",
    step: str = "",
) -> dict[str, Any]:
    """A planner snapshot: ``ours``/``theirs`` rows are (id, name, tapped, entered, extra fields)."""

    def rows(entries, controller):
        return [
            card(iid, name, controller, is_tapped=tapped, turn_entered_battlefield=entered, **(extra or {}))
            for iid, name, tapped, entered, *rest in entries
            for extra in [rest[0] if rest else None]
        ]

    return {
        "match_id": "race-math-20261019",
        "local_seat_id": LOCAL,
        "opponent_seat_id": OPPONENT,
        "turn": {"turn_number": turn, "active_player": active, "priority_player": active, "phase": phase,
                 "step": step},
        "players": [
            {"seat_id": LOCAL, "life_total": life, "is_local": True, "lands_played": lands_played},
            {"seat_id": OPPONENT, "life_total": their_life, "is_local": False, "lands_played": 0},
        ],
        "battlefield": rows(ours, LOCAL) + rows(theirs, OPPONENT),
        "hand": [card(iid, name, LOCAL) for iid, name in hand],
        "graveyard": [],
        "zones": {"library_count": 30, "library_count_source": "log_zone_membership",
                  "opponent_hand_count": opponent_hand},
        "deck_cards": [],
    }  # fmt: skip


_MARSHAL_44 = {"power": 4, "toughness": 4, "keywords": ["flying", "trample", "lifelink"]}
_MARSHAL_55 = {
    "power": 5,
    "toughness": 5,
    "keywords": ["flying", "trample", "lifelink"],
    "counters": {"+1/+1": 1},
}
_THREE_MOUNTAINS = [(401, "Mountain", False, 1), (402, "Mountain", False, 3), (403, "Mountain", False, 5)]
_THEIR_LANDS = [(501, "Plains", False, 2), (502, "Forest", False, 4), (503, "Plains", False, 6)]

# 41753907 T7, 09:02:26 (local seat 2 in the game, 1 here).
MARSHAL_T7 = board(
    turn=7, life=16, their_life=10, lands_played=0, opponent_hand=6,
    ours=[*_THREE_MOUNTAINS, (289, "Emberheart Challenger", False, 3)],
    theirs=[*_THEIR_LANDS, (294, "Dawnwing Marshal", True, 4, _MARSHAL_44),
            (314, "Unflinching Courage", False, 6, {"attached_to_id": 294})],
    hand=[(310, "Fleeting Effigy"), (297, "Slickshot Show-Off"), (221, "Lightning Strike"), (318, "Burst Lightning")],
)  # fmt: skip

# The combined-kill worst case for the search's latency: seven burn spells and a creature
# in hand, five lands, four creatures of theirs that no single spell kills.
BURN_HAND = board(
    turn=11, life=12, their_life=14, lands_played=0, opponent_hand=3,
    ours=[*_THREE_MOUNTAINS, (404, "Mountain", False, 7), (405, "Mountain", False, 9),
          (289, "Emberheart Challenger", False, 3)],
    theirs=[*_THEIR_LANDS, (294, "Dawnwing Marshal", False, 4, _MARSHAL_55),
            (314, "Unflinching Courage", False, 6, {"attached_to_id": 294}),
            (601, "Hill Giant", False, 6, {"power": 4, "toughness": 4}), (602, "Hill Giant", False, 7),
            (603, "Grizzly Bears", False, 8, {"power": 3, "toughness": 3})],
    hand=[(701, "Shock"), (702, "Shock"), (703, "Lightning Strike"), (704, "Lightning Strike"),
          (705, "Burst Lightning"), (706, "Burst Lightning"), (707, "Boltwave"), (708, "Fleeting Effigy")],
)  # fmt: skip

# 41753907 T11, 09:04:09: Marshal 5/5 (Felling Blow's counter), a summoning-sick Unicorn.
MARSHAL_T11 = board(
    turn=11, life=6, their_life=14, lands_played=0, opponent_hand=5,
    ours=[*_THREE_MOUNTAINS, (339, "Slickshot Show-Off", False, 9)],
    theirs=[*_THEIR_LANDS, (294, "Dawnwing Marshal", True, 4, _MARSHAL_55),
            (314, "Unflinching Courage", False, 6, {"attached_to_id": 294}),
            (346, "Good-Fortune Unicorn", False, 10)],
    hand=[(343, "Fleeting Effigy"), (336, "Shock"), (318, "Burst Lightning"), (350, "Lightning Strike")],
)  # fmt: skip
