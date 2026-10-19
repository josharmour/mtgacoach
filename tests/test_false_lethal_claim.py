"""A "lethal this turn" claim the board math contradicts is narrated as the math (bug_20261009_080525).

08:05:01: "putting the +1/+1 counter on Marwyn, the Preserver makes total
attacking power 6, exactly lethal against their 6 life this turn" — Marwyn, the
Clearcutter was tapped from its loot, so 4 power attacked into 6 life.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.strategic_states import actions_decision, cast

from arenamcp import board_assessment
from arenamcp.action_planner import ActionPlanner

CLAIM = (
    "Putting the +1/+1 counter on Marwyn, the Preserver makes total attacking power 6, "
    "exactly lethal against their 6 life this turn."
)


def _planner(monkeypatch, reasoning: str, facts) -> ActionPlanner:
    planner = ActionPlanner.__new__(ActionPlanner)

    def answer(decision, game_state):
        planner._last_decision_reasoning = reasoning
        planner._last_decision_option_ids = ["idx:0"]
        return ["idx:0"]

    monkeypatch.setattr(planner, "_llm_decision_options", answer)
    monkeypatch.setattr("arenamcp.action_planner.filter_play_options", lambda d, s: d)
    monkeypatch.setattr(board_assessment, "assess", lambda state: facts)
    return planner


def _decision():
    return actions_decision(
        [("idx:0", "Cast Tam's Resistance", cast(501), True), ("pass", "Pass", None, None)]
    )


def _facts(**kw):
    base = dict(lethal_now=False, posture="attack", lines=[], opp_life=6)
    base.update(kw)
    return SimpleNamespace(**base)


def test_false_lethal_claim_is_replaced_by_the_board_math(monkeypatch):
    line = SimpleNamespace(win_at=2, win_turn=27)
    planner = _planner(monkeypatch, CLAIM, _facts(lines=[line]))
    chosen = planner.plan_decision_options(_decision(), {"turn": {}})
    assert chosen == ["idx:0"]  # the play stands; only the story changes
    reasoning = planner.get_decision_reasoning(chosen)
    assert "exactly lethal" not in reasoning
    assert reasoning == "Board math: not lethal this turn (they are at 6; the best line is lethal on T27)."
    assert planner.get_last_decision_trace()["false_lethal_claim"] == [CLAIM]


def test_true_lethal_claim_is_kept(monkeypatch):
    planner = _planner(monkeypatch, CLAIM, _facts(lethal_now=True))
    chosen = planner.plan_decision_options(_decision(), {"turn": {}})
    assert planner.get_decision_reasoning(chosen) == CLAIM
    assert "false_lethal_claim" not in planner.get_last_decision_trace()


@pytest.mark.parametrize(
    "reasoning",
    [
        "Deploying Marwyn advances the planned clock, saving No Admittance for exact lethal next turn.",
        "Casting Garruk now sets up a lethal attack on T27 while the opponent is tapped out.",
        "Both Marwyns swing toward lethal; nothing is castable otherwise.",
    ],
)
def test_claims_about_later_turns_are_left_alone(monkeypatch, reasoning):
    planner = _planner(monkeypatch, reasoning, _facts())
    chosen = planner.plan_decision_options(_decision(), {"turn": {}})
    assert planner.get_decision_reasoning(chosen) == reasoning


def test_other_sentences_survive_the_correction(monkeypatch):
    text = "Pump the Preserver before combat. " + CLAIM
    planner = _planner(monkeypatch, text, _facts())
    chosen = planner.plan_decision_options(_decision(), {"turn": {}})
    assert planner.get_decision_reasoning(chosen) == (
        "Pump the Preserver before combat. Board math: not lethal this turn (they are at 6)."
    )


# --- 2026-10-19: only OUR lethal THIS turn is a claim ----------------------------

MARSHAL = (
    "Dawnwing Marshal alone is lethal next attack and can't be burned down with 3 mana, so race with "
    "maximum face damage now — cast Lightning Strike to the face, leaving {R} to deploy Fleeting Effigy "
    "as a second hasty attacker this turn."
)


@pytest.mark.parametrize(
    "reasoning",
    [
        # match 41753907 T13: the opponent's lethal, not ours.
        MARSHAL,
        "We're dead to their lethal crackback regardless, so converting mana into 3 face damage with "
        "Boltwave advances the only realistic path—burn reach—while a land drop doesn't affect this turn.",
        "Deploy the first Fleeting Effigy now so both haste attackers come down this turn per the T12 "
        "lethal line; Boltwave is better saved as face reach since opponents sit at 8.",
        "Playing the fourth Mountain follows the lethal line without spending mana, keeping Strike banked "
        "as reach and leaving burst options open this turn.",
        "With 1 mana and no enemy creatures, a second hasty Fleeting Effigy swings for 2 this turn versus "
        "Tectonic Hazard's single face damage, advancing the lethal race.",
        "Playing the Mountain first unlocks a second red mana so I can cast a Fleeting Effigy this turn and "
        "attack alongside Slickshot, advancing the lethal-on-T10 line.",
        "Attacking is not lethal this turn, so the blockers stay home.",
    ],
)
def test_reasoning_that_claims_no_lethal_of_ours_this_turn_is_left_alone(monkeypatch, reasoning):
    planner = _planner(monkeypatch, reasoning, _facts(opp_life=15))
    chosen = planner.plan_decision_options(_decision(), {"turn": {}})
    assert planner.get_decision_reasoning(chosen) == reasoning
    assert "false_lethal_claim" not in planner.get_last_decision_trace()
    remembered = planner._memory().this_turn({"turn": {}})[-1].reasoning
    assert reasoning.startswith(remembered)  # the model's words, capped at 40


@pytest.mark.parametrize(
    "reasoning",
    [
        "Cast Boltwave first (opponent to 5), leaving exactly {R} to also cast Fleeting Effigy, then attack "
        "with all three hasty creatures for 6 — lethal this turn.",
        "Strike to face drops them to 1, and the pumped flying Slickshot plus Hired Claw's attack ping "
        "finishes lethal this turn.",
    ],
)
def test_our_lethal_this_turn_is_still_checked(monkeypatch, reasoning):
    planner = _planner(monkeypatch, reasoning, _facts(opp_life=8))
    chosen = planner.plan_decision_options(_decision(), {"turn": {}})
    assert planner.get_decision_reasoning(chosen) == "Board math: not lethal this turn (they are at 8)."
    assert planner.get_last_decision_trace()["false_lethal_claim"] == [reasoning]


def test_decision_memory_keeps_the_original_reasoning_after_a_correction(monkeypatch):
    line = SimpleNamespace(win_at=2, win_turn=27)
    planner = _planner(monkeypatch, CLAIM, _facts(lines=[line]))
    planner.plan_decision_options(_decision(), {"turn": {}})
    remembered = planner._memory().this_turn({"turn": {}})[-1].reasoning
    correction = "Board math: not lethal this turn (they are at 6; the best line is lethal on T27)."
    assert remembered == f"{CLAIM} {correction}"
    # A long reasoning keeps its opening words (the target, the sequencing) and the correction.
    long_claim = "Cast Lightning Strike to the face " + "and keep swinging " * 12 + "for exactly lethal now."
    planner = _planner(monkeypatch, long_claim, _facts())
    planner.plan_decision_options(_decision(), {"turn": {}})
    remembered = planner._memory().this_turn({"turn": {}})[-1].reasoning
    assert remembered.startswith("Cast Lightning Strike to the face")
    assert remembered.endswith("Board math: not lethal this turn (they are at 6).")
    assert len(remembered.split()) <= 40
