"""Exact #350 cast/target choices: legally offered Forest cannot protect stack Notary."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.stack_protection import (
    correct_protection_claims,
    protection_facts,
    protection_prompt_note,
    review_protection_claim,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/stack_protection_350.json").read_text())


def context(kind="cast"):
    item = deepcopy(FIXTURE[kind])
    raw = item["decision"]
    raw["request_id"] = tuple(raw["request_id"])
    raw["options"] = tuple(DecisionOption(**o) for o in raw["options"])
    raw["slots"] = tuple(TargetSlot(**s) for s in raw["slots"])
    return PendingDecision(**raw), item["state"], item["chosen"], item["reasoning"]


@pytest.mark.parametrize("kind", ["cast", "target"])
def test_retained_false_protection_corrected_without_relabeling_legal_action(kind):
    decision, state, chosen, reason = context(kind)
    before = deepcopy(state)
    assert decision.selection_is_valid(chosen)
    facts = protection_facts(decision, state, chosen)
    assert facts and facts.spell_id == 684
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = reason
    planner._last_decision_trace = {}
    planner._note_false_claims(state, decision, chosen)
    actual = planner._last_decision_reasoning
    assert "not a battlefield permanent" in actual
    assert "not offered" in actual and "gain 2 life" in actual
    assert "counter fizzles" not in actual and "Serenade fizzles" not in actual
    assert planner._last_decision_trace["uncorrected_reasoning"] == reason
    assert chosen == FIXTURE[kind]["chosen"] and state == before


def test_typed_planner_adds_prompt_facts_and_reconsiders_cast_once():
    decision, state, chosen, reason = context()
    calls = []

    class Backend:
        def complete(self, system, user, **kwargs):
            calls.append(user)
            if "STACK PROTECTION CHECK:" in user:
                return json.dumps(
                    {
                        "option_ids": ["pass"],
                        "reasoning": "Pass; gaining two life is not worth this card here.",
                    }
                )
            return json.dumps({"option_ids": chosen, "reasoning": reason})

    planner = ActionPlanner(Backend(), timeout=2)
    assert planner.plan_decision_options(decision, state) == ["pass"]
    assert len(calls) == 2 and "STACK PROTECTION FACTS:" in calls[0]
    check = planner._last_decision_trace["stack_protection_check"]
    assert check["original_ids"] == chosen and check["original_reasoning"] == reason
    assert check["revised_ids"] == ["pass"]


@pytest.mark.parametrize("kind", ["cast", "target"])
def test_review_keeps_deliberate_life_gain_or_permanent_protection(kind):
    decision, state, chosen, reason = context(kind)
    trace = {}
    data = {"reasoning": reason}
    revised = {"reasoning": "Use Safekeeping to gain 2 life and protect Forest from removal."}
    ask = Mock(return_value="answer")
    parse = Mock(return_value=(revised, chosen))
    assert review_protection_claim(decision, state, data, chosen, ask, parse, "prompt", trace) == (
        revised,
        chosen,
    )
    ask.assert_called_once()
    assert ask.call_args.kwargs["budget"] == 12.0
    assert "Life gain or protection" in ask.call_args.args[0]


@pytest.mark.parametrize("failure", ["timeout", "invalid", "repeated"])
def test_review_failure_is_bounded_and_never_blanket_bans_real_life_gain(failure):
    decision, state, chosen, reason = context("target")
    data = {"reasoning": reason}
    trace = {}
    ask = Mock(side_effect=TimeoutError() if failure == "timeout" else None, return_value="answer")
    parse = Mock(return_value=(data, ["tgt:684"] if failure == "invalid" else chosen))
    revised, picked = review_protection_claim(decision, state, data, chosen, ask, parse, "prompt", trace)
    assert picked == chosen and decision.selection_is_valid(picked)
    assert "not a battlefield permanent" in revised["reasoning"]
    assert "gain 2 life" in revised["reasoning"]
    assert trace["uncorrected_reasoning"] == reason
    ask.assert_called_once()


@pytest.mark.parametrize(
    "reason",
    [
        "Safekeeping cannot stop the counterspell; use it only for life.",
        "If hexproof could protect spells, the counter would fizzle.",
        "Cast Safekeeping to gain 2 life.",
        "Casting Safekeeping triggers our counter ability, which protects Notary from Strix Serenade.",
        "Safekeeping protects Forest from the removal spell.",
        "Counterspell stops Strix Serenade; Safekeeping gains 2 life.",
        "Counterspell stops Strix Serenade and Safekeeping gains 2 life.",
        "Safekeeping gains 2 life and Counterspell stops Strix Serenade.",
        "Hold Safekeeping until Notary resolves, then protect it from removal.",
    ],
)
def test_negated_hypothetical_real_benefits_and_other_counterspells_survive(reason):
    decision, state, chosen, _ = context()
    assert correct_protection_claims(reason, decision, state, chosen) == (reason, [])
    ask = Mock()
    data = {"reasoning": reason}
    assert review_protection_claim(decision, state, data, chosen, ask, Mock(), "prompt", {}) == (data, chosen)
    ask.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "no_stack",
        "unknown_counter_rules",
        "unknown_link",
        "other_link",
        "own_counter",
        "permanent_victim",
        "ability_victim",
        "missing_controller",
        "missing_source",
        "wrong_source_group",
        "mixed_effect",
        "real_anticounter",
        "menu_offers_spell",
        "unknown_menu_target",
        "other_target_controller",
    ],
)
def test_missing_ambiguous_or_different_rules_abstain(change):
    decision, state, chosen, reason = context()
    source = state["hand"][0]
    counter = state["stack"][0]
    victim = state["stack"][1]
    if change == "no_stack":
        state["stack"] = []
    elif change == "unknown_counter_rules":
        counter["oracle_text"] = ""
    elif change == "unknown_link":
        counter.pop("targeting")
    elif change == "other_link":
        counter["targeting"] = [999]
    elif change == "own_counter":
        counter["controller_seat_id"] = 2
    elif change == "permanent_victim":
        state["battlefield"].append(victim)
    elif change == "ability_victim":
        victim["object_kind"] = "ABILITY"
    elif change == "missing_controller":
        victim.pop("controller_seat_id")
    elif change == "missing_source":
        state["hand"] = []
    elif change == "wrong_source_group":
        source["grp_id"] = 999
    elif change == "mixed_effect":
        source["oracle_text"] += " Spells you control cannot be countered this turn."
    elif change == "real_anticounter":
        source["oracle_text"] = "Target spell you control cannot be countered."
    elif change in {"menu_offers_spell", "unknown_menu_target"}:
        option = decision.options[0]
        meta = deepcopy(option.meta)
        meta["targets"][0]["targets"].append(
            {"targetInstanceId": 684 if change == "menu_offers_spell" else 999}
        )
        decision = replace(decision, options=(replace(option, meta=meta), decision.options[1]))
    elif change == "other_target_controller":
        state["battlefield"][0]["controller_seat_id"] = 1
    assert protection_facts(decision, state, chosen) is None
    assert correct_protection_claims(reason, decision, state, chosen) == (reason, [])


def test_target_request_source_mismatch_abstains():
    decision, state, chosen, reason = context("target")
    decision = replace(decision, source_instance_id=999)
    assert not protection_prompt_note(decision, state)
    assert correct_protection_claims(reason, decision, state, chosen) == (reason, [])


def test_real_removal_targeting_battlefield_is_not_counterspell_confusion():
    decision, state, chosen, reason = context()
    state["stack"][0].update(oracle_text="Destroy target creature.", targeting=[546])
    assert not protection_facts(decision, state, chosen)


def test_offered_targets_exclude_stack_spell_but_forest_is_legal():
    decision, state, chosen, _ = context("target")
    assert decision.selection_is_valid(["tgt:683"])
    assert not decision.selection_is_valid(["tgt:684"])
    assert {o.meta["instanceId"] for o in decision.options} == {546, 553, 657, 664, 670, 683}


@pytest.mark.parametrize("change", ["duplicate_board", "source_also_stack", "second_counter"])
def test_contradictory_zones_or_ambiguous_threats_abstain(change):
    decision, state, chosen, reason = context()
    if change == "duplicate_board":
        state["battlefield"].append(deepcopy(state["battlefield"][0]))
    elif change == "source_also_stack":
        state["stack"].append(deepcopy(state["hand"][0]))
    else:
        state["stack"].append(dict(state["stack"][0], instance_id=999))
    assert correct_protection_claims(reason, decision, state, chosen) == (reason, [])


def test_parenthetical_anticounter_effect_is_not_discarded():
    decision, state, chosen, reason = context()
    state["hand"][0]["oracle_text"] += " (Spells you control cannot be countered.)"
    assert correct_protection_claims(reason, decision, state, chosen) == (reason, [])


@pytest.mark.parametrize("failure", ["timeout", "invalid", "repeated"])
def test_unsupported_protection_cast_passes_on_failed_reconsideration(failure):
    decision, state, chosen, reason = context()
    data = {"option_ids": chosen, "reasoning": reason}
    ask = Mock(side_effect=TimeoutError() if failure == "timeout" else None)
    parse = Mock(return_value=(data, ["tgt:684"] if failure == "invalid" else chosen))
    trace = {}
    revised, picked = review_protection_claim(decision, state, data, chosen, ask, parse, "", trace)
    assert revised["option_ids"] == picked == ["pass"]
    assert revised["reasoning"].startswith("Pass")
    assert trace["uncorrected_reasoning"] == reason
    ask.assert_called_once()


@pytest.mark.parametrize(
    "extra",
    [
        " I need the two life to survive the next attack.",
        " Protect Forest from the removal spell as well.",
    ],
)
def test_failed_recheck_preserves_an_independent_cast_reason(extra):
    decision, state, chosen, reason = context()
    data = {"option_ids": chosen, "reasoning": reason + extra}
    revised, picked = review_protection_claim(
        decision, state, data, chosen, Mock(side_effect=TimeoutError), Mock(), "", {}
    )
    assert revised["option_ids"] == picked == chosen
    assert extra.strip() in revised["reasoning"]


def test_failed_recheck_never_synthesizes_a_missing_pass():
    decision, state, chosen, reason = context()
    decision = replace(decision, can_pass=False, options=(decision.options[0],))
    _, picked = review_protection_claim(
        decision, state, {"reasoning": reason}, chosen, Mock(side_effect=TimeoutError), Mock(), "", {}
    )
    assert picked == chosen
