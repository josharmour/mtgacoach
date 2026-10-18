"""Cast after combat what changes nothing about the attack (pro YFRA game, 2026-10-18, match 3 turn 7).

The pro attacked with Konstrari Improviser first and cast Enlightened
Confidant (and Valgavoth) after combat. The line search applied every T cast
before combat and the planner's text said "cast ..., attack with ...";
coach_prompts covered only lands and 0-power creatures.
"""

from __future__ import annotations

from copy import deepcopy

from tests.boards_pro_yfra_2026_10_18 import M3_T7, card

from arenamcp.board_model import build_board_model
from arenamcp.line_search import _Search


def _turn(source, attack: bool):
    model = build_board_model(deepcopy(source))
    search = _Search(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None,
                     groups=8, width=8, k_next=4)  # fmt: skip
    node = next(iter(dict(search.roots()).values()))
    _base, sources = search.sources(node, None)
    casts = next(c for c in search.cast_sets(node, sources, instant_only=False) if c)
    mid = search.begin(node, None, casts)
    ids = tuple(b["instance_id"] for b in node.ours) if attack else ()
    return search.finish(mid, ("all" if attack else "none", ids)).history[-1]


def test_vanilla_creature_waits_for_main_two():
    step = _turn(M3_T7, attack=True)
    assert step.casts == ("Enlightened Confidant",)
    assert step.post_combat == ("Enlightened Confidant",)
    assert step.text() == "attack with Konstrari Improviser, then cast Enlightened Confidant after combat"


def test_no_attack_no_reordering():
    step = _turn(M3_T7, attack=False)
    assert step.text() == "Enlightened Confidant"


def test_a_hasty_or_team_pumping_creature_goes_first():
    source = deepcopy(M3_T7)
    source["hand"] = [
        {**card(195, "Enlightened Confidant", 2), "name": "Lord", "oracle_text": "Other creatures you control get +1/+1."}
    ]
    assert _turn(source, attack=True).post_combat == ()
