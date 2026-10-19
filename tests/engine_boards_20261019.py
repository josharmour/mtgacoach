"""Boards for their growing creatures and drain engines (autopilot-improvements ledger #128, #129,
#132, #133, #143, #147, #154), from the 2026-10-19 Mono-Red Burn ladder run.

Each board is the game's own, read from the match's Player.log (the GRE game states at the
gameStateId named) and checked against its timeline and coach log; we are seat 1 here whatever
our seat was. Card text is Arena's (the MTGA card database), Ajani Resolute's without its loyalty
costs as Arena gives it.

- 3e545401 T17 (gsid 397, 13:02:17): we are at 12 with Slickshot Show-Off and four Mountains,
  Fleeting Effigy in hand; they are at 14 with a tapped 8/7 flying first-strike Unflinching
  Hortimancer (Angelic Destiny, two +1/+1 counters), a fresh Ajani's Pridemate 2/2, Ajani
  Resolute at 2 loyalty and Authority of the Consuls. On T18 Ajani's life gain made them 9/8
  and 3/3: 12 into our 12.
- 16ed84d0 T7 (gsid 162, 13:15:24): we are at 18 with four Mountains, Hired Claw and Iroh's
  Demonstration in hand; they are at 14 with Biotech Specialist 1/3, The Sackville-Bagginses
  2/2 and a Lander token. Iroh's killed the Bagginses; Biotech dealt 13 of our 20.
- 16ed84d0 T8 (gsid 194, 13:15:52): their main phase, we are at 18 with Hired Claw; they have
  Biotech, the second Bagginses and the Lander. "They kill us in 10" — we lost 7 that turn.
- 16ed84d0 T12 (13:17:43, their main phase before the triggers): we are at 4 with no creature;
  they have two Biotech Specialists (2/4 and a fresh 1/3), a fresh Bagginses, Gene Pollinator,
  a Clue and Obsessive Pursuit. The concede check said "not dead next attack"; two Biotech
  triggers killed us 8 s later.
- 970d9b41 T12 (gsid 244, 13:34:07): we are at 20 with Emberheart Challenger, three untapped
  Mountains and Iroh's Demonstration; they are at 19 with Ravenhill Flock 1/2 and Confusticate
  and Bebother (its mode draws two) in their graveyard. Iroh's 1-damage mode left the Flock; it
  grew on every draw: 18, 15, 10, 2.
- 1b4af0f9 T6 (13:38:12): we are at 19, they at 14 with no creature; Tectonic Hazard, Iroh's
  Demonstration, Boltwave and two Mountains in hand, two Mountains on the battlefield. Hazard
  went face; Hulking Raptor (T7) and Bonehoard Dracosaur (T9) followed.
- f5bc17dc T6 (14:02:46, their beginning of combat): we are at 20 with three Mountains, two
  Burst Lightnings and Shock; they are at 14 with Doom Reigns Supreme and lands. All three went
  face (14 -> 8); Baron Helmut Zemo came on T8 and grew into the 5/5 that killed us.
"""

from __future__ import annotations

from tests.race_boards_20261019 import CARDS, board

