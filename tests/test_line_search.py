"""The bounded multi-turn line search (``arenamcp.line_search``) on real recorded states.

Field report (2026-10-06 FRA game 1, standalone.log 15:41-15:45): at T12 the
autopilot cast a mana rock and landcycled its only castable creature, and at
T14 it chose Archive Arbiter's "destroy target noncreature, nonland permanent"
mode over "gain 4 life" at 4 life (match packet decision 18) and died to the
next attack. The greedy projection could not see either: it never compared
lines. These tests pin decision outcomes and invariants on the recorded states
in tests/strategic_states.py, not tuned numbers: the T12 land + Witness pick,
the T14 lifegain mode surviving the first attack, the only surviving line
after the rock, lethal never missed, the baseline never beaten by a worse
line, the rules (sickness, tapped creatures, mana, landcycling, X spells,
counters, bounce), determinism, JSON safety and the latency budget.
"""

from __future__ import annotations

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
    G1_T14_MODE_CHOSEN,
    G1_T14_MODE_MENU,
    G1_T14_MODE_REQUEST_ID,
    G1_T14_MODE_STATE,
    G1_T14_ON_STACK,
    G1_T15_FROM_OPPONENT,
    G3_T10_BLOCKS,
    actions_decision,
    amend,
    bridge_card,
    card,
    cast,
    log_phase,
    mac_phase,
    modal_decision,
    state,
)

from arenamcp import board_assessment as ba
from arenamcp.board_assessment import TurnProjection
from arenamcp.board_model import build_board_model
from arenamcp.line_search import (
    ALIVE,
    DEAD,
    LINE_TOL,
    NOTHING,
    WIN,
    action_key,
    compare_modes,
    evaluate_plan,
    proxy_outcome,
    search_lines,
)
from arenamcp.line_search_moves import bullets, classify

FIXTURES = {
    "G1_T8": G1_T8,
    "G1_T10": G1_T10,
    "G1_T12": G1_T12,
    "G1_T12_AFTER_VOLUME": G1_T12_AFTER_VOLUME,
    "G1_T14": G1_T14,
    "G1_T14_ON_STACK": G1_T14_ON_STACK,
    "G1_T14_MODE_STATE": G1_T14_MODE_STATE,
    "G1_T15_FROM_OPPONENT": G1_T15_FROM_OPPONENT,
    "G3_T10_BLOCKS": G3_T10_BLOCKS,
    "BUG_135027": BUG_135027,
    "BUG_174855": BUG_174855,
    "BUG_180436": BUG_180436,
}
ISLAND = ("land", (frozenset({"U"}), False))


def _hints(source: dict) -> tuple[bool, bool]:
    """(survival, lethal_now) as the assessment passes them, from today's greedy facts."""
    ba._CACHE.clear()
    with mock.patch.dict(os.environ, {"ARENAMCP_LINE_SEARCH": "0"}):
        assessment = ba.assess(deepcopy(source))
    ba._CACHE.clear()
    return assessment.survival_mode and not assessment.lethal_now, assessment.lethal_now


def _search(source: dict, **kwargs):
    survival, lethal_now = _hints(source)
    model = build_board_model(deepcopy(source))
    assert model is not None
    return search_lines(model, survival=survival, lethal_now=lethal_now, **kwargs)


def _all_lines(result) -> list:
    return [result.best, result.baseline, *result.lines, *result.first_action.values()]


def _two_creatures(*, phase: str = "Phase_Main1", opp_life: int = 20, theorix: dict | None = None) -> dict:
    """Our turn 10 at 20 life: our Theorix Metamage (950) vs their Cadet (951), no hands."""
    return amend(
        state(
            turn=10, active=1, phase=phase, step="", life={1: 20, 2: opp_life}, lands_played={1: 1, 2: 0},
            library=20, opponent_hand=0, battlefield=[(950, "Theorix Metamage", 1, False, 5), (951, "Cadet", 2, False, 5)],
            hand=[], graveyard=[],
        ),
        cards={950: theorix or {}},
    )  # fmt: skip


# --- the recorded decisions -----------------------------------------------------------


def test_t12_best_t_step_is_island_and_undulating_witness():
    result = _search(G1_T12)
    t = result.best.steps[0]
    assert (t.land, t.casts) == ("Island", ("Undulating Witness",))
    assert t.life_after == 7 and result.best.cls == ALIVE
    # The autopilot's actual T12 (the rock, then landcycling Witness) loses 3 life more now.
    rock = result.first_action[("cast", 217, None)]
    cycle = result.first_action[("cycle", 229)]
    assert rock.steps[0].life_after == cycle.steps[0].life_after == 4
    assert rock.score < result.best.score and cycle.score < result.best.score
    assert ("cast", 229, None) in result.best.first_actions and ISLAND in result.best.first_actions


def test_t14_lifegain_mode_survives_their_first_attack_and_destroy_does_not():
    result = _search(G1_T14)
    gain = result.first_action[("cast", 200, 1)]
    destroy = result.first_action[("cast", 200, 0)]
    assert gain.steps[0].modes == (("Archive Arbiter", "gain 4 life"),)
    assert gain.steps[0].life_after == 2 and gain.dead_at == 2
    assert destroy.dead_at == 1 and destroy.steps[0].life_after <= 0
    assert result.best is gain or result.best.first_sig == gain.first_sig
    assert (result.dead_in, result.dead_in_greedy) == (2, 1)
    assert result.only_first_attack_survivor and not result.all_dead_at_first
    # Casting Arbiter at all maps to the lifegain line (the mode is chosen later).
    assert result.first_action[("cast", 200, None)].first_sig == gain.first_sig


