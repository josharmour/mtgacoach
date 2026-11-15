"""Regressions at the collection, source, provenance and desktop boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp import deck_candidates as dc
from arenamcp.mtggoldfish import _declared_commander


def test_print_identity_local_alias_and_cheapest_craft():
    database = dc._ArenaPrices(
        [
            (1, "<nobr>Deep-Cavern</nobr> Bat", "rare"),
            (2, "Deep-Cavern Bat", "uncommon"),
            (3, "Lórien Revealed", "common"),
            (4, "Card de Fée", "rare"),
            (5, "Cárd de Fee", "mythic"),
        ]
    )
    deck = {"mainboard": ["2 Deep-Cavern Bat", "1 DEEP-CAVERN BAT", "1 Lorien Revealed", "1 Card de Fee"]}
    cost = dc.compare_collection(deck, {1: 2, 3: 1}, {"uncommon": 1}, database)
    assert cost["spend"] == {"uncommon": 1} and cost["unknown"] == ["Card de Fee"]
    assert cost["missing"] == {"Deep-Cavern Bat": 1, "Card de Fee": 1}
    assert not cost["affordable"]


def test_real_print_identity_query_uses_unformatted_primary_and_fallback(qapp, tmp_path):
    import sqlite3
    from contextlib import closing
    from threading import RLock

    from arenamcp.mtgadb import MTGADatabase

    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript("""
            CREATE TABLE Cards (GrpId, TitleId, Rarity, IsPrimaryCard, IsToken);
            CREATE TABLE Localizations_enUS (LocId, Formatted, Loc);
            INSERT INTO Cards VALUES (1,1,4,1,0),(2,2,2,1,0),(3,2,3,0,0),(4,1,2,1,1);
            INSERT INTO Localizations_enUS VALUES (1,0,'Card-A'),(1,1,'<nobr>Card-A</nobr>'),(2,1,'Card B');
        """)
        native = SimpleNamespace(
            _conn=conn, _conn_lock=RLock(), _available=True, RARITY_NAMES=MTGADatabase.RARITY_NAMES
        )
        assert MTGADatabase.deck_price_prints(native) == [(1, "Card-A", "rare"), (2, "Card B", "common")]


def test_public_commander_trailer_escaped_apostrophe_and_wrong_format():
    trailer = r", '', false, 'brawl', '', 'K\'rrik, Son of Yawgmoth')"
    assert _declared_commander(trailer) == "K'rrik, Son of Yawgmoth"
    assert _declared_commander(trailer.replace("'brawl'", "'standard'")) == ""


def test_brawl_shape_and_companion_are_priced_and_exported():
    database = dc._ArenaPrices([(1, "Example", "rare"), (2, "Lórien", "mythic")])
    for text in ("Deck\n100 Island", "Commander\n2 Example\nDeck\n98 Island"):
        with pytest.raises(ValueError):
            dc.arena_export(dc.split_export(text), format_name="brawl")
    deck = dc.split_export("Companion\n1 Lorien\nCommander\n1 Example\nDeck\n99 Island\nSideboard\n1 Lórien")
    export = dc.arena_export(deck, format_name="brawl", database=database)
    assert export.startswith("Commander\n1 Example\n\nCompanion\n1 Lórien\n\nDeck\n99 Island")
    assert export.endswith("Sideboard\n1 Lórien")
    assert dc.compare_collection(deck, {2: 1}, {}, database)["spend"] == {"rare": 1}
    # Some sites specify only a Companion section. Arena also expects a sideboard copy.
    deck["sideboard"] = ["2 Lórien"]
    with pytest.raises(ValueError, match="sideboard"):
        dc.arena_export(deck, format_name="brawl", database=database)
    standard = dict(deck, commander=[], mainboard=["60 Island"])
    assert dc.compare_collection(standard, {2: 1}, {}, database)["spend"] == {"mythic": 1}
    deck["sideboard"] = []
    assert dc.arena_export(deck, database=database).endswith("Sideboard\n1 Lórien")
    assert dc.compare_collection(deck, {2: 1}, {}, database)["spend"] == {"rare": 1}


def test_ranked_queue_identity_no_reuse_or_local_fleet_double_count():
    candidate = {"format": "Brawl", "name": "Mono Red", "cost": {}, "arena": {"games": 48, "win_rate": 1}}
    exact = dict(candidate, name="Mono Red Burn")
    fleet = {
        "name": "Mono-Red Burn",
        "formats": ["Brawl_Ladder"],
        "games": 20,
        "wins": 12,
        "losses": 8,
        "win_rate": 0.6,
        "sample_source": "fleet",
    }
    local = dict(fleet, sample_source="local", wins=10, losses=10, win_rate=0.5)
    rows = dc.rank_candidates([candidate, exact], [local, fleet])
    assert [r.get("arena_sample") for r in rows] == ["fleet", None]
    assert rows[0]["arena"] == {"games": 20, "win_rate": 0.6} and "arena" not in rows[1]
    for fields in (
        {"formats": ["Standard Brawl"]},
        {"formats": ["Brawl"]},
        {"formats": ["Brawl Ladder", "Other"]},
        {"wins": 21},
        {"win_rate": 1},
    ):
        assert "arena" not in dc.rank_candidates([exact], [dict(fleet, **fields)])[0]


def test_last_match_scope_precedes_autopilot_filter_and_string_is_not_consent(tmp_path):
    def bundle(file, match, auto, result):
        payload = {
            "match_id": match,
            "settings_subset": {"autopilot_enabled": auto},
            "result": result,
            "final_state": {"format_name": "Brawl Ladder"},
            "packet": {"deck_strategy": "DECK PLAYBOOK v2: Flyers: Win combat"},
        }
        (tmp_path / file).write_text(json.dumps(payload))

    for n in range(19):
        bundle(f"{n}.json", str(n), True, "win")
    bundle("a.json", "mixed", True, "win")
    bundle("b.json", "mixed", False, "loss")
    bundle("c.json", "advisor", "false", "win")
    bundle("d.json", "unfinished-match", True, None)
    final_game = json.loads((tmp_path / "d.json").read_text())
    final_game["final_state"]["last_game_result"] = "win"
    (tmp_path / "d.json").write_text(json.dumps(final_game))
    assert dc.fleet_autopilot_results(str(tmp_path)) == [
        {
            "games": 19,
            "wins": 19,
            "losses": 0,
            "win_rate": None,
            "formats": ["Brawl Ladder"],
            "name": "Flyers",
        }
    ]


def test_local_history_error_still_prices_and_reports_warning(qapp, monkeypatch):
    from arenamcp.desktop import deck_builder as builder

    monkeypatch.setattr(builder, "local_autopilot_results", Mock(side_effect=OSError("unavailable")))
    monkeypatch.setattr(builder, "fleet_autopilot_results", lambda: [])
    monkeypatch.setattr(builder, "load_candidates", lambda *_: [{"name": "Example", "format": "Standard"}])
    received = []
    worker = builder._LoadDecks({}, {})
    worker.result.ready.connect(lambda rows, warning: received.append((rows, warning)))
    worker.run()
    assert received == [([{"name": "Example", "format": "Standard"}], "Local match history unavailable.")]


def test_source_tiles_require_goldfish_deck_identity(tmp_path, monkeypatch):
    from bs4 import BeautifulSoup

    from arenamcp import mtggoldfish

    monkeypatch.setattr(mtggoldfish, "CACHE_DIR", tmp_path)
    client = mtggoldfish.MTGGoldfishClient()

    def tile(path):
        return BeautifulSoup(
            f'<div class="archetype-tile-title"><a href="{path}">Deck</a></div>', "html.parser"
        )

    assert client._parse_tile(tile("/deck/8390123#paper"))["url"].endswith("/deck/8390123#paper")
    assert client._parse_tile(tile("//other.example/archetype/standard")) is None
    assert client._parse_tile(tile("/settings")) is None
