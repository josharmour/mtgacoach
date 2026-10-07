"""The shadow-first line and mode guards, option tags and fallback pick (``arenamcp.line_guard``).

Field report (2026-10-06 FRA game 1, standalone.log 15:41-15:45): at T12 the
autopilot cast a mana rock and landcycled Undulating Witness, the only creature
it could have cast (match packet decision 13), and at T14 it chose Archive
Arbiter's destroy mode over "gain 4 life" at 4 life (decision 18) and died to
the next attack. These tests run the guards on those recorded states
(tests/strategic_states.py) and pin decisions, not tuned numbers: the T12 rock
and landcycling picks are replaced by the Island that pays for Witness, then by
Witness itself; the T14 destroy mode is replaced by the lifegain mode but never
applied (Splinter Twin's copies are not modelled); lethal, winning lines, passes,
the opponent's turn, a non-empty stack, unknown bodies and truncated searches
are never touched; guards stay in shadow unless ARENAMCP_LINE_GUARD /
ARENAMCP_MODE_GUARD say 'on'.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from unittest import mock

import pytest
from tests.strategic_states import (
    BUG_135027,
    BUG_174855,
    BUG_180436,
    G1_T8,
    G1_T10,
    G1_T12,
    G1_T12_AFTER_VOLUME,
    G1_T12_MENU,
    G1_T14,
    G1_T14_MODE_MENU,
    G1_T14_MODE_REQUEST_ID,
    G1_T14_MODE_STATE,
    G1_T14_ON_STACK,
    G1_T15_FROM_OPPONENT,
    G3_T10_BLOCKS,
    actions_decision,
    after_land_drop,
    card,
    cast,
    modal_decision,
    play,
)

from arenamcp import board_assessment as ba
from arenamcp import line_guard as lg
from arenamcp.board_model import build_board_model
from arenamcp.line_search import ALIVE, DEAD, WIN, ModeComparison, compare_modes, search_lines

ROCK, LANDCYCLE, ISLAND = "idx:1", "idx:4", "idx:5"
FIXTURES = {
    "G1_T8": G1_T8,
    "G1_T10": G1_T10,
    "G1_T12": G1_T12,
    "G1_T12_AFTER_VOLUME": G1_T12_AFTER_VOLUME,
    "G1_T14": G1_T14,
    "G1_T15_FROM_OPPONENT": G1_T15_FROM_OPPONENT,
    "G3_T10_BLOCKS": G3_T10_BLOCKS,
    "BUG_135027": BUG_135027,
    "BUG_174855": BUG_174855,
    "BUG_180436": BUG_180436,
}


@pytest.fixture(autouse=True)
def _clean_guard_env(monkeypatch):
    monkeypatch.delenv("ARENAMCP_LINE_GUARD", raising=False)
    monkeypatch.delenv("ARENAMCP_MODE_GUARD", raising=False)
    lg._MODE_CACHE.clear()
    yield
    lg._MODE_CACHE.clear()


def _assessment(source: dict):
    """Today's greedy facts (survival_mode, lethal_now, our_turn) as the planner passes them."""
    ba._CACHE.clear()
    with mock.patch.dict(os.environ, {"ARENAMCP_LINE_SEARCH": "0"}):
        assessment = ba.assess(deepcopy(source))
    ba._CACHE.clear()
    return assessment


def _search(source: dict, **kwargs):
    assessment = _assessment(source)
    model = build_board_model(deepcopy(source))
    assert model is not None
    result = search_lines(
        model,
        survival=assessment.survival_mode and not assessment.lethal_now,
        lethal_now=assessment.lethal_now,
        **kwargs,
    )
    return result, assessment


def _guard(source: dict, decision, chosen_id: str, *, result=None, **overrides):
    found, assessment = _search(source)
    kwargs = {
        "survival_mode": assessment.survival_mode,
        "lethal_now": assessment.lethal_now,
        "our_turn": assessment.our_turn,
        "unknown_bodies": [],
        **overrides,
    }
    return lg.line_guard(found if result is None else result, decision, chosen_id, source, **kwargs)


def _with_life(source: dict, life: int) -> dict:
    result = deepcopy(source)
    for player in result["players"]:
        if player["is_local"]:
            player["life_total"] = life
    return result


def _after_island() -> tuple[dict, object]:
    """G1_T12 once the Island is down: Witness is castable now (the autopilot cycled it)."""
    source = after_land_drop(G1_T12, 284)
    menu = [
        ("idx:1", "Cast Murmuring Volume", cast(217, 106419), True),
        ("idx:2", "Cast Undulating Witness", cast(229, 106272), True),
        G1_T12_MENU[1],
        ("pass", "Pass", None, None),
    ]
    return source, actions_decision(menu)


