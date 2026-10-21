"""2026-10-18 18:35:41: an unanswered library Search lost the game on time.

Kodama of the West Tree's combat-damage trigger raised a Search. Player.log
summarized that GreToClientEvent (the 89-card library reveal exceeds its
50-object limit), so the log-derived state never saw the SearchReq
("Quiet: priority_gained ... pending=None"). The native bridge read the
candidates from ``CurrentGameState`` while combat playback still trailed the
GRE, so every card had grpId 0 and the autopilot yielded "Waiting for Arena
to reveal the search choices…". Nothing woke it again: the poller's change
signature ignored search candidates and the backstop keyed on the log's
empty pending decision. Arena's inactivity timer ended the game at 18:38:20
(ResultReason_Timeout) with no MANUAL REQUIRED.
"""

from __future__ import annotations

import pytest
from tests.mac_bridge_fakes import FakeGame, enum, listing

from arenamcp.decisions import build_pending_decision
from arenamcp.gre_bridge import BridgeDecisionPoller
from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.standalone import StandaloneCoach

FOREST = 75553  # the basic Forest Kodama fetched at 14:39:56 (SearchResp itemsFound [314])
CARDS = {FOREST: {"name": "Forest", "type_line": "Basic Land — Forest"}}


def kodama_search(*, revealed: bool) -> dict:
    """The bridge's Search as the macOS adapter reports it (shape of _cmd_get_pending_actions)."""
    grp = FOREST if revealed else 0
    return {
        "ok": True,
        "has_pending": True,
        "request_type": "Search",
        "request_class": "SearchRequest",
        "can_cancel": False,
        "allow_undo": False,
        "bridge_runtime": "il2cpp-macos",
        "game_state_id": 165,
        "msg_id": 222,
        "select_n_min": 0,
        "select_n_max": 1,
        "search_zones": [32],
        "search_candidates": [{"instanceId": 314, "grpId": grp}, {"instanceId": 318, "grpId": grp}],
    }


@pytest.fixture
def card_names(monkeypatch):
    monkeypatch.setattr("arenamcp.server.get_card_info", lambda grp: CARDS.get(grp, {}))


# --- the native bridge reads the request's own (latest) game state -------------


class LaggingPlaybackGame(FakeGame):
    """A Search raised mid-combat: the GRE's latest state knows the library cards, playback doesn't yet."""

    def __init__(self, latest: dict | None, current: dict | None):
        request = {
            "$c": "GreClient.Rules.SearchRequest",
            "$h": 55,
            "Min": 0,
            "Max": 1,
            "Options": listing(314, 318),
            "ZonesToSearch": listing(32),
        }
        super().__init__(request, {"Type": enum("Search", 0)})
        self.latest, self.current = latest, current

    def send(self, command, timeout):
        result = super().send(command, timeout)
        if not result["ok"]:
            return result
        ops = command["ops"]
        for index, op in enumerate(ops):
            if op.get("method") == "GetCardById":
                state = ops[op["target"]["ref"]].get("member")
                cards = self.latest if state == "LatestGameState" else self.current
                result["results"][index] = None if cards is None else cards.get(op["args"][0]["uint"], 0)
            elif op.get("member") == "GrpId":
                result["results"][index] = result["results"][op["target"]["ref"]]
        return result


def test_search_candidates_come_from_the_latest_state_while_playback_lags():
    game = LaggingPlaybackGame(latest={314: FOREST, 318: FOREST}, current={314: 0, 318: 0})
    response = MacBridgeAdapter(game.send).handle({"action": "get_pending_actions"})
    assert response["search_candidates"] == [
        {"instanceId": 314, "grpId": FOREST},
        {"instanceId": 318, "grpId": FOREST},
    ]


def test_search_candidates_fall_back_to_the_shown_state():
    game = LaggingPlaybackGame(latest=None, current={314: FOREST, 318: 0})
    response = MacBridgeAdapter(game.send).handle({"action": "get_pending_actions"})
    assert response["search_candidates"] == [
        {"instanceId": 314, "grpId": FOREST},
        {"instanceId": 318, "grpId": 0},
    ]


def test_a_search_read_during_playback_lag_is_named(card_names):
    game = LaggingPlaybackGame(latest={314: FOREST, 318: FOREST}, current={314: 0, 318: 0})
    decision = build_pending_decision(MacBridgeAdapter(game.send).handle({"action": "get_pending_actions"}))
    assert [option.label for option in decision.options] == ["Forest", "Forest"]
    assert all(option.meta["identity_known"] for option in decision.options)


# --- the poller wakes the autopilot when the names land -------------------------


class ScriptedBridge:
    connected = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get_pending_actions(self):
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]

    def connect(self):
        return True

    def resolve_pending_dynamic_cards(self):
        pass

    def prewarm_grp_ids(self, ids):
        pass


def test_revealed_search_identities_are_a_new_decision():
    # The autopilot yields an unidentified Search "to the normal bridge pump";
    # the pump must report the reveal, or nothing ever re-plans the Search.
    bridge = ScriptedBridge([kodama_search(revealed=False)] * 2 + [kodama_search(revealed=True)])
    poller = BridgeDecisionPoller(bridge)
    first = poller.poll()
    assert first and first["trigger"] == "decision_required" and first["request_type"] == "Search"
    assert poller.poll() is None  # same unidentified choices: nothing new
    revealed = poller.poll()
    assert revealed and revealed["trigger"] == "decision_required"


