"""Real board states from the 2026-10-08 FRA QuickDraft matches (post-match review).

Reconstructed by replaying MTGA's Player.log through arenamcp's own LogParser +
GameState (``server.get_game_state`` at each GRE request) and keeping only the
fields the strategic layer reads: the whitelist is the ``_SPEC`` rows below.
Oracle text is the card database's text with Arena's duplicated formatting
variants collapsed.

Game 1 (match 07c043d8, we were seat 1, on the play, lost T16 to an
auto-concede at 6 life): the opponent's Tetsuko Umezawa made every 1-power
creature unblockable; Traxos 1/5 blocked our ground attackers for free.
Game 2 (match 3288cb65, we were seat 2, lost T21): Arni looted before combat
every turn instead of attacking, and a lone attacker with two legal
recipients (the opponent and their Jace) went MANUAL REQUIRED on T18.

Tests import these instead of reading ~/.arenamcp or Player.log.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

MATCH_IDS = {1: "07c043d8-e07b-4c15-9f05-173d2fe178ee", 2: "3288cb65-1479-4d78-873c-c8829e5a2543"}

CARDS: dict[str, dict[str, Any]] = {
    "Arni, Humble Scribe": {
        "card_types": ["Creature"],
        "mana_cost": "{2}{U}",
        "oracle_text": "Whenever another nontoken creature you control enters, untap Arni.\n"
        "{oT}: Draw a card, then discard a card.",
        "power": 3,
        "toughness": 2,
        "type_line": "Legendary Creature — Human Wizard",
    },
    "Blazing Crescendo": {
        "card_types": ["Instant"],
        "mana_cost": "{1}{R}",
        "oracle_text": "Target creature gets +3/+1 until end of turn. Exile the top card of "
        "your library. Until the end of your next turn, you may play that "
        "card.",
        "type_line": "Instant",
    },
    "Cadet": {
        "card_types": ["Creature"],
        "mana_cost": "",
        "oracle_text": "",
        "power": 2,
        "toughness": 2,
        "type_line": "Token Creature — Wizard Soldier",
    },
    "Carnivorous Cultivator": {
        "card_types": ["Creature"],
        "keywords": ["deathtouch"],
        "mana_cost": "{1}{G}",
        "oracle_text": "Deathtouch\n"
        "This creature enters prepared.\n"
        "Whenever this creature deals combat damage to a player, return "
        "target land card from your graveyard to your hand.",
        "power": 2,
        "toughness": 3,
        "type_line": "Creature — Elf Warlock",
    },
    "Divining Duelist": {
        "card_types": ["Creature"],
        "mana_cost": "{2}{U}",
        "oracle_text": "Flash\n"
        "When this creature enters, choose one —\n"
        "•Tap target creature.\n"
        "•Untap target creature.\n"
        "•Draw a card, then discard a card.",
        "power": 3,
        "toughness": 2,
        "type_line": "Creature — Merfolk Wizard",
    },
    "Eye of Jace": {
        "card_types": ["Artifact"],
        "mana_cost": "{1}",
        "oracle_text": "At the beginning of your upkeep, surveil 1. Then if there are seven or more "
        "cards in your graveyard, sacrifice this artifact, it deals 2 damage to each "
        "opponent, and you gain 2 life.",
        "type_line": "Artifact",
    },
    "Fatehold Chronologist": {
        "card_types": ["Creature"],
        "keywords": ["flying"],
        "mana_cost": "{1}{W/U}",
        "oracle_text": "Flying\nThis creature enters prepared.",
        "power": 1,
        "toughness": 2,
        "type_line": "Creature — Bird Wizard",
    },
    "Fblthp, Impossibly Lost": {
        "card_types": ["Creature"],
        "mana_cost": "{1}{U}",
        "oracle_text": "When one or more of your opponents are dealt combat damage during your "
        "turn, draw two cards. If your library has no cards in it, you win the game. "
        "Fblthp's owner shuffles him into their library.",
        "power": 1,
        "toughness": 1,
        "type_line": "Legendary Creature — Homunculus",
    },
    "Forest": {
        "card_types": ["Land"],
        "mana_cost": "",
        "oracle_text": "({T}: Add {G}.)",
        "type_line": "Basic Land — Forest",
    },
    "Garruk, Veiled Butcher": {
        "card_types": ["Planeswalker"],
        "mana_cost": "{3}{B}{B}",
        "oracle_text": "If a creature an opponent controls would die, exile it instead.\n"
        "Up to one target creature gets -4/-1 until your next turn.\n"
        "Each player sacrifices a creature of their choice. If you "
        "sacrificed a creature this way, create a 4/4 green Beast "
        "creature token with trample.\n"
        "Each opponent discards two cards. For each opponent who didn't "
        "discard two nonland cards this way, you draw a card.",
        "type_line": "Legendary Planeswalker — Garruk",
    },
    "Geist of Saint Thalia": {
        "card_types": ["Creature"],
        "keywords": ["flying"],
        "mana_cost": "{1}{U}",
        "oracle_text": "Flying\nNoncreature spells you cast cost {o1} less to cast.",
        "power": 1,
        "toughness": 2,
        "type_line": "Legendary Creature — Spirit Cleric",
    },
    "Heartstring Puller": {
        "card_types": ["Creature"],
        "keywords": ["trample"],
        "mana_cost": "{3}{R}",
        "oracle_text": "Trample\n"
        "When this creature enters, create a 2/2 colorless Wizard Soldier "
        "creature token named Cadet.",
        "power": 3,
        "toughness": 1,
        "type_line": "Creature — Elf Sorcerer",
    },
    "Icy Reception": {
        "card_types": ["Instant"],
        "mana_cost": "{1}{U}",
        "oracle_text": "Choose one —\n"
        "•Counter target creature or legendary spell unless its controller pays "
        "{o3}.\n"
        "•Target creature gets -5/-0 until end of turn.",
        "type_line": "Instant",
    },
    "Island": {
        "card_types": ["Land"],
        "mana_cost": "",
        "oracle_text": "({T}: Add {U}.)",
        "type_line": "Basic Land — Island",
    },
    "Jace": {
        "card_types": ["Planeswalker"],
        "mana_cost": "",
        "oracle_text": "Surveil 1.\nDraw a card.",
        "type_line": "Token Planeswalker — Jace",
    },
    "Jiang Yanggu, Alone": {
        "card_types": ["Creature"],
        "keywords": ["menace"],
        "mana_cost": "{4}{R}",
        "oracle_text": "Menace\n"
        "Whenever a creature you control attacks a player alone, discard a "
        "card, then draw a card. Then put a +1/+1 counter on that creature "
        "for each card you've discarded this turn.",
        "power": 4,
        "toughness": 4,
        "type_line": "Legendary Creature — Human Berserker",
    },
    "Keeper of the Quiet Hour": {
        "card_types": ["Artifact", "Creature"],
        "mana_cost": "{3}",
        "oracle_text": "When this creature enters, empower Jace 2.",
        "power": 3,
        "toughness": 2,
        "type_line": "Artifact Creature — Chimera",
    },
    "Koth, the Geomancer": {
        "card_types": ["Creature"],
        "keywords": ["reach"],
        "mana_cost": "{2}{R}",
        "oracle_text": "Reach\n"
        "Landfall — Whenever a land you control enters, Koth deals 1 damage "
        "to each opponent. If that land is a Mountain, add {oR}.",
        "power": 3,
        "toughness": 2,
        "type_line": "Legendary Creature — Human Warrior",
    },
    "Leviathan": {
        "card_types": ["Creature"],
        "keywords": ["hexproof"],
        "mana_cost": "{5}{U}{U}{U}{U}",
        "oracle_text": "Hexproof",
        "power": 8,
        "toughness": 8,
        "type_line": "Creature — Leviathan",
    },
    "Living Library": {
        "card_types": ["Artifact", "Creature"],
        "mana_cost": "{2}",
        "oracle_text": "{o6}, Sacrifice this creature: Choose target creature or planeswalker an "
        "opponent controls. Its owner shuffles it into their library.",
        "power": 0,
        "toughness": 4,
        "type_line": "Artifact Creature — Book Illusion",
    },
    "Mindseeker Oculus": {
        "card_types": ["Creature"],
        "mana_cost": "{2}{U}",
        "oracle_text": "When this creature enters, empower Jace 4.",
        "power": 2,
        "toughness": 1,
        "type_line": "Creature — Homunculus",
    },
    "Mountain": {
        "card_types": ["Land"],
        "mana_cost": "",
        "oracle_text": "({T}: Add {R}.)",
        "type_line": "Basic Land — Mountain",
    },
    "Murmuring Volume": {
        "card_types": ["Artifact"],
        "mana_cost": "{3}",
        "oracle_text": "{oT}: Add one mana of any color.\n{o2}, {oT}, Discard a card: Draw a card.",
        "type_line": "Artifact — Book",
    },
    "Plan for All Outcomes": {
        "card_types": ["Enchantment"],
        "mana_cost": "{3}{U}",
        "oracle_text": "When this enchantment enters, the owner of up to one other target "
        "nonland permanent puts it on their choice of the top or bottom of "
        "their library.\n"
        "Whenever you cast your first noncreature spell each turn, empower "
        "Jace 1.",
        "type_line": "Enchantment",
    },
    "Protege's Awakening": {
        "card_types": ["Sorcery"],
        "mana_cost": "{3}{U}",
        "oracle_text": "Empower Jace 6.\nDraw a card.",
        "type_line": "Sorcery",
    },
    "Recursive Recruitment": {
        "card_types": ["Sorcery"],
        "mana_cost": "{2}{U}{B}",
        "oracle_text": "Create two 2/2 colorless Wizard Soldier creature tokens named "
        "Cadet. If this spell was cast from a graveyard, put a +1/+1 "
        "counter on each of them for every three cards in your graveyard.\n"
        "Flashback {o6oUoB}",
        "type_line": "Sorcery",
    },
    "Screeching Soulbreaker": {
        "card_types": ["Creature"],
        "keywords": ["flying"],
        "mana_cost": "{2}{B}",
        "oracle_text": "Flying\n"
        "Whenever this creature attacks, it deals 1 damage to each "
        "opponent and you gain 1 life.",
        "power": 1,
        "toughness": 4,
        "type_line": "Creature — Siren Bard",
    },
    "Semester Foreseer": {
        "card_types": ["Creature"],
        "mana_cost": "{3}{U}",
        "oracle_text": "This creature enters prepared.\nWhen this creature enters, surveil 1.",
        "power": 3,
        "toughness": 4,
        "type_line": "Creature — Human Wizard",
    },
    "Silence the Echo": {
        "card_types": ["Sorcery"],
        "mana_cost": "{1}{B}",
        "oracle_text": "As an additional cost to cast this spell, sacrifice a creature or "
        "planeswalker or pay {o3}.\n"
        "Destroy target creature or planeswalker.",
        "type_line": "Sorcery",
    },
    "Sphinx's Approach": {
        "card_types": ["Instant"],
        "mana_cost": "{1}{U}{U}",
        "oracle_text": "Draw two cards. Then you may exile this spell and four cards named "
        "Sphinx's Approach from your graveyard. If you do, search your library "
        "for a Sphinx creature card, put it onto the battlefield, then "
        "shuffle.\n"
        "A deck can have any number of cards named Sphinx's Approach.",
        "type_line": "Instant",
    },
    "Surveillance Phantasm": {
        "card_types": ["Creature"],
        "keywords": ["defender", "flying", "vigilance"],
        "mana_cost": "{1}{U}",
        "oracle_text": "Defender\n"
        "Flying\n"
        "Vigilance\n"
        "As long as you've scried or surveilled this turn, this creature "
        "can attack as though it didn't have defender.\n"
        "{o3oU}: Surveil 1.",
        "power": 2,
        "toughness": 3,
        "type_line": "Creature — Bird Illusion",
    },
    "Swamp": {
        "card_types": ["Land"],
        "mana_cost": "",
        "oracle_text": "({T}: Add {B}.)",
        "type_line": "Basic Land — Swamp",
    },
    "Tetsuko Umezawa, Fugitive": {
        "card_types": ["Creature"],
        "mana_cost": "{1}{U}",
        "oracle_text": "Creatures you control with power or toughness 1 or less can't be blocked.",
        "power": 1,
        "toughness": 3,
        "type_line": "Legendary Creature — Human Rogue",
    },
    "Traxos, Academy Guardian": {
        "card_types": ["Artifact", "Creature"],
        "keywords": ["flying", "vigilance"],
        "mana_cost": "{3}{U}",
        "oracle_text": "This spell costs {o2} less to cast if you've cast a "
        "noncreature spell this turn.\n"
        "Flying\n"
        "Vigilance\n"
        "Prowess",
        "power": 1,
        "toughness": 5,
        "type_line": "Legendary Artifact Creature — Dragon Construct",
    },
    "Twinned Vision": {
        "card_types": ["Instant"],
        "mana_cost": "{1}{U/R}",
        "oracle_text": "Draw a card. If this spell wasn't cast from your hand, draw two cards "
        "instead.\n"
        "Flashback—{o1o(U/R)o(U/R)}, Discard a card.",
        "type_line": "Instant",
    },
    "Void Extrapolator": {
        "card_types": ["Creature"],
        "mana_cost": "{1}{B}",
        "oracle_text": "This creature enters prepared.\n"
        "Threshold — This creature gets +1/+1 as long as there are seven or "
        "more cards in your graveyard.",
        "power": 3,
        "toughness": 3,
        "type_line": "Creature — Aetherborn Warlock",
    },
    "Way of the Deathbringer": {
        "card_types": ["Enchantment"],
        "mana_cost": "{2}{B}",
        "oracle_text": "When Way of the Deathbringer enters, empower Jace 5.\n"
        'Planeswalkers you control have "[-2]: You may sacrifice a '
        "creature. If you do, create a 4/4 green Beast creature token "
        'with trample."',
        "type_line": "Legendary Enchantment",
    },
}

_SPEC_G1_T9_MAIN1 = {
    # 17:08:38, before "Activate Ability: Jace [-3: Draw a card.]" took Jace from 5 to 2
    # loyalty into Tetsuko's board (Tetsuko, Geist, Traxos: three power, all unblockable).
    "turn": 9,
    "active": 1,
    "phase": "Phase_Main1",
    "step": "",
    "local": 1,
    "life": {1: 14, 2: 20},
    "lands_played": {1: 0, 2: 0},
    "library": 24,
    "opponent_hand": 3,
    "battlefield": [
        (251, "Mindseeker Oculus", 1, False, 9),
        (247, "Traxos, Academy Guardian", 2, False, 8),
        (242, "Mountain", 2, False, 8),
        (227, "Keeper of the Quiet Hour", 1, False, 7),
        (226, "Swamp", 1, False, 7),
        (222, "Tetsuko Umezawa, Fugitive", 2, True, 6),
        (221, "Island", 2, True, 6),
        (216, "Arni, Humble Scribe", 1, True, 5),
        (215, "Island", 1, True, 5),
        (211, "Geist of Saint Thalia", 2, True, 4),
        (210, "Island", 2, True, 4),
        (203, "Island", 1, True, 3),
        (201, "Mountain", 2, True, 2),
        (199, "Island", 1, True, 1),
        (236, "Jace", 1, False, 7, {"counters": {"Loyalty": 5, "unknown": 5}, "object_kind": "TOKEN"}),
    ],
    "hand": [
        (250, "Geist of Saint Thalia"),
        (238, "Fatehold Chronologist"),
        (122, "Icy Reception"),
        (119, "Garruk, Veiled Butcher"),
    ],
    "graveyard": [
        (262, "Sphinx's Approach", 1),
        (257, "Keeper of the Quiet Hour", 1),
        (239, "Swamp", 1),
        (233, "Living Library", 1),
        (208, "Twinned Vision", 1),
        (246, "Blazing Crescendo", 2),
    ],
    "decision": {"type": "actions_available"},
    "stack": [(260, "Ability (ID: 1186)", "{oT}: Draw a card, then discard a card.", "ABILITY")],
    "legal_actions": [
        "Cast Garruk, Veiled Butcher",
        "Cast Icy Reception",
        "Cast Twinned Vision",
        "Cast Fatehold Chronologist",
        "Cast Geist of Saint Thalia",
        "Activate Ability: Jace",
        "Activate Ability: Jace",
        "Action: Activate_Mana",
        "Pass",
        "Action: FloatMana",
    ],
}

_SPEC_G1_T12_THEIR_MAIN1 = {
    # 17:10:20, their T12 main phase: Fblthp on the stack, four of their six lands untapped,
    # and the model cast Icy Reception to counter it "(opponent has only ~2 open mana)".
    "turn": 12,
    "active": 2,
    "phase": "Phase_Main1",
    "step": "",
    "local": 1,
    "life": {1: 14, 2: 20},
    "lands_played": {1: 0, 2: 1},
    "library": 19,
    "opponent_hand": 1,
    "battlefield": [
        (298, "Island", 2, False, 12),
        (283, "Semester Foreseer", 1, False, 11),
        (275, "Jiang Yanggu, Alone", 2, False, 10),
        (274, "Island", 2, False, 10),
        (266, "Geist of Saint Thalia", 1, False, 9),
        (265, "Island", 1, False, 9),
        (251, "Mindseeker Oculus", 1, False, 9),
        (247, "Traxos, Academy Guardian", 2, False, 8),
        (242, "Mountain", 2, False, 8),
        (227, "Keeper of the Quiet Hour", 1, False, 7),
        (226, "Swamp", 1, True, 7),
        (222, "Tetsuko Umezawa, Fugitive", 2, False, 6),
        (221, "Island", 2, True, 6),
        (216, "Arni, Humble Scribe", 1, True, 5),
        (215, "Island", 1, True, 5),
        (211, "Geist of Saint Thalia", 2, False, 4),
        (210, "Island", 2, True, 4),
        (203, "Island", 1, True, 3),
        (201, "Mountain", 2, False, 2),
        (199, "Island", 1, True, 1),
    ],
    "hand": [(295, "Protege's Awakening"), (289, "Murmuring Volume"), (122, "Icy Reception")],
    "graveyard": [
        (296, "Garruk, Veiled Butcher", 1),
        (290, "Surveillance Phantasm", 1),
        (272, "Fatehold Chronologist", 1),
        (262, "Sphinx's Approach", 1),
        (257, "Keeper of the Quiet Hour", 1),
        (239, "Swamp", 1),
        (233, "Living Library", 1),
        (208, "Twinned Vision", 1),
        (246, "Blazing Crescendo", 2),
    ],
    "decision": {"type": "actions_available"},
    "stack": [(299, "Fblthp, Impossibly Lost", CARDS["Fblthp, Impossibly Lost"]["oracle_text"], "CARD", 2)],
    "legal_actions": [
        "Cast Icy Reception [OK]",
        "Cast Twinned Vision",
        "Action: Activate_Mana",
        "Pass",
        "Action: FloatMana",
    ],
}

_SPEC_G1_T9_ATTACK = {
    "turn": 9,
    "active": 1,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 1,
    "life": {1: 14, 2: 20},
    "lands_played": {1: 1, 2: 0},
    "library": 22,
    "opponent_hand": 3,
    "battlefield": [
        (266, "Geist of Saint Thalia", 1, False, 9),
        (265, "Island", 1, True, 9),
        (251, "Mindseeker Oculus", 1, False, 9),
        (247, "Traxos, Academy Guardian", 2, False, 8),
        (242, "Mountain", 2, False, 8),
        (227, "Keeper of the Quiet Hour", 1, False, 7),
        (226, "Swamp", 1, True, 7),
        (222, "Tetsuko Umezawa, Fugitive", 2, True, 6),
        (221, "Island", 2, True, 6),
        (216, "Arni, Humble Scribe", 1, True, 5),
        (215, "Island", 1, True, 5),
        (211, "Geist of Saint Thalia", 2, True, 4),
        (210, "Island", 2, True, 4),
        (203, "Island", 1, True, 3),
        (201, "Mountain", 2, True, 2),
        (199, "Island", 1, True, 1),
        (236, "Jace", 1, False, 7, {"counters": {"Loyalty": 2, "unknown": 2}, "object_kind": "TOKEN"}),
    ],
    "hand": [(271, "Surveillance Phantasm"), (122, "Icy Reception"), (119, "Garruk, Veiled Butcher")],
    "graveyard": [
        (272, "Fatehold Chronologist", 1),
        (262, "Sphinx's Approach", 1),
        (257, "Keeper of the Quiet Hour", 1),
        (239, "Swamp", 1),
        (233, "Living Library", 1),
        (208, "Twinned Vision", 1),
        (246, "Blazing Crescendo", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": ["Keeper of the Quiet Hour"],
        "legal_attacker_ids": [227],
        "raw_attackers": [
            {
                "attackerInstanceId": 227,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            }
        ],
    },
    "stack": [(270, "Ability (ID: 1186)", "{oT}: Draw a card, then discard a card.", "ABILITY")],
    "legal_actions": ["Attack with: Keeper of the Quiet Hour", "Done (confirm attackers)"],
}

_SPEC_G1_T11_ATTACK = {
    "turn": 11,
    "active": 1,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 1,
    "life": {1: 14, 2: 20},
    "lands_played": {1: 0, 2: 0},
    "library": 19,
    "opponent_hand": 2,
    "battlefield": [
        (283, "Semester Foreseer", 1, False, 11),
        (275, "Jiang Yanggu, Alone", 2, False, 10),
        (274, "Island", 2, True, 10),
        (266, "Geist of Saint Thalia", 1, False, 9),
        (265, "Island", 1, False, 9),
        (251, "Mindseeker Oculus", 1, False, 9),
        (247, "Traxos, Academy Guardian", 2, False, 8),
        (242, "Mountain", 2, True, 8),
        (227, "Keeper of the Quiet Hour", 1, False, 7),
        (226, "Swamp", 1, True, 7),
        (222, "Tetsuko Umezawa, Fugitive", 2, True, 6),
        (221, "Island", 2, True, 6),
        (216, "Arni, Humble Scribe", 1, True, 5),
        (215, "Island", 1, True, 5),
        (211, "Geist of Saint Thalia", 2, True, 4),
        (210, "Island", 2, True, 4),
        (203, "Island", 1, True, 3),
        (201, "Mountain", 2, True, 2),
        (199, "Island", 1, True, 1),
    ],
    "hand": [(295, "Protege's Awakening"), (289, "Murmuring Volume"), (122, "Icy Reception")],
    "graveyard": [
        (296, "Garruk, Veiled Butcher", 1),
        (290, "Surveillance Phantasm", 1),
        (272, "Fatehold Chronologist", 1),
        (262, "Sphinx's Approach", 1),
        (257, "Keeper of the Quiet Hour", 1),
        (239, "Swamp", 1),
        (233, "Living Library", 1),
        (208, "Twinned Vision", 1),
        (246, "Blazing Crescendo", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": ["Keeper of the Quiet Hour", "Mindseeker Oculus", "Geist of Saint Thalia"],
        "legal_attacker_ids": [227, 251, 266],
        "raw_attackers": [
            {
                "attackerInstanceId": 227,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
            {
                "attackerInstanceId": 251,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
            {
                "attackerInstanceId": 266,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
        ],
    },
    "stack": [(294, "Ability (ID: 1186)", "{oT}: Draw a card, then discard a card.", "ABILITY")],
    "legal_actions": [
        "Attack with: Keeper of the Quiet Hour",
        "Attack with: Mindseeker Oculus",
        "Attack with: Geist of Saint Thalia",
        "Done (confirm attackers)",
    ],
}

_SPEC_G1_T13_ATTACK = {
    "turn": 13,
    "active": 1,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 1,
    "life": {1: 11, 2: 20},
    "lands_played": {1: 1, 2: 0},
    "library": 13,
    "opponent_hand": 3,
    "battlefield": [
        (353, "Divining Duelist", 1, False, 13),
        (352, "Island", 1, True, 13),
        (298, "Island", 2, False, 12),
        (283, "Semester Foreseer", 1, False, 11),
        (274, "Island", 2, True, 10),
        (265, "Island", 1, True, 9),
        (251, "Mindseeker Oculus", 1, False, 9),
        (247, "Traxos, Academy Guardian", 2, False, 8),
        (242, "Mountain", 2, True, 8),
        (227, "Keeper of the Quiet Hour", 1, False, 7),
        (226, "Swamp", 1, True, 7),
        (222, "Tetsuko Umezawa, Fugitive", 2, True, 6),
        (221, "Island", 2, True, 6),
        (216, "Arni, Humble Scribe", 1, True, 5),
        (215, "Island", 1, True, 5),
        (211, "Geist of Saint Thalia", 2, True, 4),
        (210, "Island", 2, True, 4),
        (203, "Island", 1, True, 3),
        (201, "Mountain", 2, True, 2),
        (199, "Island", 1, True, 1),
    ],
    "hand": [(363, "Island"), (295, "Protege's Awakening")],
    "graveyard": [
        (364, "Island", 1),
        (361, "Screeching Soulbreaker", 1),
        (348, "Swamp", 1),
        (345, "Murmuring Volume", 1),
        (310, "Geist of Saint Thalia", 1),
        (307, "Icy Reception", 1),
        (296, "Garruk, Veiled Butcher", 1),
        (290, "Surveillance Phantasm", 1),
        (272, "Fatehold Chronologist", 1),
        (262, "Sphinx's Approach", 1),
        (257, "Keeper of the Quiet Hour", 1),
        (239, "Swamp", 1),
        (233, "Living Library", 1),
        (311, "Jiang Yanggu, Alone", 2),
        (246, "Blazing Crescendo", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": ["Keeper of the Quiet Hour", "Mindseeker Oculus", "Semester Foreseer"],
        "legal_attacker_ids": [227, 251, 283],
        "raw_attackers": [
            {
                "attackerInstanceId": 227,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
            {
                "attackerInstanceId": 251,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
            {
                "attackerInstanceId": 283,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
        ],
    },
    "stack": [(362, "Ability (ID: 1186)", "{oT}: Draw a card, then discard a card.", "ABILITY")],
    "legal_actions": [
        "Attack with: Keeper of the Quiet Hour",
        "Attack with: Mindseeker Oculus",
        "Attack with: Semester Foreseer",
        "Done (confirm attackers)",
    ],
}

_SPEC_G1_T15_ATTACK = {
    "turn": 15,
    "active": 1,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 1,
    "life": {1: 7, 2: 17},
    "lands_played": {1: 1, 2: 0},
    "library": 5,
    "opponent_hand": 1,
    "battlefield": [
        (397, "Island", 1, True, 15),
        (392, "Void Extrapolator", 1, False, 15),
        (376, "Koth, the Geomancer", 2, False, 14),
        (369, "Heartstring Puller", 2, False, 14),
        (368, "Mountain", 2, True, 14),
        (366, "Eye of Jace", 2, False, 14),
        (353, "Divining Duelist", 1, False, 13),
        (352, "Island", 1, True, 13),
        (298, "Island", 2, True, 12),
        (283, "Semester Foreseer", 1, False, 11),
        (274, "Island", 2, True, 10),
        (265, "Island", 1, True, 9),
        (251, "Mindseeker Oculus", 1, False, 9),
        (247, "Traxos, Academy Guardian", 2, False, 8),
        (242, "Mountain", 2, True, 8),
        (227, "Keeper of the Quiet Hour", 1, False, 7),
        (226, "Swamp", 1, True, 7),
        (222, "Tetsuko Umezawa, Fugitive", 2, True, 6),
        (221, "Island", 2, True, 6),
        (216, "Arni, Humble Scribe", 1, True, 5),
        (215, "Island", 1, True, 5),
        (211, "Geist of Saint Thalia", 2, True, 4),
        (210, "Island", 2, True, 4),
        (203, "Island", 1, True, 3),
        (201, "Mountain", 2, True, 2),
        (199, "Island", 1, True, 1),
        (375, "Cadet", 2, False, 14, {"object_kind": "TOKEN"}),
        (389, "Jace", 1, False, 15, {"counters": {"Loyalty": 3, "unknown": 3}, "object_kind": "TOKEN"}),
    ],
    "hand": [(408, "Swamp"), (401, "Swamp")],
    "graveyard": [
        (405, "Island", 1),
        (404, "Island", 1),
        (403, "Recursive Recruitment", 1),
        (402, "Plan for All Outcomes", 1),
        (391, "Protege's Awakening", 1),
        (388, "Island", 1),
        (364, "Island", 1),
        (361, "Screeching Soulbreaker", 1),
        (348, "Swamp", 1),
        (345, "Murmuring Volume", 1),
        (310, "Geist of Saint Thalia", 1),
        (307, "Icy Reception", 1),
        (296, "Garruk, Veiled Butcher", 1),
        (290, "Surveillance Phantasm", 1),
        (272, "Fatehold Chronologist", 1),
        (262, "Sphinx's Approach", 1),
        (257, "Keeper of the Quiet Hour", 1),
        (239, "Swamp", 1),
        (233, "Living Library", 1),
        (311, "Jiang Yanggu, Alone", 2),
        (246, "Blazing Crescendo", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": [
            "Keeper of the Quiet Hour",
            "Mindseeker Oculus",
            "Semester Foreseer",
            "Divining Duelist",
        ],
        "legal_attacker_ids": [227, 251, 283, 353],
        "raw_attackers": [
            {
                "attackerInstanceId": 227,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
            {
                "attackerInstanceId": 251,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
            {
                "attackerInstanceId": 283,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
            {
                "attackerInstanceId": 353,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
            },
        ],
    },
    "stack": [(407, "Ability (ID: 208425)", "Draw a card.", "ABILITY")],
    "legal_actions": [
        "Attack with: Keeper of the Quiet Hour",
        "Attack with: Mindseeker Oculus",
        "Attack with: Semester Foreseer",
        "Attack with: Divining Duelist",
        "Done (confirm attackers)",
    ],
}

_SPEC_G1_T16_THEIR_ATTACK = {
    "turn": 16,
    "active": 2,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 1,
    "life": {1: 6, 2: 17},
    "lands_played": {1: 0, 2: 1},
    "library": 5,
    "opponent_hand": 1,
    "battlefield": [
        (411, "Mountain", 2, False, 16),
        (397, "Island", 1, True, 15),
        (392, "Void Extrapolator", 1, False, 15),
        (376, "Koth, the Geomancer", 2, False, 14),
        (369, "Heartstring Puller", 2, False, 14),
        (368, "Mountain", 2, False, 14),
        (366, "Eye of Jace", 2, False, 14),
        (353, "Divining Duelist", 1, False, 13),
        (352, "Island", 1, True, 13),
        (298, "Island", 2, False, 12),
        (283, "Semester Foreseer", 1, False, 11),
        (274, "Island", 2, False, 10),
        (265, "Island", 1, True, 9),
        (251, "Mindseeker Oculus", 1, False, 9),
        (247, "Traxos, Academy Guardian", 2, False, 8),
        (242, "Mountain", 2, False, 8),
        (227, "Keeper of the Quiet Hour", 1, False, 7),
        (226, "Swamp", 1, True, 7),
        (222, "Tetsuko Umezawa, Fugitive", 2, False, 6),
        (221, "Island", 2, False, 6),
        (216, "Arni, Humble Scribe", 1, True, 5),
        (215, "Island", 1, True, 5),
        (211, "Geist of Saint Thalia", 2, False, 4),
        (210, "Island", 2, False, 4),
        (203, "Island", 1, True, 3),
        (201, "Mountain", 2, False, 2),
        (199, "Island", 1, True, 1),
        (375, "Cadet", 2, False, 14, {"object_kind": "TOKEN"}),
        (389, "Jace", 1, False, 15, {"counters": {"Loyalty": 3, "unknown": 3}, "object_kind": "TOKEN"}),
    ],
    "hand": [(408, "Swamp"), (401, "Swamp")],
    "graveyard": [
        (405, "Island", 1),
        (404, "Island", 1),
        (403, "Recursive Recruitment", 1),
        (402, "Plan for All Outcomes", 1),
        (391, "Protege's Awakening", 1),
        (388, "Island", 1),
        (364, "Island", 1),
        (361, "Screeching Soulbreaker", 1),
        (348, "Swamp", 1),
        (345, "Murmuring Volume", 1),
        (310, "Geist of Saint Thalia", 1),
        (307, "Icy Reception", 1),
        (296, "Garruk, Veiled Butcher", 1),
        (290, "Surveillance Phantasm", 1),
        (272, "Fatehold Chronologist", 1),
        (262, "Sphinx's Approach", 1),
        (257, "Keeper of the Quiet Hour", 1),
        (239, "Swamp", 1),
        (233, "Living Library", 1),
        (311, "Jiang Yanggu, Alone", 2),
        (246, "Blazing Crescendo", 2),
    ],
    "decision": {},
    "stack": [
        (
            412,
            "Ability (ID: 208380)",
            "Landfall — Whenever a land you control enters, CARDNAME deals 1 damage to each opponent. If that land is "
            "a Mountain, add {oR}.",
            "ABILITY",
        )
    ],
    "legal_actions": [],
}

_SPEC_G2_T8_MAIN1 = {
    "turn": 8,
    "active": 2,
    "phase": "Phase_Main1",
    "step": "",
    "local": 2,
    "life": {1: 20, 2: 20},
    "lands_played": {1: 0, 2: 0},
    "library": 29,
    "opponent_hand": 6,
    "battlefield": [
        (221, "Arni, Humble Scribe", 2, False, 6),
        (220, "Swamp", 2, False, 6),
        (211, "Way of the Deathbringer", 1, False, 5),
        (210, "Forest", 1, False, 5),
        (206, "Surveillance Phantasm", 2, False, 4),
        (205, "Island", 2, False, 4),
        (203, "Swamp", 1, False, 3),
        (201, "Island", 2, False, 2),
        (199, "Forest", 1, False, 1),
        (216, "Jace", 1, False, 5, {"counters": {"Loyalty": 3, "unknown": 3}, "object_kind": "TOKEN"}),
    ],
    "hand": [
        (228, "Swamp"),
        (219, "Fatehold Chronologist"),
        (200, "Murmuring Volume"),
        (163, "Mindseeker Oculus"),
        (161, "Icy Reception"),
        (159, "Garruk, Veiled Butcher"),
    ],
    "graveyard": [(227, "Fatehold Chronologist", 1), (218, "Silence the Echo", 1)],
    "decision": {"type": "actions_available"},
    "stack": [],
    "legal_actions": [
        "Cast Garruk, Veiled Butcher",
        "Cast Icy Reception [OK]",
        "Cast Mindseeker Oculus [OK]",
        "Cast Murmuring Volume [OK]",
        "Cast Fatehold Chronologist [OK]",
        "Activate Ability: Surveillance Phantasm [NEED:{3}{U}]",
        "Activate Ability: Arni, Humble Scribe",
        "Play Land: Swamp",
        "Action: Activate_Mana",
        "Action: Activate_Mana",
        "Action: Activate_Mana",
        "Pass",
        "Action: FloatMana",
    ],
}

_SPEC_G2_T8_ATTACK = {
    "turn": 8,
    "active": 2,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 2,
    "life": {1: 20, 2: 20},
    "lands_played": {1: 0, 2: 0},
    "library": 27,
    "opponent_hand": 6,
    "battlefield": [
        (229, "Mindseeker Oculus", 2, False, 8),
        (221, "Arni, Humble Scribe", 2, True, 6),
        (220, "Swamp", 2, True, 6),
        (211, "Way of the Deathbringer", 1, False, 5),
        (210, "Forest", 1, False, 5),
        (206, "Surveillance Phantasm", 2, False, 4),
        (205, "Island", 2, True, 4),
        (203, "Swamp", 1, False, 3),
        (201, "Island", 2, True, 2),
        (199, "Forest", 1, False, 1),
        (216, "Jace", 1, False, 5, {"counters": {"Loyalty": 3, "unknown": 3}, "object_kind": "TOKEN"}),
        (238, "Jace", 2, False, 8, {"counters": {"Loyalty": 3, "unknown": 3}, "object_kind": "TOKEN"}),
    ],
    "hand": [
        (241, "Divining Duelist"),
        (234, "Semester Foreseer"),
        (219, "Fatehold Chronologist"),
        (200, "Murmuring Volume"),
        (161, "Icy Reception"),
    ],
    "graveyard": [
        (227, "Fatehold Chronologist", 1),
        (218, "Silence the Echo", 1),
        (242, "Garruk, Veiled Butcher", 2),
        (235, "Swamp", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": ["Surveillance Phantasm"],
        "legal_attacker_ids": [206],
        "raw_attackers": [
            {
                "attackerInstanceId": 206,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            }
        ],
    },
    "stack": [(239, "Ability (ID: 208424)", "Surveil 1.", "ABILITY")],
    "legal_actions": ["Attack with: Surveillance Phantasm", "Done (confirm attackers)"],
}

_SPEC_G2_T10_ATTACK = {
    "turn": 10,
    "active": 2,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 2,
    "life": {1: 18, 2: 20},
    "lands_played": {1: 0, 2: 1},
    "library": 23,
    "opponent_hand": 5,
    "battlefield": [
        (261, "Island", 2, False, 10),
        (249, "Divining Duelist", 2, False, 10),
        (245, "Surveillance Phantasm", 1, False, 9),
        (244, "Island", 1, True, 9),
        (229, "Mindseeker Oculus", 2, False, 8),
        (221, "Arni, Humble Scribe", 2, True, 6),
        (220, "Swamp", 2, True, 6),
        (211, "Way of the Deathbringer", 1, False, 5),
        (210, "Forest", 1, False, 5),
        (206, "Surveillance Phantasm", 2, False, 4),
        (205, "Island", 2, True, 4),
        (203, "Swamp", 1, False, 3),
        (201, "Island", 2, True, 2),
        (199, "Forest", 1, True, 1),
        (216, "Jace", 1, False, 5, {"counters": {"Loyalty": 3, "unknown": 3}, "object_kind": "TOKEN"}),
        (238, "Jace", 2, False, 8, {"counters": {"Loyalty": 2, "unknown": 2}, "object_kind": "TOKEN"}),
    ],
    "hand": [
        (264, "Screeching Soulbreaker"),
        (234, "Semester Foreseer"),
        (200, "Murmuring Volume"),
        (161, "Icy Reception"),
    ],
    "graveyard": [
        (227, "Fatehold Chronologist", 1),
        (218, "Silence the Echo", 1),
        (265, "Island", 2),
        (260, "Fatehold Chronologist", 2),
        (255, "Keeper of the Quiet Hour", 2),
        (242, "Garruk, Veiled Butcher", 2),
        (235, "Swamp", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": ["Surveillance Phantasm", "Mindseeker Oculus"],
        "legal_attacker_ids": [206, 229],
        "raw_attackers": [
            {
                "attackerInstanceId": 206,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 229,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
        ],
    },
    "stack": [(262, "Ability (ID: 208424)", "Surveil 1.", "ABILITY")],
    "legal_actions": [
        "Attack with: Surveillance Phantasm",
        "Attack with: Mindseeker Oculus",
        "Done (confirm attackers)",
    ],
}

_SPEC_G2_T12_ATTACK = {
    "turn": 12,
    "active": 2,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 2,
    "life": {1: 18, 2: 20},
    "lands_played": {1: 0, 2: 1},
    "library": 18,
    "opponent_hand": 5,
    "battlefield": [
        (291, "Island", 2, False, 12),
        (276, "Semester Foreseer", 2, False, 12),
        (274, "Swamp", 1, False, 11),
        (261, "Island", 2, True, 10),
        (249, "Divining Duelist", 2, False, 10),
        (245, "Surveillance Phantasm", 1, False, 9),
        (244, "Island", 1, True, 9),
        (229, "Mindseeker Oculus", 2, False, 8),
        (221, "Arni, Humble Scribe", 2, True, 6),
        (220, "Swamp", 2, True, 6),
        (211, "Way of the Deathbringer", 1, False, 5),
        (210, "Forest", 1, True, 5),
        (206, "Surveillance Phantasm", 2, False, 4),
        (205, "Island", 2, True, 4),
        (203, "Swamp", 1, True, 3),
        (201, "Island", 2, True, 2),
        (199, "Forest", 1, True, 1),
        (216, "Jace", 1, False, 5, {"counters": {"Loyalty": 9, "unknown": 9}, "object_kind": "TOKEN"}),
        (238, "Jace", 2, False, 8, {"counters": {"Loyalty": 1, "unknown": 1}, "object_kind": "TOKEN"}),
    ],
    "hand": [(282, "Murmuring Volume"), (264, "Screeching Soulbreaker"), (161, "Icy Reception")],
    "graveyard": [
        (273, "Protege's Awakening", 1),
        (227, "Fatehold Chronologist", 1),
        (218, "Silence the Echo", 1),
        (293, "Island", 2),
        (290, "Murmuring Volume", 2),
        (287, "Island", 2),
        (283, "Twinned Vision", 2),
        (265, "Island", 2),
        (260, "Fatehold Chronologist", 2),
        (255, "Keeper of the Quiet Hour", 2),
        (242, "Garruk, Veiled Butcher", 2),
        (235, "Swamp", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": ["Surveillance Phantasm", "Mindseeker Oculus", "Divining Duelist"],
        "legal_attacker_ids": [206, 229, 249],
        "raw_attackers": [
            {
                "attackerInstanceId": 206,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 229,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 249,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
        ],
    },
    "stack": [(292, "Ability (ID: 208424)", "Surveil 1.", "ABILITY")],
    "legal_actions": [
        "Attack with: Surveillance Phantasm",
        "Attack with: Mindseeker Oculus",
        "Attack with: Divining Duelist",
        "Done (confirm attackers)",
    ],
}

_SPEC_G2_T14_ATTACK = {
    "turn": 14,
    "active": 2,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 2,
    "life": {1: 15, 2: 20},
    "lands_played": {1: 0, 2: 1},
    "library": 14,
    "opponent_hand": 4,
    "battlefield": [
        (312, "Murmuring Volume", 2, False, 14),
        (311, "Swamp", 2, True, 14),
        (303, "Screeching Soulbreaker", 2, False, 14),
        (298, "Carnivorous Cultivator", 1, False, 13),
        (295, "Surveillance Phantasm", 1, False, 13),
        (291, "Island", 2, True, 12),
        (276, "Semester Foreseer", 2, False, 12),
        (274, "Swamp", 1, False, 11),
        (261, "Island", 2, True, 10),
        (249, "Divining Duelist", 2, False, 10),
        (245, "Surveillance Phantasm", 1, False, 9),
        (244, "Island", 1, True, 9),
        (229, "Mindseeker Oculus", 2, False, 8),
        (221, "Arni, Humble Scribe", 2, True, 6),
        (220, "Swamp", 2, True, 6),
        (211, "Way of the Deathbringer", 1, False, 5),
        (210, "Forest", 1, True, 5),
        (206, "Surveillance Phantasm", 2, False, 4),
        (205, "Island", 2, True, 4),
        (203, "Swamp", 1, True, 3),
        (201, "Island", 2, True, 2),
        (199, "Forest", 1, True, 1),
        (216, "Jace", 1, False, 5, {"counters": {"Loyalty": 7, "unknown": 7}, "object_kind": "TOKEN"}),
    ],
    "hand": [(317, "Island")],
    "graveyard": [
        (273, "Protege's Awakening", 1),
        (227, "Fatehold Chronologist", 1),
        (218, "Silence the Echo", 1),
        (321, "Plan for All Outcomes", 2),
        (318, "Protege's Awakening", 2),
        (310, "Icy Reception", 2),
        (293, "Island", 2),
        (290, "Murmuring Volume", 2),
        (287, "Island", 2),
        (283, "Twinned Vision", 2),
        (265, "Island", 2),
        (260, "Fatehold Chronologist", 2),
        (255, "Keeper of the Quiet Hour", 2),
        (242, "Garruk, Veiled Butcher", 2),
        (235, "Swamp", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": [
            "Surveillance Phantasm",
            "Mindseeker Oculus",
            "Divining Duelist",
            "Semester Foreseer",
        ],
        "legal_attacker_ids": [206, 229, 249, 276],
        "raw_attackers": [
            {
                "attackerInstanceId": 206,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 229,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 249,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 276,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
        ],
    },
    "stack": [(319, "Ability (ID: 208424)", "Surveil 1.", "ABILITY")],
    "legal_actions": [
        "Attack with: Surveillance Phantasm",
        "Attack with: Mindseeker Oculus",
        "Attack with: Divining Duelist",
        "Attack with: Semester Foreseer",
        "Done (confirm attackers)",
    ],
}

_SPEC_G2_T18_ATTACK = {
    "turn": 18,
    "active": 2,
    "phase": "Phase_Combat",
    "step": "Step_DeclareAttack",
    "local": 2,
    "life": {1: 11, 2: 9},
    "lands_played": {1: 0, 2: 0},
    "library": 4,
    "opponent_hand": 1,
    "battlefield": [
        (393, "Geist of Saint Thalia", 2, False, 18),
        (385, "Arni, Humble Scribe", 1, False, 17),
        (382, "Island", 1, True, 17),
        (361, "Swamp", 2, True, 16),
        (343, "Keeper of the Quiet Hour", 2, False, 16),
        (326, "Island", 1, True, 15),
        (312, "Murmuring Volume", 2, True, 14),
        (311, "Swamp", 2, True, 14),
        (303, "Screeching Soulbreaker", 2, False, 14),
        (298, "Carnivorous Cultivator", 1, True, 13),
        (291, "Island", 2, True, 12),
        (276, "Semester Foreseer", 2, False, 12),
        (274, "Swamp", 1, True, 11),
        (261, "Island", 2, True, 10),
        (245, "Surveillance Phantasm", 1, False, 9),
        (244, "Island", 1, True, 9),
        (221, "Arni, Humble Scribe", 2, True, 6),
        (220, "Swamp", 2, True, 6),
        (211, "Way of the Deathbringer", 1, False, 5),
        (210, "Forest", 1, True, 5),
        (206, "Surveillance Phantasm", 2, False, 4),
        (205, "Island", 2, True, 4),
        (203, "Swamp", 1, True, 3),
        (201, "Island", 2, True, 2),
        (199, "Forest", 1, True, 1),
        (216, "Jace", 1, False, 5, {"counters": {"Loyalty": 2, "unknown": 2}, "object_kind": "TOKEN"}),
        (338, "Leviathan", 1, False, 15, {"object_kind": "TOKEN"}),
        (384, "Leviathan", 1, False, 17, {"object_kind": "TOKEN"}),
    ],
    "hand": [],
    "graveyard": [
        (381, "Protege's Awakening", 1),
        (332, "Way of the Deathbringer", 1),
        (324, "Surveillance Phantasm", 1),
        (273, "Protege's Awakening", 1),
        (227, "Fatehold Chronologist", 1),
        (218, "Silence the Echo", 1),
        (436, "Swamp", 2),
        (433, "Swamp", 2),
        (429, "Void Extrapolator", 2),
        (404, "Living Library", 2),
        (371, "Island", 2),
        (367, "Sphinx's Approach", 2),
        (354, "Island", 2),
        (351, "Island", 2),
        (323, "Divining Duelist", 2),
        (322, "Mindseeker Oculus", 2),
        (321, "Plan for All Outcomes", 2),
        (318, "Protege's Awakening", 2),
        (310, "Icy Reception", 2),
        (293, "Island", 2),
        (290, "Murmuring Volume", 2),
        (287, "Island", 2),
        (265, "Island", 2),
        (260, "Fatehold Chronologist", 2),
        (255, "Keeper of the Quiet Hour", 2),
        (242, "Garruk, Veiled Butcher", 2),
        (235, "Swamp", 2),
    ],
    "decision": {
        "type": "declare_attackers",
        "legal_attackers": [
            "Surveillance Phantasm",
            "Semester Foreseer",
            "Screeching Soulbreaker",
            "Keeper of the Quiet Hour",
        ],
        "legal_attacker_ids": [206, 276, 303, 343],
        "raw_attackers": [
            {
                "attackerInstanceId": 206,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 276,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 303,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
            {
                "attackerInstanceId": 343,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
                ],
            },
        ],
    },
    "stack": [(434, "Ability (ID: 208424)", "Surveil 1.", "ABILITY")],
    "legal_actions": [
        "Attack with: Surveillance Phantasm",
        "Attack with: Semester Foreseer",
        "Attack with: Screeching Soulbreaker",
        "Attack with: Keeper of the Quiet Hour",
        "Done (confirm attackers)",
    ],
}

_SPEC: dict[str, dict[str, Any]] = {
    "G1_T9_MAIN1": _SPEC_G1_T9_MAIN1,
    "G1_T9_ATTACK": _SPEC_G1_T9_ATTACK,
    "G1_T12_THEIR_MAIN1": _SPEC_G1_T12_THEIR_MAIN1,
    "G1_T11_ATTACK": _SPEC_G1_T11_ATTACK,
    "G1_T13_ATTACK": _SPEC_G1_T13_ATTACK,
    "G1_T15_ATTACK": _SPEC_G1_T15_ATTACK,
    "G1_T16_THEIR_ATTACK": _SPEC_G1_T16_THEIR_ATTACK,
    "G2_T8_MAIN1": _SPEC_G2_T8_MAIN1,
    "G2_T8_ATTACK": _SPEC_G2_T8_ATTACK,
    "G2_T10_ATTACK": _SPEC_G2_T10_ATTACK,
    "G2_T12_ATTACK": _SPEC_G2_T12_ATTACK,
    "G2_T14_ATTACK": _SPEC_G2_T14_ATTACK,
    "G2_T18_ATTACK": _SPEC_G2_T18_ATTACK,
}


def card(instance_id: int, name: str, controller: int, **extra: Any) -> dict[str, Any]:
    info = deepcopy(CARDS[name])
    result = {
        "instance_id": instance_id,
        "grp_id": 0,
        "name": name,
        "owner_seat_id": controller,
        "controller_seat_id": controller,
        **info,
    }
    if "Token" in info["type_line"]:
        result["object_kind"] = "TOKEN"
    result.update(extra)
    return result


def build(spec: dict[str, Any], game: int) -> dict[str, Any]:
    """Planner-shape snapshot (server.get_game_state) from a compact spec row."""
    local = spec["local"]
    opponent = 2 if local == 1 else 1
    turn = spec["turn"]
    battlefield = []
    for row in spec["battlefield"]:
        iid, name, ctrl, tapped, entered = row[:5]
        extra = deepcopy(row[5]) if len(row) > 5 else {}  # a test may edit the counters it gets
        battlefield.append(
            card(
                iid,
                name,
                ctrl,
                is_tapped=tapped,
                turn_entered_battlefield=entered,
                summoning_sickness=entered is not None and entered >= turn,
                **extra,
            )
        )
    decision = deepcopy(spec.get("decision") or {})
    return {
        "match_id": MATCH_IDS[game],
        "local_seat_id": local,
        "opponent_seat_id": opponent,
        "turn": {
            "turn_number": turn,
            "active_player": spec["active"],
            "priority_player": spec["active"],
            "phase": spec["phase"],
            "step": spec["step"],
        },
        "players": [
            {
                "seat_id": seat,
                "life_total": spec["life"][seat],
                "is_local": seat == local,
                "lands_played": spec["lands_played"][seat],
                "mana_pool": {},
            }
            for seat in (1, 2)
        ],
        "battlefield": battlefield,
        "hand": [card(iid, name, local) for iid, name in spec["hand"]],
        "graveyard": [card(iid, name, owner) for iid, name, owner in spec["graveyard"]],
        "stack": [
            {
                "instance_id": row[0],
                "name": row[1],
                "oracle_text": row[2],
                "object_kind": row[3],
                # A fifth element names another controller (their spell on the stack).
                "controller_seat_id": row[4] if len(row) > 4 else local,
            }
            for row in spec.get("stack") or []
        ],
        "exile": [],
        "command": [],
        "zones": {
            "library_count": spec["library"],
            "library_count_source": "log_zone_membership",
            "opponent_hand_count": spec["opponent_hand"],
        },
        "deck_cards": [],
        "decision_context": decision,
        "pending_decision": "Declare Attackers" if decision.get("type") == "declare_attackers" else None,
        "legal_actions": list(spec.get("legal_actions") or []),
        "_bridge_request_type": "DeclareAttackers" if decision.get("type") == "declare_attackers" else None,
    }


def state(name: str) -> dict[str, Any]:
    """A fresh copy of the named window (``G1_T9_ATTACK`` ...)."""
    spec = _SPEC[name]
    return build(spec, 1 if name.startswith("G1") else 2)