def _mode_decision(**meta):
    menu = [(oid, label, {**option, **meta}) for oid, label, option in G1_T14_MODE_MENU]
    return modal_decision(menu, G1_T14_MODE_REQUEST_ID)


# --- modes --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "shadow"), ("off", "off"), ("shadow", "shadow"), (" ON ", "on"), ("Shadow", "shadow"),
     ("1", "shadow"), ("true", "shadow"), ("", "shadow")],
)  # fmt: skip
def test_guard_modes_default_to_shadow(monkeypatch, value, expected):
    for name in ("ARENAMCP_LINE_GUARD", "ARENAMCP_MODE_GUARD"):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert lg.guard_mode("line") == expected and lg.guard_mode("mode") == expected
    assert lg.guard_mode("unknown") == "shadow"


# --- the line guard on the recorded T12 -----------------------------------------------------


@pytest.mark.parametrize(
    ("chosen", "label"), [(ROCK, "Cast Murmuring Volume"), (LANDCYCLE, "Activate: Undulating Witness")]
)
def test_t12_rock_or_landcycling_gives_way_to_the_island_that_casts_witness(chosen, label):
    verdict = _guard(G1_T12, actions_decision(G1_T12_MENU), chosen)
    assert verdict is not None and verdict.option_id == ISLAND and verdict.replaced == chosen
    assert (verdict.kind, verdict.setting, verdict.applies) == ("line", "shadow", False)
    assert verdict.best_line.steps[0].land == "Island"
    assert verdict.best_line.steps[0].casts == ("Undulating Witness",)
    assert verdict.chosen_line.steps[0].life_after == 4 and verdict.best_line.steps[0].life_after == 7
    # Both lines survive: a value gap in survival mode; a +2/+0 trick kills only the rock line.
    assert verdict.best_line.cls == verdict.chosen_line.cls == ALIVE and verdict.robust
    assert verdict.reason.startswith("Line guard: Island + Undulating Witness")
    assert f"instead of {label} (" in verdict.reason and "+2/+0 trick" in verdict.reason
    assert verdict.summary.startswith(f"Island + Undulating Witness instead of {label}:")
    assert len(verdict.summary) <= 200 and "\n" not in verdict.summary
    assert verdict.contingent == []
    json.dumps(verdict.as_trace())


def test_once_the_island_is_down_witness_replaces_the_rock_and_the_cycling():
    source, decision = _after_island()
    for chosen in ("idx:1", LANDCYCLE):
        verdict = _guard(source, decision, chosen)
        assert verdict is not None and verdict.option_id == "idx:2", chosen
    assert _guard(source, decision, "idx:2") is None


def test_on_mode_applies_and_off_mode_is_silent(monkeypatch):
    decision = actions_decision(G1_T12_MENU)
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "on")
    verdict = _guard(G1_T12, decision, ROCK)
    assert verdict.applies and verdict.setting == "on" and verdict.as_trace()["applied"]
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "off")
    assert _guard(G1_T12, decision, ROCK) is None


def test_the_best_lines_own_plays_and_pass_are_never_replaced():
    decision = actions_decision(G1_T12_MENU)
    assert _guard(G1_T12, decision, ISLAND) is None
    assert _guard(G1_T12, decision, "pass") is None
    assert _guard(G1_T12, decision, "idx:99") is None


def _doctored_win(result):
    key = ("cast", 217, None)
    win = dataclasses.replace(result.first_action[key], cls=WIN, timing=-1, win_at=1, win_turn=12)
    return dataclasses.replace(result, first_action={**result.first_action, key: win})


