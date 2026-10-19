"""Our cast triggers pump our attackers in the lines (ledger #94, match 18b1601a T9, 2026-10-19).

standalone.log 12:05:03-12:05:06: two Slickshot Show-Offs ("Whenever you cast a
noncreature spell, this creature gets +2/+0 until end of turn") and a prowess
Emberheart Challenger, one Mountain open, Fleeting Effigy and Tectonic Hazard
in hand; the opponent at 7 with every land tapped and no flier or reach.
Hazard's 1 plus two 3/2 fliers was exact, uninteractable lethal. The search
never applied the pumps (``_apply_casts``), valued the Hazard line at 3
damage, tagged "Cast Fleeting Effigy [LINE best: lethal T11]" and the
autopilot played Effigy; the game was lost on T10. Now each cast of ours fires
our creatures' cast triggers for this turn's attack (creature spells cast
first), and the T9 board is lethal on T9.
"""

from __future__ import annotations

from copy import deepcopy

from tests.race_boards_20261019 import SLICKSHOTS_T9, SLICKSHOTS_T9_START, board, card
from tests.strategic_states import actions_decision, cast

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model
from arenamcp.cast_triggers import cast_pumps, spell_kinds
from arenamcp.line_guard import lines_summary
from arenamcp.line_search import WIN, action_key, search_lines


def _search(source: dict):
    return search_lines(build_board_model(deepcopy(source)), survival=True, lethal_now=False, hard_ms=1e9)


def test_cast_pumps_read_prowess_and_written_out_triggers():
    assert cast_pumps(card(1, "Slickshot Show-Off", 1)) == (("noncreature", 2, 0, "self"),)
    assert cast_pumps(card(2, "Emberheart Challenger", 1)) == (("noncreature", 1, 1, "self"),)
    assert cast_pumps(card(3, "Fleeting Effigy", 1)) == ()
    # Arena's own wording ("Prowess" on its line, CARDNAME) and a team pump.
    assert cast_pumps({"name": "Monk", "oracle_text": "Haste\nProwess"}) == (("noncreature", 1, 1, "self"),)
    assert cast_pumps(
        {
            "name": "Bird",
            "oracle_text": "Whenever you cast a noncreature spell, CARDNAME gets +2/+0 until end of turn.",
        }
    ) == (("noncreature", 2, 0, "self"),)
    lightscribe = {
        "name": "Leonin Lightscribe",
        "oracle_text": "Magecraft — Whenever you cast or copy an instant or sorcery spell, creatures you control "
        "get +1/+1 until end of turn.",
    }
    assert cast_pumps(lightscribe) == (("instant or sorcery", 1, 1, "team"),)
    # Not a pump of this turn's attack: counters, a condition on the spell, the second spell.
    for text in (
        "Whenever you cast a noncreature spell, put a +1/+1 counter on this creature.",
        "Whenever you cast your second spell each turn, this creature gets +2/+2 until end of turn.",
        "Whenever you cast a spell that targets this creature, it gets +2/+2 until end of turn.",
    ):
        assert cast_pumps({"name": "X", "oracle_text": text}) == (), text


def test_spell_kinds():
    assert spell_kinds(card(1, "Tectonic Hazard", 1)) >= {"noncreature", "sorcery", "instant or sorcery"}
    assert "instant" in spell_kinds(card(2, "Shock", 1))
    assert spell_kinds(card(3, "Fleeting Effigy", 1)) == frozenset({"any"})  # a creature spell


def test_t9_hazard_and_two_pumped_slickshots_are_lethal_now():
    result = _search(SLICKSHOTS_T9)
    best = result.best
    assert best.cls == WIN and best.win_turn == 9, best.summary()
    first = best.steps[0]
    assert first.casts == ("Tectonic Hazard",), best.summary()
    assert first.attack.count("Slickshot Show-Off") == 2
    assert first.opp_life_after <= 0
    assert result.posture == "lethal"
    # What the decision prompt and the board facts say (packet decision #23 had
    # "best Fleeting Effigy, attack with ... — lethal on T11").
    assert "best Tectonic Hazard, attack with" in lines_summary(result), lines_summary(result)
    facts = ba.assess(deepcopy(SLICKSHOTS_T9))
    assert any(flag.startswith("LETHAL LINE: Tectonic Hazard") for flag in facts.flags), facts.flags
    # The Effigy line it chose is not lethal now: on the menu, only the Hazard is the kill.
    decision = actions_decision(
        [
            ("idx:0", "Cast Fleeting Effigy", cast(436, 95628), True),
            ("idx:1", "Cast Tectonic Hazard", cast(446, 87323), True),
            ("pass", "Pass", None, None),
        ]
    )
    key = action_key(decision.options[0], SLICKSHOTS_T9)
    effigy = result.first_action[key]
    assert not (effigy.cls == WIN and effigy.win_turn == 9), effigy.summary()
    assert action_key(decision.options[1], SLICKSHOTS_T9) in best.first_actions


def test_the_creature_is_cast_before_the_spell_that_pumps_it():
    # 12:04:57, before Slickshot #2: Slickshot (2) + Hazard (1) is lethal only cast in that order.
    result = _search(SLICKSHOTS_T9_START)
    best = result.best
    assert best.cls == WIN and best.win_turn == 9, best.summary()
    assert best.steps[0].casts == ("Slickshot Show-Off", "Tectonic Hazard"), best.summary()
    assert best.summary().startswith("Slickshot Show-Off + Tectonic Hazard, attack with")


def _one_slickshot(hand: list[tuple], their_life: int) -> dict:
    return board(
        turn=5, life=20, their_life=their_life, lands_played=0, opponent_hand=3,
        ours=[(401, "Mountain", False, 1), (402, "Mountain", False, 1), (403, "Mountain", False, 1),
              (320, "Slickshot Show-Off", False, 3)],
        theirs=[(501, "Forest", True, 2)], hand=hand,
    )  # fmt: skip


def test_a_spell_that_only_draws_still_pumps():
    # Any noncreature spell cast counts, whatever it does: a 1/2 flier attacks as a 3/2.
    from tests.race_boards_20261019 import CARDS

    CARDS.setdefault(
        "Think Twice",
        {
            "type_line": "Instant", "mana_cost": "{1}{U}", "card_types": ["CardType_Instant"],
            "oracle_text": "Draw a card.",
        },
    )  # fmt: skip
    hand = [(601, "Think Twice")]
    source = _one_slickshot(hand, their_life=3)
    for land in source["battlefield"][:3]:
        land["oracle_text"] = "({T}: Add {U} or {R}.)"
    result = _search(source)
    assert result.best.cls == WIN and result.best.win_turn == 5, result.best.summary()
    assert result.best.steps[0].casts == ("Think Twice",)


def test_landcycling_is_not_a_cast():
    from tests.race_boards_20261019 import CARDS

    # A noncreature card (a cast of it would pump) that we can only cycle: cycling is an
    # activated ability, not a cast.
    CARDS.setdefault(
        "Test Landcycling Sorcery",
        {
            "type_line": "Sorcery", "mana_cost": "{5}{R}{R}", "card_types": ["CardType_Sorcery"],
            "oracle_text": "Draw two cards.\nBasic landcycling {1}{R} ({1}{R}, Discard this card: Search your "
            "library for a basic land card, reveal it, put it into your hand, then shuffle.)",
        },
    )  # fmt: skip
    result = _search(_one_slickshot([(602, "Test Landcycling Sorcery")], their_life=3))
    # Only the 1/2 flier's own power: 3 -> 2, never lethal this turn.
    assert not (result.best.cls == WIN and result.best.win_turn == 5), result.best.summary()
