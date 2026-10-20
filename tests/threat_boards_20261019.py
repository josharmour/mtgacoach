"""Real boards for their attack's pumps, grants and extra combats (ledger #191, #193, #194).

Rebuilt from MTGA's Player.log (2026-10-19), card texts from the MTGA card
database (``server.enrich_with_oracle_text``):

- ``lannery_t11_blocks``: match 62d61e75 T11 at the DeclareBlockersReq
  (gameStateId 298, Player.log L60910, 16:56:35). Three Captain Lannery Storms
  (476 and Delina's two non-legendary copies 537/538, made 2/2 at gsid
  294/296), Isshin 3/4 and Delina 3/2 attack us at 19; they have four
  untapped Treasures (506, 507 and 535/536 from Lannery's attack, doubled by
  Isshin). After blocks they sacrificed all four (gsid 301-332): each Lannery
  +4, 3+6+6+6 = 21 (gsid 334).
- ``aurelia_t13``: match f80ca0cf T13 (we are seat 1 at 7 life): at Main 1
  before combat (gsid 393) or at the DeclareBlockersReq (gsid 405, attackers
  untapped by Aurelia's trigger, doubled by Windcrag Siege).
- ``harbin_t11``: match 3ac9d511 T11 Main 1 (gsid 295, we are seat 1 at 8):
  Harbin 6/5 flying, Shackle Slinger 3/2, Siege Veteran 2/2 and a 1/1 Soldier
  token: four Soldiers that can attack, one short of Harbin's trigger.
"""

from __future__ import annotations

LANNERY = (
    "Haste\nWhenever Captain Lannery Storm attacks, create a Treasure token.\n"
    "Whenever you sacrifice a Treasure, Captain Lannery Storm gets +1/+0 until end of turn."
)
DELINA = (
    "Whenever Delina attacks, choose target creature you control, then roll a d20. 1-14 | Create a tapped "
    "and attacking token that's a copy of that creature, except it's not legendary and it has \"At end of "
    'combat, exile this token." 15-20 | Create one of those tokens. You may roll again.'
)
ISSHIN = (
    "If a creature attacking causes a triggered ability of a permanent you control to trigger, that ability "
    "triggers an additional time."
)
TREASURE = "{oT}, Sacrifice this token: Add one mana of any color."
HARE = (
    "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to the number of "
    "other creatures you control named Hare Apparent.\nA deck can have any number of cards named Hare Apparent."
)
AURELIA = (
    "Flying\nVigilance\nHaste\nWhenever Aurelia attacks for the first time each turn, untap all creatures you "
    "control. After this phase, there is an additional combat phase."
)
WINDCRAG = (
    "As this enchantment enters, choose Mardu or Jeskai.\n•<indent=4%>Mardu — If a creature attacking causes a "
    "triggered ability of a permanent you control to trigger, that ability triggers an additional time."
    "</indent>\n•<indent=4%>Jeskai — At the beginning of your upkeep, create a 1/1 red Goblin creature token. "
    "It gains lifelink and haste until end of turn.</indent>"
)
HARBIN = (
    "Flying\nWhenever you attack with five or more Soldiers, creatures you control get +1/+1 and gain flying "
    "until end of turn."
)


def card(
    iid, name, seat, *, power=None, toughness=None, types=("CardType_Creature",), subtypes=(), type_line="",
    text="", tapped=False, attacking=False, token=False, sick=False, entered=None, grp=0,
):  # fmt: skip
    out = {
        "instance_id": iid,
        "grp_id": grp,
        "name": name,
        "type_line": type_line,
        "card_types": list(types),
        "subtypes": [f"SubType_{s}" for s in subtypes],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": tapped,
        "is_attacking": attacking,
        "object_kind": "TOKEN" if token else "CARD",
        "oracle_text": text,
        "keywords": [],
    }
    if power is not None:
        out["power"], out["toughness"] = power, toughness
    if sick:
        out["summoning_sickness"] = True
    if entered is not None:
        out["turn_entered_battlefield"] = entered
    return out