CARDS.setdefault("Unflinching Hortimancer", {
    "type_line": "Creature — Human Cleric", "mana_cost": "{1}{W}", "power": 2, "toughness": 1,
    "card_types": ["CardType_Creature"], "subtypes": ["Human", "Cleric"],
    "oracle_text": "Ward {o1}\nWhenever you gain life, put a +1/+1 counter on this creature.",
})  # fmt: skip
CARDS.setdefault("Ajani's Pridemate", {
    "type_line": "Creature — Cat Soldier", "mana_cost": "{1}{W}", "power": 2, "toughness": 2,
    "card_types": ["CardType_Creature"], "subtypes": ["Cat", "Soldier"],
    "oracle_text": "Whenever you gain life, put a +1/+1 counter on this creature.",
})  # fmt: skip
CARDS.setdefault("Lyra, Archangel of Dawn", {
    "type_line": "Legendary Creature — Angel Knight", "mana_cost": "{2}{W}", "power": 3, "toughness": 3,
    "keywords": ["flying"], "card_types": ["CardType_Creature"], "subtypes": ["Angel", "Knight"],
    "oracle_text": "Flying\nWhenever you gain life, put a +1/+1 counter on each Angel you control.",
})  # fmt: skip
CARDS.setdefault("Healer's Hawk", {
    "type_line": "Creature — Bird", "mana_cost": "{W}", "power": 1, "toughness": 1,
    "keywords": ["flying", "lifelink"], "card_types": ["CardType_Creature"], "subtypes": ["Bird"],
    "oracle_text": "Flying\nLifelink",
})  # fmt: skip
CARDS.setdefault("Angelic Destiny", {
    "type_line": "Enchantment — Aura", "mana_cost": "{2}{W}{W}", "card_types": ["CardType_Enchantment"],
    "oracle_text": "Enchant creature\nEnchanted creature gets +4/+4, has flying and first strike, and is an Angel "
    "in addition to its other types.\nWhen enchanted creature dies, return this card to its owner's hand.",
})  # fmt: skip
CARDS.setdefault("Authority of the Consuls", {
    "type_line": "Enchantment", "mana_cost": "{W}", "card_types": ["CardType_Enchantment"],
    "oracle_text": "Creatures your opponents control enter tapped.\nWhenever a creature an opponent controls "
    "enters, you gain 1 life.",
})  # fmt: skip
CARDS.setdefault("Stasis Snare", {
    "type_line": "Enchantment", "mana_cost": "{1}{W}{W}", "card_types": ["CardType_Enchantment"],
    "oracle_text": "Flash\nWhen this enchantment enters, exile target creature an opponent controls until this "
    "enchantment leaves the battlefield.",
})  # fmt: skip
CARDS.setdefault("Giant Cindermaw", {
    "type_line": "Creature — Dinosaur Beast", "mana_cost": "{2}{R}", "power": 4, "toughness": 3,
    "keywords": ["trample"], "card_types": ["CardType_Creature"], "subtypes": ["Dinosaur", "Beast"],
    "oracle_text": "Trample\nPlayers can't gain life.",
})  # fmt: skip
CARDS.setdefault("Biotech Specialist", {
    "type_line": "Creature — Insect Scientist", "mana_cost": "{R}{G}", "power": 1, "toughness": 3,
    "card_types": ["CardType_Creature"], "subtypes": ["Insect", "Scientist"],
    "oracle_text": "When this creature enters, create a Lander token.\nWhenever you sacrifice an artifact, this "
    "creature deals 2 damage to target opponent.",
})  # fmt: skip
CARDS.setdefault("The Sackville-Bagginses", {
    "type_line": "Legendary Creature — Halfling Citizen", "mana_cost": "{1}{B}", "power": 2, "toughness": 2,
    "card_types": ["CardType_Creature"], "subtypes": ["Halfling", "Citizen"],
    "oracle_text": "When The Sackville-Bagginses enter, you may sacrifice another creature or artifact. If you "
    "do, draw a card and create a Treasure token.\nWhenever you sacrifice a token, target opponent loses 1 life.",
})  # fmt: skip
CARDS.setdefault("Lander", {
    "type_line": "Token Artifact — Lander", "card_types": ["CardType_Artifact"], "subtypes": ["Lander"],
    "object_kind": "TOKEN",
    "oracle_text": "{o2}, {oT}, Sacrifice this token: Search your library for a basic land card, put it onto the "
    "battlefield tapped, then shuffle.",
})  # fmt: skip
CARDS.setdefault("Clue", {
    "type_line": "Token Artifact — Clue", "card_types": ["CardType_Artifact"], "subtypes": ["Clue"],
    "object_kind": "TOKEN", "oracle_text": "{o2}, Sacrifice this token: Draw a card.",
})  # fmt: skip
CARDS.setdefault("Gene Pollinator", {
    "type_line": "Artifact Creature — Robot Insect", "mana_cost": "{G}", "power": 1, "toughness": 2,
    "card_types": ["CardType_Artifact", "CardType_Creature"], "subtypes": ["Robot", "Insect"],
    "oracle_text": "{oT}, Tap an untapped permanent you control: Add one mana of any color.",
})  # fmt: skip
CARDS.setdefault("Obsessive Pursuit", {
    "type_line": "Enchantment", "mana_cost": "{1}{B}", "card_types": ["CardType_Enchantment"],
    "oracle_text": "When this enchantment enters and at the beginning of your upkeep, you lose 1 life and create "
    "a Clue token.\nWhenever you attack, put X +1/+1 counters on target attacking creature, where X is the number "
    "of permanents you've sacrificed this turn. If X is three or more, that creature gains lifelink until end of "
    "turn.",
})  # fmt: skip
CARDS.setdefault("Ravenhill Flock", {
    "type_line": "Creature — Bird", "mana_cost": "{3}{U}", "power": 1, "toughness": 2, "keywords": ["flying"],
    "card_types": ["CardType_Creature"], "subtypes": ["Bird"],
    "oracle_text": "Flying\nWhenever you draw a card, put a +1/+1 counter on this creature.",
})  # fmt: skip
CARDS.setdefault("Lakeshore Apothecary", {
    "type_line": "Creature — Human Cleric", "mana_cost": "{1}{U}", "power": 1, "toughness": 2,
    "keywords": ["vigilance"], "card_types": ["CardType_Creature"], "subtypes": ["Human", "Cleric"],
    "oracle_text": "Vigilance\nWhenever you draw your second card each turn, put a +1/+1 counter on this creature.",
})  # fmt: skip
CARDS.setdefault("Bilbo, Luckwearer", {
    "type_line": "Legendary Creature — Halfling Rogue", "mana_cost": "{1}{U}", "power": 1, "toughness": 1,
    "card_types": ["CardType_Creature"], "subtypes": ["Halfling", "Rogue"],
    "oracle_text": "Bilbo can't be blocked.\nWhenever Bilbo deals combat damage to a player, draw a card, then "
    "discard a card.",
})  # fmt: skip
CARDS.setdefault("Iroh's Demonstration", {
    "type_line": "Sorcery — Lesson", "mana_cost": "{1}{R}", "card_types": ["CardType_Sorcery"],
    "oracle_text": "Choose one — \n•Iroh's Demonstration deals 1 damage to each creature your opponents control.\n"
    "•Iroh's Demonstration deals 4 damage to target creature.",
})  # fmt: skip
CARDS.setdefault("Doom Reigns Supreme", {
    "type_line": "Enchantment — Plan", "mana_cost": "{2}{B}{B}", "card_types": ["CardType_Enchantment"],
    "oracle_text": "Whenever a Villain you control enters, each opponent loses 1 life and you gain 1 life. Put a "
    "plan counter on this enchantment.\nWhen the fifth plan counter is put on this enchantment, sacrifice it. When "
    "you do, target opponent exiles the top five cards of their library. You may cast up to two spells from among "
    "the exiled cards without paying their mana costs.",
})  # fmt: skip
for _land, _mana in (("Island", "U"), ("Swamp", "B")):
    CARDS.setdefault(
        _land, {"type_line": f"Basic Land — {_land}", "oracle_text": f"({{T}}: Add {{{_mana}}}.)",
                "card_types": ["CardType_Land"]},
    )  # fmt: skip

