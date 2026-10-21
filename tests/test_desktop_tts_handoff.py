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
    manager._last_text = "Something"
    manager.shutdown()
    assert manager._last_text == ""  # nothing for a fallback voice to re-speak

    manager.request_speech(text="Late advice", voice_id="af_heart", voice_name="Heart", speed=1.0)
    manager._handle_stdout_line(json.dumps({"type": "rendered", "generation": 0, "path": "late.wav"}))

    start.assert_not_called()
    play.assert_not_called()
    cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# Built-in voice fallbacks: macOS `say`, Windows SAPI via PowerShell
# ---------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def spawned(monkeypatch):
    """Every subprocess the fallback voices start: (argv, process)."""
    calls = []

    def popen(argv, **_kwargs):
        proc = Mock(stdin=_FakeStdin())
        proc.poll.return_value = None
        calls.append((argv, proc))
        return proc

    monkeypatch.setattr("arenamcp.desktop.tts_manager.subprocess.Popen", popen)
    return calls


@pytest.mark.parametrize(
    ("speed", "wpm"),
    [(1.0, 175), (1.2, 210), (0, 175), (0.01, 90), (10.0, 450)],
    ids=["normal", "faster", "unset", "floor", "ceiling"],
)
def test_say_fallback_speaks_at_a_clamped_rate(qapp, spawned, speed, wpm):
    manager = TtsManager()
    manager._speak_via_say("Hello there", speed)
    ((argv, proc),) = spawned
    assert argv == ["say", "-r", str(wpm)]
    assert proc.stdin.data == b"Hello there" and proc.stdin.closed
    manager._speak_via_say("Next line", 1.0)  # a new line stops the old one
    proc.terminate.assert_called_once()


@pytest.mark.parametrize(("speed", "rate"), [(1.0, 0), (2.0, 5), (9.0, 10), (-3.0, -10)])
def test_windows_fallback_escapes_quotes_and_clamps_the_rate(qapp, spawned, speed, rate):
    manager = TtsManager()
    manager._speak_via_windows("it's your turn", speed)
    ((argv, _proc),) = spawned
    assert argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    script = argv[4]
    assert "System.Speech" in script and f"$s.Rate = {rate};" in script
    assert "$s.Speak('it''s your turn')" in script  # PowerShell apostrophe escaping


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

    manager._handle_stdout_line(json.dumps({"type": "error", "generation": 1, "message": "render failed"}))
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


def test_remote_tts_offload_when_server_configured_and_lag_detected(qapp, monkeypatch):
    manager = TtsManager()
    settings_dict = {
        "tts_server_url": "http://10.0.0.99:8000/v1",
        "tts_mode": "auto",
        "tts_lag_threshold_ms": 1000,
    }
    monkeypatch.setattr(
        "arenamcp.settings.get_settings", lambda: Mock(get=lambda k, d=None: settings_dict.get(k, d))
    )
    manager._last_local_latencies = [2.5, 3.1, 2.8]

    remote_mock = Mock()
    monkeypatch.setattr(manager, "_request_remote_speech", remote_mock)

    manager.request_speech(text="Cast Lightning Bolt", voice_id="af_heart", voice_name="Heart", speed=1.0)
    remote_mock.assert_called_once()
    assert remote_mock.call_args[1]["text"] == "Cast Lightning Bolt"


def test_remote_tts_does_not_offload_when_no_server(qapp, monkeypatch):
    manager = TtsManager()
    settings_dict = {
        "tts_server_url": "",
        "tts_mode": "auto",
        "tts_lag_threshold_ms": 1000,
    }
    monkeypatch.setattr(
        "arenamcp.settings.get_settings", lambda: Mock(get=lambda k, d=None: settings_dict.get(k, d))
    )
    manager._last_local_latencies = [2.5, 3.1, 2.8]
    monkeypatch.setattr(TtsManager, "is_running", property(lambda self: True))
    dispatch_mock = Mock()
    monkeypatch.setattr(manager, "_dispatch_pending", dispatch_mock)

    remote_mock = Mock()
    monkeypatch.setattr(manager, "_request_remote_speech", remote_mock)

    manager.request_speech(text="Cast Lightning Bolt", voice_id="af_heart", voice_name="Heart", speed=1.0)
    remote_mock.assert_not_called()
    dispatch_mock.assert_called_once()


def test_remote_tts_fallback_to_local_on_error(qapp, monkeypatch):
    manager = TtsManager()
    monkeypatch.setattr(TtsManager, "is_running", property(lambda self: True))
    dispatch_mock = Mock()
    monkeypatch.setattr(manager, "_dispatch_pending", dispatch_mock)

    manager._generation = 5
    manager._busy = True
    manager._on_remote_failed(
        generation=5,
        text="Attack with all",
        voice_id="af_heart",
        voice_name="Heart",
        speed=1.0,
        err_msg="Connection refused",
    )

    assert not manager._busy
    assert manager._pending_request is not None
    assert manager._pending_request["text"] == "Attack with all"
    dispatch_mock.assert_called_once()