def land(iid, name, seat, *, tapped=False, text="{oT}: Add {oW}.", subtypes=()):
    return card(
        iid,
        name,
        seat,
        types=("CardType_Land",),
        subtypes=subtypes,
        type_line="Land",
        text=text,
        tapped=tapped,
    )


def state(
    battlefield,
    *,
    match,
    local,
    life,
    opp_life,
    turn,
    active,
    phase,
    step,
    their_hand,
    raw_blockers=None,
    hand=None,
):
    out = {
        "match_id": match,
        "local_seat_id": local,
        "players": [
            {"seat_id": local, "is_local": True, "life_total": life},
            {"seat_id": 3 - local, "is_local": False, "life_total": opp_life},
        ],
        "turn": {"turn_number": turn, "active_player": active, "phase": phase, "step": step},
        "battlefield": battlefield,
        "hand": hand or [],
        "stack": [],
        "opponent_hand_count": their_hand,
    }
    if raw_blockers is not None:
        out["decision_context"] = {"raw_blockers": raw_blockers}
    for permanent in battlefield:  # unmarked creatures came in on an earlier turn (not summoning sick)
        if "CardType_Creature" in permanent["card_types"]:
            permanent.setdefault("turn_entered_battlefield", 1)
    return out


# --- match 62d61e75 T11 -----------------------------------------------------------------------

US, THEM = 2, 1  # 62d61e75: we are seat 2
LANNERY_IDS = (476, 537, 538)
PLANNED_62D61E75 = {517: 499, 521: 455}  # the plan: Hare Apparent -> Delina, Rabbit -> Isshin


def lannery_t11_blocks(*, treasures=(506, 507, 535, 536), life=19):
    attackers = [455, 537, 499, 538, 476]
    theirs = [
        card(
            525,
            "Fable of the Mirror-Breaker",
            THEM,
            types=("CardType_Enchantment",),
            subtypes=("Saga",),
            type_line="Enchantment — Saga",
            text="(As this Saga enters and after your draw step, add a lore counter.)",
        ),  # fmt: skip
        land(524, "Sacred Foundry", THEM, tapped=True, text="{oT}: Add {oR}.\n{oT}: Add {oW}."),
        land(498, "Caves of Koilos", THEM, tapped=True, text="{oT}: Add {oC}."),
        land(466, "Plains", THEM, text="({T}: Add {W}.)", subtypes=("Plains",)),
        land(454, "Plains", THEM, tapped=True, text="({T}: Add {W}.)", subtypes=("Plains",)),
        land(443, "Inspiring Vantage", THEM, tapped=True, text="{oT}: Add {oR} or {oW}."),
        land(438, "Raucous Theater", THEM, text="{oT}: Add {oR}.\n{oT}: Add {oB}."),
        card(
            444,
            "Boros Signet",
            THEM,
            types=("CardType_Artifact",),
            type_line="Artifact",
            text="{o1}, {oT}: Add {oRoW}.",
        ),
        card(
            499,
            "Delina, Wild Mage",
            THEM,
            power=3,
            toughness=2,
            subtypes=("Elf", "Shaman"),
            type_line="Legendary Creature — Elf Shaman",
            text=DELINA,
            tapped=True,
            attacking=True,
        ),  # fmt: skip
        card(
            476,
            "Captain Lannery Storm",
            THEM,
            power=2,
            toughness=2,
            subtypes=("Human", "Pirate"),
            type_line="Legendary Creature — Human Pirate",
            text=LANNERY,
            tapped=True,
            attacking=True,
        ),  # fmt: skip
        card(
            455,
            "Isshin, Two Heavens as One",
            THEM,
            power=3,
            toughness=4,
            subtypes=("Human", "Samurai"),
            type_line="Legendary Creature — Human Samurai",
            text=ISSHIN,
            tapped=True,
            attacking=True,
        ),  # fmt: skip
        card(
            530,
            "Goblin Shaman",
            THEM,
            power=2,
            toughness=2,
            subtypes=("Goblin", "Shaman"),
            token=True,
            type_line="Token Creature — Goblin Shaman",
            text="Whenever this token attacks, create a Treasure token.",
            sick=True,
            entered=11,
        ),  # fmt: skip
        *(
            card(
                iid,
                "Captain Lannery Storm",
                THEM,
                power=2,
                toughness=2,
                subtypes=("Human", "Pirate"),
                token=True,
                type_line="Creature — Human Pirate",
                text=LANNERY,
                tapped=True,
                attacking=True,
                entered=11,
            )  # fmt: skip
            for iid in (537, 538)
        ),
        *(
            card(
                iid,
                "Treasure",
                THEM,
                types=("CardType_Artifact",),
                subtypes=("Treasure",),
                token=True,
                type_line="Token Artifact — Treasure",
                text=TREASURE,
            )  # fmt: skip
            for iid in treasures
        ),
    ]
    ours = [
        card(
            517,
            "Hare Apparent",
            US,
            power=2,
            toughness=2,
            subtypes=("Rabbit", "Noble"),
            type_line="Creature — Rabbit Noble",
            text=HARE,
            sick=True,
            entered=10,
        ),  # fmt: skip
        card(
            460,
            "Hare Apparent",
            US,
            power=2,
            toughness=2,
            subtypes=("Rabbit", "Noble"),
            type_line="Creature — Rabbit Noble",
            text=HARE,
            tapped=True,
        ),  # fmt: skip
        card(
            496,
            "Rabbit",
            US,
            power=1,
            toughness=1,
            subtypes=("Rabbit",),
            token=True,
            type_line="Token Creature — Rabbit",
            tapped=True,
        ),  # fmt: skip
        card(
            521,
            "Rabbit",
            US,
            power=1,
            toughness=1,
            subtypes=("Rabbit",),
            token=True,
            type_line="Token Creature — Rabbit",
            sick=True,
            entered=10,
        ),  # fmt: skip
        land(448, "Plains", US, tapped=True, text="({T}: Add {W}.)", subtypes=("Plains",)),
        land(441, "Plains", US, tapped=True, text="({T}: Add {W}.)", subtypes=("Plains",)),
    ]
    hand = [
        card(
            iid,
            "Hare Apparent",
            US,
            power=2,
            toughness=2,
            subtypes=("Rabbit", "Noble"),
            type_line="Creature — Rabbit Noble",
            text=HARE,
        )  # fmt: skip
        for iid in (516, 491, 459, 447, 440)
    ]
    raw = [
        {"blockerInstanceId": 517, "attackerInstanceIds": attackers, "maxAttackers": 1},
        {"blockerInstanceId": 521, "attackerInstanceIds": attackers, "maxAttackers": 1},
    ]
    return state(
        [*theirs, *ours], match="62d61e75", local=US, life=life, opp_life=16, turn=11, active=THEM,
        phase="Phase_Combat", step="Step_DeclareBlock", their_hand=0, raw_blockers=raw, hand=hand,
    )  # fmt: skip