@pytest.mark.parametrize("source", [G1_T14_MODE_STATE, G1_T14_ON_STACK], ids=["resolved-trigger", "on-stack"])
def test_compare_modes_on_the_recorded_mode_menu(source):
    decision = modal_decision(G1_T14_MODE_MENU, G1_T14_MODE_REQUEST_ID)
    comparison = compare_modes(deepcopy(source), decision)
    assert comparison is not None and comparison.source == "Archive Arbiter"
    assert comparison.modes == {"idx:0": "other", "idx:1": "gain 4 life"}
    gain, destroy = comparison.lines["idx:1"], comparison.lines["idx:0"]
    assert gain.lives()[0] == 2 and gain.dead_at == 2
    assert destroy.dead_at == 1
    assert G1_T14_MODE_CHOSEN == ["idx:0"]  # the recorded pick is the one that dies at once
    # Splinter Twin's copies are not modelled: the verdict is contingent, never applied.
    assert any("Splinter Twin" in note and "token/copy engine" in note for note in comparison.contingent)
    assert comparison.complete
    json.dumps([line.as_payload() for line in comparison.lines.values()])


def test_after_the_rock_only_the_lifegain_line_survives():
    result = _search(G1_T12_AFTER_VOLUME)
    assert result.baseline.dead_at == 2 and result.dead_in_greedy == 2  # greedy casts Witness at T14
    assert result.best.cls == ALIVE and result.dead_in is None
    then = result.best.steps[1]
    assert then.casts == ("Archive Arbiter",) and then.modes == (("Archive Arbiter", "gain 4 life"),)
    # Not the ONLY survivor (2026-10-07 review): landcycling the Witness first and
    # casting Arbiter next turn also lives, so the uniqueness claim would be false.
    survivors = [line for line in result.lines if line.cls != DEAD]
    assert len({line.steps[0].sig for line in survivors}) >= 2
    assert any(line.steps[0].cycles for line in survivors)
    assert not result.only_survivor and result.greedy_dies


def test_t8_close_call_keeps_the_greedy_theorix_pick():
    result = _search(G1_T8)
    assert result.baseline.steps[0].casts == ("Theorix Metamage",)
    assert result.best.steps[0].casts == ("Theorix Metamage",)
    tetsuko = result.first_action[("cast", 247, None)]
    assert (tetsuko.cls, tetsuko.timing) == (result.best.cls, result.best.timing)


def test_lethal_on_board_is_found_and_marked():
    result = _search(G1_T15_FROM_OPPONENT)
    assert result.best.cls == WIN and result.best.win_at == 1 and result.best.win_turn == 15
    assert result.posture == "lethal" and result.best.steps[0].attack
    rows = result.to_projections()
    assert [row.label for row in rows] == ["T", "T+1", "T+2"]
    assert rows[0].life_after is None


def test_their_lethal_attack_under_way_leaves_no_lines():
    result = _search(BUG_180436)
    assert result.dead_now and result.dead_in == 1 and result.dead_in_greedy == 1
    assert result.all_dead_at_first and result.exact_first_attack
    assert result.lines == [] and result.now_life <= 0
    rows = result.to_projections()
    assert len(rows) == 3 and all(row.life_after is None for row in rows)
    assert result.prompt_line().startswith("LINES")


def test_declared_attackers_offer_advisory_block_variants():
    result = _search(G3_T10_BLOCKS)
    assert result.first_action[("block", "best")].now_life == 20  # the solver's blocks stop it all
    assert result.first_action[("block", "none")].now_life == 12  # 4 + 2 + 2 unblocked
    assert ("block", "crackback") in result.first_action
    # Advisory only: the facts follow the solver's blocks, whatever the variants score.
    assert result.best.block == "best" and result.now_life == 20
    assert any(line.block in ("none", "crackback") for line in result.lines)
    # Our only creature left after those blocks is a defender: no choice, no "only line".
    assert result.posture == "either" and not result.only_survivor


# --- invariants on every fixture -----------------------------------------------------


@pytest.mark.parametrize("name", list(FIXTURES))
def test_best_is_never_worse_than_the_greedy_baseline(name):
    result = _search(FIXTURES[name])
    best, base = result.best, result.baseline
    assert (best.cls, best.timing, best.v) >= (base.cls, base.timing, base.v)
    if (best.cls, best.timing) == (base.cls, base.timing) and best is not base:
        assert best.v - base.v >= LINE_TOL or best.first_sig == base.first_sig
    assert not result.truncated and not result.bounded
    if result.lines:
        assert result.lines[0] is best and len(result.lines) <= 5
        assert len({line.first_sig for line in result.lines}) == len(result.lines)
    for key, line in result.first_action.items():
        assert key in line.first_actions
        assert line.score <= max(line.score for line in [best, *result.first_action.values()])


@pytest.mark.parametrize("name", list(FIXTURES))
def test_payloads_are_json_safe_and_the_prompt_line_is_short(name):
    result = _search(FIXTURES[name])
    text = result.prompt_line()
    assert text.startswith("LINES") and len(text) <= 320
    json.dumps([line.as_payload() for line in _all_lines(result)])
    json.dumps(result.stats())
    for line in _all_lines(result):
        assert len(line.summary()) <= 110


