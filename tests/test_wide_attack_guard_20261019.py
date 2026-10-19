"""The losing-attack guard no longer thins a wide token attack one creature at a time (ledger #168).

Match 639c5fe1 (2026-10-19, Brawl, Leonardo, Big Brother), boards from the
match's Player.log at each DeclareAttackers request:

- T11 (gsid 281, 14:53:47): the opponent at 10, every land tapped, four
  untapped creatures (Lathiel, the Bounteous Dawn 2/2 lifelink, Valgavoth 3/5
  flying, Honor Troll 3/4, Lifecreed Duo 3/4 flying). The model swung with all
  ten (24 power, 6 through their best blocks). The guard charged Lathiel's 2
  lifelink to every Rabbit and re-judged the shrinking attack after each hold:
  it held 9 of 10, Leonardo attacked alone, traded and dealt 0.
- T9 (gsid 209): one Hare Apparent held (2 damage lost).
- T13 (gsid 366): 12 of 13 held; Leonardo alone into Lathiel, Honor Troll,
  Lifecreed Duo 7/8 and four Pests.
"""

from __future__ import annotations

import logging

import pytest

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_strategy import (
    _copies_answered,
    _TheirBlocks,
    _thinned_declaration,
    losing_attackers,
)

US, THEM = 1, 2
LEO = "Sneak {W}\nLeonardo, Big Brother gets +1/+0 for each other creature you control."
HARE = (
    "When Hare Apparent enters, create a number of 1/1 white Rabbit creature tokens equal to the number of "
    "other creatures you control named Hare Apparent.\nA deck can have any number of cards named Hare Apparent."
)
LATHIEL = (
    "Lifelink\nAt the beginning of each end step, if you gained life this turn, distribute up to that many "
    "+1/+1 counters among any number of other target creatures."
)
VALGAVOTH = "Flying\nWhenever you gain life, creature cards in your hand and graveyard perpetually get +1/+1."
TROLL = (
    "Vigilance\nIf you would gain life, you gain that much life plus 1 instead.\nHonor Troll gets +2/+1 as "
    "long as you have 25 or more life."
)
DUO = "Flying\nWhenever another creature you control enters, you gain 1 life."
PEST = "When this creature dies, you gain 1 life."
STRATEGY = "Full swing for 24; crackback is safe at 25 life. All-in swarm attack to close the race."