# --- match f80ca0cf T13 -----------------------------------------------------------------------

ME, OPP = 1, 2  # f80ca0cf and 3ac9d511: we are seat 1
AURELIA_ATTACKERS = (559, 471, 540, 503, 493, 548)
PLANNED_F80CA0CF = {489: 540, 490: 503, 517: 493, 522: 471, 524: 559}  # five Rabbits chump the ground


def aurelia_t13(*, blocks=True, our_rabbits=None, life=7):
    """``blocks``: at the DeclareBlockersReq (gsid 405), else Main 1 before combat (gsid 393)."""
    attacking = blocks

    def attacker(iid, name, power, toughness, subtypes, text, type_line, types=("CardType_Creature",)):
        return card(iid, name, OPP, power=power, toughness=toughness, subtypes=subtypes, text=text,
                    type_line=type_line, types=types, attacking=attacking)  # fmt: skip

    theirs = [
        card(
            595,
            "Reyav, Master Smith",
            OPP,
            power=6,
            toughness=2,
            subtypes=("Dwarf", "Artificer"),
            type_line="Legendary Creature — Dwarf Artificer",
            sick=True,
            entered=13,
            text="Whenever a creature you control that's enchanted or equipped attacks, that creature gains "
            "double strike until end of turn.",
        ),  # fmt: skip
        card(
            591,
            "Windcrag Siege",
            OPP,
            types=("CardType_Enchantment",),
            type_line="Enchantment",
            text=WINDCRAG,
        ),
        attacker(
            559,
            "Bofur, Reliable Guardian",
            5,
            1,
            ("Dwarf", "Scout"),
            "Lifelink",
            "Legendary Creature — Dwarf Scout",
        ),  # fmt: skip
        attacker(548, "Aurelia, the Warleader", 3, 4, ("Angel",), AURELIA, "Legendary Creature — Angel"),
        attacker(
            540,
            "Bombur, Gentle Dreamer",
            9,
            3,
            ("Dwarf", "Bard"),
            "Storied\nBombur doesn't untap during your untap step unless you have an enduring story.",
            "Legendary Creature — Dwarf Bard",
        ),  # fmt: skip
        attacker(
            503,
            "Glóin, Dwarf Emissary",
            7,
            3,
            ("Dwarf", "Advisor"),
            "Whenever you cast a historic spell, create a Treasure token. This ability triggers only once "
            "each turn.\n{oT}, Sacrifice a Treasure: Goad target creature.",
            "Legendary Creature — Dwarf Advisor",
        ),  # fmt: skip
        attacker(
            493,
            "Roaming Throne",
            8,
            4,
            ("Golem", "Dwarf"),
            "Ward {o2}\nAs this creature enters, choose a creature type.\nThis creature is the chosen type "
            "in addition to its other types.\nIf a triggered ability of another creature you control of the "
            "chosen type triggers, it triggers an additional time.",
            "Artifact Creature — Golem",
            types=("CardType_Artifact", "CardType_Creature"),
        ),  # fmt: skip
        attacker(
            471,
            "Thorin, King of Durin's Folk",
            4,
            4,
            ("Dwarf", "Noble"),
            "Whenever Thorin or another Dwarf you control enters, create a Treasure token.\nOther Dwarves "
            "you control get +1/+0 for each artifact token you control.",
            "Legendary Creature — Dwarf Noble",
        ),  # fmt: skip
        *(
            land(iid, "Plains", OPP, tapped=True, text="({T}: Add {W}.)", subtypes=("Plains",))
            for iid in (538, 453, 445)
        ),
        land(440, "Wind-Scarred Crag", OPP, tapped=True, text="{oT}: Add {oR} or {oW}."),
        *(
            card(
                iid,
                "Treasure",
                OPP,
                types=("CardType_Artifact",),
                subtypes=("Treasure",),
                token=True,
                type_line="Token Artifact — Treasure",
                text=TREASURE,
            )  # fmt: skip
            for iid in (567, 600, 603, 604)
        ),
    ]
    hares = [
        card(
            iid,
            "Hare Apparent",
            ME,
            power=2,
            toughness=2,
            subtypes=("Rabbit", "Noble"),
            type_line="Creature — Rabbit Noble",
            text=HARE,
            sick=sick,
            entered=12 if sick else None,
        )  # fmt: skip
        for iid, sick in ((582, True), (575, True), (518, False), (512, False), (485, False))
    ]
    rabbit_ids = (
        our_rabbits
        if our_rabbits is not None
        else (489, 490, 517, 522, 524, 579, 580, 581, 586, 587, 588, 589)
    )
    rabbits = [
        card(
            iid,
            "Rabbit",
            ME,
            power=1,
            toughness=1,
            subtypes=("Rabbit",),
            token=True,
            type_line="Token Creature — Rabbit",
            sick=iid >= 579,
            entered=12 if iid >= 579 else None,
        )  # fmt: skip
        for iid in rabbit_ids
    ]
    ours = [
        *hares,
        *rabbits,
        land(574, "Plains", ME, text="({T}: Add {W}.)", subtypes=("Plains",)),
        *(
            land(iid, "Plains", ME, tapped=True, text="({T}: Add {W}.)", subtypes=("Plains",))
            for iid in (479, 459, 447, 443)
        ),
    ]
    raw = None
    if blocks:
        blockers = [c["instance_id"] for c in [*hares, *rabbits]]
        raw = [
            {"blockerInstanceId": iid, "attackerInstanceIds": [559, 471, 540, 503, 493], "maxAttackers": 1}
            for iid in blockers
        ]
    return state(
        [*theirs, *ours], match="f80ca0cf", local=ME, life=life, opp_life=12, turn=13, active=OPP,
        phase="Phase_Combat" if blocks else "Phase_Main1", step="Step_DeclareBlock" if blocks else "",
        their_hand=1, raw_blockers=raw,
    )  # fmt: skip


