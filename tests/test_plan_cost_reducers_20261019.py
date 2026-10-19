"""The game plan's mana check counts our cost reducers (ledger #170).

Match 639c5fe1 (2026-10-19, Brawl, Leonardo), turn 6 at 14:51:14: with Pearl
Medallion ("White spells you cast cost {1} less to cast.") on our battlefield
the plan's next turn was Hare Apparent ({1}{W}, so {W}) and Leonardo, Big
Brother from the command zone ({2}{W}, so {1}{W}): 3 of our 4 mana. The model
said so ("Medallion makes Hare cost W and Leonardo {1}{W}, both fit the 4-mana
budget"); the validator trimmed by printed mana value: "T: dropped Leonardo, Big
Brother — not mana-legal (plan needs 5 mana, budget 4 W)".
"""

from __future__ import annotations

import json

import pytest
from tests.leonardo_boards_20261019 import LOCAL, OPPONENT, board, card

from arenamcp import board_assessment as ba
from arenamcp.board_assessment import assess
from arenamcp.game_plan import GamePlanManager, _reduction_for, _static_reducers, validate_plan

MEDALLION = {
    "instance_id": 442,
    "name": "Pearl Medallion",
    "type_line": "Artifact",
    "card_types": ["CardType_Artifact"],
    "mana_cost": "{2}",
    "oracle_text": "White spells you cast cost {1} less to cast.",
    "owner_seat_id": LOCAL,
    "controller_seat_id": LOCAL,
    "is_tapped": False,
    "turn_entered_battlefield": 4,
}
PLAN = {
    "role": "aggressor",
    "role_reason": "our clock is faster",
    "turns": [
        {"turn": "T", "cast": ["Hare Apparent", "Leonardo, Big Brother"], "attack": "none"},
    ],
    "win_conditions": ["Swing wide with width-buffed Leonardo"],
    "path": "T7 Hare + Leonardo, then attack every turn",
    "threat": "",
    "develop_next": "",
}


@pytest.fixture(autouse=True)
def _fresh_assessments():
    ba._CACHE.clear()


def t7_main(*, medallion=True):
    """Our T7 main phase: four untapped Plains, three Hares and three Rabbits out, Leonardo in command."""
    ours = [(iid, "Hare Apparent", False, 5) for iid in (459, 472, 495)]
    ours += [(iid, "Rabbit", False, 5) for iid in (462, 463, 475)]
    state = board(
        turn=7,
        life=25,
        their_life=27,
        ours=ours,
        theirs=[],
        hand=[(500, "Hare Apparent")],
        command=[(478, "Leonardo, Big Brother")],
        lands=4,
        lands_played=1,
    )
    duo = card(447, "Rabbit", OPPONENT, power=1, toughness=2, keywords=["flying"], object_kind="CARD")
    duo.update(name="Lifecreed Duo", type_line="Creature — Bird Bard", turn_entered_battlefield=3)
    duo["oracle_text"] = "Flying\nWhenever another creature you control enters, you gain 1 life."
    state["battlefield"].append(duo)
    if medallion:
        state["battlefield"].append(dict(MEDALLION))
    return state


def validated(state):
    plan = GamePlanManager._parse(json.dumps(PLAN), 7)
    assert plan is not None
    validate_plan(plan, assess(state), state)
    return plan


def test_medallion_makes_hare_and_leonardo_fit_four_mana():
    plan = validated(t7_main())
    assert plan.turn_plan[0]["cast"] == ["Hare Apparent", "Leonardo, Big Brother"]
    assert not [issue for issue in plan.issues if "not mana-legal" in issue]


def test_without_the_medallion_leonardo_is_still_trimmed():
    plan = validated(t7_main(medallion=False))
    assert plan.turn_plan[0]["cast"] == ["Hare Apparent"]
    assert "T: dropped Leonardo, Big Brother — not mana-legal (plan needs 5 mana, budget 4 W)" in plan.issues


def test_static_reducers_read_whole_sentences_and_qualifiers():
    hare = card(500, "Hare Apparent", LOCAL)
    leonardo = card(478, "Leonardo, Big Brother", LOCAL)
    pips_w = (frozenset("W"),)
    medallion = _static_reducers([MEDALLION])
    assert medallion == [([["white"]], 1)]
    assert _reduction_for(hare, pips_w, medallion) == 1 and _reduction_for(leonardo, pips_w, medallion) == 1
    # A red spell is not white.
    shock = {"name": "Shock", "type_line": "Instant", "mana_cost": "{R}"}
    assert _reduction_for(shock, (frozenset("R"),), medallion) == 0
    # "Instant and sorcery": either one; "noncreature" and no qualifier at all.
    electromancer = _static_reducers(
        [{"oracle_text": "Instant and sorcery spells you cast cost {1} less to cast."}]
    )
    assert _reduction_for(shock, (frozenset("R"),), electromancer) == 1
    assert _reduction_for(hare, pips_w, electromancer) == 0
    noncreature = _static_reducers([{"oracle_text": "Noncreature spells you cast cost {1} less to cast."}])
    assert _reduction_for(shock, (frozenset("R"),), noncreature) == 1
    assert _reduction_for(hare, pips_w, noncreature) == 0
    every = _static_reducers([{"oracle_text": "Flying\nSpells you cast cost {1} less to cast."}])
    assert _reduction_for(hare, pips_w, every) == 1
    # A condition or count after it, the opponents' spells and a spell's own reduction are not read.
    for text in (
        "Artifact spells you cast cost {1} less to cast for each artifact you control.",
        "Spells your opponents cast cost {1} more to cast.",
        "This spell costs {1} less to cast for each creature you control.",
        "The first creature spell you cast each turn costs {1} less to cast.",
    ):
        assert _static_reducers([{"oracle_text": text}]) == [], text