@pytest.mark.parametrize("name", list(FIXTURES))
def test_two_cold_runs_give_identical_lines(name):
    first, second = _search(FIXTURES[name]), _search(FIXTURES[name])

    def facts(result):
        return (
            [line.as_payload() for line in result.lines],
            result.best.as_payload(),
            result.baseline.as_payload(),
            sorted((repr(key), line.summary()) for key, line in result.first_action.items()),
            result.posture,
            result.posture_reason,
            (result.only_survivor, result.only_first_attack_survivor, result.all_dead_at_first),
        )

    assert facts(first) == facts(second)


@pytest.mark.parametrize(
    "name", ["G3_T10_BLOCKS", "BUG_135027", "BUG_174855", "G1_T12", "G1_T15_FROM_OPPONENT"]
)
def test_bridge_and_log_phase_names_give_the_same_lines(name):
    bridge, log = _search(mac_phase(FIXTURES[name])), _search(log_phase(FIXTURES[name]))
    assert [line.as_payload() for line in bridge.lines] == [line.as_payload() for line in log.lines]
    assert bridge.best.as_payload() == log.best.as_payload()


@pytest.mark.parametrize("name", list(FIXTURES))
def test_creatures_never_attack_the_turn_they_are_cast(name):
    for line in _all_lines(_search(FIXTURES[name])):
        for step in line.steps:
            assert not set(step.attack) & set(step.casts), (name, step)
    # Archive Arbiter entered this turn on the mode-menu board: never attacks at T.
    for line in _all_lines(_search(G1_T14_MODE_STATE)):
        assert not line.steps or "Archive Arbiter" not in line.steps[0].attack


def test_counters_and_x_spells_are_never_cast():
    source = deepcopy(G1_T12)
    source["hand"].append(
        {
            "instance_id": 990, "name": "Blaze", "owner_seat_id": 1, "controller_seat_id": 1,
            "type_line": "Sorcery", "mana_cost": "{X}{R}", "oracle_text": "Blaze deals X damage to any target.",
            "card_types": ["CardType_Sorcery"],
        }
    )  # fmt: skip
    result = _search(source)
    for line in _all_lines(result):
        for step in line.steps:
            assert "Countersculpt" not in step.casts and "Blaze" not in step.casts
    assert ("cast", 990, None) not in result.first_action and ("cast", 120, None) not in result.first_action


# --- rules ----------------------------------------------------------------------------


def test_creatures_tapped_in_the_snapshot_cannot_block_at_t():
    source = deepcopy(G1_T12)
    # Their only untapped creature leaves; their Shaper, Puller and Cadet attacked last turn.
    source["battlefield"] = [c for c in source["battlefield"] if c["instance_id"] != 280]
    source["battlefield"].append(card(990, "Cadet", 1, is_tapped=False, turn_entered_battlefield=10))
    line = _search(source).first_action[("attack", frozenset({990}))]
    assert line.steps[0].attack == ("Cadet",) and line.steps[0].opp_life_after == 18


@pytest.mark.parametrize(("keywords", "life_after"), [([], 18), (["vigilance"], 20)])
def test_our_attackers_stay_tapped_through_their_turn(keywords, life_after):
    result = _search(_two_creatures(theorix={"keywords": keywords}))
    attack = result.first_action[("attack", frozenset({950}))]
    hold = result.first_action[("noattack",)]
    assert attack.steps[0].opp_life_after == 18
    assert attack.steps[0].life_after == life_after  # a tapped Theorix can't block their Cadet
    assert hold.steps[0].life_after == 20


def test_a_suicidal_attack_is_dropped_for_a_safe_one():
    # Their Cadet attacking into our 3/3 dies and our crackback is then lethal (3 life):
    # the paranoid opponent keeps it home instead of handing us the win.
    result = _search(_two_creatures(phase="Phase_Main2", opp_life=3, theorix={"power": 3, "toughness": 3}))
    step = result.best.steps[0]
    assert step.policy == "hold-dying" and step.life_after == 20 and step.their_creatures_after == 1


def test_a_lethal_policy_is_never_filtered_out():
    class Cautious:
        def attack_policies(self, moves, able, blockers, life):
            return [("cautious", ())]

        def chance(self, moves, life):
            return [(1.0, NOTHING)]

    class CautiousThenEverything(Cautious):
        def attack_policies(self, moves, able, blockers, life):
            return [("cautious", ()), ("everything", tuple(able))]

    survival, lethal_now = _hints(G1_T14)
    model = build_board_model(deepcopy(G1_T14))
    calm = search_lines(model, survival=survival, lethal_now=lethal_now, opponent=Cautious())
    paranoid = search_lines(
        model, survival=survival, lethal_now=lethal_now, opponent=CautiousThenEverything()
    )
    assert calm.best.cls == ALIVE and calm.dead_in is None
    assert paranoid.best.cls == DEAD and paranoid.dead_in == 2


def test_a_land_that_enters_tapped_adds_mana_from_the_next_turn():
    result = _search(BUG_174855)
    room = result.first_action[("land", (frozenset("WUBRG"), True))]
    assert room.steps[0].land == "Room of Refuge"
    assert (room.steps[0].mana, room.steps[1].mana) == (4, 5)


def test_landcycling_fetches_a_basic_for_the_next_turn():
    result = _search(BUG_135027)
    line = result.first_action[("cycle", 240)]  # either Witness: copies share the lowest id
    t, then = line.steps[0], line.steps[1]
    assert t.cycles == ("Undulating Witness",) and t.mana == 2 and not t.land
    assert then.land == "Island" and then.mana == 3


