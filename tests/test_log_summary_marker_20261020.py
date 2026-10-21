"""Every summarized GreToClientEvent marks the log, not only a summarized SearchReq (ledger #207).

2026-10-20 17:41:41 (match 43f27633): Arena summarized a board wipe plus the Optional Action that
followed it. The parser only re-emitted summaries holding a SearchReq, so no marker was set, the
autopilot's log catch-up gate waited for gameStateId 234 that the log never reached (it stayed at
227), and the decision watchdog answered after 29 s.
"""

from unittest.mock import MagicMock

from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.parser import LogParser
from tests.fakes import make_engine

OPTIONAL_ACTION_SUMMARY = """\
[UnityCrossThreadLogger]10/20/2026 5:41:41 PM: Match to FMRW2YNB5NFRPBE7I5I7CQ2RYI: GreToClientEvent
[Message summarized because one or more GameStateMessages exceeded the 50 GameObject or 50 Annotation limit.]
::: QueuedGameStateMessage
:: GameObject Count = 1
:: Annotation Count = 2
::: QueuedGameStateMessage
:: GameObject Count = 0
:: Annotation Count = 0
::: GameStateMessage
:: GameObject Count = 5
:: Annotation Count = 26
::: GameStateMessage
:: GameObject Count = 46
:: Annotation Count = 101
::: OptionalActionMessage
UnityEngine.DebugLogHandler:Internal_Log(LogType, LogOption, String, Object)
GreClient.Network.GREConnection:HandleMatchServiceMessage(MatchServiceToClientMessage)
MatchManager:Update()
PAPA:Update()

[UnityCrossThreadLogger]10/20/2026 5:41:43 PM: Match to FMRW2YNB5NFRPBE7I5I7CQ2RYI: GreToClientEvent
{ "timestamp": "1792543303770", "greToClientEvent": { "greToClientMessages": [ { "type": \
"GREMessageType_TimerStateMessage", "systemSeatIds": [ 1, 2 ], "msgId": 296, "gameStateId": 234, \
"timerStateMessage": { "seatId": 2, "timers": [ { "timerId": 10, "type": "TimerType_NonActivePlayer", \
"durationSec": 45, "running": true, "behavior": "TimerBehavior_TakeControl", "warningThresholdSec": 30 } ] } } ] } }
"""


def _parse(text: str, log_state_id: int) -> tuple[GameState, list[list[str]]]:
    state = GameState()
    state.log_game_state_id = log_state_id
    handler = create_game_state_handler(state)
    events: list[list[str]] = []

    def record(payload):
        events.append([message["type"] for message in payload["greToClientEvent"]["greToClientMessages"]])
        handler(payload)

    parser = LogParser()
    parser.register_handler("GreToClientEvent", record)
    parser.process_chunk(text)
    return state, events


def test_an_optional_action_summary_marks_the_log_state_id():
    state, events = _parse(OPTIONAL_ACTION_SUMMARY, 227)
    assert events == [["GREMessageType_Summarized"], ["GREMessageType_TimerStateMessage"]]
    assert state.log_summarized_at_gsid == 227
    assert state.log_game_state_id == 227  # the timer message's id 234 is not a state the log saw
    assert state.pending_decision is None  # the bridge, not the summary, reports the Optional Action


def test_the_catch_up_gate_stops_waiting_after_that_summary():
    state, _ = _parse(OPTIONAL_ACTION_SUMMARY, 227)
    snapshot = state.get_snapshot()
    bridge = MagicMock()
    bridge.connected = True
    engine = make_engine(bridge, MagicMock(), game_state=lambda: snapshot)
    engine._LOG_CATCH_UP_TIMEOUT_S = 0.05  # without the marker the gate times out and returns None
    poll = {"has_pending": True, "game_state_id": 234, "request_type": "OptionalAction"}
    assert engine._await_log_catch_up(poll, snapshot) is snapshot
