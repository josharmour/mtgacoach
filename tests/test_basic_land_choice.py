"""Multiversal Passage reports 151016 / 152006: enum choices, never card IDs.

The user's manual 152006 response [3] advanced to OptionalAction and the
land became a Swamp. Local client BasicLandType.cs and SelectBasicLandWorkflow
independently establish the 1..5 mapping and StaticSubset semantics.
"""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from tests.fakes import TypedBridge, make_engine, planner_with, target_state
from tests.mac_bridge_fakes import FakeGame, adapter_for, submitted_uint_values
from tests.test_color_choice import color_request

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.basic_land_choice import basic_land_ids, basic_land_ids_for_names
from arenamcp.decisions import build_pending_decision, submit_option
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.gre_bridge import GREBridge, enrich_snapshot_from_pending_response
from arenamcp.rules_engine import RulesEngine

REPORTS = json.loads((Path(__file__).parent / "fixtures/basic_land_type_20261031.json").read_text())
NAMES = ["Plains", "Island", "Swamp", "Mountain", "Forest"]


def request(**kwargs):
    return color_request(static_list="BasicLandTypes", **kwargs)


def poll(report=REPORTS[0], **kwargs):
    raw = report["request"]
    game = FakeGame(request(**kwargs), gsid=raw["gameStateId"], msg=raw["msgId"])
    result = adapter_for(game).handle({"action": "get_pending_actions"})
    result["request_payload"]["prompt"] = raw["prompt"]
    return result


class SelectionBridge(TypedBridge):
    def submit_selection(self, ids, **kwargs):
        self.submitted.append((list(ids), kwargs))
        return True


@pytest.mark.parametrize("report", REPORTS, ids=lambda r: r["report"])
def test_report_requests_expose_actual_basic_land_types(report):
    pending = poll(report)
    assert [o["name"] for o in pending["select_n_basic_land_options"]] == NAMES
    decision = build_pending_decision(pending, resolve_instance=lambda _: "Multiversal Passage")
    assert decision.source_label == "Multiversal Passage"
    assert [o.option_id for o in decision.options] == [f"sel:{i}" for i in range(1, 6)]
    assert decision.find("sel:1").label == "Plains (white mana)"
    assert decision.find("sel:3").label == "Swamp (black mana)"
    assert (decision.min_select, decision.max_select) == (1, 1)
    for rejected in report["rejected_ids"]:
        assert not decision.selection_is_valid([f"sel:{i}" for i in rejected])
    assert decision.selection_is_valid(["sel:3"])
    if "manual_ids" in report:
        [manual_id] = report["manual_ids"]
        assert decision.find(f"sel:{manual_id}").meta["name"] == report["resulting_land_subtype"]


@pytest.mark.parametrize(
    ("index", "expected"), [(1, "Plains"), (2, "Island"), (3, "Swamp"), (4, "Mountain"), (5, "Forest")]
)
def test_protocol_enum_not_database_subtypes(index, expected):
    decision = build_pending_decision(poll())
    assert decision.find(f"sel:{index}").label.startswith(expected)
    game = FakeGame(request(), gsid=decision.request_id[0], msg=decision.request_id[1])
    bridge = GREBridge()
    bridge._send_safe = adapter_for(game).handle
    assert submit_option(bridge, decision, [f"sel:{index}"])
    [batch] = game.submits()
    assert batch[-1]["method"] == "SubmitSelection"
    assert submitted_uint_values(batch) == [index]
    assert not any(op.get("method") == "SubmitArbitrary" for op in batch)


@pytest.mark.parametrize("ids", [[], [0], [54], [75556], [1, 2], [1, 1], [True], [1.5]])
def test_adapter_refuses_both_report_failures_and_other_invalid_answers(ids):
    game = FakeGame(request())
    result = adapter_for(game).handle({"action": "submit_selection", "ids": ids})
    assert not result["ok"]
    assert game.submits() == []


@pytest.mark.parametrize(
    ("ids", "expected"),
    [([4, 1], [1, 4]), ([], []), ([0, 54, 75556], []), ([3, 3, 4], [3, 4])],
)
def test_static_subset_never_invents_unoffered_choices(ids, expected):
    pending = poll(list_type="StaticSubset", ids=ids)
    assert basic_land_ids(pending) == expected
    assert build_pending_decision(pending).option_ids() == {f"sel:{i}" for i in expected}


def test_static_list_uses_full_domain_and_other_static_lists_stay_separate():
    assert basic_land_ids(poll(ids=[4])) == [1, 2, 3, 4, 5]
    assert (
        basic_land_ids(
            {"select_n_list_type": "StaticSubset", "select_n_static_list": "SubTypes", "select_n_ids": [54]}
        )
        is None
    )
    assert basic_land_ids({"request_payload": "not a structured request"}) is None
    assert basic_land_ids(
        {
            "select_n_list_type": "StaticSubset",
            "select_n_static_list": "BasicLandTypes",
            "select_n_ids": [True, 1.5, "4"],
        }
    ) == [4]
    game = FakeGame(request(list_type="StaticSubset", ids=[1, 4]))
    assert not adapter_for(game).handle({"action": "submit_selection", "ids": [3]})["ok"]
    assert not game.submits()


def test_optional_empty_choice_is_explicit_never_arbitrary():
    pending = poll(min_sel=0, max_sel=1)
    decision = build_pending_decision(pending)
    game = FakeGame(request(min_sel=0, max_sel=1), gsid=decision.request_id[0], msg=decision.request_id[1])
    bridge = GREBridge()
    bridge._send_safe = adapter_for(game).handle
    assert submit_option(bridge, decision, [])
    [batch] = game.submits()
    assert batch[-1]["method"] == "SubmitSelection"
    assert submitted_uint_values(batch) == []