@pytest.mark.parametrize(
    "case",
    ["lethal_now", "their_turn", "unknown_bodies", "unknown_body_on_board", "stack", "request_type",
     "truncated", "no_result", "chosen_wins", "dead_now"],
)  # fmt: skip
def test_the_guard_never_fires(case):
    decision, source, result, overrides = actions_decision(G1_T12_MENU), deepcopy(G1_T12), None, {}
    assert _guard(G1_T12, decision, ROCK) is not None  # the unchanged board does fire
    if case == "lethal_now":
        overrides["lethal_now"] = True
    elif case == "their_turn":
        overrides["our_turn"] = False
    elif case == "unknown_bodies":
        overrides["unknown_bodies"] = ["Fatehold Chronologist has unknown power/toughness"]
    elif case == "unknown_body_on_board":
        for entry in source["battlefield"]:
            if entry["instance_id"] == 280:
                entry.update(power=None, toughness=None)
    elif case == "stack":
        source["stack"] = [
            {"instance_id": 999, "name": "Ability", "type_line": "Ability", "controller_seat_id": 2}
        ]
    elif case == "request_type":
        decision = dataclasses.replace(decision, request_type="SelectTargets")
    elif case == "truncated":
        result = _search(G1_T12, hard_ms=0.0)[0]
        assert result.truncated
    elif case == "no_result":
        assert (
            lg.line_guard(None, decision, ROCK, source, survival_mode=True, lethal_now=False, our_turn=True)
            is None
        )
        return
    elif case == "chosen_wins":
        result = _doctored_win(_search(G1_T12)[0])
    elif case == "dead_now":
        result = dataclasses.replace(_search(G1_T12)[0], dead_now=True)
    assert _guard(source, decision, ROCK, result=result, **overrides) is None


def test_close_lines_and_value_gaps_outside_survival_are_left_alone():
    # G1_T8: Tetsuko (the autopilot's pick) is within a few points of Theorix.
    menu = [
        ("idx:0", "Cast Tetsuko Umezawa, Fugitive", cast(247), True),
        ("idx:1", "Cast Theorix Metamage", cast(125), True),
        ("pass", "Pass", None, None),
    ]
    assert _guard(G1_T8, actions_decision(menu), "idx:0") is None
    # The T12 rock line is alive too: a value gap alone needs survival mode and an unbounded search.
    decision = actions_decision(G1_T12_MENU)
    assert _guard(G1_T12, decision, ROCK, survival_mode=False) is None
    bounded = dataclasses.replace(_search(G1_T12)[0], bounded=True)
    assert _guard(G1_T12, decision, ROCK, result=bounded) is None


def test_a_class_gain_fires_even_when_bounded_or_outside_survival():
    source = _with_life(G1_T12, 7)  # the rock line now dies to their T13 attack
    decision = actions_decision(G1_T12_MENU)
    result, _ = _search(source)
    assert result.first_action[("cast", 217, None)].cls == DEAD and result.best.cls == ALIVE
    for kwargs in ({}, {"result": dataclasses.replace(result, bounded=True)}, {"survival_mode": False}):
        verdict = _guard(source, decision, ROCK, **kwargs)
        assert verdict is not None and verdict.option_id == ISLAND and verdict.robust, kwargs
    assert "dead on T13" in verdict.reason


@pytest.mark.parametrize(("life", "fires"), [(11, False), (7, True)])
def test_a_land_pick_needs_a_class_or_timing_gain(life, fires):
    source = _with_life(G1_T12, life)
    source["hand"].append(card(990, "Room of Refuge", 1))  # enters tapped: no Witness this turn
    menu = [
        *G1_T12_MENU[:3],
        ("idx:6", "Play land: Room of Refuge", play(990), None),
        ("pass", "Pass", None, None),
    ]
    verdict = _guard(source, actions_decision(menu), "idx:6")
    assert (verdict is not None) is fires
    if fires:
        assert verdict.option_id == ISLAND and verdict.chosen_line.cls == DEAD


def test_removal_on_an_attacker_is_preferred_over_a_creature():
    source = deepcopy(G1_T12)
    source["battlefield"] += [
        card(960 + i, "Island", 1, is_tapped=False, turn_entered_battlefield=3) for i in (0, 1)
    ]
    source["hand"].append(card(991, "Unsummon", 1))
    menu = [
        ("idx:1", "Cast Murmuring Volume", cast(217), True),
        ("idx:2", "Cast Undulating Witness", cast(229), True),
        ("idx:3", "Cast Unsummon", cast(991), True),
        ("idx:5", "Play land: Island", play(284), None),
        ("pass", "Pass", None, None),
    ]
    decision = actions_decision(menu)
    result, _ = _search(source)
    assert "Unsummon" in dict(result.best.steps[0].targets)
    assert _guard(source, decision, "idx:1").option_id == "idx:3"
    assert lg.line_fallback_pick(result, decision, source) == ["idx:3"]
    # Not payable right now: the creature is next.
    unpayable = actions_decision(
        [(o, label, meta, False if o == "idx:3" else pay) for o, label, meta, pay in menu]
    )
    assert _guard(source, unpayable, "idx:1").option_id == "idx:2"


