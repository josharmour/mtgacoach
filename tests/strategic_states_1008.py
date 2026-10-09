"""Real board states from the 2026-10-08 FRA QuickDraft match (game 1, a loss).

Reconstructed by replaying MTGA's Player.log (match
07c043d8-e07b-4c15-9f05-173d2fe178ee, lines 48671-54980) through arenamcp's own
LogParser + GameState and taking the planner snapshot (``server.get_game_state``)
at the moments standalone.log printed "Board facts" (17:05-17:14); only the
fields the strategic layer reads are kept. We were seat 1 and played first
(odd turns are ours). Oracle text is the card database's text with Arena's
duplicated formatting variants collapsed.

The opponent's Tetsuko Umezawa, Fugitive makes their 1-power creatures
(Geist of Saint Thalia, Traxos, Fblthp, Heartstring Puller) unblockable; the
planner snapshot carries that as ``cant_be_blocked`` (``annotate_cant_be_blocked``
in ``server.get_game_state``), which ``state()`` reproduces.

Tests import these instead of reading ~/.arenamcp or Player.log.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from arenamcp.combat_keywords import annotate_cant_be_blocked

MATCH_ID = "07c043d8-e07b-4c15-9f05-173d2fe178ee"

CARDS: dict[str, dict[str, Any]] = {
    "Arni, Humble Scribe": {
        "type_line": "Legendary Creature — Human Wizard",
        "mana_cost": "{2}{U}",
        "oracle_text": "Whenever another nontoken creature you control enters, untap Arni.\n"
        "{T}: Draw a card, then discard a card.",
        "power": 3,
        "toughness": 2,
        "card_types": ["CardType_Creature"],
    },
    "Cadet": {
        "type_line": "Token Creature — Wizard Soldier",
        "mana_cost": "",
        "oracle_text": "",
        "power": 2,
        "toughness": 2,
        "card_types": ["CardType_Creature"],
    },
    "Divining Duelist": {
        "type_line": "Creature — Merfolk Wizard",
        "mana_cost": "{2}{U}",
        "oracle_text": "Flash\nWhen this creature enters, choose one —\n•Tap target creature.\n"
        "•Untap target creature.\n•Draw a card, then discard a card.",
        "power": 3,
        "toughness": 2,
        "keywords": ["flash"],
        "card_types": ["CardType_Creature"],
    },
    "Eye of Jace": {
        "type_line": "Artifact",
        "mana_cost": "{1}",
        "oracle_text": "At the beginning of your upkeep, surveil 1. Then if there are seven or more cards "
        "in your graveyard, sacrifice this artifact, it deals 2 damage to each opponent, and you gain 2 life.",
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
    "Garruk, Veiled Butcher": {
        "type_line": "Legendary Planeswalker — Garruk",
        "mana_cost": "{3}{B}{B}",
        "oracle_text": "If a creature an opponent controls would die, exile it instead.\n"
        "Up to one target creature gets -4/-1 until your next turn.\n"
        "Each player sacrifices a creature of their choice. If you sacrificed a creature this way, "
        "create a 4/4 green Beast creature token with trample.\n"
        "Each opponent discards two cards. For each opponent who didn't discard two nonland cards "
        "this way, you draw a card.",
        "card_types": ["CardType_Planeswalker"],
    },
    "Geist of Saint Thalia": {
        "type_line": "Legendary Creature — Spirit Cleric",
        "mana_cost": "{1}{U}",
        "oracle_text": "Flying\nNoncreature spells you cast cost {1} less to cast.",
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
    "Icy Reception": {
        "type_line": "Instant",
        "mana_cost": "{1}{U}",
        "oracle_text": "Choose one —\n•Counter target creature or legendary spell unless its controller "
        "pays {3}.\n•Target creature gets -5/-0 until end of turn.",
        "card_types": ["CardType_Instant"],
    },
    "Island": {
        "type_line": "Basic Land — Island",
        "mana_cost": "",
        "oracle_text": "({T}: Add {U}.)",
        "card_types": ["CardType_Land"],
    },
    "Jiang Yanggu, Alone": {
        "type_line": "Legendary Creature — Human Berserker",
        "mana_cost": "{4}{R}",
        "oracle_text": "Menace\nWhenever a creature you control attacks a player alone, discard a card, "
        "then draw a card. Then put a +1/+1 counter on that creature for each card you've discarded "
        "this turn.",
        "power": 4,
        "toughness": 4,
        "keywords": ["menace"],
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
    "Koth, the Geomancer": {
        "type_line": "Legendary Creature — Human Warrior",
        "mana_cost": "{2}{R}",
        "oracle_text": "Reach\nLandfall — Whenever a land you control enters, Koth deals 1 damage to each "
        "opponent. If that land is a Mountain, add {R}.",
        "power": 3,
        "toughness": 2,
        "keywords": ["reach"],
        "card_types": ["CardType_Creature"],
    },
    "Living Library": {
        "type_line": "Artifact Creature — Book Illusion",
        "mana_cost": "{2}",
        "oracle_text": "{6}, Sacrifice this creature: Choose target creature or planeswalker an opponent "
        "controls. Its owner shuffles it into their library.",
        "power": 0,
        "toughness": 4,
        "card_types": ["CardType_Artifact", "CardType_Creature"],
    },
    "Mindseeker Oculus": {
        "type_line": "Creature — Homunculus",
        "mana_cost": "{2}{U}",
        "oracle_text": "When this creature enters, empower Jace 4.",
        "power": 2,
        "toughness": 1,
        "card_types": ["CardType_Creature"],
    },
    "Mountain": {
        "type_line": "Basic Land — Mountain",
        "mana_cost": "",
        "oracle_text": "({T}: Add {R}.)",
        "card_types": ["CardType_Land"],
    },
    "Murmuring Volume": {
        "type_line": "Artifact — Book",
        "mana_cost": "{3}",
        "oracle_text": "{T}: Add one mana of any color.\n{2}, {T}, Discard a card: Draw a card.",
        "card_types": ["CardType_Artifact"],
    },
    "Plan for All Outcomes": {
        "type_line": "Enchantment",
        "mana_cost": "{3}{U}",
        "oracle_text": "When this enchantment enters, the owner of up to one other target nonland permanent "
        "puts it on their choice of the top or bottom of their library.\n"
        "Whenever you cast your first noncreature spell each turn, empower Jace 1.",
        "card_types": ["CardType_Enchantment"],
    },
    "Protege's Awakening": {
        "type_line": "Sorcery",
        "mana_cost": "{3}{U}",
        "oracle_text": "Empower Jace 6.\nDraw a card.",
        "card_types": ["CardType_Sorcery"],
    },
    "Screeching Soulbreaker": {
        "type_line": "Creature — Siren Bard",
        "mana_cost": "{2}{B}",
        "oracle_text": "Flying\nWhenever this creature attacks, it deals 1 damage to each opponent and you "
        "gain 1 life.",
        "power": 1,
        "toughness": 4,
        "keywords": ["flying"],
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
    "Surveillance Phantasm": {
        "type_line": "Creature — Bird Illusion",
        "mana_cost": "{1}{U}",
        "oracle_text": "Defender\nFlying\nVigilance\nAs long as you've scried or surveilled this turn, this "
        "creature can attack as though it didn't have defender.\n{3}{U}: Surveil 1.",
        "power": 2,
        "toughness": 3,
        "keywords": ["defender", "flying", "vigilance"],
        "card_types": ["CardType_Creature"],
    },
    "Swamp": {
        "type_line": "Basic Land — Swamp",
        "mana_cost": "",
        "oracle_text": "({T}: Add {B}.)",
        "card_types": ["CardType_Land"],
    },
    "Tetsuko Umezawa, Fugitive": {
        "type_line": "Legendary Creature — Human Rogue",
        "mana_cost": "{1}{U}",
        "oracle_text": "Creatures you control with power or toughness 1 or less can't be blocked.",
        "power": 1,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
    "Traxos, Academy Guardian": {
        "type_line": "Legendary Artifact Creature — Dragon Construct",
        "mana_cost": "{3}{U}",
        "oracle_text": "This spell costs {2} less to cast if you've cast a noncreature spell this turn.\n"
        "Flying\nVigilance\nProwess",
        "power": 1,
        "toughness": 5,
        "keywords": ["flying", "vigilance", "prowess"],
        "card_types": ["CardType_Artifact", "CardType_Creature"],
    },
    "Void Extrapolator": {
        "type_line": "Creature — Aetherborn Warlock",
        "mana_cost": "{1}{B}",
        "oracle_text": "This creature enters prepared.\nThreshold — This creature gets +1/+1 as long as "
        "there are seven or more cards in your graveyard.",
        "power": 3,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
}


def card(instance_id: int, name: str, controller: int, **extra: Any) -> dict[str, Any]:
    """A card from CARDS with these fields (``power``/``toughness`` overrides model a pump)."""
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
    library: int,
    opponent_hand: int,
    battlefield: list[tuple],
    hand: list[tuple],
    local: int = 1,
) -> dict[str, Any]:
    """Planner-shape snapshot (server.get_game_state) from compact rows.

    Battlefield rows: ``(instance_id, name, controller, tapped, turn_entered)`` with
    optional trailing ``{"is_attacking": True, "power": 4, ...}`` overrides.
    """
    opponent = 2 if local == 1 else 1
    cards = []
    for row in battlefield:
        iid, name, ctrl, tapped, entered = row[:5]
        extra = row[5] if len(row) > 5 else {}
        cards.append(card(iid, name, ctrl, is_tapped=tapped, turn_entered_battlefield=entered, **extra))
    annotate_cant_be_blocked(cards)
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
        "battlefield": cards,
        "hand": [card(iid, name, local) for iid, name in hand],
        "graveyard": [],
        "zones": {
            "library_count": library,
            "library_count_source": "log_zone_membership",
            "opponent_hand_count": opponent_hand,
        },
    }


# 17:05:35 — their T4 main phase: Geist of Saint Thalia just entered, nothing on
# our board. Logged: "ours none, theirs 20; 0 vs 1 creatures, 0 vs 1 power".
T4_GEIST = state(
    turn=4, active=2, phase="Phase_Main1", step="",
    life={1: 20, 2: 20}, lands_played={1: 0, 2: 1}, library=31, opponent_hand=6,
    battlefield=[(211, "Geist of Saint Thalia", 2, False, 4), (210, "Island", 2, True, 4), (203, "Island", 1, True, 3), (201, "Mountain", 2, True, 2), (199, "Island", 1, True, 1)],
    hand=[(207, "Living Library"), (125, "Arni, Humble Scribe"), (123, "Swamp"), (122, "Icy Reception"), (121, "Keeper of the Quiet Hour"), (119, "Garruk, Veiled Butcher")],
)  # fmt: skip

# 17:05:50 — their T6 main phase: our Arni (3/2, cast T5) vs their Geist (1/2
# flyer). Logged: "ours 8, theirs 20; 1 vs 1 creatures, 3 vs 1 power".
T6_ARNI_VS_GEIST = state(
    turn=6, active=2, phase="Phase_Main1", step="",
    life={1: 20, 2: 20}, lands_played={1: 0, 2: 0}, library=30, opponent_hand=7,
    battlefield=[(216, "Arni, Humble Scribe", 1, False, 5), (215, "Island", 1, True, 5), (211, "Geist of Saint Thalia", 2, False, 4), (210, "Island", 2, False, 4), (203, "Island", 1, True, 3), (201, "Mountain", 2, False, 2), (199, "Island", 1, True, 1)],
    hand=[(207, "Living Library"), (123, "Swamp"), (122, "Icy Reception"), (121, "Keeper of the Quiet Hour"), (119, "Garruk, Veiled Butcher")],
)  # fmt: skip

# 17:07:43 — their T8 main phase at 19 life: Keeper (untapped) and Arni (tapped
# from looting on our T7) vs Tetsuko (1/3) and Geist (1/2 flyer), both
# unblockable through Tetsuko. Logged: "ours 8, theirs 10; 2 vs 2 creatures,
# 6 vs 2 power".
T8_TWO_UNBLOCKABLE = state(
    turn=8, active=2, phase="Phase_Main1", step="",
    life={1: 19, 2: 20}, lands_played={1: 0, 2: 0}, library=27, opponent_hand=5,
    battlefield=[(242, "Mountain", 2, False, 8), (227, "Keeper of the Quiet Hour", 1, False, 7), (226, "Swamp", 1, False, 7), (222, "Tetsuko Umezawa, Fugitive", 2, False, 6), (221, "Island", 2, False, 6), (216, "Arni, Humble Scribe", 1, True, 5), (215, "Island", 1, True, 5), (211, "Geist of Saint Thalia", 2, False, 4), (210, "Island", 2, False, 4), (203, "Island", 1, True, 3), (201, "Mountain", 2, False, 2), (199, "Island", 1, True, 1)],
    hand=[(238, "Fatehold Chronologist"), (225, "Mindseeker Oculus"), (122, "Icy Reception"), (119, "Garruk, Veiled Butcher")],
)  # fmt: skip

# 17:08:11 — the same T8, declare-blockers step: Tetsuko and Geist attack and
# Blazing Crescendo (+3/+1 until end of turn) has resolved on Geist (4/3); the
# damage is not in yet (19 life). Logged: "their clock 4 vs ours 8 (2 vs 2
# creatures, 6 vs 5 power)". The snapshot cannot tell the pump is temporary.
T8_BLOCKS_PUMPED_GEIST = state(
    turn=8, active=2, phase="Phase_Combat", step="Step_DeclareBlock",
    life={1: 19, 2: 20}, lands_played={1: 0, 2: 1}, library=27, opponent_hand=4,
    battlefield=[(242, "Mountain", 2, False, 8), (227, "Keeper of the Quiet Hour", 1, False, 7), (226, "Swamp", 1, False, 7), (222, "Tetsuko Umezawa, Fugitive", 2, True, 6, {"is_attacking": True}), (221, "Island", 2, False, 6), (216, "Arni, Humble Scribe", 1, True, 5), (215, "Island", 1, True, 5), (211, "Geist of Saint Thalia", 2, True, 4, {"is_attacking": True, "power": 4, "toughness": 3}), (210, "Island", 2, False, 4), (203, "Island", 1, True, 3), (201, "Mountain", 2, True, 2), (199, "Island", 1, True, 1)],
    hand=[(238, "Fatehold Chronologist"), (225, "Mindseeker Oculus"), (122, "Icy Reception"), (119, "Garruk, Veiled Butcher")],
)  # fmt: skip

# 17:08:54 — their T10 main phase at 14 life: our Geist (1/2 flyer), Oculus
# (2/1), Keeper, Arni (tapped) vs Traxos (1/5 flyer, vigilance), Tetsuko, Geist,
# all three unblockable. Logged: "their clock 5 vs ours none (4 vs 3 creatures,
# 9 vs 3 power)".
T10_THREE_UNBLOCKABLE = state(
    turn=10, active=2, phase="Phase_Main1", step="",
    life={1: 14, 2: 20}, lands_played={1: 0, 2: 0}, library=22, opponent_hand=4,
    battlefield=[(266, "Geist of Saint Thalia", 1, False, 9), (265, "Island", 1, True, 9), (251, "Mindseeker Oculus", 1, False, 9), (247, "Traxos, Academy Guardian", 2, False, 8), (242, "Mountain", 2, False, 8), (227, "Keeper of the Quiet Hour", 1, False, 7), (226, "Swamp", 1, True, 7), (222, "Tetsuko Umezawa, Fugitive", 2, False, 6), (221, "Island", 2, False, 6), (216, "Arni, Humble Scribe", 1, True, 5), (215, "Island", 1, True, 5), (211, "Geist of Saint Thalia", 2, False, 4), (210, "Island", 2, False, 4), (203, "Island", 1, True, 3), (201, "Mountain", 2, False, 2), (199, "Island", 1, True, 1)],
    hand=[(271, "Surveillance Phantasm"), (122, "Icy Reception"), (119, "Garruk, Veiled Butcher")],
)  # fmt: skip

# 17:10:09 — their T12 main phase at 14 life: Semester Foreseer (3/4) joined us;
# Jiang Yanggu (4/4 menace) joined them. Logged: "their clock 5 vs ours none
# (5 vs 4 creatures, 12 vs 7 power)".
T12_JIANG = state(
    turn=12, active=2, phase="Phase_Main1", step="",
    life={1: 14, 2: 20}, lands_played={1: 0, 2: 0}, library=19, opponent_hand=2,
    battlefield=[(283, "Semester Foreseer", 1, False, 11), (275, "Jiang Yanggu, Alone", 2, False, 10), (274, "Island", 2, False, 10), (266, "Geist of Saint Thalia", 1, False, 9), (265, "Island", 1, False, 9), (251, "Mindseeker Oculus", 1, False, 9), (247, "Traxos, Academy Guardian", 2, False, 8), (242, "Mountain", 2, False, 8), (227, "Keeper of the Quiet Hour", 1, False, 7), (226, "Swamp", 1, True, 7), (222, "Tetsuko Umezawa, Fugitive", 2, False, 6), (221, "Island", 2, False, 6), (216, "Arni, Humble Scribe", 1, True, 5), (215, "Island", 1, True, 5), (211, "Geist of Saint Thalia", 2, False, 4), (210, "Island", 2, False, 4), (203, "Island", 1, True, 3), (201, "Mountain", 2, False, 2), (199, "Island", 1, True, 1)],
    hand=[(295, "Protege's Awakening"), (289, "Murmuring Volume"), (122, "Icy Reception")],
)  # fmt: skip

# 17:11:31 — our T13 main phase at 11 life (Jiang and our Geist traded on their
# T12): Divining Duelist (summoning sick), Foreseer, Oculus, Keeper, Arni vs
# Traxos (untapped, vigilance) and the tapped Tetsuko and Geist. Logged: "our
# clock 3 beats their 4 (5 vs 3 creatures, 14 vs 3 power)".
T13_OUR_ATTACK = state(
    turn=13, active=1, phase="Phase_Main1", step="",
    life={1: 11, 2: 20}, lands_played={1: 1, 2: 0}, library=14, opponent_hand=3,
    battlefield=[(353, "Divining Duelist", 1, False, 13), (352, "Island", 1, True, 13), (298, "Island", 2, False, 12), (283, "Semester Foreseer", 1, False, 11), (274, "Island", 2, True, 10), (265, "Island", 1, True, 9), (251, "Mindseeker Oculus", 1, False, 9), (247, "Traxos, Academy Guardian", 2, False, 8), (242, "Mountain", 2, True, 8), (227, "Keeper of the Quiet Hour", 1, False, 7), (226, "Swamp", 1, True, 7), (222, "Tetsuko Umezawa, Fugitive", 2, True, 6), (221, "Island", 2, True, 6), (216, "Arni, Humble Scribe", 1, False, 5), (215, "Island", 1, True, 5), (211, "Geist of Saint Thalia", 2, True, 4), (210, "Island", 2, True, 4), (203, "Island", 1, True, 3), (201, "Mountain", 2, True, 2), (199, "Island", 1, True, 1)],
    hand=[(360, "Island"), (349, "Screeching Soulbreaker"), (295, "Protege's Awakening")],
)  # fmt: skip

# 17:13:00 — our T15 main phase at 7 life vs 17: Void Extrapolator (summoning
# sick), Duelist, Foreseer, Oculus, Keeper, Arni vs Koth (3/2 reach),
# Heartstring Puller (3/1 trample, unblockable), Traxos, the tapped Tetsuko and
# Geist, and a Cadet. Logged: "their clock 2 vs ours none (6 vs 6 creatures,
# 17 vs 11 power)".
T15_WIDE_BOARDS = state(
    turn=15, active=1, phase="Phase_Main1", step="",
    life={1: 7, 2: 17}, lands_played={1: 0, 2: 0}, library=10, opponent_hand=1,
    battlefield=[(397, "Island", 1, False, 15), (392, "Void Extrapolator", 1, False, 15), (376, "Koth, the Geomancer", 2, False, 14), (369, "Heartstring Puller", 2, False, 14), (368, "Mountain", 2, True, 14), (366, "Eye of Jace", 2, False, 14), (353, "Divining Duelist", 1, False, 13), (352, "Island", 1, True, 13), (298, "Island", 2, True, 12), (283, "Semester Foreseer", 1, False, 11), (274, "Island", 2, True, 10), (265, "Island", 1, True, 9), (251, "Mindseeker Oculus", 1, False, 9), (247, "Traxos, Academy Guardian", 2, False, 8), (242, "Mountain", 2, True, 8), (227, "Keeper of the Quiet Hour", 1, False, 7), (226, "Swamp", 1, True, 7), (222, "Tetsuko Umezawa, Fugitive", 2, True, 6), (221, "Island", 2, True, 6), (216, "Arni, Humble Scribe", 1, False, 5), (215, "Island", 1, True, 5), (211, "Geist of Saint Thalia", 2, True, 4), (210, "Island", 2, True, 4), (203, "Island", 1, True, 3), (201, "Mountain", 2, True, 2), (199, "Island", 1, True, 1), (375, "Cadet", 2, False, 14)],
    hand=[(387, "Plan for All Outcomes")],
)  # fmt: skip
