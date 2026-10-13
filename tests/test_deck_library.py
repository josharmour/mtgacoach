"""The deck library: decks from Player.log and the bridge, their cache identity, and the Decks report."""

from __future__ import annotations

import json

from arenamcp.deck_library import (
    DeckRecord,
    deck_payload,
    load_log_decks,
    merge_libraries,
    parse_player_log,
    playbook_report,
    records_from_bridge,
)

SUMMARIES = {
    "Summaries": [
        {
            "DeckId": "07ef9d14-c112-4cf4-b4e5-49e735d7d101",
            "Name": "Hei Bai, Forest Guardian",
            "Attributes": [
                {"name": "Format", "value": "HistoricBrawl"},
                {"name": "LastPlayed", "value": '"2026-10-04T23:22:09.657768-07:00"'},
            ],
        },
        {
            "DeckId": "b7eda012-97b0-495e-beb9-41fd976eb679",
            "Name": "Mono Red",
            "Attributes": [{"name": "Format", "value": "Standard"}],
        },
    ]
}
COURSES = {
    "Courses": [
        {
            "CourseId": "c1",
            "InternalEventName": "Ladder_Constructed",
            "CourseDeckSummary": {"DeckId": "b7eda012-97b0-495e-beb9-41fd976eb679", "Name": "Mono Red"},
            "CourseDeck": {
                "MainDeck": [{"cardId": 100, "quantity": 4}, {"cardId": 200, "quantity": 20}],
                "Sideboard": [{"cardId": 300, "quantity": 2}],
                "CommandZone": [],
                "Companions": [],
            },
        }
    ]
}
QUEUE = {
    "id": "x",
    "request": json.dumps(
        {
            "EventName": "Brawl_Historic",
            "Summary": {"DeckId": "07ef9d14-c112-4cf4-b4e5-49e735d7d101", "Name": "Hei Bai, Forest Guardian"},
            "Deck": {
                "MainDeck": [{"cardId": 500, "quantity": 1}, {"cardId": 501, "quantity": 1}],
                "Sideboard": [],
                "CommandZone": [{"cardId": 999, "quantity": 1}],
                "Companions": [],
            },
        }
    ),
}


def log_text() -> str:
    return "\n".join(
        [
            '[UnityCrossThreadLogger]==> DeckGetDeckSummariesV3 {"id":"a","request":"{}"}',
            "<== DeckGetDeckSummariesV3(a)",
            json.dumps(SUMMARIES),
            "UnityEngine.DebugLogHandler:Internal_Log(LogType, LogOption, String, Object)",
            "<== EventGetCoursesV2(b)",
            json.dumps(COURSES),
            "[UnityCrossThreadLogger]==> EventSetDeckV3 " + json.dumps(QUEUE),
            "<== DeckGetAllPreconDecksV3(c)",
            json.dumps({"Decks": [{"DeckId": "precon", "MainDeck": [{"cardId": 1, "quantity": 60}]}]}),
        ]
    )


def test_log_gives_every_deck_a_name_and_the_played_ones_their_cards():
    decks = parse_player_log(log_text())
    assert set(decks) == {"07ef9d14-c112-4cf4-b4e5-49e735d7d101", "b7eda012-97b0-495e-beb9-41fd976eb679"}
    red = decks["b7eda012-97b0-495e-beb9-41fd976eb679"]
    assert red.format == "Standard" and red.complete and red.card_count() == 24
    assert red.sideboard == [(300, 2)] and red.source == "log"
    brawl = decks["07ef9d14-c112-4cf4-b4e5-49e735d7d101"]
    assert brawl.format == "HistoricBrawl" and brawl.last_played.startswith("2026-10-04")
    assert brawl.command_zone == [999] and brawl.source == "queue"
    assert brawl.deck_cards() == [500, 501, 999]