@pytest.mark.parametrize("setting", ["off", "shadow", "on"])
def test_lethal_on_board_is_never_touched_in_any_mode(monkeypatch, setting):
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", setting)
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", setting)
    result, assessment = _search(G1_T15_FROM_OPPONENT)
    assert assessment.lethal_now and result.best.cls == WIN
    decision = actions_decision([("pass", "Pass", None, None)])
    assert _guard(G1_T15_FROM_OPPONENT, decision, "pass") is None
    assert lg.line_fallback_pick(result, decision, G1_T15_FROM_OPPONENT) is None  # the win is the attack
    assert lg.lines_summary(result).startswith("LINES")
    mode = _mode_decision()
    assert lg.mode_guard(None, mode, ["idx:0"], deepcopy(G1_T14_MODE_STATE), lethal_now=True) is None


@pytest.mark.parametrize("name", ["BUG_135027", "BUG_174855", "G3_T10_BLOCKS", "BUG_180436"])
def test_nothing_on_the_opponents_turn(name):
    source = FIXTURES[name]
    result, assessment = _search(source)
    assert not assessment.our_turn
    menu = [
        (f"idx:{i}", f"Cast {c['name']}", cast(c["instance_id"]), True) for i, c in enumerate(source["hand"])
    ] + [("pass", "Pass", None, None)]
    decision = actions_decision(menu)
    for option in decision.options:
        assert _guard(source, decision, option.option_id, our_turn=True) is None
        assert lg.option_note(result, option, source) == ""
    assert lg.line_fallback_pick(result, decision, source) is None


# --- the mode guard on the recorded T14 mode menu --------------------------------------------


@pytest.mark.parametrize("source", [G1_T14_MODE_STATE, G1_T14_ON_STACK], ids=["resolved-trigger", "on-stack"])
@pytest.mark.parametrize("setting", ["shadow", "on"])
def test_t14_destroy_mode_gives_way_to_lifegain_but_is_contingent(monkeypatch, source, setting):
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", setting)
    decision = _mode_decision()
    verdict = lg.mode_guard(None, decision, ["idx:0"], deepcopy(source), lethal_now=False)
    assert verdict is not None and verdict.option_id == "idx:1" and verdict.replaced == "idx:0"
    assert verdict.kind == "mode" and verdict.setting == setting
    assert verdict.chosen_line.dead_at == 1 and verdict.best_line.dead_at == 2
    assert verdict.best_line.lives()[0] == 2
    # Splinter Twin's copies are not modelled: never applied, even in 'on' mode.
    assert any("Splinter Twin" in note for note in verdict.contingent) and not verdict.applies
    assert decision.selection_is_valid([verdict.option_id])
    assert verdict.reason.startswith(
        "Mode guard: Archive Arbiter: gain 4 life (dead on T17) instead of Mode 1"
    )
    assert "contingent" in verdict.reason and len(verdict.summary) <= 200
    json.dumps(verdict.as_trace())
    assert lg.mode_guard(None, decision, ["idx:1"], deepcopy(source), lethal_now=False) is None


@pytest.mark.parametrize(("setting", "applies"), [("shadow", False), ("on", True)])
def test_a_non_contingent_mode_verdict_applies_only_when_on(monkeypatch, setting, applies):
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", setting)
    source = deepcopy(G1_T14_MODE_STATE)
    source["battlefield"] = [c for c in source["battlefield"] if c["instance_id"] != 324]  # no Splinter Twin
    verdict = lg.mode_guard(None, _mode_decision(), ["idx:0"], source, lethal_now=False)
    assert verdict is not None and verdict.option_id == "idx:1" and verdict.contingent == []
    assert verdict.applies is applies


