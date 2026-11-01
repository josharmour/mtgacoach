"""#329 retained Hydra action/pass conflict; no strategic preference asserted."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from arenamcp.decision_claims import action_claim_mismatch
from arenamcp.decision_consistency import consistent_action, explicit_current_pass_conclusion
from arenamcp.decisions import DecisionOption, PendingDecision


def retained():
    record = json.loads((Path(__file__).parent / "fixtures/live_329/1f24194a-hydra.json").read_text())
    raw = record["pending_decision"]
    decision = PendingDecision(
        **{
            **raw,
            "request_id": tuple(raw["request_id"]),
            "options": tuple(DecisionOption(**o) for o in raw["options"]),
        }
    )
    # Action-identity reproduction only; the original full planner state is
    # not reconstructed or required to identify Cast versus explicit Pass.
    return decision, {"local_seat_id": 1, "turn": record["turn"], "stack": []}, record


def test_full_retained_hydra_reason_rechecks_even_without_source_enrichment():
    decision, state, record = retained()
    reason = record["trace"]["reasoning"]
    assert reason.endswith("so pass.")
    assert explicit_current_pass_conclusion(reason)
    issue = action_claim_mismatch(decision, state, record["chosen_options"], reason)
    assert issue and "Goldvein Hydra" in issue.reason
    assert "does not establish which strategic choice is better" in issue.reason
    assert consistent_action(decision, state, record["chosen_options"], reason)[0] == "pass"


@pytest.mark.parametrize(
    "reason",
    [
        "Pass.",
        "We should pass priority now.",
        "I choose pass.",
        "I prefer passing.",
        "Let's pass and hold mana.",
        "The better play is to pass.",
        "Hydra is not worth the mana, therefore pass.",
        "Cast Goldvein Hydra for a body; however, holding the mana is stronger, so pass.",
        "Cast Goldvein Hydra. Instead, pass priority.",
        "We used the other spell; thus we should pass.",
        "So pass. Goldvein Hydra enters with X counters.",
        "If we cast Goldvein Hydra first, the rest of the mana is wasted. Pass priority.",
        "Do not cast Goldvein Hydra first. Pass priority.",
    ],
)
def test_affirmative_current_pass_conclusions_are_independent_of_card_name(reason):
    assert explicit_current_pass_conclusion(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "Do not pass.",
        "Don't pass; cast Goldvein Hydra.",
        "We should not pass.",
        "Never choose pass.",
        "If no other play is useful, pass.",
        "We could pass.",
        "Perhaps pass.",
        "Should we pass?",
        "Pass if nothing changes.",
        "After casting Goldvein Hydra, pass.",
        "Cast Goldvein Hydra, then pass.",
        "Cast Goldvein Hydra; then pass.",
        "Next turn, pass.",
        "We should pass next turn.",
        "Pass later.",
        "Pass the turn, but first cast Goldvein Hydra.",
        "Pass the turn; but first cast Goldvein Hydra.",
        "Pass; then cast Goldvein Hydra.",
        "We should pass no priority until Hydra is cast.",
        "Pass only when Hydra has resolved.",
        "We should pass, but not until Hydra resolves.",
        "Pass; however, cast Goldvein Hydra for a hasty attacker.",
        "Pass on Kogla and cast Goldvein Hydra.",
        "Pass up Kogla and cast Goldvein Hydra.",
        "Cast Goldvein Hydra now; pass priority to resolve it.",
        "Cast Goldvein Hydra now; pass priority.",
        "Cast Goldvein Hydra first. Pass priority.",
        "Cast Goldvein Hydra now, then retain the rest of the mana. Pass priority.",
        "We should cast Goldvein Hydra before combat; pass priority.",
        "Hold it and pass; instead cast Goldvein Hydra now.",
        "Consider passing, but instead cast Goldvein Hydra for a hasty attacker.",
        "Pass; instead cast Goldvein Hydra for a hasty attacker.",
        "Cast Goldvein Hydra now and hold Kogla for next turn.",
        'The old advice said "so pass"; cast Goldvein Hydra now.',
        "The old advice said 'so pass'; cast Goldvein Hydra now.",
        "Passing is worse than developing a creature.",
        "No reason to pass.",
    ],
)
def test_negation_hypotheses_questions_future_sequences_and_later_casts_are_preserved(reason):
    assert not explicit_current_pass_conclusion(reason)
    decision, state, record = retained()
    assert action_claim_mismatch(decision, state, record["chosen_options"], reason) is None
    assert consistent_action(decision, state, record["chosen_options"], reason) is None


def test_earlier_cast_does_not_globally_veto_final_pass_with_known_source():
    decision, state, record = retained()
    state["hand"] = [{"instance_id": 249, "grp_id": 90513, "name": "Goldvein Hydra", "type_line": "Creature"}]
    reason = "Cast Goldvein Hydra for a body; holding mana is better, so pass."
    assert action_claim_mismatch(decision, state, record["chosen_options"], reason)


def test_final_boundary_does_not_require_empty_stack_to_identify_contradiction():
    decision, state, record = retained()
    state["stack"] = [{"instance_id": 800, "controller_seat_id": 2, "type_line": "Instant"}]
    assert (
        consistent_action(decision, state, record["chosen_options"], record["trace"]["reasoning"])[0]
        == "pass"
    )


def test_no_offered_pass_does_not_create_an_unavailable_option():
    decision, state, record = retained()
    decision = replace(decision, options=tuple(o for o in decision.options if o.option_id != "pass"))
    assert consistent_action(decision, state, record["chosen_options"], "So pass.") is None
    assert action_claim_mismatch(decision, state, record["chosen_options"], "So pass.") is None
