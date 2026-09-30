"""Counted removal must preserve enemy picks and submit the complete target set."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.autopilot import AutopilotEngine
from arenamcp.decisions import (
    TargetSlot,
    assign_target_slots,
    build_pending_decision,
    expand_target_selection,
    submit_option,
)
from arenamcp.target_effects import target_effect_is_harmful


@pytest.mark.parametrize("verb", ["Exile", "Destroy", "Counter", "Return", "Sacrifice", "Fights"])
@pytest.mark.parametrize("quantity", ["", "two ", "up to three ", "any number of ", "another ", "X "])
def test_counted_harm_is_not_misclassified_as_a_buff(verb, quantity):
    assert target_effect_is_harmful(f"{verb} {quantity}target permanents.") is True


@pytest.mark.parametrize(
    ("oracle", "expected"),
    [
        ("", None),
        ("Choose two target permanents.", None),
        ("Target creature gains hexproof until end of turn.", False),
        ("Put a +1/+1 counter on target creature.", False),
        ("Target creature gets +2/+2 until end of turn.", False),
        ("Exile target creature, then return it to the battlefield under its owner's control.", False),
        ("Exile target creature. Target creature gets +2/+2.", None),
        ("Choose one —\n• Exile two target permanents.\n• Target creature gains hexproof.", None),
        ("Target creature you control fights target creature you don't control.", None),
        ("This deals 3 damage to up to two target creatures.", True),
    ],
)
def test_only_known_unambiguous_effects_have_polarity(oracle, expected):
    assert target_effect_is_harmful(oracle) is expected


def counted_decision():
    candidates = [{"targetInstanceId": instance_id} for instance_id in (10, 11, 20, 30)]
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectTargets",
            "source_card": "Ulamog, the Ceaseless Hunger",
            "target_candidates": candidates,
            "target_selections": [{"targetIdx": 1, "minTargets": 2, "maxTargets": 2, "targets": candidates}],
        }
    )


def removal_state(oracle="When you cast this spell, exile two target permanents."):
    return {
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2}],
        "battlefield": [
            {"instance_id": 10, "controller_seat_id": 1, "power": 1},
            {"instance_id": 11, "controller_seat_id": 1, "power": 2},
            {"instance_id": 20, "controller_seat_id": 2, "type_line": "Land"},
            {"instance_id": 30, "controller_seat_id": 2, "type_line": "Land"},
        ],
        "stack": [{"instance_id": 900, "name": "Ulamog, the Ceaseless Hunger", "oracle_text": oracle}],
        "decision_context": {"source_id": 900},
    }


def planner_with_picks(picks):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._llm_decision_options = lambda *args: picks
    return planner


def test_enemy_lands_chosen_for_counted_exile_reach_bridge_unchanged():
    decision = counted_decision()
    planner = planner_with_picks(["tgt:20", "tgt:30"])
    picked = planner.plan_decision_options(decision, removal_state())
    submitted = []
    bridge = SimpleNamespace(submit_targets=lambda ids: submitted.append(ids) or True)
    assert picked == ["tgt:20", "tgt:30"]
    assert submit_option(bridge, decision, picked)
    assert submitted == [[20, 30]]


def test_counted_removal_own_picks_are_redirected_to_enemies():
    planner = planner_with_picks(["tgt:10", "tgt:11"])
    assert planner.plan_decision_options(counted_decision(), removal_state()) == ["tgt:20", "tgt:30"]


def test_optional_counted_exile_keeps_enemy_target():
    decision = replace(
        counted_decision(), min_select=0, max_select=1, slots=(TargetSlot(1, 0, 1, 0, (10, 20)),)
    )
    state = removal_state(
        "When you cast this spell, exile up to one target permanent that's one or more colors."
    )
    assert planner_with_picks(["tgt:20"]).plan_decision_options(decision, state) == ["tgt:20"]


def test_unknown_effect_keeps_model_enemy_choices_instead_of_inventing_a_buff():
    planner = planner_with_picks(["tgt:20", "tgt:30"])
    state = removal_state("Choose two target permanents.")
    assert planner.plan_decision_options(counted_decision(), state) == ["tgt:20", "tgt:30"]


def test_unknown_effect_does_not_blindly_fallback_to_own_first_option():
    planner = planner_with_picks([])
    assert planner.plan_decision_options(counted_decision(), removal_state("Unknown effect.")) == [
        DECLINE_DECISION
    ]


@pytest.mark.parametrize("choices", [["tgt:20"], ["tgt:20", "tgt:20"]])
def test_incomplete_or_duplicate_plan_is_not_silently_filled(choices):
    decision = counted_decision()
    assert expand_target_selection(decision, choices) == []
    bridge = SimpleNamespace(submit_targets=lambda ids: pytest.fail("must not submit partial targets"))
    assert not submit_option(bridge, decision, choices)
    assert planner_with_picks(choices).plan_decision_options(decision, removal_state()) == [DECLINE_DECISION]


def test_fallback_with_too_few_enemies_declines_instead_of_filling_with_own():
    decision = counted_decision()
    state = removal_state()
    state["battlefield"][-1]["controller_seat_id"] = 1
    assert planner_with_picks([]).plan_decision_options(decision, state) == [DECLINE_DECISION]


def test_overlapping_slots_are_matched_without_adding_unrequested_targets():
    slots = (TargetSlot(1, 1, 1, 0, (20, 30)), TargetSlot(2, 1, 1, 0, (20,)))
    assert assign_target_slots(slots, [20, 30]) == [[30], [20]]
    assert assign_target_slots(slots, [20]) is None


def test_optional_slots_preserve_explicit_choices_and_zero_minimum():
    slot = TargetSlot(1, 0, 2, 0, (20, 30))
    assert assign_target_slots((slot,), [20, 30]) == [[20, 30]]
    assert assign_target_slots((slot,), []) == [[]]
    decision = replace(counted_decision(), slots=(slot,), min_select=0)
    assert expand_target_selection(decision, ["tgt:20", "tgt:30"]) == [20, 30]


def test_satisfied_slots_are_not_reselected_or_toggled():
    slots = (TargetSlot(1, 1, 1, 1, (10,)), TargetSlot(2, 2, 2, 1, (20, 30)))
    assert assign_target_slots(slots, [30]) == [[], [30]]
    assert assign_target_slots(slots, [10, 30]) is None


def test_typed_choices_exclude_unselectable_and_already_selected_targets():
    candidates = [
        {"targetInstanceId": 10, "legalAction": "Unselect"},
        {"targetInstanceId": 20, "legalAction": "Select"},
        {"targetInstanceId": 30, "legalAction": "None"},
    ]
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectTargets",
            "target_candidates": candidates,
            "target_selections": [
                {
                    "targetIdx": 1,
                    "minTargets": 0,
                    "maxTargets": 2,
                    "selectedTargets": 1,
                    "targets": candidates,
                }
            ],
        }
    )
    assert decision.min_select == 0
    assert decision.max_select == 1
    assert decision.option_ids() == {"tgt:20"}
    assert decision.slots[0].candidate_ids == (20,)


@pytest.mark.parametrize("oracle", ["Exile up to two target permanents.", "Unknown effect."])
def test_single_own_candidate_never_auto_selected_for_counted_removal_or_unknown(oracle):
    engine = AutopilotEngine.__new__(AutopilotEngine)
    state = removal_state(oracle)
    state["_bridge_last_poll"] = {"has_pending": True, "target_candidates": [{"targetInstanceId": 10}]}
    assert engine._pick_single_target_candidate(state) is None
