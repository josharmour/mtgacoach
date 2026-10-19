"""Brawl boards from match be1a945f (2026-10-19 14:13-14:18, ledger #156-#159, #161).

We play Leonardo, Big Brother (commander) with many Hare Apparents; the
opponent (Temur Dragons) has Ureni of the Unwritten, Lathliss, Dragon Queen,
Ran and Shaw, a 5/5 Dragon token and Dragonlord's Servant. The boards are the
game's own, replayed from Player.log through ``gamestate`` (instance ids,
power/toughness, tapped and attacking flags, turn entered):

- T10 declare attackers (gsid 243, Player.log L354149): we are at 25, they are
  at 12. Able: Leonardo 16/3 (1 + 15 other creatures), Hare Apparents 744,
  757, 763, Rabbits 761, 767, 768; summoning sick: Hares 791, 798 and seven
  Rabbits. Their Lathliss 6/6 flier, Ureni 8/8 flying trample hexproof and
  Servant 1/3 are untapped. Hand: Hare Apparent 756. The search held for a
  "lethal on T14" line over attacking them to 5.
- T12 main 1 (gsid 297, L355086): 17 vs 17. Leonardo is in the command zone
  (Ugin exiled him on T11; one earlier cast: tax {2}). Ours: five Hare
  Apparents and ten Rabbits, all able; theirs untapped: Ran and Shaw 4/4,
  Lathliss 6/6 and the Dragon 5/5 (fliers) and the Servant 1/3; Ureni tapped.
  Hand: two Hare Apparents and a Plains; five untapped Plains. We cast both
  Hares (4 mana) and attacked for 12 (17 -> 5); Sneak Leonardo ({2}{W}) after
  blocks was 26 or more.
- T12 declare blockers (gsid 322, L355919): our fifteen attackers, Hares 744,
  757, 763, 791 blocked by Dragon 834, Ran and Shaw 825, Lathliss 782 and the
  Servant 540; the rest unblocked.

Card text is the MTGA card database's (Arena writes "Sneak {oW}").
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

LOCAL, OPPONENT = 1, 2

CARDS: dict[str, dict[str, Any]] = {
    "Plains": {
        "grp_id": 75556, "type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)",
        "card_types": ["CardType_Land"],
    },
    "Leonardo, Big Brother": {
        "grp_id": 100471, "type_line": "Legendary Creature — Mutant Ninja Turtle", "mana_cost": "{2}{W}",
        "power": 1, "toughness": 3, "card_types": ["CardType_Creature"],
        "oracle_text": "Sneak {oW}\nLeonardo gets +1/+0 for each other creature you control.",
    },
    "Hare Apparent": {
        "grp_id": 93728, "type_line": "Creature — Rabbit Noble", "mana_cost": "{1}{W}", "power": 2,
        "toughness": 2, "card_types": ["CardType_Creature"], "subtypes": ["Rabbit", "Noble"],
        "oracle_text": "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to "
        "the number of other creatures you control named Hare Apparent.\nA deck can have any number of cards "
        "named Hare Apparent.",
    },
    "Rabbit": {
        "grp_id": 94160, "type_line": "Token Creature — Rabbit", "power": 1, "toughness": 1,
        "card_types": ["CardType_Creature"], "object_kind": "TOKEN", "oracle_text": "",
    },
    "Ran and Shaw": {
        "type_line": "Legendary Creature — Dragon", "mana_cost": "{3}{R}{R}", "power": 4, "toughness": 4,
        "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying, firebending 2\n{3}{R}: Dragons you control get +2/+0 until end of turn.",
    },
    "Lathliss, Dragon Queen": {
        "type_line": "Legendary Creature — Dragon", "mana_cost": "{4}{R}{R}", "power": 6, "toughness": 6,
        "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying\nWhenever another nontoken Dragon you control enters, create a 5/5 red Dragon "
        "creature token with flying.\n{1}{R}: Dragons you control get +1/+0 until end of turn.",
    },
    "Ureni of the Unwritten": {
        "type_line": "Legendary Creature — Spirit Dragon", "mana_cost": "{4}{G}{U}{R}", "power": 8,
        "toughness": 8, "keywords": ["flying", "trample", "hexproof"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying, trample, hexproof\nWhenever Ureni enters or attacks, look at the top eight cards "
        "of your library. You may put a Dragon creature card from among them onto the battlefield. Put the "
        "rest on the bottom of your library in a random order.",
    },
    "Dragonlord's Servant": {
        "type_line": "Creature — Goblin Shaman", "mana_cost": "{1}{R}", "power": 1, "toughness": 3,
        "card_types": ["CardType_Creature"], "oracle_text": "Dragon spells you cast cost {1} less to cast.",
    },
    "Dragon": {
        "type_line": "Token Creature — Dragon", "power": 5, "toughness": 5, "keywords": ["flying"],
        "card_types": ["CardType_Creature"], "object_kind": "TOKEN", "oracle_text": "Flying",
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
    command: list[tuple] = (),
    lands: int = 0,
    tapped_lands: int = 0,
    lands_played: int = 0,
    phase: str = "Phase_Main1",
    step: str = "",
    commander_casts: int = 0,
) -> dict[str, Any]:
    """A planner snapshot: ``ours``/``theirs`` rows are (id, name, tapped, entered, extra fields)."""

    def rows(entries, controller):
        return [
            card(iid, name, controller, is_tapped=tapped, turn_entered_battlefield=entered, **(extra or {}))
            for iid, name, tapped, entered, *rest in entries
            for extra in [rest[0] if rest else None]
        ]

    plains = [
        card(9000 + i, "Plains", LOCAL, is_tapped=i < tapped_lands, turn_entered_battlefield=1)
        for i in range(lands)
    ]
    state = {
        "match_id": "be1a945f-7610-431d-b4a0-69a8227045c2",
        "local_seat_id": LOCAL,
        "opponent_seat_id": OPPONENT,
        "turn": {"turn_number": turn, "active_player": LOCAL, "priority_player": LOCAL, "phase": phase,
                 "step": step},
        "players": [
            {"seat_id": LOCAL, "life_total": life, "is_local": True, "lands_played": lands_played},
            {"seat_id": OPPONENT, "life_total": their_life, "is_local": False, "lands_played": 0},
        ],
        "battlefield": rows(ours, LOCAL) + plains + rows(theirs, OPPONENT),
        "hand": [card(iid, name, LOCAL) for iid, name in hand],
        "command": [card(iid, name, LOCAL) for iid, name in command],
        "commander_grp_ids": [100471],
        "commander_casts": {"100471": commander_casts},
        "graveyard": [],
        "stack": [],
        "zones": {"library_count": 60, "library_count_source": "log_zone_membership", "opponent_hand_count": 4},
        "deck_cards": [],
    }  # fmt: skip
    return state


HARES_T12 = (744, 757, 763, 791, 798)
RABBITS_T12 = (761, 767, 768, 795, 796, 797, 802, 803, 804, 805)


def t12_main1() -> dict[str, Any]:
    """Match be1a945f T12, main 1 before any cast (gsid 297)."""
    ours = [(iid, "Hare Apparent", False, 8 if iid < 790 else 10) for iid in HARES_T12]
    ours += [(iid, "Rabbit", False, 8 if iid < 790 else 10) for iid in RABBITS_T12]
    theirs = [
        (825, "Ran and Shaw", False, 11),
        (782, "Lathliss, Dragon Queen", False, 9),
        (772, "Ureni of the Unwritten", True, 9),
        (540, "Dragonlord's Servant", False, 3),
        (834, "Dragon", False, 11),
    ]
    return board(
        turn=12, life=17, their_life=17, ours=ours, theirs=theirs,
        hand=[(835, "Hare Apparent"), (790, "Plains"), (756, "Hare Apparent")],
        command=[(821, "Leonardo, Big Brother")], lands=5, commander_casts=1,
    )  # fmt: skip


def t12_declare_blockers(*, sneak_mana: int = 3) -> dict[str, Any]:
    """T12 after blocks (gsid 322): fifteen attackers, four of them blocked; ``sneak_mana``
    Plains untapped (the game had two left after both Hares)."""
    blocked = {744: 834, 757: 825, 763: 782, 791: 540}
    ours = [
        (
            iid,
            "Hare Apparent",
            True,
            8 if iid < 790 else 10,
            {"is_attacking": True, "is_blocked": iid in blocked},
        )
        for iid in HARES_T12
    ]
    ours += [
        (iid, "Rabbit", True, 8 if iid < 790 else 10, {"is_attacking": True, "is_blocked": False})
        for iid in RABBITS_T12
    ]
    theirs = [
        (825, "Ran and Shaw", False, 11, {"is_blocking": True}),
        (782, "Lathliss, Dragon Queen", False, 9, {"is_blocking": True}),
        (772, "Ureni of the Unwritten", True, 9),
        (540, "Dragonlord's Servant", False, 3, {"is_blocking": True}),
        (834, "Dragon", False, 11, {"is_blocking": True}),
    ]
    return board(
        turn=12, life=17, their_life=17, ours=ours, theirs=theirs, hand=[(756, "Hare Apparent")],
        command=[(821, "Leonardo, Big Brother")], lands=6, tapped_lands=6 - sneak_mana, lands_played=1,
        phase="Phase_Combat", step="Step_DeclareBlock", commander_casts=1,
    )  # fmt: skip


def t10_declare_attackers() -> dict[str, Any]:
    """Match be1a945f T10, our declare-attackers step (gsid 243): 25 vs 12."""
    ours = [(750, "Leonardo, Big Brother", False, 6, {"power": 16})]
    ours += [(iid, "Hare Apparent", False, 6 if iid == 744 else 8) for iid in (744, 757, 763)]
    ours += [(iid, "Hare Apparent", False, 10) for iid in (791, 798)]
    ours += [(iid, "Rabbit", False, 8) for iid in (761, 767, 768)]
    ours += [(iid, "Rabbit", False, 10) for iid in (795, 796, 797, 802, 803, 804, 805)]
    theirs = [
        (782, "Lathliss, Dragon Queen", False, 9),
        (772, "Ureni of the Unwritten", False, 9),
        (540, "Dragonlord's Servant", False, 3),
    ]
    return board(
        turn=10, life=25, their_life=12, ours=ours, theirs=theirs, hand=[(756, "Hare Apparent")],
        lands=5, tapped_lands=4, lands_played=1, phase="Phase_Combat", step="Step_DeclareAttack",
        commander_casts=1,
    )  # fmt: skip