# Arena's Ajani Resolute (MTGA card database grpId 106440): the loyalty costs are not in its text.
AJANI_RESOLUTE_TEXT = (
    "Whenever you gain life, put a loyalty counter on Ajani.\nYou gain 1 life.\nCreate a 2/2 white Cat Soldier "
    'creature token named Ajani\'s Pridemate with "Whenever you gain life, put a +1/+1 counter on this token."\n'
    'You get an emblem with "Creatures you control get +2/+2."'
)
CONFUSTICATE = {
    "instance_id": 334, "name": "Confusticate and Bebother", "type_line": "Instant", "owner_seat_id": 2,
    "controller_seat_id": 2, "card_types": ["CardType_Instant"],
    "oracle_text": "Choose one — \n•Counter target spell unless its controller pays {o4}.\n•Draw two cards, then "
    "discard a card.",
}  # fmt: skip

_HORTIMANCER_87 = {
    "power": 8, "toughness": 7, "counters": {"P1P1": 2}, "keywords": ["flying", "first strike"],
    "subtypes": ["Human", "Cleric", "Angel"],
}  # fmt: skip
_PLAINS_3E5 = [
    (iid, "Plains", True, turn) for iid, turn in ((286, 2), (299, 4), (315, 6), (327, 8), (340, 10))
]

