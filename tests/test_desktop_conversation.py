"""Desktop side of Conversation Mode: the engine's mode status and the proactive-only sidebar."""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject

pytest.importorskip("PySide6")
from arenamcp.desktop.coach_session import CoachSession
from arenamcp.desktop.compact_coach import CompactCoachPanel
from arenamcp.desktop.tts_manager import TtsManager
from arenamcp.settings import DEFAULTS


def test_settings_defaults_include_conversation_keys():
    assert DEFAULTS["conversation_mode"] == "turn_advice"
    assert DEFAULTS["auto_queue_enabled"] is False


# ---------------------------------------------------------------------------
# CoachSession signal wiring (no subprocess — events fed directly)
# ---------------------------------------------------------------------------


@pytest.fixture
def session(qapp, monkeypatch):
    """CoachSession with the TTS worker start disabled (no subprocess)."""
    monkeypatch.setattr(TtsManager, "start", Mock())
    s = CoachSession()
    yield s
    s.shutdown()


def test_mode_status_event_maps_to_mode_changed(session):
    received: list[str] = []
    statuses: list[tuple[str, str]] = []
    session.modeChanged.connect(received.append)
    session.statusChanged.connect(lambda k, v: statuses.append((k, v)))
    session._handle_process_event({"type": "status", "key": "MODE", "value": "conversation"})
    assert received == ["conversation"]
    assert statuses == [("MODE", "conversation")]


def test_set_mode_stops_speech_and_sends_command(session, monkeypatch):
    commands: list[tuple[str, Any]] = []
    stop_calls: list[bool] = []
    monkeypatch.setattr(session._tts, "stop_speech", lambda: stop_calls.append(True))
    monkeypatch.setattr(session, "send_command", lambda cmd, *a: commands.append((cmd, a)))
    session.set_mode("conversation")
    assert stop_calls == [True]
    assert commands == [("set_mode", ("conversation",))]


# ---------------------------------------------------------------------------
# CompactCoachPanel keeps the desktop on turn advice (Mock session)
# ---------------------------------------------------------------------------


class MockSession(QObject):
    """Mock CoachSession: a real QObject (needed by Qt signal machinery) with
    CoachSession's signal set — but no subprocess."""

    gameStateChanged = CoachSession.gameStateChanged
    turnPlanChanged = CoachSession.turnPlanChanged
    gamePlanChanged = CoachSession.gamePlanChanged
    statusChanged = CoachSession.statusChanged
    spokenLine = CoachSession.spokenLine
    adviceReceived = CoachSession.adviceReceived
    logEmitted = CoachSession.logEmitted
    errorOccurred = CoachSession.errorOccurred
    mctsUpdated = CoachSession.mctsUpdated
    bugReportSaved = CoachSession.bugReportSaved
    modeChanged = CoachSession.modeChanged
    started = CoachSession.started

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[str, Any]] = []

    def toggle_autopilot(self) -> None:
        self.commands.append(("toggle_autopilot", ()))

    def set_auto_queue(self, enabled: bool) -> None:
        self.commands.append(("set_auto_queue", enabled))

    def toggle_mute(self) -> None:
        self.commands.append(("toggle_mute", ()))

    def toggle_style(self) -> None:
        self.commands.append(("toggle_style", ()))

    def cycle_speed(self) -> None:
        self.commands.append(("cycle_speed", ()))

    def cycle_voice(self) -> None:
        self.commands.append(("cycle_voice", ()))

    def set_mode(self, mode: str) -> None:
        self.commands.append(("set_mode", mode))

    def stop_speaking(self) -> None:
        self.commands.append(("stop_speech", ()))

    def trigger_debug_report(self) -> None:
        self.commands.append(("debug_report", ()))


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    """Fresh Settings instance backed by tmp_path — the panel fixture must
    never read/write the user's real ~/.arenamcp/settings.json."""
    import arenamcp.settings as settings_module
    from arenamcp.settings import Settings

    real_dir = settings_module.SETTINGS_DIR
    real_file = settings_module.SETTINGS_FILE
    monkeypatch.setattr(settings_module, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", tmp_path / "settings.json")
    instance = Settings()
    monkeypatch.setattr(settings_module, "get_settings", lambda: instance)
    yield instance
    monkeypatch.setattr(settings_module, "SETTINGS_DIR", real_dir, raising=False)
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", real_file, raising=False)


@pytest.fixture
def panel(qapp, isolated_settings):
    s = MockSession()
    p = CompactCoachPanel(session=s)  # type: ignore[arg-type]
    yield p
    with contextlib.suppress(RuntimeError):
        p.close()


def test_saved_chat_preference_cannot_disable_proactive_desktop(panel, isolated_settings):
    isolated_settings.set("conversation_mode", "conversation")
    p = CompactCoachPanel(session=panel.session)  # type: ignore[arg-type]
    try:
        assert p._settings is isolated_settings
        p.session.started.emit()
        assert ("set_mode", "turn_advice") in p.session.commands
        assert not p.log_view.isHidden()
        assert not hasattr(p, "chat_mode_btn")
    finally:
        p.close()


def test_stale_chat_mode_ack_requests_proactive_coaching(panel):
    panel.session.modeChanged.emit("conversation")
    assert panel.session.commands == [("set_mode", "turn_advice")]
    assert not panel.log_view.isHidden()
    assert panel.feed_caption.text() == "FEED"


def test_unknown_mode_ack_leaves_proactive_feed_visible(panel):
    panel.session.modeChanged.emit("weird_mode")
    assert panel.session.commands == []
    assert not panel.log_view.isHidden()