@pytest.mark.parametrize(
    "case",
    ["off", "lethal_now", "their_turn", "other_stack_object", "choose_two", "two_chosen", "search",
     "not_modal", "incomplete", "no_comparison", "unknown_body", "raises"],
)  # fmt: skip
def test_the_mode_guard_never_fires(monkeypatch, case):
    source, decision, chosen = deepcopy(G1_T14_MODE_STATE), _mode_decision(), ["idx:0"]
    result_fn, lethal_now = None, False
    assert lg.mode_guard(None, decision, chosen, deepcopy(source), lethal_now=False) is not None
    if case == "off":
        monkeypatch.setenv("ARENAMCP_MODE_GUARD", "off")
    elif case == "lethal_now":
        lethal_now = True
    elif case == "their_turn":
        source["turn"]["active_player"] = 2
    elif case == "other_stack_object":
        source["stack"].insert(
            0, {"instance_id": 999, "name": "Shock", "type_line": "Instant", "controller_seat_id": 2}
        )
    elif case == "choose_two":
        decision = _mode_decision(min=2, max=2)  # one mode alone is not a valid answer
    elif case == "two_chosen":
        chosen = ["idx:0", "idx:1"]
    elif case == "search":
        decision = dataclasses.replace(decision, request_type="Search")
    elif case == "not_modal":
        decision = _mode_decision(choiceKind="choose_or_cost")
    elif case == "incomplete":
        full = compare_modes(deepcopy(source), decision)
        result_fn = dataclasses.replace(full, lines={"idx:0": full.lines["idx:0"]})
        assert not result_fn.complete
    elif case == "no_comparison":
        result_fn = lambda state, decision: None  # noqa: E731
    elif case == "unknown_body":
        for entry in source["battlefield"]:
            if entry["instance_id"] == 333:
                entry.update(power=None, toughness=None)
    elif case == "raises":

        def result_fn(state, decision):
            raise RuntimeError("boom")

    lg._MODE_CACHE.clear()
    assert lg.mode_guard(result_fn, decision, chosen, source, lethal_now=lethal_now) is None


def test_the_mode_comparison_is_shared_between_tags_and_the_guard():
    decision = _mode_decision()
    with mock.patch.object(lg, "compare_modes", wraps=compare_modes) as spy:
        comparison = lg.mode_comparison(deepcopy(G1_T14_MODE_STATE), decision)
        notes = {o.option_id: lg.option_note(comparison, o, G1_T14_MODE_STATE) for o in decision.options}
        verdict = lg.mode_guard(None, decision, ["idx:0"], deepcopy(G1_T14_MODE_STATE), lethal_now=False)
        assert spy.call_count == 1
        lg.mode_comparison(deepcopy(G1_T14_ON_STACK), decision)  # another stack: another comparison
        assert spy.call_count == 2
    assert verdict.option_id == "idx:1"
    assert notes == {
        "idx:0": "[LINE: dead T15; effect unmodelled]",
        "idx:1": "[LINE best: survives T15 at 2, dead T17]",
    }
    # A ModeComparison or a callable can be passed instead.
    assert (
        lg.mode_guard(comparison, decision, ["idx:0"], G1_T14_MODE_STATE, lethal_now=False).option_id
        == "idx:1"
    )
    assert lg.mode_guard(compare_modes, decision, ["idx:0"], G1_T14_MODE_STATE, lethal_now=False) is not None
    assert isinstance(comparison, ModeComparison)


# --- option tags and the LINES line --------------------------------------------------------


def test_t12_option_tags():
    result, _ = _search(G1_T12)
    decision = actions_decision(G1_T12_MENU)
    notes = {o.option_id: lg.option_note(result, o, G1_T12) for o in decision.options}
    assert notes[ISLAND].startswith("[LINE best: 7 after T13, 5 after T15")
    assert notes[ROCK].startswith("[LINE: -") and "vs best; 4 after T13" in notes[ROCK]
    assert notes[LANDCYCLE].startswith("[LINE: -")
    assert notes["pass"] == ""  # Main1: passing goes to combat, not to the end of the turn
    assert all(len(note) <= 60 for note in notes.values())


def test_a_dead_line_and_pass_in_main2_are_tagged():
    source = _with_life(G1_T12, 7)
    result, _ = _search(source)
    rock = actions_decision(G1_T12_MENU).options[0]
    assert lg.option_note(result, rock, source) == "[LINE: dead T13]"
    main2 = after_land_drop(G1_T12, 284)
    main2["turn"]["phase"] = "Phase_Main2"
    result, _ = _search(main2)
    decision = actions_decision(
        [("idx:2", "Cast Undulating Witness", cast(229), True), ("pass", "Pass", None, None)]
    )
    witness, passing = decision.options
    assert lg.option_note(result, witness, main2).startswith("[LINE best: 7 after T13")
    assert lg.option_note(result, passing, main2) == "[LINE: 4 after T13, dead T15]"


def test_tags_need_a_usable_search_and_the_right_result():
    result, _ = _search(G1_T12)
    rock = actions_decision(G1_T12_MENU).options[0]
    assert lg.option_note(None, rock, G1_T12) == ""
    assert lg.option_note(_search(G1_T12, hard_ms=0.0)[0], rock, G1_T12) == ""
    modal = _mode_decision().options[1]
    assert lg.option_note(result, modal, G1_T12) == ""  # a mode needs the decision's ModeComparison
    countersculpt = actions_decision([("idx:9", "Cast Countersculpt", cast(120), True)]).options[0]
    assert lg.option_note(result, countersculpt, G1_T12) == ""  # counters are never searched
    assert lg.option_note(result, object(), G1_T12) == ""


