"""#328 typed false reserve, distinct from a ward trigger already on the stack.

The original 5958263f/49a7cff3 packets are unavailable. These factual regressions
exercise the current typed boundary that previously accepted a false promise.
"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from tests.test_tactical_timing_backlog import board, card, cast, menu
from tests.test_typed_decision_context import _CapturingBackend

from arenamcp.action_planner import ActionPlanner, SecondLookError
from arenamcp.resource_intent import removal_reservation_conflict

REASON = "Cast Development, keeping enough mana for Answer on Warded engine this turn."


@pytest.mark.parametrize(("mana", "ward"), [(4, 3), (3, 1)])
def test_typed_reserve_accounts_for_development_before_full_ward_cost(mana, ward):
    state = board(mana, ward)
    original = deepcopy(state)
    why = removal_reservation_conflict(menu(cast(20), cast(30)), state, ["idx:20"], REASON)
    assert "ward mana" in why and "cannot both be funded" in why
    assert state == original


def test_affordable_lines_and_explicit_development_tradeoffs_stand():
    decision = menu(cast(20), cast(30))
    assert not removal_reservation_conflict(decision, board(6, 3), ["idx:20"], REASON)
    assert not removal_reservation_conflict(
        decision,
        board(4, 3),
        ["idx:20"],
        "Develop now, accepting that Answer on Warded engine must wait until our next turn.",
    )
    assert not removal_reservation_conflict(
        decision, board(4, 3), ["idx:20"], "Hold Answer for later while developing."
    )
    assert not removal_reservation_conflict(
        decision,
        board(4, 3),
        ["idx:20"],
        "Do not keep mana for Answer on Warded engine; pressure is more important.",
    )
    assert not removal_reservation_conflict(
        decision, board(4, 3), ["idx:20"], "If removal is needed, keep mana for Answer on Warded engine."
    )


def test_ambiguous_or_illegal_target_and_unknown_mana_abstain():
    for change in ("namesake", "hexproof", "mana", "refund", "future_source", "ward_answer", "stack"):
        state = board(4, 3)
        if change == "namesake":
            state["battlefield"].append({**state["battlefield"][-1], "instance_id": 91})
        elif change == "hexproof":
            state["battlefield"][-1]["oracle_text"] += "\nHexproof"
        elif change == "mana":
            state["battlefield"].append(
                card(
                    40,
                    "Multiplier",
                    "If you tap a land for mana, it produces twice that much mana.",
                    "Enchantment",
                )
            )
        elif change == "refund":
            state["hand"][0]["oracle_text"] = "When this creature enters, add {W}{W}."
        elif change == "future_source":
            state["hand"][0]["oracle_text"] = (
                "When this creature enters, search your library for a basic land and put it onto the battlefield."
            )
        elif change == "ward_answer":
            state["hand"][0]["oracle_text"] = "When this creature enters, destroy target enchantment."
        else:
            state["stack"] = [card(50, "Unrelated spell", "", "Instant", seat=2)]
        assert not removal_reservation_conflict(menu(cast(20), cast(30)), state, ["idx:20"], REASON)


def test_current_offered_price_and_free_cast_are_used():
    state = board(4, 3)
    cheaper = cast(20, manaCost=[{"color": ["Generic"], "count": 0}])
    assert not removal_reservation_conflict(menu(cheaper, cast(30)), state, ["idx:20"], REASON)
    state["hand"][0]["oracle_text"] = "Vigilance"
    free = cast(20, alternativeGrpId=149, alternativeCostText="Cast without paying mana cost", manaCost=None)
    assert not removal_reservation_conflict(menu(free, cast(30)), state, ["idx:20"], REASON)


def test_recheck_can_keep_development_with_supported_changed_priority():
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    revised_reason = "Develop now and delay Answer; the mana cannot cover its ward this turn."
    ask = Mock(return_value=json.dumps({"option_ids": ["idx:20"], "reasoning": revised_reason}))

    def parse(raw):
        data = json.loads(raw)
        return data, data["option_ids"]

    data, picks = planner._recheck_resource_claim(
        menu(cast(20), cast(30)), board(4, 3), {"reasoning": REASON}, ["idx:20"], ask, parse, ""
    )
    assert picks == ["idx:20"] and data["reasoning"] == revised_reason
    assert ask.call_count == 1


def test_repeated_false_reserve_cannot_be_committed_as_a_valid_plan(monkeypatch):
    backend = _CapturingBackend(json.dumps({"option_ids": ["idx:20"], "reasoning": REASON}))
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *a, **kw: "")
    with pytest.raises(SecondLookError):
        planner._llm_decision_options(menu(cast(20), cast(30)), board(4, 3))
    assert sum("RESOURCE RESERVATION RECHECK" in p for p in backend.prompts) == 1
    assert planner._last_decision_trace["resource_reservation_recheck"]["fallback"] == "current_board"
