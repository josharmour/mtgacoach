"""#344: Oct 31 match 9f593806, request [508, 673], Drana return.

The saved raw transition is graveyard 889 -> battlefield 1081, owner/controller
seat 1; local seat 2. This establishes a false benefit claim, not a worse pick.
"""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, select_n_note
from arenamcp.selection_claims import correct_return_claims, opponent_return_note

PROMPT = "Choose a nonlegendary creature card for your opponent to return to the battlefield."
REASON = (
    "Gifted Aetherborn is the best defensive body: deathtouch/lifelink blocks and trades "
    "with their attackers, gaining life to survive the next swing."
)
CARD = {
    "name": "Gifted Aetherborn",
    "oracle_text": "Deathtouch\nLifelink",
    "type_line": "Creature — Aetherborn Vampire",
    "mana_cost": "{B}{B}",
    "cmc": 2,
}


def decision():
    return PendingDecision(
        request_id=(508, 673),
        request_type="SelectN",
        source_instance_id=973,
        prompt_text=PROMPT,
        options=(
            DecisionOption("sel:889", "Gifted Aetherborn", meta={"card": dict(CARD)}),
            DecisionOption(
                "sel:859",
                "Bloodthirsty Conqueror",
                meta={
                    "card": {
                        "name": "Bloodthirsty Conqueror",
                        "type_line": "Creature — Vampire Knight",
                        "oracle_text": "Flying\nDeathtouch\nWhenever an opponent loses life, you gain that much life.",
                    }
                },
            ),
        ),
    )


def state():
    return {
        "local_seat_id": 2,
        "turn": {
            "turn_number": 17,
            "active_player": 1,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [{"seat_id": 2, "is_local": True, "life_total": 6}, {"seat_id": 1, "life_total": 27}],
        "graveyard": [dict(CARD, instance_id=889, owner_seat_id=1, controller_seat_id=1)],
        "battlefield": [],
        "hand": [],
        "stack": [],
    }


def test_exact_recorded_false_claim_removed_and_pick_kept_through_typed_planner():
    prompts = []

    class Backend:
        def complete(self, system, user, **kwargs):
            prompts.append(user)
            return json.dumps({"option_ids": ["sel:889"], "reasoning": REASON})

    planner = ActionPlanner(Backend(), timeout=1)
    assert planner.plan_decision_options(decision(), state()) == ["sel:889"]
    assert "RETURN RECIPIENT:" in prompts[0]
    assert "under the OPPONENT's control" in prompts[0]
    assert "their attackers" not in planner._last_decision_reasoning
    assert "Gifted Aetherborn returns under the opponent's control" in planner._last_decision_reasoning
    assert planner._last_decision_trace["uncorrected_reasoning"] == REASON
    assert planner._last_decision_trace["false_claims"]
    assert planner._last_decision_trace["reasoning"] == planner._last_decision_reasoning


@pytest.mark.parametrize("owner,controller", [(1, 1), (1, 2), (2, 1), (2, 2), (None, None)])
def test_graveyard_owner_and_last_controller_do_not_override_explicit_recipient(owner, controller):
    snapshot = state()
    snapshot["graveyard"][0].update(owner_seat_id=owner, controller_seat_id=controller)
    assert "under the OPPONENT's control" in select_n_note(decision(), snapshot)
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = REASON
    planner._last_decision_trace = {}
    planner._note_false_claims(snapshot, decision(), ["sel:889"])
    assert "under the opponent's control" in planner._last_decision_reasoning


@pytest.mark.parametrize(
    "prompt",
    [
        "",
        "Choose a creature card.",
        "Choose a creature card from your opponent's graveyard.",
        "Return a creature card from your opponent's graveyard to the battlefield under your control.",
        "Choose a creature card for your opponent to return to the battlefield under your control.",
    ],
)
def test_ambiguous_or_local_return_is_not_inferred_from_ownership_or_old_prompt(prompt):
    current = replace(decision(), prompt_text=prompt)
    snapshot = state()
    snapshot["decision_context"] = {"request_id": [507, 672], "prompt_text": PROMPT}
    assert opponent_return_note(current) == ""
    assert "RETURN RECIPIENT:" not in select_n_note(current, snapshot)
    assert correct_return_claims(REASON, current, ["sel:889"]) == (REASON, [])


@pytest.mark.parametrize(
    "text",
    [
        "Gifted Aetherborn gives them a lifelink creature but is weaker than Bloodthirsty Conqueror.",
        "Gifted Aetherborn is not our blocker; its lifelink benefits them.",
        "If we steal Gifted Aetherborn, it becomes our blocker.",
        "Gifted Aetherborn would be our blocker if we gained control of it.",
        "Our blockers are weak. Gifted Aetherborn is the least dangerous choice.",
        "Gifted Aetherborn threatens our blockers but is the smallest choice.",
    ],
)
def test_correct_conditional_and_independent_claims_survive(text):
    assert correct_return_claims(text, decision(), ["sel:889"]) == (text, [])


def test_preserves_independent_choice_comparison_and_raw_rejected_claim():
    text = REASON + " Bloodthirsty Conqueror is a larger flying threat."
    corrected, notes = correct_return_claims(text, decision(), ["sel:889"])
    assert corrected.startswith("Bloodthirsty Conqueror is a larger flying threat.")
    assert len(notes) == 1
    assert "best defensive body" not in corrected


def test_zone_change_does_not_reuse_graveyard_id_or_past_selection_context():
    snapshot = state()
    snapshot["graveyard"] = []
    snapshot["battlefield"] = [dict(CARD, instance_id=1081, owner_seat_id=1, controller_seat_id=1)]
    snapshot["decision_context"] = {"request_id": [508, 673], "prompt_text": PROMPT}
    current = replace(
        decision(), request_id=(509, 674), prompt_text="Choose blockers.", request_type="DeclareBlockers"
    )
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = "Gifted Aetherborn is their creature; pass blocks."
    planner._last_decision_trace = {}
    planner._note_false_claims(snapshot, current, [])
    assert planner._last_decision_reasoning == "Gifted Aetherborn is their creature; pass blocks."
    assert planner._last_decision_trace == {}