def land(iid, name, seat, tapped):
    return {
        "instance_id": iid,
        "name": name,
        "oracle_text": "",
        "type_line": f"Basic Land — {name}",
        "card_types": ["Land"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": tapped,
    }


def creature(iid, name, seat, power, toughness, text="", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "oracle_text": text,
        "type_line": "Creature",
        "card_types": ["Creature"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "power": power,
        "toughness": toughness,
        "is_tapped": False,
        **extra,
    }


def leonardo(iid, power=13):
    return creature(iid, "Leonardo, Big Brother", US, power, 3, LEO)


def hare(iid, **extra):
    return creature(iid, "Hare Apparent", US, 2, 2, HARE, **extra)


def rabbit(iid, **extra):
    return creature(
        iid, "Rabbit", US, 1, 1, type_line="Token Creature — Rabbit", object_kind="TOKEN", **extra
    )


def lathiel(iid=512, power=2):
    return creature(iid, "Lathiel, the Bounteous Dawn", THEM, power, power, LATHIEL, keywords=["Lifelink"])


MEDALLION = {
    "instance_id": 442,
    "name": "Pearl Medallion",
    "oracle_text": "White spells you cast cost {1} less to cast.",
    "type_line": "Artifact",
    "card_types": ["Artifact"],
    "owner_seat_id": US,
    "controller_seat_id": US,
    "is_tapped": False,
}


def board(turn, ours, theirs, opp_life, lands, *, home=()):
    for card in ours:
        card.setdefault("turn_entered_battlefield", turn - 2)
    for card in home:
        card.setdefault("turn_entered_battlefield", turn)  # entered this turn: summoning sick
    return {
        "match_id": "639c5fe1",
        "turn": {
            "turn_number": turn,
            "active_player": US,
            "priority_player": US,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "local_seat_id": US,
        "players": [
            {"seat_id": US, "life_total": 25, "is_local": True, "mana_pool": {}},
            {"seat_id": THEM, "life_total": opp_life, "is_local": False, "mana_pool": {}},
        ],
        "battlefield": [*lands, MEDALLION, *ours, *home, *theirs],
        "hand": [],
        "graveyard": [],
        "recent_events": [],
        "decision_context": {
            "type": "declare_attackers",
            "legal_attacker_ids": [card["instance_id"] for card in ours],
        },
    }


def t11():
    """gsid 281: the ten untapped attackers; Hare Apparent 521 and two Rabbits entered this turn."""
    ours = [leonardo(478), hare(495), hare(455), *(rabbit(i) for i in (463, 475, 476, 477, 498, 499, 500))]
    home = [hare(521), rabbit(524), rabbit(525)]
    theirs = [
        lathiel(),
        creature(486, "Valgavoth, the Lifeline", THEM, 3, 5, VALGAVOTH, keywords=["Flying"]),
        creature(466, "Honor Troll", THEM, 3, 4, TROLL, keywords=["Vigilance"]),
        creature(447, "Lifecreed Duo", THEM, 3, 4, DUO, keywords=["Flying"]),
    ]
    lands = [
        *(land(i, "Plains", US, False) for i in (501, 481, 451, 441)),
        land(437, "Plains", US, True),
        *(land(i, "Forest", THEM, True) for i in (511, 485, 465, 446)),
        land(439, "Plains", THEM, True),
    ]
    return board(11, ours, theirs, 10, lands, home=home), ours


def t9():
    """gsid 209: Hare Apparent 455 was held; 495 and three Rabbits entered this turn."""
    ours = [leonardo(478), hare(472), hare(459), hare(455), *(rabbit(i) for i in (462, 463, 475, 476, 477))]
    home = [hare(495), *(rabbit(i) for i in (498, 499, 500))]
    theirs = [
        creature(486, "Valgavoth, the Lifeline", THEM, 2, 4, VALGAVOTH, keywords=["Flying"]),
        creature(466, "Honor Troll", THEM, 2, 3, TROLL, keywords=["Vigilance"]),
        creature(447, "Lifecreed Duo", THEM, 1, 2, DUO, keywords=["Flying"]),
    ]
    lands = [
        *(land(i, "Plains", US, False) for i in (501, 481, 451, 441)),
        land(437, "Plains", US, True),
        *(land(i, "Forest", THEM, True) for i in (485, 465, 446)),
        land(439, "Plains", THEM, True),
    ]
    return board(9, ours, theirs, 23, lands, home=home), ours


def t13():
    """gsid 366: thirteen attackers into seven untapped blockers, four of them fresh Pests."""
    ours = [
        leonardo(530),
        *(hare(i) for i in (521, 495, 455)),
        *(rabbit(i) for i in (463, 475, 476, 477, 498, 499, 500, 524, 525)),
    ]
    theirs = [
        lathiel(power=3),
        creature(466, "Honor Troll", THEM, 3, 4, TROLL, keywords=["Vigilance"]),
        creature(447, "Lifecreed Duo", THEM, 7, 8, DUO, keywords=["Flying"]),
        *(
            creature(i, "Pest", THEM, 2, 2, PEST, type_line="Token Creature — Pest", object_kind="TOKEN")
            for i in (544, 545, 546, 547)
        ),
    ]
    lands = [
        *(land(i, "Plains", US, False) for i in (646, 501, 481, 451, 441, 437)),
        land(536, "Plains", THEM, True),
        *(land(i, "Forest", THEM, True) for i in (511, 485, 465, 446)),
        land(439, "Plains", THEM, True),
    ]
    return board(13, ours, theirs, 18, lands), ours


def ids(cards):
    return [card["instance_id"] for card in cards]


# --- the match's three declarations -----------------------------------------------------


@pytest.mark.parametrize("line_search", ["on", "off"])
def test_the_t11_all_in_stands(line_search, monkeypatch, caplog):
    if line_search == "off":
        monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    state, ours = t11()
    with caplog.at_level(logging.INFO, logger="arenamcp.combat_strategy"):
        assert losing_attackers(state, ids(ours), strategy_text=STRATEGY) == {}
    if line_search == "off":
        # Without the search, the per-creature holds are undone by the comparison with the declaration.
        assert "with them the attack gets 4 through their best blocks net of lifelink, holding them 0" in (
            caplog.text
        )


def test_their_actual_blocks_charge_lathiels_lifelink_once():
    state, ours = t11()
    blockers = [
        card for card in state["battlefield"] if card["controller_seat_id"] == THEM and "power" in card
    ]
    theirs = _TheirBlocks(blockers, 10)
    through, on = theirs.blocks(ours)
    assert through == 6
    assert sum(1 for group in on.values() for card in group if card["instance_id"] == 512) == 1
    assert theirs.net(ours) == 4  # 6 through, Lathiel gains 2 once
    for card in ours:
        marginal, taken, mine = theirs.marginal(card, ours)
        assert marginal == 1, card["instance_id"]  # every attacker adds a point through their best blocks
        assert taken == (2 if 512 in {blocker["instance_id"] for blocker in mine} else 0)


def test_no_more_rabbits_are_held_than_their_blockers_answer():
    state, ours = t11()
    blockers = [
        card for card in state["battlefield"] if card["controller_seat_id"] == THEM and "power" in card
    ]
    battlefield = state["battlefield"]
    assert _copies_answered(rabbit(463), blockers, battlefield=battlefield, strategy_text="") == 4
    # Lathiel 2/2 only trades with a Hare: three blockers eat one for free.
    assert _copies_answered(hare(495), blockers, battlefield=battlefield, strategy_text="") == 3


def test_the_planner_swings_with_all_ten():
    state, ours = t11()
    names = [card["name"] for card in ours]
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=names,
                attacker_instance_ids=ids(ours),
                attacker_targets=dict.fromkeys(names, "Opponent"),
            )
        ],
        overall_strategy=STRATEGY,
    )
    ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_instance_ids == ids(ours)
    assert plan.fallback_reason != "planner_losing_attack"