# 3e545401 T17, gsid 397 (their Giant Cindermaw of ours is exiled by Stasis Snare).
G3E5_T17 = board(
    turn=17, life=12, their_life=14, lands_played=1, opponent_hand=0,
    ours=[(281, "Mountain", False, 1), (293, "Mountain", False, 3), (305, "Mountain", False, 5),
          (333, "Mountain", False, 9), (347, "Slickshot Show-Off", False, 11)],
    theirs=[*_PLAINS_3E5, (353, "Unflinching Hortimancer", True, 12, _HORTIMANCER_87),
            (366, "Angelic Destiny", False, 14, {"attached_to_id": 353}), (389, "Ajani's Pridemate", False, 16),
            (341, "Ajani Resolute", False, 10, {"oracle_text": AJANI_RESOLUTE_TEXT, "counters": {"Loyalty": 2}}),
            (302, "Authority of the Consuls", False, 4), (380, "Stasis Snare", False, 16)],
    hand=[(392, "Fleeting Effigy")],
)  # fmt: skip

_THEIR_LANDS_16E = [(283, "Swamp", True, 2), (300, "Forest", True, 4), (308, "Forest", True, 6)]

# 16ed84d0 T7, gsid 162 (after our land drop).
G16E_T7 = board(
    turn=7, life=18, their_life=14, lands_played=1, opponent_hand=5,
    ours=[(279, "Mountain", False, 1), (287, "Mountain", False, 3), (306, "Mountain", False, 5),
          (325, "Mountain", False, 7)],
    theirs=[*_THEIR_LANDS_16E, (317, "Biotech Specialist", False, 6), (309, "The Sackville-Bagginses", False, 6),
            (323, "Lander", False, 6)],
    hand=[(324, "Hired Claw"), (222, "Iroh's Demonstration")],
)  # fmt: skip

# 16ed84d0 T8, gsid 194: their main phase, the second Bagginses just resolved (its enters trigger
# left out), our Hired Claw cast on T7.
G16E_T8 = board(
    turn=8, life=18, their_life=15, lands_played=1, opponent_hand=3, active=2,
    ours=[(279, "Mountain", True, 1), (287, "Mountain", True, 3), (306, "Mountain", True, 5),
          (325, "Mountain", False, 7), (331, "Hired Claw", False, 7)],
    theirs=[*_THEIR_LANDS_16E, (334, "Forest", True, 8), (317, "Biotech Specialist", False, 6),
            (336, "The Sackville-Bagginses", False, 8), (323, "Lander", False, 6)],
    hand=[(305, "Mountain")],
)  # fmt: skip

# 16ed84d0 T12, 13:17:43: their main phase after Lively Dirge, before the Biotech triggers.
G16E_T12 = board(
    turn=12, life=4, their_life=13, lands_played=0, opponent_hand=3, active=2,
    ours=[(279, "Mountain", False, 1), (287, "Mountain", False, 3), (306, "Mountain", False, 5),
          (325, "Mountain", False, 7), (353, "Mountain", False, 9), (373, "Mountain", False, 11)],
    theirs=[*_THEIR_LANDS_16E, (334, "Forest", True, 8), (355, "Forest", True, 10),
            (317, "Biotech Specialist", False, 6, {"power": 2, "toughness": 4, "counters": {"P1P1": 1}}),
            (429, "Biotech Specialist", False, 12), (428, "The Sackville-Bagginses", False, 12),
            (361, "Gene Pollinator", False, 10), (375, "Clue", False, 12), (356, "Obsessive Pursuit", False, 10)],
    hand=[(350, "Hired Claw")],
)  # fmt: skip