# --- match 3ac9d511 T11 -----------------------------------------------------------------------


def harbin_t11(*, fifth_soldier=False):
    """Their T11 Main 1 (gsid 295); ``fifth_soldier``: Rescue Retriever, cast that turn, able to attack."""
    soldier = ("Human", "Soldier")
    theirs = [
        card(
            444,
            "Harbin, Vanguard Aviator",
            OPP,
            power=6,
            toughness=5,
            subtypes=soldier,
            type_line="Legendary Creature — Human Soldier",
            text=HARBIN,
        ),  # fmt: skip
        card(
            473,
            "Shackle Slinger",
            OPP,
            power=3,
            toughness=2,
            subtypes=soldier,
            type_line="Creature — Human Soldier",
            text="Whenever you cast your second spell each turn, choose target creature an opponent controls. If "
            "it's tapped, put a stun counter on it. Otherwise, tap it.",
        ),  # fmt: skip
        card(
            455,
            "Siege Veteran",
            OPP,
            power=2,
            toughness=2,
            subtypes=soldier,
            type_line="Creature — Human Soldier",
            text="At the beginning of combat on your turn, put a +1/+1 counter on target creature you control.\n"
            "Whenever another nontoken Soldier you control dies, create a 1/1 colorless Soldier artifact "
            "creature token.",
        ),  # fmt: skip
        card(
            528,
            "Soldier",
            OPP,
            power=1,
            toughness=1,
            subtypes=("Soldier",),
            token=True,
            types=("CardType_Artifact", "CardType_Creature"),
            type_line="Token Artifact Creature — Soldier",
            text="Lifelink",
            entered=10,
        ),  # fmt: skip
        *(land(iid, "Plains", OPP, text="({T}: Add {W}.)", subtypes=("Plains",)) for iid in (472, 443)),
        land(454, "Island", OPP, text="({T}: Add {U}.)", subtypes=("Island",)),
        land(439, "Land", OPP, text="{oT}: Add {oW} or {oU}."),
    ]
    if fifth_soldier:
        theirs.append(
            card(
                531,
                "Rescue Retriever",
                OPP,
                power=3,
                toughness=3,
                subtypes=("Dog", "Soldier"),
                type_line="Creature — Dog Soldier",
                text="Flash\nWhen this creature enters, put a +1/+1 counter on each other Soldier you control.\n"
                "Prevent all damage that would be dealt to other attacking Soldiers you control.",
            )  # fmt: skip
        )
    hare = dict(subtypes=("Rabbit", "Noble"), type_line="Creature — Rabbit Noble", text=HARE)
    rabbit = dict(subtypes=("Rabbit",), token=True, type_line="Token Creature — Rabbit")
    ours = [
        *(
            card(iid, "Hare Apparent", ME, power=2, toughness=2, sick=True, entered=10, **hare)
            for iid in (516, 509)
        ),
        *(
            card(iid, "Hare Apparent", ME, power=2, toughness=2, tapped=True, **hare)
            for iid in (493, 488, 462)
        ),
        *(
            card(iid, "Rabbit", ME, power=1, toughness=1, tapped=True, **rabbit)
            for iid in (466, 492, 497, 498)
        ),
        *(
            card(iid, "Rabbit", ME, power=1, toughness=1, sick=True, entered=10, **rabbit)
            for iid in (513, 514, 515, 520, 521, 522, 523)
        ),
        land(508, "Plains", ME, text="({T}: Add {W}.)", subtypes=("Plains",)),
        *(
            land(iid, "Plains", ME, tapped=True, text="({T}: Add {W}.)", subtypes=("Plains",))
            for iid in (487, 461, 448, 441)
        ),
    ]
    return state(
        [*theirs, *ours], match="3ac9d511", local=ME, life=8, opp_life=12, turn=11, active=OPP,
        phase="Phase_Main1", step="", their_hand=5,
    )  # fmt: skip
