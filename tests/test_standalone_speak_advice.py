"""Unit tests for speak_advice filtering logic in standalone.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arenamcp import settings as settings_mod
from arenamcp.standalone import StandaloneCoach


@pytest.fixture()
def default_conversation_settings(monkeypatch, tmp_path):
    # speak_advice behavior in the legacy path must not depend on a user's
    # saved conversation_mode: isolate settings so the coach starts turn_advice.
    monkeypatch.setattr(settings_mod, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    fresh = settings_mod.Settings()
    monkeypatch.setattr(settings_mod, "get_settings", lambda: fresh)
    return fresh


def test_speak_advice_filtering(default_conversation_settings):
    # Instantiate StandaloneCoach with register_hotkeys=False to avoid Linux keyboard root requirement
    coach = StandaloneCoach(register_hotkeys=False, backend="proxy")
    coach._voice_output = MagicMock()

    # 1. Verify standard silence triggers are still silenced
    coach.speak_advice("Wait for opponent.")
    assert coach._voice_output.speak.call_count == 0

    coach.speak_advice("Pass priority.")
    assert coach._voice_output.speak.call_count == 0

    # 2. Verify active decision phrases are NOT silenced
    coach._voice_output.reset_mock()
    coach.speak_advice("Decline the optional action and pass priority.")
    assert coach._voice_output.speak.call_count == 1
    assert coach._voice_output.speak.call_args[0][0] == "Decline the optional action and pass priority."

    coach._voice_output.reset_mock()
    coach.speak_advice("Accept the optional trigger.")
    assert coach._voice_output.speak.call_count == 1

    coach._voice_output.reset_mock()
    coach.speak_advice("Keep this hand.")
    assert coach._voice_output.speak.call_count == 1

    coach._voice_output.reset_mock()
    coach.speak_advice("Mulligan this hand.")
    assert coach._voice_output.speak.call_count == 1


def test_is_passive_advice():
    p = StandaloneCoach._is_passive_advice
    # Passive "do nothing" lines
    assert p("pass priority.") is True
    assert p("Wait.") is True
    assert p("Wait (Opponent has priority)") is True
    assert p("No actions available.") is True
    # Real actions are NOT passive (even if they mention pass)
    assert p("Decline the optional action and pass priority.") is False
    assert p("Block their Grizzly Bears with your Llanowar Elves.") is False
    assert p("Cast Lightning Bolt.") is False
    assert p("Attack with Michelangelo, Weirdness to 11.") is False
    # Empty / None
    assert p("") is False
    assert p(None) is False