def test_bounce_returns_their_creature_after_their_next_attack():
    source = deepcopy(G1_T12)
    source["hand"].append(card(991, "Unsummon", 1))
    line = _search(source).first_action[("cast", 991, None)]
    t = line.steps[0]
    (_spell, bounced), = [target for target in t.targets if target[0] == "Unsummon"]  # fmt: skip
    power = {"Heartstring Puller": 3, "Cadet": 2, "Paradox Shaper": 1, "Fatehold Chronologist": 1}
    assert t.their_creatures_after == 4  # back after the attack it missed
    assert t.life_after == 11 - (7 - power[bounced])


def test_removal_without_a_killable_target_is_held():
    source = _two_creatures()  # our turn; their only creature is a 2/2 Cadet
    source["battlefield"] += [
        card(960 + i, "Mountain", 1, is_tapped=False, turn_entered_battlefield=1) for i in (0, 1)
    ]
    burn = {"type_line": "Instant", "mana_cost": "{R}", "card_types": ["CardType_Instant"], "owner_seat_id": 1,
            "controller_seat_id": 1}  # fmt: skip
    source["hand"] = [
        {
            **burn,
            "instance_id": 990,
            "name": "Ping",
            "oracle_text": "Ping deals 1 damage to target creature.",
        },
        {**burn, "instance_id": 991, "name": "Zap", "oracle_text": "Zap deals 2 damage to target creature."},
    ]
    result = _search(source)
    assert ("cast", 990, None) not in result.first_action  # nothing for 1 damage to kill
    zap = result.first_action[("cast", 991, None)]
    assert zap.steps[0].targets == (("Zap", "Cadet"),)


def test_held_removal_answers_their_attack_under_way():
    with_bounce = deepcopy(BUG_174855)
    with_bounce["hand"].append(bridge_card(990, 0, "Unsummon", 2, "hand"))
    assert _search(BUG_174855).now_life == 12
    # Their attacking Hortimancer has ward {1}: with our one open Island, Unsummon
    # ({U}) can't also pay the ward, so it doesn't answer the attack.
    assert _search(with_bounce).now_life == 12
    # A second open land pays the ward: Unsummon bounces the attacker.
    with_bounce["battlefield"].append(bridge_card(989, 106529, "Island", 2, "battlefield", entered=1))
    assert _search(with_bounce).now_life == 14


def test_modal_text_is_deduplicated_and_other_modes_are_worth_nothing():
    text = (
        "Flying\nWhen this creature enters, choose one — \n•Destroy target noncreature, nonland permanent. \n"
        "•You gain 4 life.\nWhen this creature enters, choose one<nobr> —</nobr> \n•<indent=4%>Destroy target "
        "noncreature, nonland permanent. </indent>\n•<indent=4%>You gain 4 life.</indent>\nWhen this creature "
        "enters, choose one — \n•Destroy target noncreature, nonland permanent. \n•You gain 4 life."
    )
    modes = bullets(text)
    assert modes == ["Destroy target noncreature, nonland permanent.", "You gain 4 life."]
    assert classify("Archive Arbiter", modes[0])[:3] == ("other", 0, None)
    assert classify("Archive Arbiter", modes[1])[:2] == ("lifegain", 4)
    assert classify("Fulminous Forte", "Fulminous Forte deals 5 damage to target creature or planeswalker.")[
        0
    ] == ("removal")


# --- interfaces -----------------------------------------------------------------------


def test_projection_rows_keep_the_lookahead_shape():
    rows = _search(G1_T12).to_projections()
    assert all(isinstance(row, TurnProjection) for row in rows)
    assert [(row.label, row.turn) for row in rows] == [("T", 12), ("T+1", 14), ("T+2", 16)]
    assert (rows[0].land, rows[0].casts, rows[0].mana, rows[0].life_after) == (
        "Island",
        ["Undulating Witness"],
        5,
        7,
    )
    assert rows[0].source_colors == ["G", "G", "G", "U", "U"]
    assert {"Undulating Witness", "Murmuring Volume"} <= set(rows[0].castable)


def test_action_keys_for_the_recorded_menu():
    result = _search(G1_T12)
    keys = {o.option_id: action_key(o, G1_T12) for o in actions_decision(G1_T12_MENU).options}
    assert keys == {"idx:1": ("cast", 217, None), "idx:4": ("cycle", 229), "idx:5": ISLAND, "pass": None}
    assert all(key in result.first_action for key in keys.values() if key is not None)
    # Copies of one card share a key: casting the second Witness is casting "Witness".
    decision = actions_decision([("idx:0", "Cast Undulating Witness", cast(304, 106272), True)])
    assert action_key(decision.options[0], BUG_135027) == ("cast", 240, None)


def test_evaluate_plan_checks_the_plan_against_its_own_mana():
    result = _search(G1_T12)
    rock = evaluate_plan(
        result,
        [
            {"turn": 12, "label": "T", "land": "Island", "cast": ["Murmuring Volume"], "attack": "", "hold": "", "mana": 5},
            {"turn": 14, "label": "T+1", "land": "", "cast": ["Archive Arbiter"], "attack": "", "hold": "", "mana": 5},
        ],
        G1_T12,
    )  # fmt: skip
    assert rock.issues == []
    assert [(b["label"], b["mana"]) for b in rock.budgets] == [
        ("T", 5),
        ("T+1", 6),
    ]  # the plan's own rock counts
    assert rock.budgets[1]["source_colors"] == ["G", "G", "G", "U", "U"]
    bad = evaluate_plan(
        result,
        [{"turn": 12, "label": "T", "land": "Island", "cast": ["Undulating Witness", "Archive Arbiter", "Twinned Vision"],
          "attack": "Undulating Witness", "hold": "", "mana": 5}],
        G1_T12,
    )  # fmt: skip
    assert "T: Undulating Witness can't attack the turn it is cast" in bad.issues
    assert any(issue.startswith("T: Archive Arbiter is unpayable") for issue in bad.issues)
    assert "T: Twinned Vision is not in hand" in bad.issues
    assert bad.line.steps[0].casts == ("Undulating Witness",) and not bad.line.steps[0].attack
    assert bad.line.score > rock.line.score  # Witness first beats the rock line, as the search says
    assert evaluate_plan(None, [], G1_T12).line is None


