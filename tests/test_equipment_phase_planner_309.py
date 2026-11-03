"""#309 planner hop: a Main2 equip's attack promise is replaced, other payoffs kept.

The retained log (8b7bed14 T12) is not bundled here; these are equivalent board
regressions built from the ledger row "phase lost across equipment selection".
The phase-aware prompt note and the claim check are merged; this pins the hop
that stores the corrected reasoning for a chosen equip.
"""

import pytest
from tests.test_activation_intent_309 import game, request

from arenamcp.action_planner import ActionPlanner

ATTACK = "Equip Greaves so the Notary Hobbits can attack now."


def corrected(phase="Phase_Main2", reasoning=ATTACK):
    state = game()
    state["turn"]["phase"] = phase
    for card in state["battlefield"]:
        if card["instance_id"] == 734:
            card["summoning_sickness"] = True
    decision = request(state, "ActionsAvailable")
    planner = ActionPlanner(None)
    planner._last_decision_reasoning = reasoning
    planner._last_decision_trace = {}
    planner._note_false_claims(state, decision, ["idx:0"])
    return planner


def test_main2_equip_attack_promise_is_replaced_with_the_timing_fact():
    planner = corrected()
    assert "attack now" not in planner._last_decision_reasoning
    assert "Rules correction:" in planner._last_decision_reasoning
    assert "attack declaration window has passed" in planner._last_decision_reasoning
    assert planner._last_decision_trace["uncorrected_reasoning"] == ATTACK
    assert planner._last_decision_trace["false_claims"]


@pytest.mark.parametrize(
    "reasoning",
    [
        "Equip Greaves so the Notary Hobbits can tap for mana.",
        "Equip Greaves for shroud protection.",
        "Equip Greaves so it can attack next turn.",
        "Equip Greaves; combat has passed so this is only for shroud.",
    ],
)
def test_supported_payoffs_and_future_attacks_stay_as_written(reasoning):
    planner = corrected(reasoning=reasoning)
    assert planner._last_decision_reasoning == reasoning
    assert planner._last_decision_trace == {}


def test_main1_equip_attack_promise_is_left_alone():
    planner = corrected("Phase_Main1")
    assert planner._last_decision_reasoning == ATTACK
    assert planner._last_decision_trace == {}
