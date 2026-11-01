"""Exact Memory Lapse replacement report; hand-bounce cases are equivalent controls."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.commander_choice import commander_zone_note
from arenamcp.decisions import DecisionOption, PendingDecision


@pytest.fixture
def replay():
    saved = json.loads((Path(__file__).parent / "fixtures/commander_replacement_20261101.json").read_text())
    raw = saved["pending_decision"]
    decision = PendingDecision(
        **{
            **raw,
            "request_id": tuple(raw["request_id"]),
            "options": tuple(DecisionOption(**o) for o in raw["options"]),
        }
    )
    return decision, saved["state"], saved


def planner(*responses):
    backend = Mock()
    backend.complete.side_effect = [
        r if isinstance(r, Exception) else json.dumps({"option_ids": [r[0]], "reasoning": r[1]})
        for r in responses
    ]
    return ActionPlanner(backend, separate_strategy_planning=True), backend


def test_exact_report_is_top_library_not_hand_and_tax_is_seven_vs_five(replay):
    decision, state, saved = replay
    assert saved["request"]["prompt"]["promptId"] == 135
    assert decision.prompt_text == "Put The Notary Hobbits in the command zone or in your library?"
    assert saved["result_command_cost"]["manaCost"] == [
        {"color": ["ManaColor_Generic"], "count": 5, "abilityGrpId": 115},
        {"color": ["ManaColor_Green"], "count": 2, "abilityGrpId": 115},
    ]
    note = commander_zone_note(decision, state)
    assert "top of your library" in note
    assert "not a return to hand" in note
    assert "from hand: {3}{G}{G}; from command: {5}{G}{G}" in note
    assert "1 previous command-zone cast(s), +{2} tax" in note
    assert "relocation does not itself increase commander tax" in note
    assert "normal draw trigger applies in either branch" in note


@pytest.mark.parametrize(
    "choice,reason",
    [
        ("optional:decline", "Put it on top; draw it next turn and recast for five instead of seven mana."),
        (
            "optional:accept",
            "Pay the two extra mana for command access while preserving our next draw for another card.",
        ),
    ],
)
def test_report_accept_without_cost_comparison_gets_one_bounded_review(replay, choice, reason):
    decision, state, saved = replay
    p, backend = planner(("optional:accept", saved["reasoning"]), (choice, reason))
    assert p.plan_decision_options(decision, state) == [choice]
    assert backend.complete.call_count == 2
    assert "COMMANDER REPLACEMENT COMPARISON" in backend.complete.call_args_list[0].args[1]
    assert "top of your library" in backend.complete.call_args_list[1].args[1]


def hand_case(replay):
    decision, state, saved = replay
    state = deepcopy(state)
    state["battlefield"].append(state["stack"][0])
    state["stack"] = []
    prompt = "Put The Notary Hobbits in the command zone or in your hand?"
    state["decision_context"]["prompt"] = prompt
    decision = replace(
        decision,
        prompt_text=prompt,
        options=tuple(replace(o, meta={**o.meta, "promptText": prompt}) for o in decision.options),
    )
    return decision, state


def test_hand_destination_gets_cost_preference_without_becoming_forced_choice(replay):
    decision, state = hand_case(replay)
    note = commander_zone_note(decision, state)
    assert "decline sends it to your hand" in note
    assert "When hand is cheaper, prefer that recast unless a concrete hand-risk" in note
    p, backend = planner(
        ("optional:accept", "Command protects it from the revealed discard spell despite the tax.")
    )
    assert p.plan_decision_options(decision, state) == ["optional:accept"]
    assert backend.complete.call_count == 1
    p, backend = planner(("optional:decline", "Keep it in hand to recast for five without command tax."))
    assert p.plan_decision_options(decision, state) == ["optional:decline"]
    assert backend.complete.call_count == 1


def test_repeated_or_failed_valuation_review_does_not_force_a_zone(replay):
    decision, state, saved = replay
    for second in (TimeoutError("budget exhausted"), ("optional:accept", saved["reasoning"])):
        p, backend = planner(("optional:accept", saved["reasoning"]), second)
        assert p.plan_decision_options(decision, state) == ["optional:accept"]
        assert backend.complete.call_count == 2


def test_tax_uses_actual_cast_count_and_unknown_is_not_assumed_one(replay):
    decision, state = hand_case(replay)
    state["commander_casts"]["103511"] = 3
    assert "from command: {9}{G}{G}" in commander_zone_note(decision, state)
    assert "+{6} tax" in commander_zone_note(decision, state)
    state["commander_casts"] = {}
    note = commander_zone_note(decision, state)
    assert "do not assume a two-mana difference" in note
    assert "from command:" not in note
    state["commander_casts"]["103511"] = 0
    p, backend = planner(("optional:accept", "Put it in command for access."))
    assert p.plan_decision_options(decision, state) == ["optional:accept"]
    assert backend.complete.call_count == 1


@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "wrong_recipient",
        "not_commander",
        "opponent",
        "token",
        "duplicate",
        "graveyard",
        "unrecognized",
    ],
)
def test_context_needs_exact_request_owned_real_commander_and_explicit_destination(replay, case):
    decision, state, saved = replay
    if case == "stale":
        decision = replace(decision, request_id=(209, 293))
    elif case == "wrong_recipient":
        state["decision_context"]["recipient_ids"] = [999]
    elif case == "not_commander":
        state["commander_grp_ids"] = []
    elif case == "opponent":
        state["stack"][0]["owner_seat_id"] = 1
    elif case == "token":
        state["stack"][0]["object_kind"] = "TOKEN"
    elif case == "duplicate":
        state["battlefield"].append(deepcopy(state["stack"][0]))
    elif case == "graveyard":
        state["graveyard"].append(state["stack"].pop(0))
    else:
        state["decision_context"]["prompt"] = "Move a card?"
    assert commander_zone_note(decision, state) == ""


def test_library_position_not_inferred_from_unrelated_or_missing_effect(replay):
    decision, state, saved = replay
    state["stack"][1]["oracle_text"] = "Counter target spell. Shuffle it into its owner's library."
    note = commander_zone_note(decision, state)
    assert "placement/recovery timing is not established" in note
    assert "top of your library" not in note
