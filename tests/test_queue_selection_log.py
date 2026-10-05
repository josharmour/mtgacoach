"""Player.log names the deck and event each queue used; requeueing reuses them."""

import json

from arenamcp import server
from arenamcp.gamestate import GameState
from arenamcp.parser import LogParser

# Shape copied from Player.log on 2026-10-04 (23:15:20, the wrong-deck join).
SET_DECK_LINE = "[UnityCrossThreadLogger]==> EventSetDeckV3 " + json.dumps(
    {
        "id": "4b85072b-a4e5-4a0f-982b-36360901d2b8",
        "request": json.dumps(
            {
                "EventName": "Play_Brawl_Historic",
                "Summary": {
                    "DeckId": "b7eda012-97b0-495e-beb9-41fd976eb679",
                    "Mana": "",
                    "Name": "Michelangelo, Weirdness to 11",
                },
            }
        ),
    }
)


def test_parser_routes_the_outgoing_set_deck_request_but_not_its_response():
    parser, seen = LogParser(), []
    parser.register_handler("EventSetDeckV3", seen.append)
    response = '<== EventSetDeckV3(4b85072b)\n{"CourseId": "c", "InternalEventName": "Play_Brawl_Historic"}\n'
    parser.process_chunk(
        SET_DECK_LINE + "\nFrontDoorConnectionAWS:SendMessage(Func`2, CmdType, Object)\n" + response
    )
    assert len(seen) == 1
    selection = server.parse_queue_selection(seen[0])
    assert (selection["event_id"], selection["deck_name"], selection["deck_id"]) == (
        "Play_Brawl_Historic",
        "Michelangelo, Weirdness to 11",
        "b7eda012-97b0-495e-beb9-41fd976eb679",
    )


def test_malformed_set_deck_requests_are_ignored():
    for payload in (
        {},
        {"request": "not json"},
        {"request": json.dumps({"EventName": "X", "Summary": "deck"})},
    ):
        assert server.parse_queue_selection(payload) is None


def test_log_scan_finds_the_last_selection(tmp_path, monkeypatch):
    log = tmp_path / "Player.log"
    older = SET_DECK_LINE.replace("Michelangelo, Weirdness to 11", "Older deck")
    log.write_text(older + "\nnoise\n" + SET_DECK_LINE + "\n", encoding="utf-8")
    monkeypatch.setattr(server, "watcher", type("W", (), {"log_path": log})())
    assert server._queue_selection_from_log()["deck_name"] == "Michelangelo, Weirdness to 11"


def complete(monkeypatch, event_id, selection, scanned=None, *, seen_start=True, logged_event=""):
    state = GameState()
    state.match_id = "one"
    monkeypatch.setattr(server, "game_state", state)
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    monkeypatch.setattr(server, "_match_event_ids", {}, raising=False)
    monkeypatch.setattr(server, "_last_queue_selection", selection)
    monkeypatch.setattr(server, "_queue_selection_from_log", lambda: scanned)
    monkeypatch.setattr(server, "_match_event_from_log", lambda match_id: logged_event, raising=False)
    monkeypatch.setattr("arenamcp.log_utils.get_local_player_id", lambda: None)
    if seen_start:
        server._handle_match_created(
            {"matchId": "one", "gameRoomConfig": {"matchId": "one", "eventId": event_id}}
        )
    # Arena's MatchCompleted room config has no eventId, and the coach has
    # reset game state by then (2026-10-04 23:58).
    state.event_id = ""
    server._handle_match_created(
        {
            "matchId": "one",
            "gameRoomConfig": {"matchId": "one", "reservedPlayers": []},
            "finalMatchResult": {"resultList": [{"scope": "MatchScope_Match", "result": "ResultType_Win"}]},
        }
    )
    return server.get_completed_match_for_navigation()


def test_match_completion_carries_the_deck_its_event_was_queued_with(monkeypatch):
    selection = {"event_id": "Brawl_Ladder", "deck_name": "The Notary Hobbits Digital", "deck_id": "d1"}
    completed = complete(monkeypatch, "Brawl_Ladder", selection)
    assert (completed["event_id"], completed["deck_name"], completed["deck_id"]) == (
        "Brawl_Ladder",
        "The Notary Hobbits Digital",
        "d1",
    )


def test_a_selection_for_another_event_is_never_used(monkeypatch):
    other = {"event_id": "Play_Brawl_Historic", "deck_name": "Michelangelo, Weirdness to 11"}
    assert complete(monkeypatch, "Brawl_Ladder", other, scanned=other)["deck_name"] == ""
    scanned = {"event_id": "Brawl_Ladder", "deck_name": "From the log", "deck_id": "d2"}
    assert complete(monkeypatch, "Brawl_Ladder", other, scanned=scanned)["deck_name"] == "From the log"


# Bot matches send only a deck id (Player.log 2026-10-04 23:32:59).
BOT_LINE = "[UnityCrossThreadLogger]==> EventAiBotMatch " + json.dumps(
    {
        "id": "887d0d98",
        "request": json.dumps({"deckId": "49fa0845", "botDeckId": "22047ee2", "botMatchType": 0}),
    }
)
SUMMARY_LINE = '{"Summaries":[{"DeckId":"49fa0845","Name":"Mono-White Auras","Attributes":[]}]}'


def test_bot_match_queue_resolves_its_deck_name_from_the_log(tmp_path, monkeypatch):
    parser, seen = LogParser(), []
    parser.register_handler("EventAiBotMatch", seen.append)
    parser.process_chunk(BOT_LINE + "\n")
    assert server.parse_queue_selection(seen[0], "EventAiBotMatch")["deck_id"] == "49fa0845"

    log = tmp_path / "Player.log"
    log.write_text(SUMMARY_LINE + "\n" + SET_DECK_LINE + "\n" + BOT_LINE + "\n", encoding="utf-8")
    monkeypatch.setattr(server, "watcher", type("W", (), {"log_path": log})())
    monkeypatch.setattr(server, "_last_queue_selection", {})
    selection = server._queue_selection_for_event("AIBotMatch")
    assert (selection["event_id"], selection["deck_name"]) == ("AIBotMatch", "Mono-White Auras")
    # The set-deck request's escaped JSON also names decks.
    assert (
        server._deck_name_from_log("b7eda012-97b0-495e-beb9-41fd976eb679") == "Michelangelo, Weirdness to 11"
    )
    assert server._deck_name_from_log("unknown") == ""


def test_event_comes_from_the_log_when_the_coach_missed_the_match_start(monkeypatch):
    selection = {"event_id": "Brawl_Ladder", "deck_name": "The Notary Hobbits Digital", "deck_id": "d1"}
    completed = complete(
        monkeypatch, "Brawl_Ladder", selection, seen_start=False, logged_event="Brawl_Ladder"
    )
    assert (completed["event_id"], completed["deck_name"]) == ("Brawl_Ladder", "The Notary Hobbits Digital")
    assert complete(monkeypatch, "Brawl_Ladder", selection, seen_start=False)["deck_name"] == ""


def test_match_event_scan_reads_the_room_line_for_that_match(tmp_path, monkeypatch):
    log = tmp_path / "Player.log"
    log.write_text(
        '{"gameRoomConfig": {"eventId": "AIBotMatch", "matchId": "other"}}\n'
        '{"gameRoomConfig": {"reservedPlayers": [], "eventId": "Brawl_Ladder", "matchId": "one"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "watcher", type("W", (), {"log_path": log})())
    assert server._match_event_from_log("one") == "Brawl_Ladder"
    assert server._match_event_from_log("missing") == ""
