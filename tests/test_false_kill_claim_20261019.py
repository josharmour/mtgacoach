"""The false-kill guard keeps a sound pick and only corrects its words (ledger #86, #86b).

standalone.log, 2026-10-19 (Mono-Red Burn ladder):

* 12:08:20 (T5, #86): "Casting Tectonic Hazard pings opponent and kills
  Konstrari Improviser while triggering Slickshot's +2/+0 and Emberheart's
  prowess before attacking." Hazard's 1 damage cannot kill the 2/2, so the
  guard threw the pick out, and the fallback cast Hazard anyway ("the best
  searched line holds this turn, so the greedy schedule's cast stands in").
  The pump and prowess were real; only the kill was wrong.
* 12:21:42 (match 5d5d32f8 T13, #86b, bug_20261019_122251): "Neither spell kills
  the 7/7 Gearhulk, so take the line that also pings the opponent to 6" was read
  as a claim that Hazard kills the Gearhulk (hotfixed in b720163).
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from tests.test_burn_targeting_20261019 import (
    HAZARD,
    LOCAL,
    OPP,
    _cast_menu,
    _creature,
    _mountain,
    _planner,
    _spell,
    _state,
)

from arenamcp import action_planner as planner_module
from arenamcp import board_assessment
from arenamcp.action_planner import ActionPlanner, line_casts_option
from arenamcp.play_safety import claimed_kills, strip_kill_claims

SAID_1208 = (
    "Casting Tectonic Hazard pings opponent and kills Konstrari Improviser while triggering "
    "Slickshot's +2/+0 and Emberheart's prowess before attacking."
)
IROH = (
    "Choose one — cardname deals 1 damage to each creature your opponents control; "
    "or cardname deals 4 damage to target creature."
)


def _improviser_window():
    """12:08:20: our main 1, Slickshot out, Hazard in hand, their 2/2 Konstrari Improviser."""
    improviser = _creature(230, "Konstrari Improviser", OPP, 2, 2)
    slickshot = _creature(225, "Slickshot Show-Off", LOCAL, 1, 2, "Flying, haste")
    hazard = _spell(221, "Tectonic Hazard", "Sorcery", "{R}", HAZARD)
    lands = [_mountain(299), _mountain(279, True), _mountain(286, True)]
    state = _state([improviser, slickshot, *lands], hand=[hazard], opp_life=17, life=20)
    return state, _cast_menu((221, "Tectonic Hazard"))


# --- #86: a false claim on a cast the line makes anyway -------------------------------------


def test_a_false_kill_claim_on_the_lines_own_cast_keeps_the_pick(caplog):
    state, menu = _improviser_window()
    planner = _planner(json.dumps({"option_ids": ["idx:0"], "reasoning": SAID_1208}))
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(menu, state) == ["idx:0"]

    trace = planner.get_last_decision_trace()
    assert trace["false_kill_claim"] == ["Konstrari Improviser"]
    assert trace["false_kill_kept"] == "the searched line casts Tectonic Hazard this turn"
    assert "cannot kill it; keeping idx:0" in caplog.text
    assert "fallback" not in trace
    # The narration keeps the real value and says what the math found.
    assert planner.get_decision_reasoning(["idx:0"]) == (
        "Casting Tectonic Hazard pings opponent while triggering Slickshot's +2/+0 and Emberheart's "
        "prowess before attacking. Board math: 1 damage from Tectonic Hazard does not kill Konstrari "
        "Improviser (2/2); the searched line casts Tectonic Hazard this turn."
    )


def test_a_false_kill_claim_the_line_does_not_back_is_still_thrown_out(monkeypatch, caplog):
    state, menu = _improviser_window()
    monkeypatch.setattr(planner_module, "line_casts_option", lambda *args, **kwargs: "")
    planner = _planner(json.dumps({"option_ids": ["idx:0"], "reasoning": SAID_1208}))
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        planner.plan_decision_options(menu, state)

    trace = planner.get_last_decision_trace()
    assert trace["false_kill_claim"] == ["Konstrari Improviser"]
    assert "false_kill_kept" not in trace
    assert "cannot kill it; not playing idx:0" in caplog.text
    assert trace.get("llm_failure") == "bad_answer"


# --- line_casts_option: the best line's casts, else the greedy schedule's ------------------------


def _assessed(monkeypatch, *, line=(), greedy=()):
    first = SimpleNamespace(casts=tuple(line), land="")
    baseline = SimpleNamespace(steps=[SimpleNamespace(casts=tuple(greedy))])
    assessment = SimpleNamespace(
        our_turn=True, lookahead=[first], line_search=SimpleNamespace(baseline=baseline, best=None)
    )
    monkeypatch.setattr(board_assessment, "assess", lambda state: assessment)
    monkeypatch.setattr(ActionPlanner, "_searched_line_target", staticmethod(lambda state, name: ("", 0)))


def test_the_greedy_schedule_stands_in_when_the_line_holds(monkeypatch):
    """12:08:20's fallback: 'the best searched line holds this turn, so the greedy schedule's cast stands in'."""
    state, menu = _improviser_window()
    _assessed(monkeypatch, greedy=["Tectonic Hazard"])
    assert line_casts_option(menu, state, "idx:0") == (
        "the searched line holds, and the greedy schedule casts Tectonic Hazard this turn"
    )
    _assessed(monkeypatch, line=["Slickshot Show-Off"], greedy=["Tectonic Hazard"])
    assert line_casts_option(menu, state, "idx:0") == ""  # the line casts something else
    _assessed(monkeypatch, line=["Tectonic Hazard"])
    assert line_casts_option(menu, state, "idx:0") == "the searched line casts Tectonic Hazard this turn"
    assert line_casts_option(menu, state, "pass") == ""


def test_only_on_our_main_phase_with_an_empty_stack(monkeypatch):
    state, menu = _improviser_window()
    _assessed(monkeypatch, line=["Tectonic Hazard"])
    combat = {**state, "turn": {**state["turn"], "phase": "Phase_Combat"}}
    assert line_casts_option(menu, combat, "idx:0") == ""
    theirs = {**state, "turn": {**state["turn"], "active_player": OPP}}
    assert line_casts_option(menu, theirs, "idx:0") == ""


def test_an_aimed_line_cast_needs_the_reasoning_to_name_its_target(monkeypatch):
    state, menu = _improviser_window()
    _assessed(monkeypatch, line=["Tectonic Hazard"])
    monkeypatch.setattr(
        ActionPlanner, "_searched_line_target", staticmethod(lambda state, name: ("opponent", 0))
    )
    assert line_casts_option(menu, state, "idx:0", "Hazard kills the Improviser.") == ""
    assert line_casts_option(menu, state, "idx:0", "Hazard pings the opponent's face.") != ""


# --- #86b: a negated sentence is no kill claim ------------------------------------------------


def test_the_reported_negated_sentence_keeps_the_pick():
    """12:21:42: Hazard was the right play and the reasoning said so; no claim, no fallback."""
    gearhulk = _creature(350, "Pyrewood Gearhulk", OPP, 7, 7, "Vigilance\nMenace")
    cindermaw = _creature(344, "Giant Cindermaw", LOCAL, 4, 3, "Trample\nPlayers can't gain life.")
    iroh = _spell(324, "Iroh's Demonstration", "Sorcery — Lesson", "{1}{R}", IROH)
    hazard = _spell(358, "Tectonic Hazard", "Sorcery", "{R}", HAZARD)
    lands = [_mountain(279), _mountain(286), _mountain(294), _mountain(310)]
    state = _state([gearhulk, cindermaw, *lands], hand=[iroh, hazard], opp_life=7, life=20)
    said = (
        "Neither spell kills the 7/7 Gearhulk, so take the line that also pings the opponent to 6 "
        "and keeps Cindermaw alive as the sole clock."
    )
    planner = _planner(json.dumps({"option_ids": ["idx:1"], "reasoning": said}))
    menu = _cast_menu((324, "Iroh's Demonstration"), (358, "Tectonic Hazard"))

    assert planner.plan_decision_options(menu, state) == ["idx:1"]
    assert "false_kill_claim" not in planner.get_last_decision_trace()
    assert planner.get_decision_reasoning(["idx:1"]) == said


@pytest.mark.parametrize(
    "said",
    [
        "The Gearhulk won't die to Hazard, but the ping matters.",
        "Pyrewood Gearhulk does not die to 1 damage.",
        "Ping them without killing the Gearhulk.",
        "Go face instead of killing the Gearhulk.",
        "Hazard rather than killing the Gearhulk with Iroh's.",
        "Hazard fails to kill the Gearhulk.",
        "Hazard doesn't kill the 7/7 Gearhulk.",
    ],
)
def test_more_negated_kill_sentences(said):
    gearhulk = {"name": "Pyrewood Gearhulk", "power": 7, "toughness": 7}
    assert claimed_kills(said, [gearhulk]) == [], said


def test_kill_claims_still_read():
    gearhulk = {"name": "Pyrewood Gearhulk", "power": 7, "toughness": 7}
    for said in (
        "Hazard kills the Gearhulk instead of going face.",
        "Instead of going face, Iroh's kills the Gearhulk.",
        "The Gearhulk dies to Iroh's plus combat.",
    ):
        assert claimed_kills(said, [gearhulk]) == [gearhulk], said


# --- the corrected narration ----------------------------------------------------------------


def test_strip_kill_claims_keeps_the_rest_of_the_reasoning():
    improviser = {"name": "Konstrari Improviser"}
    marshal = {"name": "Dawnwing Marshal"}
    assert strip_kill_claims(SAID_1208, [improviser]) == (
        "Casting Tectonic Hazard pings opponent while triggering Slickshot's +2/+0 and Emberheart's "
        "prowess before attacking."
    )
    assert (
        strip_kill_claims(
            "Tectonic Hazard for {R} kills their only creature, the flying Dawnwing Marshal, and pings "
            "them for 1.",
            [marshal],
        )
        == "Tectonic Hazard for {R} and pings them for 1."
    )
    assert strip_kill_claims("Hazard pings them. The Marshal dies to it. Then attack.", [marshal]) == (
        "Hazard pings them. Then attack."
    )
    assert strip_kill_claims("Hazard kills the Dawnwing Marshal.", [marshal]) == ""
    # A negated mention is no claim and stays.
    assert strip_kill_claims("Hazard doesn't kill the Marshal, but it pings them.", [marshal]) == (
        "Hazard doesn't kill the Marshal, but it pings them."
    )
