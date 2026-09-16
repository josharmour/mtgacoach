import json
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.tts_manager import TtsManager


def test_new_kokoro_request_stops_fallback_voice(qapp, monkeypatch):
    manager = TtsManager()
    monkeypatch.setattr(TtsManager, "is_running", property(lambda self: True))
    monkeypatch.setattr(manager, "_dispatch_pending", Mock())
    monkeypatch.setattr(manager, "_stop_playback", Mock())
    fallback = Mock()
    fallback.poll.return_value = None
    manager._say_process = fallback

    manager.request_speech(text="Play a land", voice_id="af_heart", voice_name="Heart", speed=1.0)

    fallback.terminate.assert_called_once()
    assert manager._say_process is None


def test_rendered_audio_stops_fallback_before_playback(qapp, monkeypatch):
    manager = TtsManager()
    fallback = Mock()
    fallback.poll.return_value = None
    manager._say_process = fallback

    def play_file(path):
        fallback.terminate.assert_called_once()
        return True

    monkeypatch.setattr("arenamcp.desktop.tts_manager.AudioPlayback.play_file", play_file)
    manager._handle_stdout_line(json.dumps({"type": "rendered", "generation": 0, "path": "voice.wav"}))

    assert manager._say_process is None


def test_slow_render_does_not_block_ui_or_replay_advice(qapp, monkeypatch):
    manager = TtsManager()
    manager._busy = True
    manager._last_text = "Old advice"
    pending = {"text": "Latest advice"}
    manager._pending_request = pending
    shutdown = Mock()
    start = Mock()
    fallback = Mock()
    monkeypatch.setattr(manager, "shutdown", shutdown)
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "_speak_fallback", fallback)

    manager._on_busy_timeout()

    shutdown.assert_not_called()
    start.assert_not_called()
    fallback.assert_not_called()
    assert manager._busy
    assert manager._pending_request is pending


def test_shutdown_rejects_late_speech(qapp, monkeypatch):
    manager = TtsManager()
    play = Mock()
    cleanup = Mock()
    start = Mock()
    monkeypatch.setattr("arenamcp.desktop.tts_manager.AudioPlayback.play_file", play)
    monkeypatch.setattr(manager, "_cleanup_path", cleanup)
    monkeypatch.setattr(manager, "start", start)
    manager.shutdown()

    manager.request_speech(text="Late advice", voice_id="af_heart", voice_name="Heart", speed=1.0)
    manager._handle_stdout_line(json.dumps({"type": "rendered", "generation": 0, "path": "late.wav"}))

    start.assert_not_called()
    play.assert_not_called()
    cleanup.assert_called_once()
