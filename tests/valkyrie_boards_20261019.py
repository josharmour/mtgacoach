"""Brawl boards from match c0af235f (2026-10-19 15:17-15:23, ledger #161, #177-#180).

We play Leonardo, Big Brother (commander) with Hare Apparents and Oketra's
Monument; the opponent (W/U fliers and lifegain) has Righteous Valkyrie,
Faramir, Prince of Ithilien, Warden of Evos Isle, Aven Gagglemaster and
Faramir's Human Soldier tokens. Starting life is 25 (Player.log
``startingLifeTotal``). The boards are the game's own, read from Player.log
(instance ids, power/toughness, tapped and summoning-sick flags, turn entered):

- T10 main 1 (gsid 237, before the Leonardo cast at 15:20:19): we are at 25,
  they are at 27 (2 under Valkyrie's +2/+2 threshold, so it is off). Leonardo
  is in the command zone (no earlier cast); Arena offers his hard cast for
  {1}{W} (Monument's discount). Ours able: Hares 553, 573, Rabbits 557, 578,
  579, Warrior 576; three Hares and twelve tokens are summoning sick. Theirs
  untapped: Valkyrie 2/4 flier, Faramir 3/3, Warden 2/2 flier (sick). Two
  Plains open, empty hand. Master hard-cast him (no Sneak was lethal); a Sneak
  after their blocks put a ~21/3 attacker in unblocked (27 -> ~5).
- T12 declare attackers (gsid 306, 15:21:36): we are at 25, they are at 32
  (= 25 + 7: Valkyrie's +2/+2 is on). Twenty able attackers: Leonardo 27/3,
  five Hares, five Warriors (vigilance), nine Rabbits; Hare 644 and six tokens
  entered this turn. Theirs, all untapped: Gagglemaster 6/5 flier (entered
  T11), Warden 4/4 flier, Faramir 5/5, Valkyrie 4/6 flier, three Human
  Soldiers 3/3. Their Plains 634 and Tranquil Cove open; three cards in hand.
  The declaration was empty ("Stabilize behind the token wall") although the
  board facts said holding is dead on T15 and attacking the only survivor;
  their T13 fliers took 14 (25 -> 11).

Card text is the MTGA card database's (Arena writes "Sneak {oW}").
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from tests import leonardo_boards_20261019 as lb

LOCAL, OPPONENT = 1, 2
MATCH = "c0af235f-e8a4-4d8f-9b9b-b279fd9f4a54"

CARDS: dict[str, dict[str, Any]] = {
    "Plains": deepcopy(lb.CARDS["Plains"]),
    "Leonardo, Big Brother": deepcopy(lb.CARDS["Leonardo, Big Brother"]),
    "Hare Apparent": deepcopy(lb.CARDS["Hare Apparent"]),
    "Rabbit": deepcopy(lb.CARDS["Rabbit"]),
    "Warrior": {
        "grp_id": 74176, "type_line": "Token Creature — Warrior", "power": 1, "toughness": 1,
        "keywords": ["vigilance"], "card_types": ["CardType_Creature"], "object_kind": "TOKEN",
        "oracle_text": "Vigilance",
    },
    "Oketra's Monument": {
        "grp_id": 74099, "type_line": "Legendary Artifact", "mana_cost": "{3}", "card_types": ["CardType_Artifact"],
        "oracle_text": "White creature spells you cast cost {o1} less to cast.\nWhenever you cast a creature "
        "spell, create a 1/1 white Warrior creature token with vigilance.",
    },
    "Righteous Valkyrie": {
        "grp_id": 93535, "type_line": "Creature — Angel Cleric", "mana_cost": "{2}{W}", "power": 2,
        "toughness": 4, "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "subtypes": ["Angel", "Cleric"],
        "oracle_text": "Flying\nWhenever another Angel or Cleric you control enters, you gain life equal to that "
        "creature's toughness.\nAs long as you have at least 7 life more than your starting life total, "
        "creatures you control get +2/+2.",
    },
    "Faramir, Prince of Ithilien": {
        "grp_id": 84894, "type_line": "Legendary Creature — Human Noble", "mana_cost": "{2}{W}{U}", "power": 3,
        "toughness": 3, "card_types": ["CardType_Creature"], "subtypes": ["Human", "Noble"],
        "oracle_text": "At the beginning of your end step, choose an opponent. At the beginning of that "
        "player's next end step, you draw a card if they didn't attack you that turn. Otherwise, create three "
        "1/1 white Human Soldier creature tokens.",
    },
    "Warden of Evos Isle": {
        "grp_id": 75479, "type_line": "Creature — Bird Wizard", "mana_cost": "{2}{U}", "power": 2,
        "toughness": 2, "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying\nCreature spells with flying you cast cost {o1} less to cast.",
    },
    "Aven Gagglemaster": {
        "grp_id": 71787, "type_line": "Creature — Bird Warrior", "mana_cost": "{3}{W}{W}", "power": 4,
        "toughness": 3, "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying\nWhen this creature enters, you gain 2 life for each creature you control with "
        "flying.",
    },
    "Human Soldier": {
        "grp_id": 84954, "type_line": "Token Creature — Human Soldier", "power": 1, "toughness": 1,
        "card_types": ["CardType_Creature"], "object_kind": "TOKEN", "oracle_text": "",
    },
    "Ossification": {
        "grp_id": 83726, "type_line": "Enchantment — Aura", "mana_cost": "{1}{W}",
        "card_types": ["CardType_Enchantment"],
        "oracle_text": "Enchant basic land you control\nWhen this Aura enters, exile target creature or "
        "planeswalker an opponent controls until this Aura leaves the battlefield.",
    },
    "Island": {
        "grp_id": 82763, "type_line": "Basic Land — Island", "oracle_text": "({T}: Add {U}.)",
        "card_types": ["CardType_Land"],
    },
    "Tranquil Cove": {
        "grp_id": 79720, "type_line": "Land", "card_types": ["CardType_Land"],
        "oracle_text": "This land enters tapped.\nWhen this land enters, you gain 1 life.\n{oT}: Add {oW} or {oU}.",
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


# (instance id, turn entered) of our creatures by T12, from Player.log.
HARES = {553: 6, 573: 8, 597: 10, 604: 10, 612: 10}
WARRIORS = {576: 8, 600: 10, 607: 10, 615: 10, 625: 10}
RABBITS = {
    557: 6,
    578: 8,
    579: 8,
    602: 10,
    603: 10,
    609: 10,
    610: 10,
    611: 10,
    617: 10,
    618: 10,
    619: 10,
    620: 10,
}
# Their lands: (id, name, tapped at our T10 / T12, turn entered).
THEIR_LANDS = [
    (442, "Plains", 1), (537, "Tranquil Cove", 3), (546, "Island", 5), (559, "Plains", 7), (583, "Plains", 9),
]  # fmt: skip
OUR_LANDS = [(535, 2), (540, 4), (552, 6), (572, 8), (596, 10)]


def _state(
    *, turn, life, their_life, battlefield, hand=(), command=(), phase, step, lands_played, hand_count
):
    return {
        "match_id": MATCH,
        "local_seat_id": LOCAL,
        "opponent_seat_id": OPPONENT,
        "turn": {"turn_number": turn, "active_player": LOCAL, "priority_player": LOCAL, "phase": phase,
                 "step": step},
        "players": [
            {"seat_id": LOCAL, "life_total": life, "is_local": True, "lands_played": lands_played},
            {"seat_id": OPPONENT, "life_total": their_life, "is_local": False, "lands_played": 0},
        ],
        "battlefield": battlefield,
        "hand": list(hand),
        "command": list(command),
        "commander_grp_ids": [100471],
        "commander_casts": {"100471": 0},
        "graveyard": [],
        "exile": [card(592, "Hare Apparent", LOCAL)],
        "stack": [],
        "zones": {"library_count": 70, "library_count_source": "log_zone_membership",
                  "opponent_hand_count": hand_count},
        "deck_cards": [],
    }  # fmt: skip


def _ours(*, upto: int, tapped_lands: set[int], gone=(), extra=()) -> list[dict[str, Any]]:
    rows = []
    for table, name in ((HARES, "Hare Apparent"), (WARRIORS, "Warrior"), (RABBITS, "Rabbit")):
        for iid, entered in table.items():
            if iid <= upto and iid not in gone:
                rows.append(card(iid, name, LOCAL, is_tapped=False, turn_entered_battlefield=entered))
    rows += [
        card(iid, "Plains", LOCAL, is_tapped=iid in tapped_lands, turn_entered_battlefield=entered)
        for iid, entered in OUR_LANDS
    ]
    rows.append(card(568, "Oketra's Monument", LOCAL, is_tapped=False, turn_entered_battlefield=8))
    return rows + list(extra)


def t10_main1() -> dict[str, Any]:
    """Match c0af235f T10 main 1 (gsid 237): after the third Hare, Leonardo in the command zone."""
    theirs = [
        card(547, "Righteous Valkyrie", OPPONENT, is_tapped=False, turn_entered_battlefield=5),
        card(560, "Faramir, Prince of Ithilien", OPPONENT, is_tapped=False, turn_entered_battlefield=7),
        card(584, "Warden of Evos Isle", OPPONENT, is_tapped=False, turn_entered_battlefield=9),
        card(588, "Ossification", OPPONENT, is_tapped=False, turn_entered_battlefield=9, attached_to_id=583),
    ]
    theirs += [
        card(iid, name, OPPONENT, is_tapped=True, turn_entered_battlefield=entered)
        for iid, name, entered in THEIR_LANDS
    ]
    battlefield = _ours(upto=620, tapped_lands={535, 540, 552}) + theirs
    return _state(
        turn=10, life=25, their_life=27, battlefield=battlefield,
        command=[card(237, "Leonardo, Big Brother", LOCAL)], phase="Phase_Main1", step="",
        lands_played=1, hand_count=4,
    )  # fmt: skip


# Arena's T10 menu at gsid 237: the hard cast ({1}{W} with Monument), two Plains, pass.
T10_MENU_ACTIONS = [
    {"actionType": "ActionType_Cast", "grpId": 100471, "instanceId": 237, "facetId": 237, "abilityGrpId": 115,
     "sourceId": 237, "manaCost": [{"color": ["ManaColor_Generic"], "count": 1},
                                   {"color": ["ManaColor_White"], "count": 1}]},
    {"actionType": "ActionType_Activate_Mana", "grpId": 75556, "instanceId": 572, "abilityGrpId": 1001},
    {"actionType": "ActionType_Activate_Mana", "grpId": 75556, "instanceId": 596, "abilityGrpId": 1001},
    {"actionType": "ActionType_Pass"},
]  # fmt: skip


def t10_declare_blockers(
    *, attackers=(557, 576, 578, 579), blocks: dict[int, int] | None = None, sneak_mana: int = 2
) -> dict[str, Any]:
    """T10's declare-blockers step as the game had it (Player.log gsid 256: Rabbits 557, 578, 579
    and Warrior 576 attacked, the Hares were held back; Valkyrie blocked 557, Faramir 578, Warden
    579; the Warrior was unblocked), but with Leonardo still in the command zone, as the Sneak hold
    keeps him, and ``sneak_mana`` Plains open."""
    blocks = {557: 547, 578: 560, 579: 584} if blocks is None else blocks
    state = t10_main1()
    attackers = set(attackers)
    for c in state["battlefield"]:
        iid = c["instance_id"]
        if iid in attackers:
            c.update(is_attacking=True, is_blocked=iid in blocks, is_tapped=c["name"] != "Warrior")
        if iid in blocks.values():
            c["is_blocking"] = True
        if c["name"] == "Plains" and c["controller_seat_id"] == LOCAL:
            c["is_tapped"] = iid not in (572, 596)[:sneak_mana]
    state["turn"].update(phase="Phase_Combat", step="Step_DeclareBlock")
    return state


def t12_declare_attackers() -> dict[str, Any]:
    """Match c0af235f T12 at the DeclareAttackers request (gsid 306): twenty able attackers."""
    fresh = [card(644, "Hare Apparent", LOCAL, is_tapped=False, turn_entered_battlefield=12)]
    fresh += [card(647, "Warrior", LOCAL, is_tapped=False, turn_entered_battlefield=12)]
    fresh += [
        card(iid, "Rabbit", LOCAL, is_tapped=False, turn_entered_battlefield=12) for iid in range(649, 654)
    ]
    leo = card(621, "Leonardo, Big Brother", LOCAL, is_tapped=False, turn_entered_battlefield=10, power=27)
    # Rabbits 557, 578 and 579 died to the T10 blocks (Player.log gsid 256, SBA_Damage).
    ours = _ours(upto=625, tapped_lands={535}, gone=(557, 578, 579), extra=[leo, *fresh])
    theirs = [
        card(635, "Aven Gagglemaster", OPPONENT, is_tapped=False, turn_entered_battlefield=11, power=6,
             toughness=5),
        card(584, "Warden of Evos Isle", OPPONENT, is_tapped=False, turn_entered_battlefield=9, power=4,
             toughness=4),
        card(560, "Faramir, Prince of Ithilien", OPPONENT, is_tapped=False, turn_entered_battlefield=7, power=5,
             toughness=5),
        card(547, "Righteous Valkyrie", OPPONENT, is_tapped=False, turn_entered_battlefield=5, power=4,
             toughness=6),
        *[card(iid, "Human Soldier", OPPONENT, is_tapped=False, turn_entered_battlefield=10, power=3,
               toughness=3) for iid in (630, 631, 632)],
        card(588, "Ossification", OPPONENT, is_tapped=False, turn_entered_battlefield=9, attached_to_id=583),
    ]  # fmt: skip
    theirs += [
        card(iid, name, OPPONENT, is_tapped=iid != 537, turn_entered_battlefield=entered)
        for iid, name, entered in THEIR_LANDS
    ]
    theirs.append(card(634, "Plains", OPPONENT, is_tapped=False, turn_entered_battlefield=11))
    state = _state(
        turn=12, life=25, their_life=32, battlefield=ours + theirs, phase="Phase_Combat",
        step="Step_DeclareAttack", lands_played=0, hand_count=3,
    )  # fmt: skip
    state["commander_casts"] = {"100471": 1}
    state["decision_context"] = {
        "type": "declare_attackers",
        "legal_attacker_ids": list(T12_ATTACKERS),
        "raw_attackers": [
            {"attackerInstanceId": iid,
             "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": OPPONENT}]}
            for iid in T12_ATTACKERS
        ],
    }  # fmt: skip
    return state


# The T12 DeclareAttackersReq's attackers, in Arena's order.
T12_ATTACKERS = (
    553, 573, 576, 597, 600, 602, 603, 604, 607, 609, 610, 611, 612, 615, 617, 618, 619, 620, 621, 625,
)  # fmt: skip


def t12_menu() -> list[str]:
    """The T12 attack menu (Arena's order, gamestate's numbering of identical names)."""
    state = t12_declare_attackers()
    names = {c["instance_id"]: c for c in state["battlefield"]}
    seen: dict[str, int] = {}
    menu = []
    for iid in T12_ATTACKERS:
        c = names[iid]
        count = sum(1 for other in T12_ATTACKERS if names[other]["name"] == c["name"])
        seen[c["name"]] = seen.get(c["name"], 0) + 1
        label = c["name"] if count == 1 else f"{c['name']} #{seen[c['name']]}"
        menu.append(f"Attack with: {label} ({c.get('power')}/{c.get('toughness')})")
    return [*menu, "Done (confirm attackers)"]