@pytest.mark.parametrize("name", list(FIXTURES))
def test_tags_are_short_and_never_name_their_cards(name):
    source = FIXTURES[name]
    result, _ = _search(source)
    local = source["local_seat_id"]
    theirs = {c["name"] for c in source["battlefield"] if c["controller_seat_id"] != local}
    menu = [
        (f"idx:{i}", f"{'Play land' if 'Land' in c['type_line'] else 'Cast'}: {c['name']}",
         play(c["instance_id"]) if "Land" in c["type_line"] else cast(c["instance_id"]), None)
        for i, c in enumerate(source["hand"])
    ] + [("pass", "Pass", None, None)]  # fmt: skip
    for option in actions_decision(menu).options:
        note = lg.option_note(result, option, source)
        assert len(note) <= 60 and not any(card_name in note for card_name in theirs), note
        assert note == "" or note.startswith("[LINE")


def test_lines_summary():
    result, _ = _search(G1_T12)
    text = lg.lines_summary(result)
    assert text.startswith("LINES") and len(text) <= 320 and "Island + Undulating Witness" in text
    assert lg.lines_summary(None) == "" and lg.lines_summary(_search(G1_T12, hard_ms=0.0)[0]) == ""
    assert lg.lines_summary(_search(BUG_180436)[0]).startswith("LINES")


# --- the fallback pick ----------------------------------------------------------------------


def test_fallback_pick_follows_the_best_line():
    result, _ = _search(G1_T12)
    decision = actions_decision(G1_T12_MENU)
    assert lg.line_fallback_pick(result, decision, G1_T12) == [ISLAND]
    source, after = _after_island()
    assert lg.line_fallback_pick(_search(source)[0], after, source) == ["idx:2"]
    unpayable = actions_decision(
        [
            (o.option_id, o.label, o.meta, False if o.option_id == "idx:2" else o.payable)
            for o in after.options
        ]
    )
    assert lg.line_fallback_pick(_search(source)[0], unpayable, source) is None


def test_fallback_pick_declines_without_a_usable_search():
    result, _ = _search(G1_T12)
    decision = actions_decision(G1_T12_MENU)
    assert lg.line_fallback_pick(None, decision, G1_T12) is None
    assert lg.line_fallback_pick(_search(G1_T12, hard_ms=0.0)[0], decision, G1_T12) is None
    assert (
        lg.line_fallback_pick(result, dataclasses.replace(decision, request_type="SelectN"), G1_T12) is None
    )
    stacked = {**deepcopy(G1_T12), "stack": [{"instance_id": 999, "name": "Ability"}]}
    assert lg.line_fallback_pick(result, decision, stacked) is None
    assert lg.line_fallback_pick(result, None, G1_T12) is None


# --- determinism, latency, isolation -------------------------------------------------------


def test_two_cold_runs_give_the_same_verdicts():
    def run():
        lg._MODE_CACHE.clear()
        line = _guard(G1_T12, actions_decision(G1_T12_MENU), ROCK)
        mode = lg.mode_guard(None, _mode_decision(), ["idx:0"], deepcopy(G1_T14_MODE_STATE), lethal_now=False)
        return line.as_trace(), line.summary, mode.as_trace(), mode.summary

    assert run() == run()


def test_guard_and_tags_stay_fast():
    result, assessment = _search(G1_T12)
    decision = actions_decision(G1_T12_MENU)
    started = time.perf_counter()
    lg.line_guard(result, decision, ROCK, G1_T12, survival_mode=True, lethal_now=False, our_turn=True)
    for option in decision.options:
        lg.option_note(result, option, G1_T12)
    lg.line_fallback_pick(result, decision, G1_T12)
    lg.mode_guard(None, _mode_decision(), ["idx:0"], deepcopy(G1_T14_MODE_STATE), lethal_now=False)
    assert (time.perf_counter() - started) * 1000 < 150


