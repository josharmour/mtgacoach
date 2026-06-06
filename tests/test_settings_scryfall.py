from __future__ import annotations

from pathlib import Path
import json
import time
import pytest
from arenamcp.scryfall import ScryfallCache
from arenamcp.settings import _migrate_settings

def test_settings_migration_local_and_auto() -> None:
    # Test that legacy "backend": "local" maps to "mode": "local" and deletes "backend"
    data = {"backend": "local"}
    assert _migrate_settings(data) is True
    assert data.get("mode") == "local"
    assert "backend" not in data

    # Test that legacy "backend": "ollama" maps to "mode": "local", deletes "backend", and migrates URL
    data = {"backend": "ollama", "ollama_url": "http://ollama"}
    assert _migrate_settings(data) is True
    assert data.get("mode") == "local"
    assert data.get("local_url") == "http://ollama"
    assert "backend" not in data

    # Test that legacy "backend": "auto" maps to "mode": "auto"
    data = {"backend": "auto"}
    assert _migrate_settings(data) is True
    assert data.get("mode") == "auto"
    assert "backend" not in data

    # Test that normal mode is preserved if "backend" is not there
    data = {"mode": "local"}
    assert _migrate_settings(data) is False
    assert data.get("mode") == "local"

def test_scryfall_cache_async_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup mock data path
    bulk_file = tmp_path / "default_cards.json"
    mock_cards = [
        {"name": "Llanowar Elves", "arena_id": 100, "oracle_text": "Add G"},
    ]
    with open(bulk_file, "w", encoding="utf-8") as f:
        json.dump(mock_cards, f)

    # Monkeypatch stale check so it doesn't try to download
    monkeypatch.setattr(ScryfallCache, "_is_cache_stale", lambda self: False)
    
    # Initialize cache
    cache = ScryfallCache(cache_dir=tmp_path)
    
    # Wait a bit to make sure it finishes loading in the background thread
    for _ in range(50):
        if cache._bulk_data_ready:
            break
        time.sleep(0.05)
        
    assert cache._bulk_data_ready is True
    card = cache.get_card_by_arena_id(100)
    assert card is not None
    assert card.name == "Llanowar Elves"
    assert card.oracle_text == "Add G"

def test_scryfall_cache_skips_api_fallback_when_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup mock file
    bulk_file = tmp_path / "default_cards.json"
    with open(bulk_file, "w", encoding="utf-8") as f:
        json.dump([], f)

    # Monkeypatch stale check to False
    monkeypatch.setattr(ScryfallCache, "_is_cache_stale", lambda self: False)

    # Mock fetch_from_api to verify it is NOT called
    api_called = False
    def mock_fetch(self, arena_id):
        nonlocal api_called
        api_called = True
        return None
    monkeypatch.setattr(ScryfallCache, "_fetch_from_api", mock_fetch)

    # Initialize cache
    cache = ScryfallCache(cache_dir=tmp_path)
    
    # Force _bulk_data_ready to False to test that API is skipped
    cache._bulk_data_ready = False
    card = cache.get_card_by_arena_id(1234)
    assert card is None
    assert not api_called

def test_screen_capture_bbox_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    from arenamcp.screen_capture import capture_mtga_png
    from PIL import ImageGrab

    grab_called = False
    def mock_grab(*args, **kwargs):
        nonlocal grab_called
        grab_called = True
        raise RuntimeError("ImageGrab.grab should not be called!")

    monkeypatch.setattr(ImageGrab, "grab", mock_grab)

    # 1. Check zero width/height (should skip ImageGrab.grab)
    res = capture_mtga_png(hwnd=None, bbox=(10, 10, 10, 20))
    assert res is None or not grab_called

    # 2. Check left >= right (should skip ImageGrab.grab)
    grab_called = False
    res = capture_mtga_png(hwnd=None, bbox=(20, 10, 10, 20))
    assert res is None or not grab_called

    # 3. Check None values (should skip ImageGrab.grab)
    grab_called = False
    res = capture_mtga_png(hwnd=None, bbox=(None, 10, 20, 20))
    assert res is None or not grab_called


def test_find_mtga_database_checks_settings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from arenamcp.mtgadb import find_mtga_database
    from arenamcp.settings import Settings

    # Set up a mock settings instance
    mock_settings = Settings()
    mock_settings._data = {"mtga_install_dir": str(tmp_path)}

    # Monkeypatch get_settings to return our mock
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: mock_settings)

    # Set up a mock database file under the mock install dir
    raw_dir = tmp_path / "MTGA_Data" / "Downloads" / "Raw"
    raw_dir.mkdir(parents=True)
    db_file = raw_dir / "Raw_CardDatabase_mock.mtga"
    db_file.touch()

    # Empty standard search paths to ensure only our settings path is used
    monkeypatch.setattr("arenamcp.mtgadb.MTGA_PATHS", [])

    resolved = find_mtga_database()
    assert resolved == db_file


def test_combat_advice_override_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    from arenamcp.coach import CoachEngine
    from arenamcp.rules_engine import RulesEngine
    from unittest.mock import MagicMock

    mock_backend = MagicMock()
    coach = CoachEngine(backend=mock_backend)

    class MockGameState:
        def __init__(self, data):
            self._data = data
        def get(self, key, default=None):
            return self._data.get(key, default)

    # Monkeypatch RulesEngine.get_legal_actions to return our mock actions
    mock_legal_actions = []
    monkeypatch.setattr(RulesEngine, "get_legal_actions", lambda gs: mock_legal_actions)

    # Setup a game state where we are in declare attackers
    game_state_atk = MockGameState({
        "pending_decision": "declare attackers",
        "turn": {"phase": "Combat", "step": "DeclareAttack"},
    })

    # Case 1: Done (confirm attackers) matches positive attack advice
    mock_legal_actions = ["Done (confirm attackers)"]
    res = coach._postprocess_advice(
        advice="Attack with all creatures.",
        game_state=game_state_atk,  # type: ignore
        style="quick"
    )
    assert res == "Attack with all creatures."

    # Case 2: Irrelevant advice gets overridden to default "Don't attack"
    res = coach._postprocess_advice(
        advice="Cast a random spell.",
        game_state=game_state_atk,  # type: ignore
        style="quick"
    )
    assert res == "Don't attack"

    # Case 3: Done (confirm blockers) matches positive block advice
    game_state_blk = MockGameState({
        "pending_decision": "declare blockers",
        "turn": {"phase": "Combat", "step": "DeclareBlockers"},
    })
    mock_legal_actions = ["Done (confirm blockers)"]
    res = coach._postprocess_advice(
        advice="Block their 5/5 creature.",
        game_state=game_state_blk,  # type: ignore
        style="quick"
    )
    assert res == "Block their 5/5 creature."