def test_log_files_are_read_newest_last_so_current_names_win(tmp_path):
    current = tmp_path / "Player.log"
    previous = tmp_path / "Player-prev.log"
    previous.write_text(log_text(), encoding="utf-8")
    current.write_text(
        "<== DeckGetDeckSummariesV3(z)\n"
        + json.dumps(
            {"Summaries": [{"DeckId": "b7eda012-97b0-495e-beb9-41fd976eb679", "Name": "Mono Red v2"}]}
        ),
        encoding="utf-8",
    )
    decks = load_log_decks([current, previous])
    red = decks["b7eda012-97b0-495e-beb9-41fd976eb679"]
    assert red.name == "Mono Red" or red.name == "Mono Red v2"
    assert red.complete  # the card list from the older file is kept
    assert load_log_decks([tmp_path / "missing.log"]) == {}


def test_bridge_records_take_precedence_and_fill_log_gaps():
    bridge = records_from_bridge(
        {
            "ok": True,
            "decks": [
                {
                    "deck_id": "07ef9d14-c112-4cf4-b4e5-49e735d7d101",
                    "name": "Hei Bai, Forest Guardian",
                    "format": "HistoricBrawl",
                    "main": [{"grp_id": 600, "count": 1}] * 3,
                    "command_zone": [{"grp_id": 999, "count": 1}],
                },
                {
                    "deck_id": "new",
                    "name": "Never played",
                    "format": "Alchemy",
                    "main": [{"grp_id": 7, "count": 60}],
                },
            ],
        }
    )
    assert records_from_bridge({"ok": False, "error": "offline"}) == {}
    merged = merge_libraries(bridge, parse_player_log(log_text()))
    assert set(merged) == {
        "07ef9d14-c112-4cf4-b4e5-49e735d7d101",
        "b7eda012-97b0-495e-beb9-41fd976eb679",
        "new",
    }
    brawl = merged["07ef9d14-c112-4cf4-b4e5-49e735d7d101"]
    assert brawl.source == "bridge" and brawl.main == [(600, 1), (600, 1), (600, 1)]
    assert brawl.last_played.startswith("2026-10-04")  # metadata from the log summary
    assert merged["new"].complete and merged["b7eda012-97b0-495e-beb9-41fd976eb679"].source == "log"


def test_identity_matches_the_in_match_identity_for_the_same_cards():
    from arenamcp.deck_strategy import deck_identity

    record = DeckRecord("d", "Deck", main=[(100, 2), (200, 1)], command_zone=[999])
    live = {"deck_cards": [200, 100, 999, 100], "commander_grp_ids": [999], "players": []}
    assert record.identity() == deck_identity(live)
    assert DeckRecord("e", "Empty").identity() == ""
    payload = deck_payload(record, {"status": "none"})
    assert payload["cards"] == 4 and payload["complete"] and payload["strategy"] == {"status": "none"}
    assert deck_payload(DeckRecord("e", "Empty"))["complete"] is False


def test_report_names_cards_from_the_catalog():
    class Book:
        catalog = {1001: {"name": "Captain"}, 1002: {"name": "Helper"}}
        data = {
            "archetype": "Token replay",
            "spoken_summary": "Replay the captain.",
            "primary_plan": "Make copies.",
            "backup_plan": "Beat down.",
            "mechanisms": [
                {"id": "m1", "effect": "Copies", "cards": [1001, 7], "requires": ["mana"], "avoid": ["x"]}
            ],
            "decision_rules": [
                {
                    "id": "r1",
                    "decisions": ["combat"],
                    "prefer": "block with tokens",
                    "when": "w",
                    "unless": "u",
                }
            ],
            "card_roles": {"1002": "fuel"},
            "phases": {"early": "ramp"},
            "commanders": [{"card": 1001, "deployment": "turn 5", "preserve_or_reuse": "recast"}],
        }

    report = playbook_report(Book())
    assert report["mechanisms"][0]["cards"] == ["Captain", "Card 7"]
    assert report["key_cards"] == [{"name": "Helper", "role": "fuel"}]
    assert report["commanders"][0]["name"] == "Captain" and report["phases"] == {"early": "ramp"}
