from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message


def test_sideboard_request_preserves_previous_game_and_is_not_mulligan():
    state = GameState()
    state.turn_info.turn_number = 21
    state.local_seat_id = 1
    state.sideboard_cards = [999]
    dirty = _handle_decision_message(
        state, "GREMessageType_SubmitDeckReq", {"submitDeckReq": {"deck": {"sideboardCards": [101, 102]}}}
    )
    assert dirty
    assert state.turn_info.turn_number == 21
    assert state.sideboard_cards == [101, 102]
    assert state.pending_decision == "Sideboard"
    assert state.decision_context == {"type": "sideboard"}
    assert state.decision_seat_id == 1
    assert state.decision_timestamp > 0


def test_empty_sideboard_clears_stale_cards_then_real_mulligan_is_recognized():
    state = GameState()
    state.sideboard_cards = [999]
    _handle_decision_message(state, "GREMessageType_SubmitDeckReq", {"submitDeckReq": {"deck": {}}})
    assert state.sideboard_cards == []
    _handle_decision_message(state, "GREMessageType_MulliganReq", {"mulliganReq": {"mulliganCount": 1}})
    assert state.pending_decision == "Mulligan"
    assert state.decision_context["mulligan_count"] == 1