def test_line_guard_imports_no_llm_backend():
    """line_guard's own import closure (package __init__ bypassed) has no backend."""
    code = (
        "import importlib.util, sys, types\n"
        "spec = importlib.util.find_spec('arenamcp')\n"
        "pkg = types.ModuleType('arenamcp')\n"
        "pkg.__path__ = list(spec.submodule_search_locations)\n"
        "sys.modules['arenamcp'] = pkg\n"
        "import arenamcp.line_guard\n"
        "bad = [m for m in sys.modules if m.startswith(('arenamcp.backends', 'arenamcp.coach'))"
        " or m in ('requests', 'httpx', 'openai')]\n"
        "print(','.join(sorted(bad)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == ""


# --- 2026-10-07 review regressions ------------------------------------------------------------


def _burn_board() -> tuple[dict, object]:
    """Our Main1 at 3 life, three Mountains; they are at 2 with a 3/1 and a 2/2. Shock wins now."""
    from tests.strategic_states import state

    source = state(
        turn=10, active=1, phase="Phase_Main1", step="", life={1: 3, 2: 2}, lands_played={1: 1, 2: 0},
        library=20, opponent_hand=1,
        battlefield=[(10, "Mountain", 1, False, 2), (11, "Mountain", 1, False, 4), (12, "Mountain", 1, False, 6),
                     (20, "Heartstring Puller", 2, False, 7), (21, "Cadet", 2, False, 7)],
        hand=[], graveyard=[],
    )  # fmt: skip
    shock = {"name": "Shock", "type_line": "Instant", "mana_cost": "{R}", "card_types": ["CardType_Instant"],
             "oracle_text": "Shock deals 2 damage to any target."}  # fmt: skip
    wall = {"name": "Stone Wall", "type_line": "Creature — Wall", "mana_cost": "{2}{R}", "oracle_text": "Defender",
            "power": 0, "toughness": 6, "keywords": ["defender"], "card_types": ["CardType_Creature"]}  # fmt: skip
    source["hand"] = [{**card(30, "Unsummon", 1), **shock}, {**card(31, "Unsummon", 1), **wall}]
    menu = [("idx:1", "Cast Shock", cast(30), True), ("idx:2", "Cast Stone Wall", cast(31), True),
            ("pass", "Pass", None, None)]  # fmt: skip
    return source, actions_decision(menu)


@pytest.mark.parametrize("setting", ["shadow", "on"])
def test_a_winning_burn_spell_is_never_replaced(monkeypatch, setting):
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", setting)
    source, decision = _burn_board()
    result, _ = _search(source)
    assert result.best.cls == WIN and result.posture == "lethal"
    assert _guard(source, decision, "idx:1") is None
    assert _guard(source, decision, "idx:2") is None  # lethal on the table: no steering at all
    assert lg.line_fallback_pick(result, decision, source) == ["idx:1"]
    assert "dead" not in lg.option_note(result, decision.find("idx:1"), source)
    assessment = _fresh_assessment(source)
    assert any(flag.startswith("LETHAL LINE") for flag in assessment.flags)
    assert not any(flag.startswith("ONLY SURVIVING LINE") for flag in assessment.flags)


def _fresh_assessment(source: dict):
    ba._CACHE.clear()
    return ba.assess(deepcopy(source))


def test_unmodelled_finishers_are_left_to_the_model():
    # A creature whose enters trigger pings a player, and a pump: the search can't value either.
    source, _ = _burn_board()
    pinger = {"name": "Test Pinger", "type_line": "Creature — Goblin", "mana_cost": "{2}{R}", "power": 1,
              "toughness": 1, "card_types": ["CardType_Creature"],
              "oracle_text": "When this creature enters, it deals 2 damage to each opponent."}  # fmt: skip
    pump = {"name": "Test Pump", "type_line": "Instant", "mana_cost": "{R}", "card_types": ["CardType_Instant"],
            "oracle_text": "Target creature gets +3/+0 until end of turn."}  # fmt: skip
    source["hand"] = [{**card(40, "Unsummon", 1), **pinger}, {**card(41, "Unsummon", 1), **pump}]
    menu = [("idx:1", "Cast Test Pinger", cast(40), True), ("idx:2", "Cast Test Pump", cast(41), True),
            ("pass", "Pass", None, None)]  # fmt: skip
    decision = actions_decision(menu)
    result, _ = _search(source)
    for option_id in ("idx:1", "idx:2"):
        assert lg._unmodelled_finisher(result, decision.find(option_id), source)
        assert _guard(source, decision, option_id) is None
        assert lg.option_note(result, decision.find(option_id), source) == ""


def _sweeper_board() -> tuple[dict, object]:
    """Our turn at 4 life vs six 1/1s; our creature's enters trigger offers a -1/-1 sweep or 4 life."""
    from tests.strategic_states import state

    source = state(
        turn=10, active=1, phase="Phase_Main1", step="", life={1: 4, 2: 20}, lands_played={1: 1, 2: 0},
        library=20, opponent_hand=1,
        battlefield=[(10 + i, "Island", 1, True, 2 * i) for i in range(5)]
        + [(40 + i, "Cadet", 2, False, 7) for i in range(6)],
        hand=[], graveyard=[],
    )  # fmt: skip
    for entry in source["battlefield"]:
        if entry["name"] == "Cadet":
            entry.update(power=1, toughness=1)
    text = ("Flying\nWhen this creature enters, choose one —\n•Creatures your opponents control get -1/-1 until end "
            "of turn.\n•You gain 4 life.")  # fmt: skip
    source["stack"] = [
        {**card(344, "Unsummon", 1), "name": "Test Sphinx", "type_line": "Creature — Sphinx", "mana_cost": "{3}{U}{U}",
         "oracle_text": text, "power": 2, "toughness": 2, "keywords": ["flying"], "card_types": ["CardType_Creature"]}
    ]  # fmt: skip
    meta = {"actionType": "CastingTimeOption", "choiceKind": "modal", "requestClass": "CastingTimeOption_ModalRequest",
            "childIndex": 0, "sourceId": 344, "min": 1, "max": 1}  # fmt: skip
    menu = [
        ("idx:0", "Mode 1: Creatures your opponents control get -1/-1 until end of turn.",
         {**meta, "label": "Mode 1", "optionIndex": 0}),
        ("idx:1", "Mode 2: You gain 4 life.", {**meta, "label": "Mode 2", "optionIndex": 1}),
    ]  # fmt: skip
    return source, modal_decision(menu, (344, 455))


def test_an_other_mode_that_hits_their_creatures_is_contingent(monkeypatch):
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", "on")
    source, decision = _sweeper_board()
    comparison = compare_modes(deepcopy(source), decision)
    assert comparison.modes["idx:0"] == "other"
    assert any("affects their creatures or combat" in note for note in comparison.contingent)
    verdict = lg.mode_guard(None, decision, ["idx:0"], source, lethal_now=False)
    assert verdict is None or not verdict.applies  # the sweep wins the game; never overridden


def test_engines_past_the_top_five_threats_still_make_a_verdict_contingent(monkeypatch):
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", "on")
    source = deepcopy(G1_T14_MODE_STATE)
    for index in range(5):  # five 6/6s outrank Splinter Twin in _threats' top five
        source["battlefield"].append(
            {**card(900 + index, "Cadet", 2, is_tapped=False, turn_entered_battlefield=5), "power": 6, "toughness": 6,
             "name": f"Big Body {index}", "type_line": "Creature — Wall"}
        )  # fmt: skip
    model = build_board_model(source)
    top = ba._threats(source["battlefield"], model.opponent, list(model.theirs), False, len(model.ours))
    assert all(threat.name != "Splinter Twin" for threat in top)
    comparison = compare_modes(deepcopy(source), _mode_decision())
    assert any("Splinter Twin" in note for note in comparison.contingent)
    verdict = lg.mode_guard(None, _mode_decision(), ["idx:0"], source, lethal_now=False)
    assert verdict is None or not verdict.applies


@pytest.mark.parametrize("setting", ["shadow", "on"])
def test_the_kill_switch_covers_the_modal_path(monkeypatch, setting):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", setting)
    source = deepcopy(G1_T14_MODE_STATE)
    source["battlefield"] = [c for c in source["battlefield"] if c["instance_id"] != 324]  # no Splinter Twin
    decision = _mode_decision()
    assert lg.mode_comparison(source, decision) is None
    assert lg.mode_guard(None, decision, ["idx:0"], source, lethal_now=False) is None
    assert all(
        lg.option_note(lg.mode_comparison(source, decision), o, source) == "" for o in decision.options
    )
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "1")  # switching back is a different cache entry
    assert lg.mode_comparison(source, decision) is not None


def test_concurrent_mode_comparisons_share_one_run():
    import threading

    decision, calls, results = _mode_decision(), [], []
    entered = threading.Event()

    def slow(state, decision):
        calls.append(1)
        entered.set()
        time.sleep(0.05)
        return compare_modes(state, decision)

    with mock.patch.object(lg, "compare_modes", side_effect=slow):
        threads = [
            threading.Thread(
                target=lambda: results.append(lg.mode_comparison(deepcopy(G1_T14_MODE_STATE), decision))
            )
            for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
    assert len(calls) == 1 and len({id(r) for r in results}) == 1 and results[0] is not None