def test_the_t9_hare_attacks():
    state, ours = t9()
    assert losing_attackers(state, ids(ours), strategy_text=STRATEGY) == {}


def test_t13_is_not_thinned_to_leonardo_alone():
    state, ours = t13()
    assert losing_attackers(state, ids(ours), strategy_text=STRATEGY) == {}


# --- what the guard still holds in a wide attack ------------------------------------------


def wide_board(ours, theirs, opp_life=20):
    lands = [land(601, "Plains", US, True), land(602, "Forest", THEM, True)]
    return board(5, ours, theirs, opp_life, lands)


def test_attackers_every_one_of_which_is_eaten_for_nothing_are_all_held():
    ours = [rabbit(701), rabbit(702), rabbit(703), hare(704)]
    theirs = [creature(800 + i, "Ogre", THEM, 3, 3) for i in range(4)]
    losing = losing_attackers(wide_board(ours, theirs), ids(ours))
    assert set(losing) == {701, 702, 703, 704}
    assert all("kill it and survive; nothing gets through" in reason for reason in losing.values())
    assert any(
        "the rest of the attack gets nothing more through for it" in reason for reason in losing.values()
    )


def test_two_rabbits_into_a_lone_lifelinker_stay_home():
    # Lathiel eats one and gains 2; the other deals 1: the attack gains them a point and loses a Rabbit.
    ours = [rabbit(701), rabbit(702)]
    losing = losing_attackers(wide_board(ours, [lathiel()]), ids(ours))
    assert set(losing) == {701, 702}
    assert any("lifelink gains them 2 life" in reason for reason in losing.values())


def test_ten_rabbits_into_a_lone_lifelinker_all_attack():
    # She eats one and gains 2 whichever attack: holding Rabbits only takes damage off the attack.
    ours = [rabbit(701 + i) for i in range(10)]
    assert losing_attackers(wide_board(ours, [lathiel()]), ids(ours)) == {}


def test_a_count_that_holds_more_copies_than_their_answers_is_never_chosen():
    ours = [rabbit(701 + i) for i in range(6)]
    removed = [(card, "reason", False) for card in ours[:5]]
    # Lathiel answers one Rabbit at a time: 2..5 Rabbits held of 6 is never the result,
    # however much it nets; one held, or none, is.
    nets = [0, -1, 3, 4, 5, 6]
    keep = _thinned_declaration(
        ours, removed, nets, [lathiel()], line_attack=None, battlefield=[], strategy_text=""
    )
    assert keep == 0
    # Every copy held is allowed.
    removed = [(card, "reason", False) for card in ours]
    keep = _thinned_declaration(
        ours, removed, [*nets, 7], [lathiel()], line_attack=None, battlefield=[], strategy_text=""
    )
    assert keep == 6
