"""Conversation Mode desktop/settings-layer tests (Wave 2a Worker D)."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from typing import Any
from unittest.mock import Mock

import pytest

from PySide6.QtCore import QObject, Signal as QtSignal

pytest.importorskip("PySide6")
from arenamcp.desktop.compact_coach import CompactCoachPanel
from arenamcp.desktop.conversation_transcript import ConversationTranscript
from arenamcp.desktop.coach_session import CoachSession
from arenamcp.desktop.ptt import VOICE_UNAVAILABLE_TOOLTIP, PttController
from arenamcp.desktop.tts_manager import TtsManager
from arenamcp.settings import DEFAULTS


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------


def test_settings_defaults_include_conversation_keys():
    assert DEFAULTS["conversation_mode"] == "turn_advice"
    assert DEFAULTS["conversation_verbosity"] == "balanced"


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


def test_conversation_reply_event_emits_signal(session):
    received: list[str] = []
    session.conversationReply.connect(received.append)
    session._handle_process_event({"type": "conversation_reply", "text": "attack now"})
    assert received == ["attack now"]


def test_empty_conversation_reply_is_ignored(session):
    received: list[str] = []
    session.conversationReply.connect(received.append)
    session._handle_process_event({"type": "conversation_reply", "text": ""})
    assert received == []


def test_mode_status_event_maps_to_mode_changed(session):
    received: list[str] = []
    statuses: list[tuple[str, str]] = []
    session.modeChanged.connect(received.append)
    session.statusChanged.connect(lambda k, v: statuses.append((k, v)))
    session._handle_process_event({"type": "status", "key": "MODE", "value": "conversation"})
    assert received == ["conversation"]
    assert statuses == [("MODE", "conversation")]


def test_convo_state_status_event_maps_to_conversation_status(session):
    received: list[str] = []
    session.conversationStatus.connect(received.append)
    session._handle_process_event({"type": "status", "key": "CONVO_STATE", "value": "thinking"})
    assert received == ["thinking"]


def test_set_mode_stops_speech_and_sends_command(session, monkeypatch):
    commands: list[tuple[str, Any]] = []
    stop_calls: list[bool] = []
    monkeypatch.setattr(session._tts, "stop_speech", lambda: stop_calls.append(True))
    monkeypatch.setattr(session, "send_command", lambda cmd, *a: commands.append((cmd, a)))
    session.set_mode("conversation")
    assert stop_calls == [True]
    assert commands == [("set_mode", ("conversation",))]


def test_set_verbosity_and_stop_speaking(session, monkeypatch):
    commands: list[tuple[str, Any]] = []
    stop_calls: list[bool] = []
    monkeypatch.setattr(session._tts, "stop_speech", lambda: stop_calls.append(True))
    monkeypatch.setattr(session, "send_command", lambda cmd, *a: commands.append((cmd, a)))
    session.set_verbosity("detailed")
    session.stop_speaking()
    assert commands == [("set_verbosity", ("detailed",)), ("stop_speech", ())]
    assert stop_calls == [True]


def test_stop_speaking_sends_stop_speech_command(session, monkeypatch):
    commands: list[tuple[str, Any]] = []
    monkeypatch.setattr(session._tts, "stop_speech", lambda: None)
    monkeypatch.setattr(session, "send_command", lambda cmd, *a: commands.append((cmd, a)))
    session.stop_speaking()
    assert commands == [("stop_speech", ())]


def test_speak_request_passes_priority_and_identity(session, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_request(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(session._tts, "request_speech", fake_request)
    identity = {"session_id": 2, "request_id": 7, "mode": "conversation"}
    session._handle_process_event(
        {
            "type": "speak_request",
            "text": "consider blocking",
            "priority": "urgent",
            "identity": identity,
        }
    )
    assert captured["text"] == "consider blocking"
    assert captured["priority"] == "urgent"
    assert captured["identity"] == identity


def test_speak_stop_event_halts_desktop_tts(session, monkeypatch):
    """C2: the engine's speak_stop event (engine-initiated preemption —
    typed question, urgent topic) must halt desktop TTS playback."""
    stop_calls: list[bool] = []
    monkeypatch.setattr(session._tts, "stop_speech", lambda: stop_calls.append(True))
    session._handle_process_event({"type": "speak_stop"})
    assert stop_calls == [True]
    # Idempotent: repeated events are safe.
    session._handle_process_event({"type": "speak_stop"})
    assert stop_calls == [True, True]


def test_speak_stop_event_holds_tts_generation_gate(qapp, monkeypatch):
    """C2 + M4 interplay: a rendered-but-late request invalidated by a
    speak_stop is discarded even if the worker renders it afterwards."""
    manager = _ready_manager(monkeypatch)
    manager.request_speech(
        text="old utterance", voice_id="af_heart", voice_name="Heart", speed=1.0
    )
    generation = manager._generation
    manager.stop_speech()  # engine-initiated preempt lands as a stop
    assert manager._pending_request is None

    play = Mock(return_value=True)
    cleanup = Mock()
    monkeypatch.setattr("arenamcp.desktop.tts_manager.AudioPlayback.play_file", play)
    monkeypatch.setattr(manager, "_cleanup_path", cleanup)
    manager._handle_stdout_line(
        json.dumps({"type": "rendered", "generation": generation, "path": "late.wav"})
    )
    play.assert_not_called()
    cleanup.assert_called_once()


def test_speak_request_without_priority_is_legacy(session, monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(session._tts, "request_speech", lambda **kw: captured.update(kw))
    session._handle_process_event({"type": "speak_request", "text": "play a land"})
    assert "priority" not in captured
    assert "identity" not in captured


# ---------------------------------------------------------------------------
# TtsManager priority/identity arbitration
# ---------------------------------------------------------------------------


def _ready_manager(monkeypatch) -> TtsManager:
    manager = TtsManager()
    monkeypatch.setattr(TtsManager, "is_running", property(lambda self: True))
    monkeypatch.setattr(manager, "_dispatch_pending", Mock())
    monkeypatch.setattr(manager, "_stop_playback", Mock())
    monkeypatch.setattr(manager, "_stop_say", Mock())
    return manager


def test_proactive_request_dropped_when_question_pending(qapp, monkeypatch):
    manager = _ready_manager(monkeypatch)
    manager.request_speech(
        text="answer", voice_id="af_heart", voice_name="Heart", speed=1.0, priority="question"
    )
    pending_before = manager._pending_request
    manager.request_speech(
        text="filler", voice_id="af_heart", voice_name="Heart", speed=1.0, priority="proactive"
    )
    assert manager._pending_request is pending_before
    assert pending_before is not None and pending_before["text"] == "answer"


def test_identity_session_change_drops_older_pending(qapp, monkeypatch):
    manager = _ready_manager(monkeypatch)
    manager.request_speech(
        text="old session advice",
        voice_id="af_heart",
        voice_name="Heart",
        speed=1.0,
        identity={"session_id": 1, "request_id": 1},
    )
    pending_old = manager._pending_request
    assert pending_old is not None
    stale_generation = pending_old["generation"]
    manager.request_speech(
        text="new session advice",
        voice_id="af_heart",
        voice_name="Heart",
        speed=1.0,
        identity={"session_id": 2, "request_id": 2},
    )
    pending_new = manager._pending_request
    assert pending_new is not None and pending_new["text"] == "new session advice"
    # The stale request's generation no longer matches the live one
    assert stale_generation != manager._generation
    assert pending_new["generation"] == manager._generation


def test_identity_same_session_replaces_normally(qapp, monkeypatch):
    manager = _ready_manager(monkeypatch)
    manager.request_speech(
        text="turn 3 advice",
        voice_id="af_heart",
        voice_name="Heart",
        speed=1.0,
        identity={"session_id": 5, "request_id": 1},
    )
    manager.request_speech(
        text="turn 4 advice",
        voice_id="af_heart",
        voice_name="Heart",
        speed=1.0,
        identity={"session_id": 5, "request_id": 2},
    )
    pending = manager._pending_request
    assert pending is not None and pending["text"] == "turn 4 advice"


def test_request_without_identity_never_dropped(qapp, monkeypatch):
    manager = _ready_manager(monkeypatch)
    manager.request_speech(text="legacy advice", voice_id="af_heart", voice_name="Heart", speed=1.0)
    manager.request_speech(text="newer legacy", voice_id="af_heart", voice_name="Heart", speed=1.0)
    pending = manager._pending_request
    assert pending is not None and pending["text"] == "newer legacy"


def test_speech_started_and_stopped_signals(qapp, monkeypatch):
    manager = TtsManager()
    started: list[bool] = []
    stopped: list[bool] = []
    manager.speechStarted.connect(lambda: started.append(True))
    manager.speechStopped.connect(lambda: stopped.append(True))
    manager._generation = 1
    manager._current_audio_path = None
    play = Mock(return_value=True)
    monkeypatch.setattr("arenamcp.desktop.tts_manager.AudioPlayback.play_file", play)
    manager._handle_stdout_line(
        json.dumps({"type": "rendered", "generation": 1, "path": "voice.wav"})
    )
    assert started == [True]

    manager._stop_playback()
    assert stopped == [True]


# ---------------------------------------------------------------------------
# ConversationTranscript
# ---------------------------------------------------------------------------


@pytest.fixture
def transcript(qapp):
    w = ConversationTranscript()
    yield w


def test_transcript_add_entry_roles(transcript):
    transcript.add_entry("user", "why not attack?")
    transcript.add_entry("coach", "they have mana open")
    transcript.add_entry("system", "voice unavailable")
    text = transcript.toPlainText()
    assert "You: why not attack?" in text
    assert "Coach: they have mana open" in text
    assert "voice unavailable" in text


def test_transcript_newest_at_bottom_and_autoscroll(transcript, qapp):
    transcript.show()
    transcript.resize(300, 120)
    for i in range(50):
        transcript.add_entry("coach", f"line {i}")
    qapp.processEvents()
    sb = transcript.verticalScrollBar()
    assert sb.value() == sb.maximum()
    assert transcript.toPlainText().splitlines()[-1] == "Coach: line 49"


def test_transcript_no_autoscroll_when_scrolled_up(transcript, qapp):
    for i in range(50):
        transcript.add_entry("coach", f"line {i}")
    transcript.show()
    transcript.resize(300, 120)
    qapp.processEvents()
    sb = transcript.verticalScrollBar()
    sb.setValue(0)
    qapp.processEvents()
    transcript.add_entry("coach", "newest line")
    qapp.processEvents()
    assert sb.value() == 0


def test_transcript_set_pending_placeholder(transcript):
    transcript.add_entry("user", "hello")
    transcript.set_pending(True)
    assert "…thinking" in transcript.toPlainText()
    transcript.set_pending(False)
    assert "…thinking" not in transcript.toPlainText()
    assert "hello" in transcript.toPlainText()


def test_transcript_trims_to_500_blocks(transcript):
    for i in range(520):
        transcript.add_entry("system", f"line {i}")
    assert transcript.document().blockCount() <= 500
    lines = transcript.toPlainText().splitlines()
    assert lines[-1] == "line 519"


def test_transcript_empty_entry_ignored(transcript):
    transcript.add_entry("user", "")
    assert transcript.toPlainText() == ""


# ---------------------------------------------------------------------------
# CompactCoachPanel mode/verbosity/stop controls (Mock session)
# ---------------------------------------------------------------------------


class _SignalProxy:
    """Stands in for a bound Qt signal on the mock session (connect/emit)."""

    def __init__(self, session: Any, name: str) -> None:
        self._session = session
        self._name = name
        self._callbacks: list[Any] = []

    def connect(self, callback: Any) -> None:
        self._callbacks.append(callback)

    def emit(self, *args: Any) -> None:
        for cb in list(self._callbacks):
            cb(*args)


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
    telemetryUpdated = CoachSession.telemetryUpdated
    reasoningChunk = CoachSession.reasoningChunk
    mctsUpdated = CoachSession.mctsUpdated
    bugReportSaved = CoachSession.bugReportSaved
    modeChanged = CoachSession.modeChanged
    conversationReply = CoachSession.conversationReply
    conversationStatus = CoachSession.conversationStatus
    started = CoachSession.started

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[str, Any]] = []
        self.stopped_speech = 0
        self.notices: list[str] = []

    def emit_local_fallback_notice(self, message: str) -> None:
        self.notices.append(message)

    def toggle_autopilot(self) -> None:
        self.commands.append(("toggle_autopilot", ()))

    def toggle_mute(self) -> None:
        self.commands.append(("toggle_mute", ()))

    def toggle_style(self) -> None:
        self.commands.append(("toggle_style", ()))

    def cycle_speed(self) -> None:
        self.commands.append(("cycle_speed", ()))

    def cycle_voice(self) -> None:
        self.commands.append(("cycle_voice", ()))

    def send_chat(self, text: str) -> None:
        self.commands.append(("chat", text))

    def set_mode(self, mode: str) -> None:
        self.commands.append(("set_mode", mode))

    def set_verbosity(self, verbosity: str) -> None:
        self.commands.append(("set_verbosity", verbosity))

    def stop_speaking(self) -> None:
        self.stopped_speech += 1
        # Mirrors the real CoachSession: a stop also notifies the engine.
        self.commands.append(("stop_speech", ()))

    def trigger_debug_report(self) -> None:
        self.commands.append(("debug_report", ()))


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    """Fresh Settings instance backed by tmp_path — the panel fixture must
    never read/write the user's real ~/.arenamcp/settings.json (a prior run
    persisted e.g. 'quiet' and broke the verbosity-cycle expectations)."""
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


def _fake_ptt_controller(button, session, recorder=None, transcriber=None, on_send=None, parent=None):
    controller = Mock()
    controller.listening = False
    return controller


# Live Python references to every PTT button created by tests — prevents the
# PySide6 GC/lifetime SIGSEGV seen in offscreen runs (a widget whose Python
# wrapper dies can crash under gc_collect during module teardown).
_PTT_BUTTONS: list[Any] = []


def test_mode_button_clicks_send_set_mode(panel):
    panel.mode_btn.click()
    assert ("set_mode", "conversation") in panel.session.commands
    panel._on_mode_changed("conversation")
    panel.mode_btn.click()
    assert ("set_mode", "turn_advice") in panel.session.commands


def test_panel_reads_isolated_settings_not_singleton(panel, isolated_settings):
    """Regression: a controller persist call must write through the ISOLATED
    Settings instance (tmp_path) and the panel must read that instance at
    construction — never the process-wide singleton or the real
    ~/.arenamcp/settings.json. Before the compact_coach call-time lookup fix,
    the panel captured get_settings at import time, so the isolated_settings
    patch never reached it and a polluted singleton leaked through here."""
    from arenamcp.conversation import CONVERSATION

    # Controller persist write under isolation (goes to tmp_path instance).
    isolated_settings.set("conversation_mode", CONVERSATION)
    assert isolated_settings.get("conversation_mode") == CONVERSATION

    # A freshly constructed panel must see the isolated value.
    p = CompactCoachPanel(session=panel.session)  # type: ignore[arg-type]
    try:
        assert p._conversation_mode == CONVERSATION
    finally:
        with contextlib.suppress(RuntimeError):
            p.close()


def test_mode_ack_updates_button_and_views(panel):
    panel.session.modeChanged.emit("conversation")
    assert panel.mode_btn.text() == "Mode: Conversation"
    assert not panel.conversation_transcript.isHidden()
    assert panel.log_view.isHidden()
    assert panel.conversation_mode == "conversation"

    panel.session.modeChanged.emit("turn_advice")
    assert panel.mode_btn.text() == "Mode: Turn Advice"
    assert panel.conversation_transcript.isHidden()
    assert not panel.log_view.isHidden()
    assert panel.conversation_mode == "turn_advice"


def test_mode_ack_ignores_unknown_mode(panel):
    panel.session.modeChanged.emit("weird_mode")
    assert panel.conversation_mode == "turn_advice"


def test_verbosity_button_cycles(panel):
    panel.verbosity_btn.click()
    assert ("set_verbosity", "detailed") in panel.session.commands
    assert panel.verbosity_btn.text() == "Detail: Detailed"
    panel.verbosity_btn.click()
    assert ("set_verbosity", "quiet") in panel.session.commands


def test_verbosity_status_ack_updates_button(panel):
    panel.session.statusChanged.emit("VERBOSITY", "quiet")
    assert panel.verbosity_btn.text() == "Detail: Quiet"


def test_stop_speech_button_calls_session(panel):
    panel.stop_speech_btn.click()
    assert panel.session.stopped_speech == 1


def test_desktop_stop_clears_engine_arbiter(session, monkeypatch):
    """M1: UI stop must release the ENGINE arbiter channel (stop_speech
    command), so subsequent proactive speech is not cancelled by a channel
    that never freed."""
    commands: list[tuple[str, Any]] = []
    monkeypatch.setattr(session._tts, "stop_speech", lambda: None)
    monkeypatch.setattr(session, "send_command", lambda cmd, *a: commands.append((cmd, a)))
    session.stop_speaking()
    assert ("stop_speech", ()) in commands


def test_stop_button_cancels_engine_pending(monkeypatch):
    """M1: engine-side stop_speech dispatch cancels pending conversation work
    — no conversation_reply/speech after stop during a slow get_advice."""
    from unittest.mock import MagicMock

    from tests.test_conversation import make_controller, wait_thread

    import arenamcp.conversation as conversation_mod
    from arenamcp.conversation import CONVERSATION
    from arenamcp.pipe_adapter import PipeAdapter

    gate = threading.Event()

    def slow_advice(snapshot, question=None, **kw):
        gate.wait(timeout=5.0)
        return "late answer that must never play"

    ctrl, coach, voice = make_controller()
    coach._coach.get_advice.side_effect = slow_advice
    coach.conversation = ctrl  # the adapter must find the REAL controller
    monkeypatch.setattr(
        conversation_mod, "get_settings", lambda: MagicMock(set=lambda k, v: None)
    )
    ctrl.set_mode(CONVERSATION, persist=False)

    adapter = PipeAdapter()
    adapter._emit = lambda event: None  # type: ignore[method-assign]
    adapter._coach = coach

    ctrl.on_user_question("why not attack?")
    # Wait until the answer thread registers the request as pending, then
    # deliver the UI stop mid-render.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not ctrl._pending:
        time.sleep(0.01)
    adapter._dispatch({"cmd": "stop_speech"})
    assert ctrl._pending == {}

    gate.set()
    wait_thread(ctrl)

    reply_events = [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"]
    assert reply_events == []
    assert voice.spoken == []


def test_ptt_press_sends_stop_speech_command(ptt_button, qapp):
    """M1: a PTT press additionally notifies the engine (stop_speech)."""
    session = MockSession()
    recorder = FakeRecorder()
    transcriber = FakeTranscriber()
    PttController(ptt_button, session, recorder=recorder, transcriber=transcriber)
    ptt_button.pressed.emit()
    assert ("stop_speech", ()) in session.commands


def test_conversation_reply_routes_to_transcript(panel):
    panel.session.conversationReply.emit("cast the counterspell")
    assert "Coach: cast the counterspell" in panel.conversation_transcript.toPlainText()


def test_conversation_status_updates_label(panel):
    panel.session.conversationStatus.emit("speaking")
    assert "Speaking" in panel.conversation_status_label.text()


def test_chat_input_routed_by_mode(panel):
    panel.chat_input.setText("why not attack?")
    panel.send_chat()
    assert ("chat", "why not attack?") in panel.session.commands

    panel.session.modeChanged.emit("conversation")
    panel.chat_input.setText("what changed?")
    panel.send_chat()
    assert ("chat", "what changed?") in panel.session.commands
    assert "You: what changed?" in panel.conversation_transcript.toPlainText()


def test_ptt_route_logs_user_entry(panel):
    panel.session.modeChanged.emit("conversation")
    panel._send_ptt_text("explain that")
    assert ("chat", "explain that") in panel.session.commands
    assert "You: explain that" in panel.conversation_transcript.toPlainText()


# ---------------------------------------------------------------------------
# PttController — fake recorder/transcriber happy path + degraded path
# ---------------------------------------------------------------------------


class FakeRecorder:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.wav = b"fake-wav"

    def start(self) -> None:
        self.started += 1

    def stop(self) -> bytes:
        self.stopped += 1
        return self.wav


class FakeTranscriber:
    def __init__(self, text: str = "why not attack") -> None:
        self._text = text
        self.transcribed: list[bytes] = []

    def transcribe(self, wav_bytes: bytes) -> str:
        self.transcribed.append(wav_bytes)
        return self._text


@pytest.fixture
def ptt_button(qapp):
    from PySide6.QtWidgets import QPushButton

    # Parent the button to the session-scoped app's top-level widget pool so a
    # GC'd Python wrapper can't leave the C++ object dangling (SIGSEGV under
    # gc_collect in offscreen runs).
    btn = QPushButton("🎙 Hold to talk")
    _PTT_BUTTONS.append(btn)
    return btn


def test_ptt_happy_path(ptt_button, qapp):
    session = MockSession()
    recorder = FakeRecorder()
    transcriber = FakeTranscriber()
    controller = PttController(ptt_button, session, recorder=recorder, transcriber=transcriber)

    assert ptt_button.isEnabled()
    ptt_button.pressed.emit()
    assert recorder.started == 1
    assert session.stopped_speech == 1
    assert controller.listening

    ptt_button.released.emit()
    assert recorder.stopped == 1
    assert transcriber.transcribed == [b"fake-wav"]
    assert ("chat", "why not attack") in session.commands
    assert not controller.listening


def test_ptt_disabled_when_transcriber_missing(ptt_button, qapp):
    session = MockSession()
    controller = PttController(ptt_button, session, recorder=FakeRecorder(), transcriber=None)
    assert not ptt_button.isEnabled()
    assert ptt_button.toolTip() == VOICE_UNAVAILABLE_TOOLTIP
    ptt_button.pressed.emit()  # disabled button never fires, but guard anyway
    assert session.stopped_speech == 0
    assert not controller.listening


def test_ptt_disabled_when_recorder_missing(ptt_button, qapp, monkeypatch):
    import arenamcp.desktop.ptt as ptt_module

    monkeypatch.setattr(ptt_module, "default_recorder", Mock(side_effect=ImportError("no mic")))
    session = MockSession()
    controller = PttController(ptt_button, session, recorder=None, transcriber=FakeTranscriber())
    assert not ptt_button.isEnabled()
    assert ptt_button.toolTip() == VOICE_UNAVAILABLE_TOOLTIP


def test_ptt_dependency_probe_monkeypatched(ptt_button, qapp, monkeypatch):
    """Import-guard probe: force 'faster_whisper missing' result."""
    import arenamcp.desktop.ptt as ptt_module

    monkeypatch.setattr(ptt_module, "default_transcriber", Mock(side_effect=ImportError("no fw")))
    session = MockSession()
    PttController(ptt_button, session, recorder=FakeRecorder())
    assert not ptt_button.isEnabled()
    assert ptt_button.toolTip() == VOICE_UNAVAILABLE_TOOLTIP


def test_ptt_empty_transcription_not_sent(ptt_button, qapp):
    session = MockSession()
    controller = PttController(
        ptt_button, session, recorder=FakeRecorder(), transcriber=FakeTranscriber(text="")
    )
    ptt_button.pressed.emit()
    ptt_button.released.emit()
    # Only the stop_speech notification happened — no chat send.
    assert session.commands == [("stop_speech", ())]
    # Empty transcription is an info-level notice on the transcript surface
    # (not a chat send) — the user must know the press produced nothing.
    assert "no speech detected" in session.notices[0]


def test_ptt_silent_recording_not_sent(ptt_button, qapp):
    session = MockSession()

    class SilentRecorder(FakeRecorder):
        def stop(self) -> bytes:
            return b""

    PttController(ptt_button, session, recorder=SilentRecorder(), transcriber=FakeTranscriber())
    ptt_button.pressed.emit()
    ptt_button.released.emit()
    assert session.commands == [("stop_speech", ())]
    assert "no speech detected" in session.notices[0]


# ---------------------------------------------------------------------------
# M5 — PTT failure notices ([LOCAL FALLBACK])
# ---------------------------------------------------------------------------


def test_ptt_transcribe_failure_emits_local_fallback_notice(ptt_button, qapp):
    session = MockSession()

    class BoomTranscriber:
        def transcribe(self, wav_bytes: bytes) -> str:
            raise RuntimeError("whisper exploded")

    PttController(ptt_button, session, recorder=FakeRecorder(), transcriber=BoomTranscriber())
    ptt_button.pressed.emit()
    ptt_button.released.emit()

    assert len(session.notices) == 1
    assert "[LOCAL FALLBACK]" in session.notices[0]
    assert "transcription failed" in session.notices[0]
    assert session.commands == [("stop_speech", ())]


def test_ptt_mic_failure_mid_press_recovers(ptt_button, qapp):
    """recorder.start() failure emits a mic notice AND the button re-arms —
    the next press works normally."""
    session = MockSession()

    class FlakyRecorder(FakeRecorder):
        def __init__(self) -> None:
            super().__init__()
            self.fail_first_start = True

        def start(self) -> None:
            if self.fail_first_start:
                self.fail_first_start = False
                raise RuntimeError("device busy")
            self.started += 1

    recorder = FlakyRecorder()
    controller = PttController(ptt_button, session, recorder=recorder, transcriber=FakeTranscriber())

    # First press fails with a user-visible mic notice.
    ptt_button.pressed.emit()
    assert not controller.listening
    assert len(session.notices) == 1
    assert "[LOCAL FALLBACK]" in session.notices[0]
    assert "microphone" in session.notices[0]

    # Button re-armed: the next press works end-to-end.
    ptt_button.pressed.emit()
    assert controller.listening
    ptt_button.released.emit()
    assert ("chat", "why not attack") in session.commands


def test_ptt_recorder_stop_failure_emits_notice(ptt_button, qapp):
    session = MockSession()

    class BoomStopRecorder(FakeRecorder):
        def stop(self) -> bytes:
            raise RuntimeError("stream dead")

    PttController(ptt_button, session, recorder=BoomStopRecorder(), transcriber=FakeTranscriber())
    ptt_button.pressed.emit()
    ptt_button.released.emit()

    assert len(session.notices) == 1
    assert "[LOCAL FALLBACK]" in session.notices[0]
    assert "microphone" in session.notices[0]
    assert session.commands == [("stop_speech", ())]


def test_ptt_no_speech_info_differs_from_failure_notice(ptt_button, qapp):
    """'no speech detected' is info-level (no 'failed' wording); a
    transcription error is a distinct failure notice."""
    session = MockSession()
    PttController(ptt_button, session, recorder=FakeRecorder(), transcriber=FakeTranscriber(text=""))
    ptt_button.pressed.emit()
    ptt_button.released.emit()
    assert "no speech detected" in session.notices[0]
    assert "failed" not in session.notices[0]

