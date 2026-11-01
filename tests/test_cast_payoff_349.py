"""#349: evaluate a payable public cast trigger without forcing its cast."""

import json
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.cast_payoffs import cast_payoff_concern, cast_payoff_note, colored_exile_payoffs
from arenamcp.decisions import decision_from_dict


@pytest.fixture
def saved():
    return json.loads((Path(__file__).parent / "fixtures/ugin_payoff_349.json").read_text())


def test_exact_request_has_payable_cast_and_public_colored_candidates(saved):
    decision = decision_from_dict(saved["decision"])
    assert decision.request_id == (206, 264)
    assert decision.find("idx:1").payable is True
    assert decision.find("idx:1").meta["hasAutoTap"]
    payoff = colored_exile_payoffs(decision, saved["state"])[0]
    assert {c["instance_id"] for c in payoff[3]} == {494, 476, 497, 460}
    note = cast_payoff_note(decision, saved["state"])
    assert "before the permanent" in note and "unmodelled search effect is not zero" in note
    assert "+2: You gain 3 life and draw a card" in note
    assert "0: Add {C}{C}{C}" in note
    assert "not selectable while it is in hand" in note
    assert "upfront cost" in note
    assert cast_payoff_concern(decision, saved["state"], ["pass"], saved["reason"])


@pytest.mark.parametrize(
    "choice,reason",
    [
        ("idx:1", "Cast Ugin for its cast-trigger exile of Dáin and then reassess the loyalty menu."),
        (
            "pass",
            "Ugin's exile has value, but tapping the animated Forest and commander gives up needed blockers.",
        ),
    ],
)
def test_false_payoff_pass_gets_one_review_with_original_budget(saved, monkeypatch, choice, reason):
    backend = Mock()
    calls = []

    def complete(*args, **kwargs):
        calls.append((args[1], kwargs["request_timeout_s"]))
        if len(calls) == 1:
            time.sleep(0.04)
            return json.dumps({"option_ids": ["pass"], "reasoning": saved["reason"]})
        return json.dumps({"option_ids": [choice], "reasoning": reason})

    backend.complete.side_effect = complete
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    monkeypatch.setattr(planner, "_sized_budget", lambda *args: 1.0)
    assert planner.plan_decision_options(decision_from_dict(saved["decision"]), saved["state"]) == [choice]
    assert len(calls) == 2
    assert 0 < calls[1][1] < calls[0][1] - 0.03
    assert "id:494" in calls[0][0] and "SECOND LOOK" in calls[1][0]
    assert "IMMEDIATE CAST PAYOFF" in planner._last_decision_trace["second_look"]["concerns"][0]


@pytest.mark.parametrize(
    "reason",
    [
        "Ugin is too costly to commit now; preserve our blockers.",
        "Ugin can exile Dáin, but keeping our creatures untapped is more important.",
        "Finale has no immediate payoff; keep Ugin for next turn.",
        "Ugin's cast trigger has removal value even if it is countered, but hold interaction.",
    ],
)
def test_payment_or_strategic_tradeoff_is_not_called_false(saved, reason):
    assert not cast_payoff_concern(decision_from_dict(saved["decision"]), saved["state"], ["pass"], reason)


@pytest.mark.parametrize(
    "case",
    [
        "unpayable",
        "unknown_payability",
        "activation",
        "enters_trigger",
        "future_cast_trigger",
        "unknown_source",
        "colorless",
        "unknown_colors",
        "our_permanents",
        "phased_out",
        "shroud",
        "ward",
        "global_protection",
    ],
)
def test_does_not_invent_current_cast_value_or_unprotected_targets(saved, case):
    state = saved["state"]
    decision = decision_from_dict(saved["decision"])
    option = decision.find("idx:1")
    if case == "unpayable":
        option = replace(option, payable=False)
    elif case == "unknown_payability":
        option = replace(option, payable=None)
    elif case == "activation":
        option.meta["actionType"] = "ActionType_Activate"
    elif case in {"enters_trigger", "future_cast_trigger"}:
        state["hand"][0]["oracle_text"] = state["hand"][0]["oracle_text"].replace(
            "When you cast this spell",
            "When this permanent enters"
            if case == "enters_trigger"
            else "Whenever you cast a colorless spell",
            1,
        )
    elif case == "unknown_source":
        state["hand"] = []
    elif case == "global_protection":
        state["battlefield"].append(
            {
                "instance_id": 999,
                "controller_seat_id": 1,
                "oracle_text": "Other enchantments you control have shroud.",
            }
        )
    else:
        for card in state["battlefield"]:
            if card["controller_seat_id"] != 1:
                continue
            if case == "colorless":
                card["colors"] = []
            elif case == "unknown_colors":
                card.pop("colors", None)
            elif case == "our_permanents":
                card["controller_seat_id"] = 2
            elif case == "phased_out":
                card["is_phased_out"] = True
            else:
                card["oracle_text"] += "\nShroud" if case == "shroud" else "\nWard {2}"
    decision = replace(
        decision, options=tuple(option if o.option_id == option.option_id else o for o in decision.options)
    )
    assert not cast_payoff_note(decision, state)
    assert not cast_payoff_concern(decision, state, ["pass"], saved["reason"])


def test_rule_driven_not_a_card_name_override(saved):
    saved["state"]["hand"][0]["name"] = "A Different Cast-Trigger Card"
    decision = decision_from_dict(saved["decision"])
    assert "A Different Cast-Trigger Card" in cast_payoff_note(decision, saved["state"])
    assert not cast_payoff_concern(decision, saved["state"], ["idx:0"], "No immediate payoff.")


def test_lands_are_not_colored_because_they_produce_colored_mana(saved):
    for card in saved["state"]["battlefield"]:
        if card["controller_seat_id"] == 1 and "CardType_Land" not in card["card_types"]:
            card["colors"] = []
    assert not cast_payoff_note(decision_from_dict(saved["decision"]), saved["state"])


def test_indestructible_does_not_prevent_exile(saved):
    for card in saved["state"]["battlefield"]:
        card["keywords"] = ["indestructible"]
    assert len(colored_exile_payoffs(decision_from_dict(saved["decision"]), saved["state"])[0][3]) == 4


def test_card_identity_does_not_override_current_color(saved):
    for card in saved["state"]["battlefield"]:
        card["colors"] = []
        card["color_identity"] = ["W"]
        card["mana_cost"] = "{W}"
    assert not cast_payoff_note(decision_from_dict(saved["decision"]), saved["state"])


def test_repeated_false_claim_does_not_become_accepted_memory(saved):
    backend = Mock()
    backend.complete.return_value = json.dumps({"option_ids": ["pass"], "reasoning": saved["reason"]})
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    assert planner.plan_decision_options(decision_from_dict(saved["decision"]), saved["state"]) == ["pass"]
    assert backend.complete.call_count == 2  # No unbounded semantic retries.
    assert "no immediate payoff" not in planner._last_decision_reasoning
    assert "did not justify" in planner._last_decision_reasoning
    assert planner._last_decision_trace["cast_payoff_claim"]["uncorrected_reasoning"] == saved["reason"]