# 970d9b41 T12, gsid 244: Emberheart Challenger cast (two Mountains tapped for it), Iroh's in hand.
G970_T12 = board(
    turn=12, life=20, their_life=19, lands_played=0, opponent_hand=3,
    ours=[(284, "Mountain", True, 2), (294, "Mountain", True, 4), (312, "Mountain", False, 6),
          (335, "Mountain", False, 8), (349, "Mountain", False, 10), (357, "Emberheart Challenger", False, 12)],
    theirs=[(282, "Island", True, 1), (289, "Island", True, 3), (324, "Island", True, 7), (337, "Island", True, 9),
            (351, "Ravenhill Flock", False, 11)],
    hand=[(325, "Iroh's Demonstration")],
)  # fmt: skip
G970_T12["graveyard"] = [CONFUSTICATE]

# 1b4af0f9 T6, 13:38:12: before our land drop.
G1B4_T6 = board(
    turn=6, life=19, their_life=14, lands_played=0, opponent_hand=4,
    ours=[(344, "Mountain", False, 2), (351, "Mountain", False, 4)],
    theirs=[(340, "Forest", False, 1), (349, "Forest", False, 3), (360, "Forest", False, 5)],
    hand=[(352, "Tectonic Hazard"), (379, "Iroh's Demonstration"), (364, "Boltwave"), (362, "Mountain"),
          (377, "Mountain")],
)  # fmt: skip

# f5bc17dc T6, 14:02:46: their beginning of combat.
F5BC_T6 = board(
    turn=6, life=20, their_life=14, lands_played=0, opponent_hand=5, active=2, phase="Phase_Combat",
    step="Step_BeginCombat",
    ours=[(283, "Mountain", False, 1), (290, "Mountain", False, 3), (300, "Mountain", False, 5)],
    theirs=[(288, "Swamp", False, 2), (295, "Island", False, 4), (302, "Swamp", False, 6),
            (296, "Doom Reigns Supreme", False, 4)],
    hand=[(301, "Burst Lightning"), (304, "Burst Lightning"), (303, "Shock")],
)  # fmt: skip

