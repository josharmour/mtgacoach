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

    monkeypatch.setattr(planner, "_llm_decision_options", answer, raising=False)
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