def test_proxy_outcome_replays_the_line_with_a_trick():
    result = _search(G1_T12)
    best, base = proxy_outcome(result, result.best), proxy_outcome(result, result.baseline)
    assert best.cls == ALIVE and base.cls == DEAD  # the greedy line has no slack for a +2/+0 trick
    assert best.steps[0].sig == result.best.steps[0].sig
    assert proxy_outcome(None, result.best) is None and proxy_outcome(result, None) is None
    calm = deepcopy(G1_T12)
    calm["zones"]["opponent_hand_count"] = 0  # no cards: no trick, the plain replay
    quiet = _search(calm)
    assert proxy_outcome(quiet, quiet.best).lives() == quiet.best.lives()


def test_posture_compares_attacking_and_holding_lines():
    assert _search(G1_T12).posture == "either"  # nothing of ours can attack
    result = _search(_two_creatures())
    assert result.posture in ("attack", "hold", "either")
    if result.posture != "either":
        assert result.posture_reason.startswith("attack with Theorix Metamage")


# --- deadlines and latency ------------------------------------------------------------


def _crowded() -> dict:
    crowded = deepcopy(G1_T14)
    for index in range(7):
        crowded["battlefield"].append(
            card(900 + index, "Cadet", 2, is_tapped=False, turn_entered_battlefield=13)
        )
        crowded["battlefield"].append(
            card(950 + index, "Theorix Metamage", 1, is_tapped=False, turn_entered_battlefield=13)
        )
    return crowded


def _worst_case() -> dict:
    """A 13-card hand (three land classes, modal, removal, flash) and 12 creatures a side."""
    source = deepcopy(G1_T12)
    names = [
        "Island", "Forest", "Mountain", "Undulating Witness", "Murmuring Volume", "Archive Arbiter",
        "Theorix Metamage", "Tetsuko Umezawa, Fugitive", "Unsummon", "Fulminous Forte", "Geist of Saint Thalia",
        "Fatehold Chronologist", "Sureshot Sower",
    ]  # fmt: skip
    source["hand"] = [card(700 + i, name, 1) for i, name in enumerate(names)]
    for index in range(6):
        source["battlefield"].append(
            card(800 + index, "Cadet", 2, is_tapped=False, turn_entered_battlefield=9)
        )
        source["battlefield"].append(
            card(850 + index, "Theorix Metamage", 1, is_tapped=False, turn_entered_battlefield=9)
        )
    for index in range(4):
        source["battlefield"].append(
            card(880 + index, "Mountain", 1, is_tapped=False, turn_entered_battlefield=3)
        )
        source["battlefield"].append(
            card(890 + index, "Island", 1, is_tapped=False, turn_entered_battlefield=3)
        )
    return source


@pytest.mark.parametrize("name", list(FIXTURES))
def test_real_boards_search_within_50_ms(name):
    result = _search(FIXTURES[name])
    assert result.elapsed_ms < 50, result.stats()


def test_crowded_board_searches_within_250_ms():
    started = time.perf_counter()
    result = _search(_crowded())
    assert (time.perf_counter() - started) * 1000 < 250 and not result.truncated


def test_worst_case_is_cut_off_by_the_deadlines():
    model = build_board_model(_worst_case())
    started = time.perf_counter()
    result = search_lines(model, survival=True, lethal_now=False)
    assert (time.perf_counter() - started) * 1000 < 400
    assert result.bounded or result.truncated
    json.dumps([line.as_payload() for line in result.lines])


def test_hard_deadline_returns_the_baseline_at_worst():
    result = _search(G1_T12, hard_ms=0.0)
    assert result.truncated and result.best is result.baseline and result.lines == [result.baseline]
    assert not result.exact_first_attack and not result.all_dead_at_first
    assert len(result.to_projections()) == 3


def test_soft_deadline_finishes_the_rest_greedily():
    result = _search(G1_T12, soft_ms=0.0)
    assert result.bounded and not result.truncated
    assert not result.exact_first_attack and not result.only_first_attack_survivor
    assert any(not line.complete for line in result.lines)
    assert (result.best.cls, result.best.timing, result.best.v) >= (
        result.baseline.cls,
        result.baseline.timing,
        result.baseline.v,
    )


