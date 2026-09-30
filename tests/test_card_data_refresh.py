import gzip
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from arenamcp.card_db import MTGADatabaseAdapter
from arenamcp.mtgjson import MTGJSONDatabase
from arenamcp.scryfall import ScryfallCache


def atomic_data(name):
    return {
        "data": {
            name: [
                {
                    "name": name,
                    "text": "Draw a card.",
                    "type": "Sorcery",
                    "manaCost": "{U}",
                    "manaValue": 1,
                    "colors": ["U"],
                }
            ]
        }
    }


def cached_database(tmp_path, *, stale):
    database = MTGJSONDatabase(tmp_path)
    data = atomic_data("Old Card")
    database._cache_file.write_text(json.dumps(data))
    if stale:
        old = time.time() - 48 * 3600
        os.utime(database._cache_file, (old, old))
    database._arena_index, database._name_index = database._build_indexes(data)
    database._save_indexes()
    return MTGJSONDatabase(tmp_path)


def test_stale_atomic_data_refreshes_even_when_indexes_exist(tmp_path, monkeypatch):
    database = cached_database(tmp_path, stale=True)

    def download():
        database._cache_file.write_text(json.dumps(atomic_data("New Release")))
        newer = time.time() + 1
        os.utime(database._cache_file, (newer, newer))
        return True

    refresh = Mock(side_effect=download)
    monkeypatch.setattr(database, "_download_data", refresh)
    assert database.load()
    refresh.assert_called_once()
    assert database.get_card_by_name("New Release").mana_cost == "{U}"


def test_fresh_indexes_load_without_downloading(tmp_path, monkeypatch):
    database = cached_database(tmp_path, stale=False)
    refresh = Mock(side_effect=AssertionError("Fresh cache must not download"))
    monkeypatch.setattr(database, "_download_data", refresh)
    assert database.load()
    assert database.get_card_by_name("Old Card") is not None
    refresh.assert_not_called()


def test_network_failure_keeps_stale_indexes_usable(tmp_path, monkeypatch):
    database = cached_database(tmp_path, stale=True)
    monkeypatch.setattr(database, "_download_data", lambda: False)
    assert database.load()
    assert database.get_card_by_name("Old Card") is not None


@pytest.mark.parametrize("payload", [b"not gzip", gzip.compress(b"{}"), gzip.compress(b'{"data":{}}')])
def test_invalid_refresh_preserves_previous_cache(tmp_path, monkeypatch, payload):
    database = cached_database(tmp_path, stale=True)
    previous = database._cache_file.read_bytes()
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = [payload]
    monkeypatch.setattr("arenamcp.mtgjson.requests.get", lambda *args, **kwargs: response)
    assert database._download_data() is False
    assert database._cache_file.read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("jsonl", [False, True])
def test_bulk_names_resolve_without_arena_mapping_or_network(tmp_path, monkeypatch, jsonl):
    monkeypatch.setattr(ScryfallCache, "_run_background_load", lambda self: None)
    cache = ScryfallCache(tmp_path)
    cache._thread.join()
    cards = [
        {
            "name": "Newest Card",
            "oracle_text": "Draw a card.",
            "mana_cost": "{U}",
            "type_line": "Sorcery",
            "lang": "en",
        }
    ]
    cache._get_bulk_data_path().write_text(
        "\n".join(json.dumps(card) for card in cards) if jsonl else json.dumps(cards)
    )
    cache._not_found_cache.add(100)
    cache._name_cache["Newest Card"] = None
    monkeypatch.setattr("arenamcp.scryfall.requests.get", Mock(side_effect=AssertionError("No API lookup")))
    cache._load_bulk_data()
    assert cache.get_card_by_name("newest CARD").oracle_text == "Draw a card."
    assert not cache._not_found_cache


def test_unknown_shores_is_a_real_bulk_card_name(tmp_path, monkeypatch):
    monkeypatch.setattr(ScryfallCache, "_run_background_load", lambda self: None)
    cache = ScryfallCache(tmp_path)
    cache._thread.join()
    cache._name_index["unknown shores"] = {
        "name": "Unknown Shores",
        "type_line": "Land",
        "oracle_text": "{T}: Add {C}.",
    }
    assert cache.get_card_by_name("Unknown Shores").oracle_text == "{T}: Add {C}."


@pytest.mark.parametrize("raw, expected", [("1,2", ["W", "U"]), ("4", ["R"]), ("0,6,7", []), ("G", ["G"])])
def test_mtga_colors_use_magic_letters_not_internal_enum_ids(raw, expected):
    card = SimpleNamespace(name="Card", oracle_text="", type_line="Creature", colors=raw, grp_id=123)
    database = Mock(available=True)
    database.get_card.return_value = card
    database.prewarm_cards.return_value = {123: card}
    adapter = MTGADatabaseAdapter(database)
    assert adapter.get_card_by_arena_id(123).colors == expected
    assert adapter.prewarm_cards([123])[123].colors == expected
