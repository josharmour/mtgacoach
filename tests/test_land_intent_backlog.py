"""#303: the recorded 97308bae req67/91 choice, plus Adeline WW payment.

Public facts from task-2/monitoring/97308bae-land-{candidate,raw}.json:
T4, one land, offered Plains159/161 and Pass280; selected idx7 promising
untapped Pass + Voice. Raw zone transition280->292 proves tapped entry.
This is a minimal factual regression, not a reconstructed full game snapshot.
"""

import json
from copy import deepcopy
from unittest.mock import Mock

from tests.test_tactical_timing_backlog import board, card, menu

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption
from arenamcp.land_intent import land_claim_mismatch

REASON = "Play Sundown Pass untapped to enable Voice of Victory this turn, the strongest modeled line."


def recorded_board():
    state = board(0)
    state["turn"]["turn_number"] = 4
    state["players"][0]["lands_played"] = 0
    state["battlefield"] = [card(281, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain")]
    state["hand"] = [
        card(159, "Plains", "{T}: Add {W}.", "Basic Land — Plains"),
        card(161, "Plains", "{T}: Add {W}.", "Basic Land — Plains"),
        card(
            280,
            "Sundown Pass",
            "This land enters tapped unless you control two or more other lands.\n{oT}: Add {oR} or {oW}.",
            "Land",
        ),
        card(200, "Voice of Victory", "", "Creature", "{1}{W}"),
    ]
    decision = menu(
        *(
            DecisionOption(
                f"idx:{i}",
                f"Play land: {c['name']}",
                meta={"actionType": "ActionType_Play", "instanceId": c["instance_id"]},
            )
            for i, c in zip((5, 6, 7), state["hand"], strict=False)
        )
    )
    return state, decision


def test_exact_live_choice_is_refuted_and_both_plains_satisfy_claim():
    state, decision = recorded_board()
    issue = land_claim_mismatch(decision, state, ["idx:7"], REASON)
    assert "enters tapped" in issue.reason and "Voice of Victory" in issue.reason
    assert issue.alternatives == ("idx:5", "idx:6")
    assert land_claim_mismatch(decision, state, ["idx:5"], REASON.replace("Sundown Pass", "Plains")) is None


def test_deliberate_tapped_land_and_later_untapped_pass_are_preserved():
    state, decision = recorded_board()
    assert (
        land_claim_mismatch(
            decision, state, ["idx:7"], "Play Sundown Pass tapped now for next turn's fixing."
        )
        is None
    )
    assert (
        land_claim_mismatch(
            decision, state, ["idx:7"], "Sundown Pass cannot enter untapped; develop it anyway."
        )
        is None
    )
    state["battlefield"].append(deepcopy(state["hand"][0]))
    assert land_claim_mismatch(decision, state, ["idx:7"], REASON) is None


def test_adeline_double_white_requires_distinct_white_sources():
    state, decision = recorded_board()
    state["battlefield"].append(deepcopy(state["hand"][0]))
    state["hand"][2] = card(280, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain")
    state["hand"][3] = card(200, "Adeline, Resplendent Cathar", "Vigilance", "Creature", "{1}{W}{W}")
    issue = land_claim_mismatch(
        decision, state, ["idx:7"], "Play Mountain to cast Adeline, Resplendent Cathar this turn."
    )
    assert issue and issue.alternatives == ("idx:5", "idx:6")


def test_unknown_cost_reducer_does_not_prove_payment_failure():
    state, decision = recorded_board()
    state["battlefield"].append(
        card(500, "Reducer", "Creature spells you cast cost {1} less to cast.", "Artifact")
    )
    assert (
        land_claim_mismatch(
            decision, state, ["idx:7"], "Play Sundown Pass to cast Voice of Victory this turn."
        )
        is None
    )


def test_failed_recheck_repairs_land_but_a_truthful_hold_can_stand():
    state, decision = recorded_board()
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}

    def parse(raw):
        data = json.loads(raw)
        return data, data["option_ids"]

    data, picks = planner._repair_land_claim(
        decision,
        state,
        {"reasoning": REASON},
        ["idx:7"],
        Mock(side_effect=RuntimeError("offline")),
        parse,
        "",
    )
    assert picks == ["idx:5"] and "Play land: Plains" in data["reasoning"]
    ask = Mock(
        return_value=json.dumps(
            {"option_ids": ["idx:7"], "reasoning": "Develop Sundown Pass tapped for next turn's fixing."}
        )
    )
    data, picks = planner._repair_land_claim(
        decision, state, {"reasoning": REASON}, ["idx:7"], ask, parse, ""
    )
    assert picks == ["idx:7"] and "tapped" in data["reasoning"]


def test_typed_decision_repairs_recorded_choice(monkeypatch):
    from tests.test_typed_decision_context import _CapturingBackend

    state, decision = recorded_board()
    backend = _CapturingBackend(json.dumps({"option_ids": ["idx:7"], "reasoning": REASON}))
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *a, **kw: "")
    assert planner._llm_decision_options(decision, state) == ["idx:5"]
    assert sum("LAND PAYMENT RECHECK" in p for p in backend.prompts) == 1


def test_basic_rules_can_be_intrinsic_and_shockland_choice_is_unknown():
    state, decision = recorded_board()
    state["hand"][0]["oracle_text"] = ""
    state["battlefield"][0]["oracle_text"] = ""
    issue = land_claim_mismatch(decision, state, ["idx:7"], REASON)
    assert issue.alternatives == ("idx:5", "idx:6")
    state["hand"][2]["oracle_text"] = (
        "As this land enters, you may pay 2 life. If you don't, it enters tapped."
    )
    assert land_claim_mismatch(decision, state, ["idx:7"], REASON) is None


def test_land_notes_show_distinct_double_white_access():
    from arenamcp.land_intent import land_payment_notes

    state, decision = recorded_board()
    state["battlefield"].append(deepcopy(state["hand"][0]))
    state["hand"][2] = card(280, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain")
    state["hand"][3] = card(200, "Adeline", "Vigilance", "Creature", "{1}{W}{W}")
    notes = land_payment_notes(decision, state)
    assert "can pay {1}{W}{W} for Adeline" in notes[0]
    assert "cannot pay {1}{W}{W} for Adeline" in notes[2]