def test_the_prefetch_thread_also_wakes_on_the_reveal():
    bridge = ScriptedBridge([kodama_search(revealed=False), kodama_search(revealed=True)])
    poller = BridgeDecisionPoller(bridge)
    assert poller._prefetch_once() is True
    assert poller._prefetch_once() is True


def test_an_unchanged_action_window_is_still_quiet():
    attack = {
        "ok": True,
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [{"actionType": "Pass"}],
    }
    poller = BridgeDecisionPoller(ScriptedBridge([attack]))
    assert poller.poll()["trigger"] == "decision_required"
    assert poller.poll() is None


# --- the backstop sees the bridge's request when the log is blind --------------


def test_latest_pending_is_the_live_request_only_while_fresh_and_actionable():
    poller = BridgeDecisionPoller(ScriptedBridge([kodama_search(revealed=False)]))
    assert poller.latest_pending() is None  # nothing polled yet
    poller.poll()
    assert poller.latest_pending()["request_type"] == "Search"
    poller._last_poll_at -= poller._LATEST_PENDING_MAX_AGE_S + 1
    assert poller.latest_pending() is None  # a stale poll describes nothing
    intermission = {
        "ok": True,
        "has_pending": True,
        "request_type": "Intermission",
        "request_class": "IntermissionRequest",
    }
    poller = BridgeDecisionPoller(ScriptedBridge([intermission]))
    poller.poll()
    assert poller.latest_pending() is None
    poller = BridgeDecisionPoller(ScriptedBridge([kodama_search(revealed=False)]))
    poller.poll()
    poller._bridge.connected = False
    assert poller.latest_pending() is None


def test_backstop_label_names_the_bridges_search_when_the_log_has_none():
    poller = BridgeDecisionPoller(ScriptedBridge([kodama_search(revealed=False)]))
    poller.poll()
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._bridge_poller = poller
    assert coach._bridge_pending_label() == "Search Library"
    poller.reset()
    assert coach._bridge_pending_label() is None


# --- the log itself records the summarized Search -------------------------------

# Verbatim from Player_1792357682.log lines 123195-123221 (18:35:41).
SUMMARIZED_SEARCH = """\
[UnityCrossThreadLogger]10/18/2026 6:35:41 PM: Match to T3MARZNUZRD4RDKHEA6GNFXM3M: GreToClientEvent
[Message summarized because one or more GameStateMessages exceeded the 50 GameObject or 50 Annotation limit.]
::: GameStateMessage
:: GameObject Count = 1
:: Annotation Count = 0
::: GameStateMessage
:: GameObject Count = 0
:: Annotation Count = 0
::: GameStateMessage
:: GameObject Count = 1
:: Annotation Count = 5
::: GameStateMessage
:: GameObject Count = 0
:: Annotation Count = 0
::: GameStateMessage
:: GameObject Count = 89
:: Annotation Count = 1
::: SearchReq
UnityEngine.DebugLogHandler:Internal_Log(LogType, LogOption, String, Object)
GreClient.Network.GREConnection:HandleMatchServiceMessage(MatchServiceToClientMessage)
GreClient.Network.GREConnection:HandleTcpEvent(ITcpEvent)
GreClient.Network.GREConnection:ProcessMessages()
GreClient.Network.RateLimitedConnection:ProcessMessages()
MatchManager:Update()
PAPA:Update()

[UnityCrossThreadLogger]10/18/2026 6:35:43 PM: Match to T3MARZNUZRD4RDKHEA6GNFXM3M: GreToClientEvent
{ "timestamp": "1792373743437", "greToClientEvent": { "greToClientMessages": [ { "type": \
"GREMessageType_TimerStateMessage", "systemSeatIds": [ 1, 2 ], "msgId": 222, "gameStateId": 165, \
"timerStateMessage": { "seatId": 1, "timers": [ { "timerId": 3, "type": "TimerType_ActivePlayer", \
"durationSec": 140, "elapsedSec": 13, "running": true, "behavior": "TimerBehavior_TakeControl", \
"warningThresholdSec": 30, "elapsedMs": 13205 } ] } } ] } }
"""


def parse_log(text: str):
    from arenamcp.gamestate import GameState, create_game_state_handler
    from arenamcp.parser import LogParser

    state = GameState()
    handler = create_game_state_handler(state)
    events: list[list[str]] = []

    def record(payload):
        messages = payload["greToClientEvent"]["greToClientMessages"]
        events.append([message["type"] for message in messages])
        handler(payload)

    parser = LogParser()
    parser.register_handler("GreToClientEvent", record)
    parser.process_chunk(text)
    return state, events


def test_a_summarized_search_is_a_pending_decision_in_the_log_state():
    state, events = parse_log(SUMMARIZED_SEARCH)
    assert state.pending_decision == "Search Library"
    assert state.decision_context["summarized"] is True
    # The JSON event after the summary still parses normally.
    assert events == [["GREMessageType_SearchReq"], ["GREMessageType_TimerStateMessage"]]


def test_a_summary_of_state_messages_alone_sets_no_decision():
    state, events = parse_log(SUMMARIZED_SEARCH.replace("::: SearchReq\n", ""))
    assert state.pending_decision is None
    # Ledger #207: the summary still emits a marker so the catch-up gate stops waiting.
    assert events == [["GREMessageType_Summarized"], ["GREMessageType_TimerStateMessage"]]
