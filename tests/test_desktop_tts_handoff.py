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


# ---------------------------------------------------------------------------
# M4 — fallback-after-stop must never re-speak the cancelled utterance
# ---------------------------------------------------------------------------


def test_tts_error_after_stop_does_not_speak_last_text(qapp, monkeypatch):
    """M4: stop_speech clears _last_text and invalidates its generation — a
    late worker error must not trigger the say/SAPI fallback for the
    cancelled utterance."""
    manager = TtsManager()
    fallback = Mock()
    monkeypatch.setattr(manager, "_speak_via_say", fallback)
    manager._generation = 1
    manager._last_text = "Cancelled utterance"
    manager._last_speed = 1.0
    manager._last_text_generation = 1

    manager.stop_speech()
    assert manager._last_text == ""
    assert manager._last_text_generation == -1

    manager._handle_stdout_line(
        json.dumps({"type": "error", "generation": 1, "message": "render failed"})
    )
    fallback.assert_not_called()


def test_tts_transient_error_speaks_last_text_when_generation_current(qapp, monkeypatch):
    """The fallback keeps working for a CURRENT utterance (no regression)."""
    manager = TtsManager()
    fallback = Mock()
    monkeypatch.setattr(manager, "_speak_via_say", fallback)
    monkeypatch.setattr("arenamcp.desktop.tts_manager.sys.platform", "darwin")
    manager._last_text = "Current utterance"
    manager._last_speed = 1.0
    manager._last_text_generation = manager._generation  # current

    manager._handle_stdout_line(
        json.dumps({"type": "error", "generation": manager._generation, "message": "render failed"})
    )
    fallback.assert_called_once_with("Current utterance", 1.0)


def test_tts_shutdown_clears_last_text(qapp):
    manager = TtsManager()
    manager._last_text = "Something"
    manager.shutdown()
    assert manager._last_text == ""


def test_tts_worker_exit_after_stop_does_not_fallback(qapp, monkeypatch):
    manager = TtsManager()
    fallback = Mock()
    monkeypatch.setattr(manager, "_speak_via_say", fallback)
    monkeypatch.setattr(manager, "_speak_fallback", Mock())
    manager._generation = 2
    manager._last_text = "Cancelled"
    manager._last_text_generation = 2
    manager.stop_speech()

    manager._on_finished(1, None)
    fallback.assert_not_called()
