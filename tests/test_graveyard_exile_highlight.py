"""Arena's Cold cue must not veto a deliberate Survival resource choice."""

import pytest
from tests.test_targeting_fallback_decline import _decision, _planner, _state

from arenamcp.action_planner import DECLINE_DECISION

SURVIVAL = (
    "Survival — At the beginning of your second main phase, if this creature is tapped, "
    "exile up to one target card from a graveyard."
)


def case():
    planner = _planner()
    state = _state(own_ids=(334,), their_ids=(), oracle=SURVIVAL)
    state["graveyard"] = state.pop("battlefield")
    state["battlefield"] = []
    state["graveyard"][0]["name"] = "Veteran Survivor"
    planner._last_decision_target_controllers = {"tgt:334": "self"}
    planner._last_decision_unusual_targets = {}
    planner._target_highlights = lambda *args: {334: "Cold"}
    return planner, state, _decision([334])


def test_recorded_survival_self_graveyard_choice_is_kept_without_extra_exception_field():
    planner, state, decision = case()
    assert planner._gate_harmful_llm_target_picks(decision, state, ["tgt:334"]) == ["tgt:334"]


@pytest.mark.parametrize("bad_context", ["unknown_zone", "battlefield", "stale_duplicate", "wrong_intent"])
def test_graveyard_exception_does_not_hide_zone_or_controller_errors(bad_context):
    planner, state, decision = case()
    if bad_context == "unknown_zone":
        state["graveyard"] = []
    elif bad_context == "battlefield":
        state["battlefield"] = state.pop("graveyard")
    elif bad_context == "stale_duplicate":
        state["battlefield"] = list(state["graveyard"])
    else:
        planner._last_decision_target_controllers = {"tgt:334": "opponent"}
    assert planner._gate_harmful_llm_target_picks(decision, state, ["tgt:334"]) != ["tgt:334"]


def test_unrelated_exile_does_not_get_graveyard_resource_exception():
    planner, state, decision = case()
    state["stack"][0]["oracle_text"] = "Exile target creature."
    assert not planner._is_graveyard_exile_target(state, 334, "Exile target creature.")
    assert planner._gate_harmful_llm_target_picks(decision, state, ["tgt:334"]) == [DECLINE_DECISION]
