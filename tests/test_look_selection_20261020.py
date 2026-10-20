"""A "look" selection is answered with an empty selection (2026-10-20 13:52, match ebd1b267, T23).

Herald's Horn's upkeep trigger looked at the top card, a Plains (instance 360),
and Arena asked SelectNReq ids [360] with minSel = maxSel = 0 (omitted from the
log JSON as protobuf zeros), prompt 104 "Click OK to continue."
SelectNRequest.IsLookSelection; SelectNWorkflow_Look's OK runs
SubmitSelection(Array.Empty<uint>()). The log parser read minCount/maxCount
(absent) as "pick 1 of 1", the model picked the Plains ("take the revealed Hare
Apparent"), that was refused, and the safe default's empty selection went out
as SubmitArbitrary (ids [0], OrderArbitraryOnce): 17 IllegalRequest
LimitViolations until the stuck guard paused autoplay.
"""

from __future__ import annotations

from unittest.mock import Mock

from tests.fakes import TypedBridge, make_engine, planner_with, target_state
from tests.mac_bridge_fakes import FakeGame, adapter_for, listing, submitted_uint_values

from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_select_n_req

LOOK_REQUEST = {
    "$c": "GreClient.Rules.SelectNRequest",
    "$h": 5,
    "MinSel": 0,
    "MaxSel": 0,
    "Ids": listing(360),
}
# The bridge poll in the bug report (capture_bridge_pending), trimmed.
LOOK_POLL = {
    "ok": True,
    "has_pending": True,
    "request_type": "SelectN",
    "request_class": "SelectNRequest",
    "can_cancel": False,
    "game_state_id": 751,
    "msg_id": 1053,
    "select_n_ids": [360],
    "select_n_id_type": "InstanceId",
    "select_n_list_type": "Dynamic",
    "select_n_context": "Resolution",
    "select_n_option_context": "Resolution",
    "select_n_static_list": "None",
    "select_n_min": 0,
    "select_n_max": 0,
    "can_pass": False,
}
# Player.log 13:52:02, seq 1494: no minSel/maxSel (both zero).
LOG_LOOK_REQ = {
    "type": "GREMessageType_SelectNReq",
    "systemSeatIds": [2],
    "msgId": 1001,
    "gameStateId": 734,
    "prompt": {
        "promptId": 104,
        "parameters": [{"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 811}],
    },
    "selectNReq": {
        "context": "SelectionContext_Resolution",
        "optionContext": "OptionContext_Resolution",
        "listType": "SelectionListType_Dynamic",
        "ids": [360],
        "prompt": {},
        "idType": "IdType_InstanceId",
        "sourceId": 1032,
        "validationType": "SelectionValidationType_NonRepeatable",
        "minWeight": -2147483648,
        "maxWeight": 2147483647,
    },
    "allowCancel": "AllowCancel_No",
}


def test_the_bridge_answers_a_look_selection_with_an_empty_selection():
    game = FakeGame(dict(LOOK_REQUEST))
    response = adapter_for(game).handle({"action": "submit_selection", "ids": []})
    assert response == {"ok": True, "submitted_type": "SelectN"}
    [batch] = game.submits()
    assert not any(op.get("method") == "SubmitArbitrary" for op in batch)
    assert submitted_uint_values(batch) == []


def test_an_unreadable_max_is_not_taken_for_a_look_selection():
    game = FakeGame({"$c": "GreClient.Rules.SelectNRequest", "$h": 5})
    assert adapter_for(game).handle({"action": "submit_selection", "ids": []})["ok"]
    [batch] = game.submits()
    assert [op.get("method") for op in batch if op.get("method", "").startswith("Submit")] == [
        "SubmitArbitrary"
    ]


def _look_state():
    state = target_state()
    state.update(_bridge_request_type="SelectN", pending_decision="Select Cards")
    return state


def test_the_autopilot_clicks_ok_on_a_look_selection_without_asking_the_model():
    bridge = TypedBridge(dict(LOOK_POLL))
    planner = planner_with('{"option_ids": ["sel:360"], "reasoning": "take the revealed Hare Apparent"}')
    planner.plan_decision_options = Mock(return_value=["sel:360"])
    engine = make_engine(bridge, planner)
    assert engine._try_typed_decision_path(_look_state(), "decision_required") is True
    assert bridge.submitted == [("selection", [])]
    planner.plan_decision_options.assert_not_called()


def _parse(monkeypatch, message):
    from arenamcp import server

    monkeypatch.setattr(server, "enrich_with_oracle_text", lambda grp_id: {})
    monkeypatch.setattr(server, "get_card_info", lambda grp_id: {})
    state = GameState()
    state.local_seat_id = 2
    _handle_select_n_req(state, message)
    return state.decision_context


def test_the_log_reads_a_look_selection_as_select_nothing(monkeypatch):
    context = _parse(monkeypatch, LOG_LOOK_REQ)
    assert (context["min"], context["max"], context["count"]) == (0, 0, 0)


def test_the_log_reads_arenas_min_and_max_selection_fields(monkeypatch):
    message = {**LOG_LOOK_REQ, "selectNReq": {**LOG_LOOK_REQ["selectNReq"], "minSel": 4, "maxSel": 6}}
    context = _parse(monkeypatch, message)
    assert (context["min"], context["max"]) == (4, 6)
