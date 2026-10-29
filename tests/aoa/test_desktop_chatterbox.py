import json
import threading
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from arenamcp import chatterbox_remote, settings
from arenamcp.desktop import tts_manager
from arenamcp.desktop.tts_manager import TtsManager


@pytest.fixture
def manager(monkeypatch):
    app = QApplication.instance() or QApplication([])
    m = TtsManager()
    values = {
        "tts_provider": "chatterbox_remote",
        "chatterbox_server_url": "https://speech.example/v1",
        "chatterbox_key_file": "/unused/test.key",
        "chatterbox_voice": "mira",
    }
    monkeypatch.setattr(settings, "get_settings", lambda: values)
    monkeypatch.setattr(TtsManager, "is_running", property(lambda self: True))
    monkeypatch.setattr(m, "_dispatch_pending", Mock())
    monkeypatch.setattr(m, "_stop_playback", Mock())
    monkeypatch.setattr(m, "_stop_say", Mock())
    monkeypatch.setattr(m, "_speak_fallback", Mock())
    monkeypatch.setattr(tts_manager.AudioPlayback, "play_file", Mock(return_value=True))
    monkeypatch.setattr(tts_manager.AudioPlayback, "is_playing", Mock(return_value=False))
    statuses = []
    m.speechStatus.connect(lambda uid, state: statuses.append((uid, state)))
    m.statuses = statuses
    m.test_app = app
    m.test_settings = values
    yield m
    m.stop_speech()
    m._busy_timer.stop()


def speak(m, uid="u1"):
    m.request_speech(text="Hello.", voice_id="am_adam", voice_name="Adam", speed=1, speech_id=uid)


def test_selection_uses_separate_voice_and_qt_thread_delivery(manager, monkeypatch):
    calls = []
    completed = threading.Event()

    def render(**kwargs):
        calls.append(kwargs)
        completed.set()
        return b"fake audio; playback is mocked"

    monkeypatch.setattr(chatterbox_remote, "render_remote", render)
    speak(manager)
    assert completed.wait(2)
    # Acquire after renderer returns to ensure the result has been emitted.
    with manager._chatterbox_lock:
        pass
    assert manager.statuses == []  # worker emitted; GUI has not processed it
    manager.test_app.processEvents()
    assert manager.statuses == [("u1", "started")]
    assert calls[0]["voice"] == "server_default"
    assert calls[0]["key_file"] == "/unused/test.key"
    manager._check_playback()
    assert manager.statuses[-1] == ("u1", "finished")


def test_busy_chatterbox_does_not_start_another_request_or_switch_provider(manager, monkeypatch):
    render = Mock()
    monkeypatch.setattr(chatterbox_remote, "render_remote", render)
    manager._chatterbox_lock.acquire()
    try:
        speak(manager)
        render.assert_not_called()
        assert manager._pending_request is None
        manager._dispatch_pending.assert_not_called()
        manager._speak_fallback.assert_not_called()
        assert manager.statuses == [("u1", "failed")]
    finally:
        manager._chatterbox_lock.release()


def test_stop_discards_late_audio_and_stale_failure_does_not_change_new_busy_state(manager, tmp_path):
    manager._generation = 7
    manager._tracked = (7, "old")
    manager._remote_cancel = threading.Event()
    manager.stop_speech()
    assert manager._remote_cancel.is_set()
    stale = tmp_path / "stale.wav"
    stale.write_bytes(b"audio")
    manager._busy = True
    manager._on_remote_rendered(7, str(stale), 1)
    manager._on_remote_failed(7, "Hello", "v", "V", 1, "failed")
    assert not stale.exists() and manager._busy
    tts_manager.AudioPlayback.play_file.assert_not_called()
    assert manager.statuses == [("old", "stopped")]


def test_worker_error_during_remote_request_cannot_start_competing_fallback(manager):
    manager._generation = manager._remote_generation = 4
    manager._last_text_generation = 4
    manager._last_text = "Hello"
    manager._handle_stdout_line(json.dumps({"type": "error", "message": "Kokoro worker init failed"}))
    manager._speak_fallback.assert_not_called()
    assert manager._worker_failed


def test_kokoro_default_retains_original_voice_and_never_calls_chatterbox(manager, monkeypatch):
    manager.test_settings["tts_provider"] = "kokoro"
    remote = Mock()
    monkeypatch.setattr(manager, "_request_chatterbox_speech", remote)
    monkeypatch.setattr(manager, "_should_offload_to_remote", lambda: False)
    speak(manager)
    remote.assert_not_called()
    assert manager._pending_request["voice_id"] == "am_adam"
    assert manager._last_text_generation == manager._generation


def test_remote_failure_can_start_local_kokoro_without_reading_live_settings(manager, monkeypatch):
    running = [False]
    monkeypatch.setattr(TtsManager, "is_running", property(lambda self: running[0]))
    monkeypatch.setattr(manager, "start", lambda: running.__setitem__(0, True))
    manager._generation = 8
    manager._tracked = (8, "u8")
    manager._on_remote_failed(8, "Hello", "am_adam", "Adam", 1, "busy")
    assert running[0] and manager._pending_request["generation"] == 8
    manager._speak_fallback.assert_not_called()