# --- 2b77f14d (Brawl, 25 life; ledger #172) ----------------------------------------------------
CARDS.setdefault("The Unbeatable Squirrel Girl", {
    "type_line": "Legendary Creature — Squirrel Human Hero", "mana_cost": "{1}{G}{G}{G}", "power": 4,
    "toughness": 4, "card_types": ["CardType_Creature"], "subtypes": ["Squirrel", "Human", "Hero"],
    "oracle_text": "<i>Do You Like Squirrels?</i> — Whenever The Unbeatable Squirrel Girl enters or attacks, "
    "create a 1/1 green Squirrel creature token.\n<i>I LOVE Squirrels!</i> — {o1oGoGoG}: Create X 1/1 green "
    "Squirrel creature tokens, where X is the number of Squirrels you control.",
})  # fmt: skip
CARDS.setdefault("Caged Sun", {
    "type_line": "Artifact", "mana_cost": "{6}", "card_types": ["CardType_Artifact"],
    "oracle_text": "As this artifact enters, choose a color.\nCreatures you control of the chosen color get "
    "+1/+1.\nWhenever a land's ability causes you to add one or more mana of the chosen color, add an "
    "additional one mana of that color.",
})  # fmt: skip
CARDS.setdefault("Bakersbane Duo", {
    "type_line": "Creature — Squirrel Raccoon", "mana_cost": "{1}{G}", "power": 2, "toughness": 2,
    "card_types": ["CardType_Creature"], "subtypes": ["Squirrel", "Raccoon"],
    "oracle_text": "When this creature enters, create a Food token.\nWhenever you expend 4, this creature gets "
    "+1/+1 until end of turn.",
})  # fmt: skip
CARDS.setdefault("Squirrel", {
    "type_line": "Token Creature — Squirrel", "power": 1, "toughness": 1, "card_types": ["CardType_Creature"],
    "subtypes": ["Squirrel"], "object_kind": "TOKEN", "oracle_text": "",
})  # fmt: skip
CARDS.setdefault("Rabbit", {
    "type_line": "Token Creature — Rabbit", "power": 1, "toughness": 1, "card_types": ["CardType_Creature"],
    "subtypes": ["Rabbit"], "object_kind": "TOKEN", "oracle_text": "",
})  # fmt: skip
CARDS.setdefault("Hare Apparent", {
    "type_line": "Creature — Rabbit Noble", "mana_cost": "{1}{W}", "power": 2, "toughness": 2,
    "card_types": ["CardType_Creature"], "subtypes": ["Rabbit", "Noble"],
    "oracle_text": "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to the "
    "number of other creatures you control named Hare Apparent.\nA deck can have any number of cards named Hare "
    "Apparent.",
})  # fmt: skip
CARDS.setdefault("Llanowar Elves", {
    "type_line": "Creature — Elf Druid", "mana_cost": "{G}", "power": 1, "toughness": 1,
    "card_types": ["CardType_Creature"], "subtypes": ["Elf", "Druid"], "oracle_text": "{oT}: Add {oG}.",
})  # fmt: skip
CARDS.setdefault("Pearl Medallion", {
    "type_line": "Artifact", "mana_cost": "{2}", "card_types": ["CardType_Artifact"],
    "oracle_text": "White spells you cast cost {o1} less to cast.",
})  # fmt: skip

# 2b77f14d T7, gsid 168 (15:03:57): their attack is declared — Squirrel Girl 5/5, Bakersbane Duo
# 4/4 and a 2/2 Squirrel (Caged Sun, green, +1/+1) — into our three Hare Apparents (2/2) and three
# Rabbits (1/1); every land of theirs is tapped. We chumped with two Rabbits. Three Hares kill her
# (6 damage into 5 toughness); on T9 two activations (Caged Sun doubling their Forests) made 4,
# then 8 Squirrels.
_ATTACKING = {"is_attacking": True}
G2B7_T7 = board(
    turn=7, life=21, their_life=25, lands_played=0, opponent_hand=2, active=2, phase="Phase_Combat",
    step="Step_DeclareBlock",
    ours=[(442, "Plains", True, 2), (457, "Plains", True, 4), (472, "Plains", True, 6),
          (458, "Pearl Medallion", False, 4), (473, "Hare Apparent", False, 6), (476, "Hare Apparent", False, 6),
          (480, "Hare Apparent", False, 6), (479, "Rabbit", False, 6), (483, "Rabbit", False, 6),
          (484, "Rabbit", False, 6)],
    theirs=[(438, "Forest", True, 1), (444, "Forest", True, 3), (450, "Forest", True, 3), (462, "Forest", True, 5),
            (486, "Forest", True, 7), (439, "Llanowar Elves", True, 1, {"power": 2, "toughness": 2}),
            (487, "Caged Sun", False, 7),
            (463, "The Unbeatable Squirrel Girl", True, 5, {"power": 5, "toughness": 5, **_ATTACKING}),
            (451, "Bakersbane Duo", True, 3, {"power": 4, "toughness": 4, **_ATTACKING}),
            (470, "Squirrel", True, 5, {"power": 2, "toughness": 2, **_ATTACKING}),
            (496, "Squirrel", False, 7, {"power": 2, "toughness": 2})],
    hand=[(471, "Hare Apparent"), (246, "Hare Apparent"), (441, "Plains")],
)  # fmt: skip