@pytest.mark.parametrize("report", REPORTS, ids=lambda r: r["report"])
def test_autopilot_answers_report_via_typed_selection(report):
    pending = poll(report)
    bridge = SelectionBridge(pending)
    planner = planner_with('{"option_ids": ["sel:1"], "reasoning": "Choose Plains for white mana."}')
    engine = make_engine(bridge, planner)
    engine._game_plan_mgr = None
    assert engine._try_typed_decision_path(target_state(), "decision_required")
    assert bridge.submitted == [
        (
            [1],
            {
                "id_type": "basic_land",
                "expected_request_id": (report["request"]["gameStateId"], report["request"]["msgId"]),
            },
        )
    ]
    assert "Plains (white mana)" in planner._backend.prompts[0]


@pytest.mark.parametrize("answer", [["sel:75556"], ["sel:4", "sel:54"], ["sel:4", "sel:4"], []])
def test_bad_model_answer_gets_a_legal_fallback_not_partial_or_arbitrary(answer):
    decision = build_pending_decision(poll(list_type="StaticSubset", ids=[1, 4]))
    planner = planner_with(json.dumps({"option_ids": answer}))
    result = planner.plan_decision_options(decision, target_state())
    assert result == ["sel:1"]
    assert planner.last_llm_failure == "bad_answer"


def test_empty_subset_is_handed_off_without_legacy_replanning_or_submission():
    bridge = SelectionBridge(poll(list_type="StaticSubset", ids=[]))
    planner = planner_with("{}")
    engine = make_engine(bridge, planner)
    engine._game_plan_mgr = None
    engine._pause_for_manual = Mock()
    assert engine._try_typed_decision_path(target_state(), "decision_required")
    assert not bridge.submitted
    assert planner._backend.calls == 0
    engine._pause_for_manual.assert_called_once()


@pytest.mark.parametrize("change", ["msg", "domain", "subset"])
def test_submission_revalidates_current_request(change):
    decision = build_pending_decision(poll())
    fresh = request()
    msg = decision.request_id[1]
    if change == "msg":
        msg += 1
    elif change == "domain":
        fresh = color_request()
    else:
        fresh = request(list_type="StaticSubset", ids=[4])
    game = FakeGame(fresh, gsid=decision.request_id[0], msg=msg)
    bridge = GREBridge()
    bridge._send_safe = adapter_for(game).handle
    assert not submit_option(bridge, decision, ["sel:1"])
    assert not game.submits()


def test_manual_answer_during_planning_does_not_answer_the_next_window():
    pending = poll(REPORTS[1])
    bridge = SelectionBridge(pending)
    planner = planner_with("")

    def answer(*args, **kwargs):
        bridge.poll_resp = {
            "has_pending": True,
            "request_type": "OptionalAction",
            "game_state_id": 68,
            "msg_id": 99,
        }
        return '{"option_ids": ["sel:1"]}'

    planner._backend.complete = answer
    engine = make_engine(bridge, planner)
    engine._game_plan_mgr = None
    assert engine._try_typed_decision_path(target_state(), "decision_required")
    assert not bridge.submitted


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["Plains"], [1]),
        ([" white "], [1]),
        (["W"], [1]),
        (["Swamp"], []),
        (["Plains", "unknown"], []),
        (["Plains", "Plains"], []),
    ],
)
def test_legacy_names_map_only_to_offered_protocol_types(names, expected):
    assert basic_land_ids_for_names(names, [1, 4]) == expected


def test_legacy_plains_selection_never_uses_card_id_75556():
    bridge = SelectionBridge(poll(REPORTS[1]))
    engine = make_engine(bridge)
    action = GameAction(action_type=ActionType.SELECT_N, select_card_names=["Plains"])
    state = target_state()
    state["hand"] = [{"name": "Plains", "grp_id": 75556}]
    assert engine._try_gre_bridge_select_n(action, state).success
    assert bridge.submitted[0][0] == [1]


def test_safe_default_never_retries_an_empty_basic_land_selection():
    bridge = SelectionBridge(poll())
    engine = make_engine(bridge)
    assert engine._try_interactive_safe_default(target_state(), "decision_required") is False
    assert not bridge.submitted


@pytest.mark.parametrize("report", REPORTS, ids=lambda r: r["report"])
def test_log_labels_and_bridge_overlay_retire_old_priority_actions(report):
    state = GameState()
    state.set_local_seat_id(2, source=2)
    state.legal_actions = ["Play Land: Multiversal Passage"]
    _handle_decision_message(state, "GREMessageType_SelectNReq", copy.deepcopy(report["request"]))
    assert state.pending_decision == "Choose a Basic Land Type"
    assert state.decision_context["option_cards"] == NAMES
    assert state.decision_context["id_type"] == "BasicLandType"
    snapshot = {
        "pending_decision": state.pending_decision,
        "decision_context": state.decision_context,
        "legal_actions": ["Play Land: Multiversal Passage"],
        "legal_actions_raw": [{"actionType": "Play"}],
    }
    enrich_snapshot_from_pending_response(snapshot, poll(report), bridge_connected=True)
    assert not snapshot["legal_actions"]
    assert snapshot["decision_context"]["type"] == "choose_basic_land_type"
    assert RulesEngine.get_legal_actions(snapshot) == [f"Choose basic land type: {name}" for name in NAMES]
    action = ActionPlanner._legal_action_to_action(planner_with("{}"), "Choose basic land type: Plains")
    assert action.action_type == ActionType.SELECT_N
    assert action.select_card_names == ["Plains"]
