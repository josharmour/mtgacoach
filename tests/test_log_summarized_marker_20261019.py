"""A summarized GreToClientEvent marks the log's state id it was summarized at (ledger #174)."""

from arenamcp.gamestate import GameState, create_game_state_handler


def test_a_summarized_event_records_the_log_state_id():
    state = GameState()
    state.log_game_state_id = 589
    handler = create_game_state_handler(state)
    handler(
        {
            "greToClientEvent": {
                "greToClientMessages": [
                    {"type": "GREMessageType_ConnectResp", "_summarized": True},
                    {"type": "GREMessageType_ActionsAvailableReq", "_summarized": True},
                ]
            }
        }
    )
    assert state.log_summarized_at_gsid == 589
    assert state.get_snapshot()["_log_summarized_at_gsid"] == 589
