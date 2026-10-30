"""Actual targeted-exile window, no model/network/live game needed."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.stack_responses import protective_pass, shield_responses


def situation():
    heroic = {
        "instance_id": 1004,
        "name": "Heroic Intervention",
        "type_line": "Instant",
        "mana_cost": "{1}{G}",
        "oracle_text": "Permanents you control gain hexproof and indestructible until end of turn.",
    }
    ugin = {
        "instance_id": 1005,
        "name": "Ugin, Eye of the Storms",
        "type_line": "Legendary Planeswalker — Ugin",
        "controller_seat_id": 2,
        "loyalty": 9,
    }
    threat = {
        "name": "Abstruse Appropriation",
        "controller_seat_id": 1,
        "targeting": [1005],
        "oracle_text": "Exile target nonland permanent.",
    }
    state = {
        "local_seat_id": 2,
        "turn": {"priority_player": 2, "turn_number": 22},
        "stack": [threat],
        "hand": [heroic],
        "battlefield": [
            ugin,
            {"type_line": "Land — Forest", "controller_seat_id": 2, "is_tapped": False},
            {
                "type_line": "Land",
                "controller_seat_id": 2,
                "is_tapped": False,
                "oracle_text": "{T}: Add {C}.",
            },
        ],
    }
    decision = PendingDecision(
        (522, 703),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Heroic Intervention",
                True,
                {"actionType": "ActionType_Cast", "instanceId": 1004},
            ),
            DecisionOption("pass", "Pass"),
        ),
        can_pass=True,
    )
    return state, decision


def test_heroic_saves_ugin_from_targeted_exile_with_actual_payable_menu():
    state, decision = situation()
    (response,) = shield_responses(state)
    assert response.answers == ("Heroic Intervention",) and response.fizzles
    assert protective_pass(decision, state, ["pass"])[0] == "idx:0"
    # Arena payment is authoritative, even if estimated sources are missing.
    state["battlefield"] = state["battlefield"][:1]
    assert protective_pass(decision, state, ["pass"])[0] == "idx:0"


@pytest.mark.parametrize(
    "case",
    [
        "no_priority",
        "unpayable",
        "not_offered",
        "gone",
        "untargeted_exile",
        "no_stack",
        "own_spell",
        "loyalty_loss",
        "indestructible_exile",
        "creatures_only",
        "conditional",
        "extra_cost",
        "not_pass",
    ],
)
def test_no_blind_or_illegal_protection(case):
    state, d = situation()
    chosen = ["pass"]
    if case == "no_priority":
        state["turn"]["priority_player"] = 1
    elif case == "unpayable":
        d = replace(d, options=(replace(d.options[0], payable=False), d.options[1]))
    elif case == "not_offered":
        d = replace(d, options=(d.options[1],))
    elif case == "gone":
        state["battlefield"] = state["battlefield"][1:]
    elif case == "untargeted_exile":
        state["stack"][0].update(targeting=[], oracle_text="Exile all nonland permanents.")
    elif case == "no_stack":
        state["stack"] = []
    elif case == "own_spell":
        state["stack"][0]["controller_seat_id"] = 2
    elif case == "loyalty_loss":
        state["hand"][0]["oracle_text"] = "Permanents you control gain indestructible until end of turn."
        state["stack"][0]["oracle_text"] = "This deals 9 damage to target planeswalker."
    elif case == "indestructible_exile":
        state["hand"][0]["oracle_text"] = "Permanents you control gain indestructible until end of turn."
    elif case == "creatures_only":
        state["hand"][0]["oracle_text"] = "Creatures you control gain hexproof until end of turn."
    elif case == "conditional":
        state["hand"][0]["oracle_text"] = (
            "If you control a Dragon, permanents you control gain hexproof until end of turn."
        )
    elif case == "extra_cost":
        state["hand"][0]["oracle_text"] = (
            "As an additional cost, sacrifice a creature. Permanents you control gain hexproof until end of turn."
        )
    elif case == "not_pass":
        chosen = ["idx:0"]
    assert protective_pass(d, state, chosen) is None


def test_indestructibility_stops_destroy_without_falsely_claiming_illegal_target():
    state, d = situation()
    state["hand"][0]["oracle_text"] = "Permanents you control gain indestructible until end of turn."
    state["stack"][0]["oracle_text"] = "Destroy target planeswalker."
    (response,) = shield_responses(state)
    assert not response.fizzles
    assert "destruction" in protective_pass(d, state, ["pass"])[1]


def test_pass_guard_is_applied_before_answer_is_cached_or_remembered(monkeypatch):
    state, d = situation()
    p = ActionPlanner.__new__(ActionPlanner)
    p._plan_decision_options = Mock(return_value=["pass"])
    p._last_decision_reasoning = "Exile cannot be stopped."
    p._last_decision_trace = {}
    p.last_llm_failure = ""
    p._cache_answer = Mock()
    p._note_resolved_burn = Mock()
    p._memory = Mock(return_value=Mock())
    p._remember_cast_intent = Mock()
    p._hold_fates = Mock(return_value=Mock())
    p.get_decision_reasoning = Mock(return_value="protected")
    p.get_last_decision_trace = Mock(return_value={})
    assert p.plan_decision_options(d, state) == ["idx:0"]
    assert p._cache_answer.call_args.args[2] == ["idx:0"]
    assert p._last_decision_trace["protection_guard"]["with"] == "idx:0"
