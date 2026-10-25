"""A contextual hostile blink must not silently become a friendly mana-rock blink."""

import pytest
from tests.test_targeting_fallback_decline import _decision, _planner, _state

from arenamcp.action_planner import DECLINE_DECISION

ORACLE = "Exile target artifact or creature, then return it to the battlefield under its owner's control with a +1/+1 counter on it."


@pytest.mark.parametrize("highlight", ["", "Hot", "Cold"])
def test_fenrir_choice_never_silently_becomes_signet(highlight):
    p = _planner()
    state = _state(own_ids=(645,), their_ids=(674,), oracle=ORACLE)
    p._target_highlights = lambda *args: {674: highlight}
    state["battlefield"][0]["name"] = "Arcane Signet"
    state["battlefield"][1]["name"] = "Summon: Fenrir"
    p._targeting_fallback_pick = lambda *args: pytest.fail("must not retarget onto Signet")
    assert p._gate_harmful_llm_target_picks(_decision([645, 674]), state, ["tgt:674"]) == [DECLINE_DECISION]


def test_explicit_opponent_token_blink_remains_available():
    p = _planner()
    state = _state(own_ids=(645,), their_ids=(674,), oracle=ORACLE)
    p._last_decision_target_controllers = {"tgt:674": "opponent"}
    p._last_decision_unusual_targets = {"tgt:674": "Exile their token; it cannot return."}
    assert p._gate_harmful_llm_target_picks(_decision([645, 674]), state, ["tgt:674"]) == ["tgt:674"]


def test_own_blink_is_kept():
    p = _planner()
    state = _state(own_ids=(645,), their_ids=(674,), oracle=ORACLE)
    assert p._gate_harmful_llm_target_picks(_decision([645, 674]), state, ["tgt:645"]) == ["tgt:645"]
