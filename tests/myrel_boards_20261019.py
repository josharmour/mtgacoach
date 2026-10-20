"""The 51-creature declare-blockers board of 2026-10-19 15:52:14 (ledger #192), from Player.log.

Match 41b07b15 (Brawl) T22, their turn: Myrel, Shield of Argive 3/4 attacks alone after its
trigger made more Soldiers; we have 17 untapped creatures that may block it (the
DeclareBlockersReq lists each with attackerInstanceIds [668]): four Hare Apparents, a Human
token and twelve Rabbit tokens. They have 34 creatures. Life 25 (us) vs 5. Replayed from
Player-prev.log line 522861 (gsid 574) through GameState and the card database.

The coaching loop stalled 19 s in ``safe_default_blocks`` -> ``optimal_blocks``: 2^17 block
assignments, each one fight whose damage order searched subsets of up to 17 blockers.
"""

from __future__ import annotations

from typing import Any

US, THEM = 2, 1
MYREL = 668
HARE_TEXT = (
    "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to the number of "
    "other creatures you control named Hare Apparent.\nA deck can have any number of cards named Hare Apparent."
)
MYREL_TEXT = (
    "During your turn, your opponents can't cast spells or activate abilities of artifacts, creatures, or "
    "enchantments.\nWhenever Myrel attacks, create X 1/1 colorless Soldier artifact creature tokens, where X is "
    "the number of Soldiers you control."
)
HARES = (678, 660, 610, 526)
RABBITS = (504, 505, 530, 531, 573, 614, 615, 664, 665, 682, 683, 684)
HUMAN = 466
SOLDIERS = (561, 562, 563, 564, 565, *range(631, 649))
WARRIORS = (586, 657, 658, 694, 695)
# Their other creatures: (instance, name, power, toughness, tapped, type line, text, keywords).
THEIR_OTHERS = (
    (686, "Hallowed Priest", 1, 1, False, "Creature — Human Cleric",
     "Whenever you gain life, put a +1/+1 counter on this creature.", []),
    (589, "Descendant of Storms", 2, 1, False, "Creature — Human Soldier",
     "Whenever this creature attacks, you may pay {o1oW}. If you do, it endures 1.", []),
    (587, "Survivor of Korlis", 1, 1, False, "Creature — Human Soldier",
     "First strike\n{o1oW}, Exile this card from your graveyard: Scry 2.", ["first strike"]),
    (514, "Herald of Anafenza", 4, 5, True, "Creature — Human Soldier",
     "Outlast {o2oW}\nWhenever you activate this creature's outlast ability, create a 1/1 white Warrior "
     "creature token.", []),
    (509, "Mondrak, Glory Dominus", 4, 4, False, "Legendary Creature — Phyrexian Horror",
     "If one or more tokens would be created under your control, twice that many of those tokens are created "
     "instead.", []),
)  # fmt: skip


def card(
    instance_id: int, name: str, controller: int, power: int, toughness: int, **extra: Any
) -> dict[str, Any]:
    token = extra.pop("token", False)
    return {
        "instance_id": instance_id,
        "name": name,
        "controller_seat_id": controller,
        "owner_seat_id": controller,
        "power": power,
        "toughness": toughness,
        "is_tapped": False,
        "card_types": ["CardType_Creature"],
        "object_kind": "TOKEN" if token else "CARD",
        "is_token": token,
        "type_line": f"Token Creature — {name}" if token else extra.pop("type_line", "Creature"),
        "oracle_text": extra.pop("oracle_text", ""),
        **extra,
    }


def t22_declare_blockers() -> dict[str, Any]:
    """The planner-shape state of the declare-blockers window at 15:52:14."""
    ours = [card(i, "Hare Apparent", US, 2, 2, type_line="Creature — Rabbit Noble", oracle_text=HARE_TEXT,
                 turn_entered_battlefield=10 + n) for n, i in enumerate(HARES)]  # fmt: skip
    ours.append(
        card(HUMAN, "Human", US, 1, 1, token=True, turn_entered_battlefield=6, parent_instance_id=465)
    )
    # The snapshot's tokens differ in the ability that made them and the turn they entered.
    ours += [
        card(i, "Rabbit", US, 1, 1, token=True, parent_instance_id=i - 1, turn_entered_battlefield=8 + n % 5,
             token_creator="Hare Apparent", token_creator_id=HARES[n % 4])
        for n, i in enumerate(RABBITS)
    ]  # fmt: skip
    theirs = [
        card(MYREL, "Myrel, Shield of Argive", THEM, 3, 4, type_line="Legendary Creature — Human Soldier",
             oracle_text=MYREL_TEXT, is_attacking=True),
    ]  # fmt: skip
    for iid, name, power, toughness, tapped, type_line, text, keywords in THEIR_OTHERS:
        theirs.append(
            card(iid, name, THEM, power, toughness, type_line=type_line, oracle_text=text, keywords=keywords,
                 is_tapped=tapped)
        )  # fmt: skip
    theirs += [card(i, "Soldier", THEM, 1, 1, token=True) for i in SOLDIERS]
    theirs += [card(i, "Warrior", THEM, 1, 1, token=True) for i in WARRIORS]
    legal = [*HARES, HUMAN, *RABBITS]
    return {
        "turn": {
            "turn_number": 22,
            "active_player": THEM,
            "phase": "Phase_Combat",
            "step": "Step_DeclareBlock",
        },
        "local_seat_id": US,
        "players": [
            {"seat_id": THEM, "life_total": 5, "is_local": False, "mana_pool": {}},
            {"seat_id": US, "life_total": 25, "is_local": True, "mana_pool": {}},
        ],
        "battlefield": ours + theirs,
        "hand": [],
        "graveyard": [],
        "zones": {"opponent_hand_count": 2},
        "decision_context": {
            "type": "declare_blockers",
            "legal_blocker_ids": legal,
            "raw_blockers": [
                {"blockerInstanceId": i, "attackerInstanceIds": [MYREL], "maxAttackers": 1} for i in legal
            ],
            "attacker_ids": [MYREL],
        },
    }