# --- b62b73fc (Brawl, 25 life; enters pings and a sacrifice outlet) ---------------------------
CARDS.setdefault("Impact Tremors", {
    "type_line": "Enchantment", "mana_cost": "{1}{R}", "card_types": ["CardType_Enchantment"],
    "oracle_text": "Whenever a creature you control enters, this enchantment deals 1 damage to each opponent.",
})  # fmt: skip
CARDS.setdefault("Goblin Bombardment", {
    "type_line": "Enchantment", "mana_cost": "{1}{R}", "card_types": ["CardType_Enchantment"],
    "oracle_text": "Sacrifice a creature: This enchantment deals 1 damage to any target.",
})  # fmt: skip
CARDS.setdefault("Krenko, Mob Boss", {
    "type_line": "Legendary Creature — Goblin Warrior", "mana_cost": "{2}{R}{R}", "power": 3, "toughness": 3,
    "card_types": ["CardType_Creature"], "subtypes": ["Goblin", "Warrior"],
    "oracle_text": "{oT}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
})  # fmt: skip
CARDS.setdefault("Battle Cry Goblin", {
    "type_line": "Creature — Goblin", "mana_cost": "{1}{R}", "power": 2, "toughness": 2,
    "card_types": ["CardType_Creature"], "subtypes": ["Goblin"],
    "oracle_text": "{o1oR}: Goblins you control get +1/+0 and gain haste until end of turn.\n<i>Pack tactics</i> — "
    "Whenever this creature attacks, if you attacked with creatures with total power 6 or greater this combat, "
    "create a 1/1 red Goblin creature token that's tapped and attacking.",
})  # fmt: skip
CARDS.setdefault("Goblin Bushwhacker", {
    "type_line": "Creature — Goblin Warrior", "mana_cost": "{R}", "power": 1, "toughness": 1,
    "card_types": ["CardType_Creature"], "subtypes": ["Goblin", "Warrior"],
    "oracle_text": "Kicker {oR}\nWhen this creature enters, if it was kicked, creatures you control get +1/+0 and "
    "gain haste until end of turn.",
})  # fmt: skip
CARDS.setdefault("Goblin", {
    "type_line": "Token Creature — Goblin", "power": 1, "toughness": 1, "card_types": ["CardType_Creature"],
    "subtypes": ["Goblin"], "object_kind": "TOKEN", "oracle_text": "",
})  # fmt: skip

# b62b73fc T13, gsid 331 (15:27:32): their main phase, Battle Cry Goblin just resolved (Impact Tremors:
# 13 -> 12). They never attacked: Krenko's 10 Goblins pinged us to 2 and Goblin Bombardment
# sacrificed two; the facts said "CONTROL/STABILIZE — we win the long game" 26 s before.
GB62_T13 = board(
    turn=13, life=12, their_life=19, lands_played=1, opponent_hand=0, active=2,
    ours=[(540, "Plains", True, 2), (547, "Plains", True, 4), (571, "Plains", True, 6), (630, "Plains", True, 12),
          (601, "Hare Apparent", False, 10), (631, "Hare Apparent", False, 12), (643, "Hare Apparent", False, 12),
          *[(iid, "Rabbit", False, 10) for iid in (570, 591, 605, 606, 635, 636, 637, 647, 648)]],
    theirs=[(538, "Mountain", True, 1), (542, "Mountain", True, 3), (553, "Mountain", False, 5),
            (582, "Mountain", False, 7), (593, "Mountain", False, 9), (543, "Impact Tremors", False, 3),
            (583, "Goblin Bombardment", False, 7), (594, "Krenko, Mob Boss", False, 9),
            (613, "Goblin Bushwhacker", False, 11), (650, "Battle Cry Goblin", False, 13),
            *[(iid, "Goblin", False, 11) for iid in (559, 560, 618, 619, 620, 621, 622)]],
    hand=[(600, "Hare Apparent"), (586, "Hare Apparent"), (574, "Hare Apparent"), (546, "Hare Apparent")],
)  # fmt: skip