def test_line_search_imports_no_llm_backend():
    """line_search's own import closure (package __init__ bypassed) has no backend."""
    code = (
        "import importlib.util, sys, types\n"
        "spec = importlib.util.find_spec('arenamcp')\n"
        "pkg = types.ModuleType('arenamcp')\n"
        "pkg.__path__ = list(spec.submodule_search_locations)\n"
        "sys.modules['arenamcp'] = pkg\n"
        "import arenamcp.line_search\n"
        "bad = [m for m in sys.modules if m.startswith(('arenamcp.backends', 'arenamcp.coach'))"
        " or m in ('requests', 'httpx', 'openai')]\n"
        "print(','.join(sorted(bad)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == ""


# --- 2026-10-07 review regressions ------------------------------------------------------------


def _plus(source: dict, *, lands: tuple[str, ...] = (), hand: tuple[str, ...] = (), first: int = 900) -> dict:
    """``source`` with untapped basics of ours and cards added to our hand."""
    result = deepcopy(source)
    local = result["local_seat_id"]
    for offset, name in enumerate(lands):
        result["battlefield"].append(
            card(first + offset, name, local, is_tapped=False, turn_entered_battlefield=1)
        )
    for offset, name in enumerate(hand, start=len(lands)):
        result["hand"].append(card(first + offset, name, local))
    return result


def _aimed(result, spell: str) -> set[str]:
    """Every target ``spell`` is aimed at in any line's steps."""
    return {
        target
        for line in _all_lines(result)
        for step in line.steps
        for name, target in step.targets
        if name == spell
    }


def test_restricted_removal_only_hits_legal_targets():
    # Surgical Precision kills only toughness >= 4: Heartstring Puller is a 3/1.
    result = _search(_plus(G1_T14, lands=("Plains",), hand=("Surgical Precision",)))
    assert "Heartstring Puller" not in _aimed(result, "Surgical Precision")
    assert result.best.cls == DEAD  # no fake survival from a free kill
    # Your Fate Ends Here needs mana value >= 3: never the 0-cost Cadet token.
    fate = _search(_plus(G1_T12_AFTER_VOLUME, lands=("Plains",), hand=("Your Fate Ends Here",)))
    assert "Cadet" not in _aimed(fate, "Your Fate Ends Here")
    # A fight needs a creature of ours (G1_T12: we have none) whose power reaches the target.
    prey = {"type_line": "Sorcery", "mana_cost": "{G}", "card_types": ["CardType_Sorcery"],
            "oracle_text": "Target creature you control fights target creature you don't control."}  # fmt: skip
    source = deepcopy(G1_T12)
    source["hand"].append({**card(990, "Unsummon", 1), "name": "Prey Upon", **prey})
    assert _aimed(_search(source), "Prey Upon") <= {"Cadet", "Paradox Shaper", "Fatehold Chronologist"}
    assert not any(step.targets for step in _search(source).best.steps[:1])
    charm = _search(_plus(G1_T12_AFTER_VOLUME, lands=("Plains",), hand=("Vigorbloom Charm",)))
    assert "hold Vigorbloom Charm" not in charm.best.steps[0].text()


def test_hexproof_creatures_are_never_targeted():
    # BUG_180436 moved to our turn 23 at 20 life: Ruric Thar has hexproof (bridge keywords).
    source = deepcopy(BUG_180436)
    source["turn"].update(turn_number=23, active_player=1, priority_player=1)
    for player in source["players"]:
        player["life_total"] = 20 if player["seat_id"] == 1 else player["life_total"]
    for entry in source["battlefield"]:
        entry["is_tapped"] = False if entry["controller_seat_id"] == 1 else entry["is_tapped"]
    source["hand"].append(bridge_card(990, 106283, "Extended Absence", 1, "hand"))
    result = _search(source)
    assert "Ruric Thar, Magecrusher" not in _aimed(result, "Extended Absence")
    assert _aimed(result, "Extended Absence")  # another creature still gets exiled


@pytest.mark.parametrize("name", ["Gideon's Memorial", "Identity Echo", "Way of the Warlord"])
def test_a_permanents_activated_or_granted_removal_is_not_cast_removal(name):
    color = {"Gideon's Memorial": "Plains"}.get(name, "Mountain")
    result = _search(_plus(G1_T12_AFTER_VOLUME, lands=(color,), hand=(name,)))
    assert not _aimed(result, name)
    assert result.best.steps[0].targets == ()
    t14 = _search(_plus(G1_T14, lands=(color,), hand=(name,)))
    assert t14.best.cls == DEAD  # without the card the honest result is dead too


def test_additional_costs_and_life_payments_are_paid():
    # Silence the Echo: {1}{B} plus "pay {3}" (the sacrifice is not modelled): 5 mana, not 2.
    model = build_board_model(_plus(G1_T14, lands=("Swamp",), hand=("Silence the Echo",)))
    assert next(s for s in model.spells if s.name == "Silence the Echo").mana_value == 5
    result = _search(_plus(G1_T14, lands=("Swamp",), hand=("Silence the Echo",)))
    assert not any(
        {"Archive Arbiter", "Silence the Echo"} <= set(line.steps[0].casts)
        for line in _all_lines(result)
        if line.steps
    )
    # Vraska's Final Mercy costs 2 life: at 2 life it is never cast.
    source = state(
        turn=10, active=1, phase="Phase_Main1", step="", life={1: 2, 2: 20}, lands_played={1: 1, 2: 0},
        library=20, opponent_hand=1,
        battlefield=[(10, "Island", 1, False, 1), (11, "Island", 1, False, 2), (12, "Swamp", 1, False, 3),
                     (13, "Swamp", 1, False, 4), (20, "Theorix Metamage", 1, False, 5), (30, "Cadet", 2, False, 7),
                     (31, "Cadet", 2, False, 7)],
        hand=[(40, "Vraska's Final Mercy")], graveyard=[],
    )  # fmt: skip
    mercy = _search(source)
    assert not any("Vraska's Final Mercy" in step.casts for line in _all_lines(mercy) for step in line.steps)
    # At 6 life it can be cast, and the 2 life is paid.
    rich = deepcopy(source)
    rich["players"][0]["life_total"] = 6
    paid = _search(rich)
    cast_line = next(
        line for line in _all_lines(paid) if line.steps and "Vraska's Final Mercy" in line.steps[0].casts
    )
    assert cast_line.steps[0].life_after is None or cast_line.steps[0].life_after <= 4


def _theorix_charm_during_their_attack(step: str) -> tuple[dict, object]:
    source = deepcopy(G3_T10_BLOCKS)
    source["turn"]["step"] = step
    source.pop("decision_context", None)
    source["players"][0]["life_total"] = 4
    source["battlefield"] = [c for c in source["battlefield"] if c["instance_id"] not in (256, 302)]
    source["stack"] = [card(995, "Theorix Charm", 1)]
    texts = [
        "Counter target noncreature spell unless its controller pays {o2}.",
        "Target creature gets -2/-2 until end of turn.",
        "Mill three cards, then draw a card.",
    ]
    meta = {"actionType": "CastingTimeOption", "choiceKind": "modal", "requestClass": "CastingTimeOption_ModalRequest",
            "childIndex": 0, "sourceId": 995, "min": 1, "max": 1}  # fmt: skip
    menu = [
        (f"idx:{i}", f"Mode {i + 1}: {text}", {**meta, "label": f"Mode {i + 1}", "optionIndex": i})
        for i, text in enumerate(texts)
    ]
    return source, modal_decision(menu, (995, 1))


def test_a_mode_that_removes_an_attacker_during_their_attack_stops_its_damage():
    source, decision = _theorix_charm_during_their_attack("Step_DeclareAttack")
    comparison = compare_modes(source, decision)
    assert comparison is not None
    lives = {oid: line.now_life for oid, line in comparison.lines.items()}
    assert lives["idx:1"] > 0 >= lives["idx:0"]  # -2/-2 on an attacker: it no longer deals damage
    assert comparison.lines["idx:1"].cls != DEAD or comparison.lines["idx:1"].dead_at > 1


def test_flash_blockers_cannot_join_blocks_already_being_declared():
    duelist = bridge_card(904, 106255, "Divining Duelist", 1, "hand")
    lands = [card(901 + i, name, 1, is_tapped=False, turn_entered_battlefield=2)
             for i, name in enumerate(("Swamp", "Island", "Swamp"))]  # fmt: skip
    blocks = deepcopy(G3_T10_BLOCKS)
    blocks["battlefield"] = [c for c in blocks["battlefield"] if c["instance_id"] != 256] + lands
    blocks["players"][0]["life_total"] = 2
    blocks["hand"].append(duelist)
    locked = _search(blocks)
    assert locked.dead_now and locked.dead_in == 1  # Step_DeclareBlock: blocks come before our priority
    before_blocks = deepcopy(blocks)
    before_blocks["turn"]["step"] = "Step_DeclareAttack"
    before_blocks.pop("decision_context", None)
    assert not _search(before_blocks).dead_now  # cast in declare attackers, the Duelist blocks
    # The unmodified fixture: the flash body changes no root block variant.
    plain = deepcopy(G3_T10_BLOCKS)
    flashy = deepcopy(G3_T10_BLOCKS)
    flashy["battlefield"] += lands
    flashy["hand"].append(duelist)

    def roots(source):
        return {line.block: line.now_life for line in _search(source).lines if line.steps}

    assert roots(flashy).get("crackback") == roots(plain).get("crackback")


def test_threshold_cast_conditions_are_respected():
    proft = bridge_card(991, 106480, "Proft, Sinister Mastermind", 2, "hand")
    source = deepcopy(BUG_174855)
    source["hand"].append(proft)
    model = build_board_model(source)
    assert model.our_graveyard == 6
    result = _search(source)
    assert not any(
        "Proft, Sinister Mastermind" in step.casts for line in _all_lines(result) for step in line.steps
    )
    assert all("Proft, Sinister Mastermind" not in step.castable for step in result.best.steps)
    seven = deepcopy(source)
    seven["graveyard"].append(bridge_card(992, 106279, "Break Under Pressure", 2, "graveyard"))
    assert build_board_model(seven).our_graveyard == 7
    assert any("Proft, Sinister Mastermind" in step.castable for step in _search(seven).best.steps)


def test_our_first_strike_damage_is_not_dealt_twice():
    source = state(
        turn=10, active=1, phase="Phase_Combat", step="Step_FirstStrikeDamage", life={1: 20, 2: 2},
        lands_played={1: 1, 2: 0}, library=20, opponent_hand=1,
        battlefield=[(10, "Island", 1, True, 1), (20, "Theorix Metamage", 1, True, 5), (30, "Cadet", 2, True, 7)],
        hand=[], graveyard=[],
    )  # fmt: skip
    source = amend(source, {20: {"is_attacking": True, "keywords": ["first strike"]}})
    result = _search(source)
    assert not (result.best.cls == WIN and result.best.win_turn == 10)
    assert result.posture != "lethal"


def test_typed_landcycling_fetches_its_own_basic():
    source = deepcopy(BUG_135027)
    for entry in source["hand"]:
        if entry["name"] == "Undulating Witness":
            entry["oracle_text"] = entry["oracle_text"].replace("Basic landcycling", "Forestcycling")
    result = _search(source)
    fetched = {
        land.name for line in _all_lines(result) if line._leaf is not None for land in line._leaf.lands
    }
    played = {step.land for line in _all_lines(result) for step in line.steps if step.land}
    assert "Island" not in fetched | played and "Forest" in played


def test_our_creature_spell_on_the_stack_enters_before_their_attack():
    model = build_board_model(G1_T14_ON_STACK)
    assert [body["name"] for body in model.our_stack_bodies] == ["Archive Arbiter"]
    with_stack = _search(G1_T14_ON_STACK)
    gone = deepcopy(G1_T14_ON_STACK)
    gone["stack"] = []
    without = _search(gone)
    assert with_stack.best.lives()[0] > without.best.lives()[0]  # the 4/4 flier blocks


def test_any_target_burn_can_go_face_for_lethal():
    shock = {"type_line": "Instant", "mana_cost": "{R}", "card_types": ["CardType_Instant"],
             "oracle_text": "Shock deals 2 damage to any target."}  # fmt: skip
    source = state(
        turn=10, active=1, phase="Phase_Main1", step="", life={1: 3, 2: 2}, lands_played={1: 1, 2: 0},
        library=20, opponent_hand=1,
        battlefield=[(10, "Mountain", 1, False, 2), (11, "Mountain", 1, False, 4), (12, "Mountain", 1, False, 6),
                     (20, "Heartstring Puller", 2, False, 7), (21, "Cadet", 2, False, 7)],
        hand=[], graveyard=[],
    )  # fmt: skip
    source["hand"].append({**card(30, "Unsummon", 1), "name": "Shock", **shock})
    result = _search(source)
    assert result.best.cls == WIN and result.best.win_turn == 10 and result.posture == "lethal"
    assert ("Shock", "opponent") in result.best.steps[0].targets
    assert result.first_action[("cast", 30, None)].cls == WIN


def test_restricted_search_is_deterministic_and_bounded_by_work_not_time():
    import threading

    from tests.strategic_states import SLOW_212608

    model = build_board_model(deepcopy(SLOW_212608))
    first = search_lines(model, survival=False, lethal_now=False, hard_ms=5000.0)
    assert first.bounded and not first.truncated
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            sum(range(1000))

    busy = [threading.Thread(target=spin, daemon=True) for _ in range(3)]
    for thread in busy:
        thread.start()
    try:  # three busy threads slow the search down; only the hard cap is a clock
        second = search_lines(model, survival=False, lethal_now=False, hard_ms=5000.0)
    finally:
        stop.set()
    assert second.bounded and not second.truncated
    assert (second.nodes, second.combats) == (first.nodes, first.combats)
    assert [line.summary() for line in second.lines] == [line.summary() for line in first.lines]


def _untruncated(source: dict):
    """A search the wall-clock hard cap didn't cut (a loaded test machine can hit it)."""
    for _attempt in range(3):
        result = _search(source)
        if not result.truncated:
            break
    return result


def test_hand_order_never_changes_the_pick():
    source = _plus(G1_T12, lands=("Island", "Island", "Forest"), hand=("Theorix Metamage", "Unsummon"))
    reference = _untruncated(source)
    for order in (list(reversed(source["hand"])), source["hand"][2:] + source["hand"][:2]):
        permuted = deepcopy(source)
        permuted["hand"] = deepcopy(order)
        result = _untruncated(permuted)
        assert result.best.summary() == reference.best.summary()
        assert result.baseline.summary() == reference.baseline.summary()
    content = [(line.block, line.steps[0].sig) for line in reference.lines if line.steps]
    assert len(content) == len(set(content))
    # The baseline's T step is one of the search's own groups, so close calls keep it.
    assert any(line.first_sig == reference.baseline.first_sig for line in reference.lines)


def test_compare_modes_runs_no_line_search_of_its_own_and_splits_the_budget(monkeypatch):
    def refuse(_state):
        raise AssertionError("compare_modes must not run the assessment's search")

    monkeypatch.setattr(ba, "assess", refuse)
    from arenamcp import line_search as ls

    budgets: list[int] = []
    original = ls._Search.__init__

    def record(self, *args, **kwargs):
        original(self, *args, **kwargs)
        budgets.append(self.budget)

    monkeypatch.setattr(ls._Search, "__init__", record)
    comparison = compare_modes(
        deepcopy(G1_T14_MODE_STATE), modal_decision(G1_T14_MODE_MENU, G1_T14_MODE_REQUEST_ID)
    )
    assert comparison is not None and comparison.complete
    assert len(budgets) >= 2 and len(set(budgets)) == 1  # every mode gets the same depth


def test_the_kill_switch_also_stops_mode_comparisons(monkeypatch):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    decision = modal_decision(G1_T14_MODE_MENU, G1_T14_MODE_REQUEST_ID)
    assert compare_modes(deepcopy(G1_T14_MODE_STATE), decision) is None


@pytest.mark.parametrize("name", ["SLOW_212608", "SLOW_202111"])
def test_the_slowest_real_boards_stay_within_the_latency_target(name):
    from tests import strategic_states

    source = getattr(strategic_states, name)
    times, stats = [], set()
    for _ in range(3):
        ba._CACHE.clear()
        assessment = ba.assess(deepcopy(source))
        times.append(assessment.elapsed_ms)
        if not assessment.search_stats["truncated"]:
            stats.add((assessment.search_stats["nodes"], assessment.search_stats["combats"]))
    assert len(stats) == 1  # the work budget, not the clock, decides where the search stops
    assert min(times) < 50, times
