"""Desktop half of the speech-completion handshake: an utterance the engine waits on
ends only when its audio has actually finished, been stopped, or been replaced."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop import audio
from arenamcp.desktop.coach_session import CoachSession
from arenamcp.desktop.tts_manager import TtsManager
from arenamcp.pipe_adapter import PipeAdapter


@pytest.fixture
def manager(qapp, monkeypatch):
    m = TtsManager()
    monkeypatch.setattr(TtsManager, "is_running", property(lambda self: True))
    monkeypatch.setattr(m, "_dispatch_pending", Mock())
    monkeypatch.setattr(m, "_should_offload_to_remote", lambda: False)
    monkeypatch.setattr(m, "_cleanup_path", Mock())
    statuses: list[tuple[str, str]] = []
    m.speechStatus.connect(lambda speech_id, state: statuses.append((speech_id, state)))
    m.statuses = statuses
    yield m
    m._playback_watch.stop()


def speak(manager, speech_id=None, text="Option 1 is blue-red."):
    manager.request_speech(text=text, voice_id="af_heart", voice_name="Heart", speed=1.0, speech_id=speech_id)
    return manager._generation


def render(manager, generation, path="voice.wav"):
    manager._handle_stdout_line(json.dumps({"type": "rendered", "generation": generation, "path": path}))


def test_finished_only_when_playback_ends_naturally(manager, monkeypatch):
    playing = {"now": True}
    monkeypatch.setattr(audio.AudioPlayback, "play_file", classmethod(lambda cls, path: True))
    monkeypatch.setattr(audio.AudioPlayback, "is_playing", classmethod(lambda cls: playing["now"]))
    render(manager, speak(manager, "deck-review"))
    assert manager.statuses == [("deck-review", "started")]
    assert manager._playback_watch.isActive()

    manager._check_playback()
    assert manager.statuses == [("deck-review", "started")]  # still playing

    playing["now"] = False
    manager._check_playback()
    assert manager.statuses[-1] == ("deck-review", "finished")
    assert not manager._playback_watch.isActive() and manager._current_audio_path is None


def test_stop_reports_stopped_and_a_late_render_cannot_finish_it(manager, monkeypatch):
    play = Mock(return_value=True)
    monkeypatch.setattr(audio.AudioPlayback, "play_file", play)
    generation = speak(manager, "deck-review")
    manager.stop_speech()
    render(manager, generation)
    play.assert_not_called()
    assert manager.statuses == [("deck-review", "stopped")]


def test_newer_speech_supersedes_and_a_stale_render_is_not_credited(manager, monkeypatch):
    monkeypatch.setattr(audio.AudioPlayback, "play_file", classmethod(lambda cls, path: True))
    monkeypatch.setattr(audio.AudioPlayback, "is_playing", classmethod(lambda cls: False))
    old = speak(manager, "deck-review")
    new = speak(manager, None, text="Unrelated advice.")
    assert manager.statuses == [("deck-review", "superseded")]
    render(manager, old)
    render(manager, new)
    manager._check_playback()
    assert manager.statuses == [("deck-review", "superseded")]


def test_playback_failure_and_closing_report_failed(manager, monkeypatch):
    monkeypatch.setattr(audio.AudioPlayback, "play_file", classmethod(lambda cls, path: False))
    render(manager, speak(manager, "a"))
    assert manager.statuses[-1] == ("a", "failed")
    manager.shutdown()
    speak(manager, "b")
    assert manager.statuses[-1] == ("b", "failed")


def test_render_error_without_a_voice_fallback_reports_failed(manager, monkeypatch):
    monkeypatch.setattr("arenamcp.desktop.tts_manager.sys.platform", "linux")
    generation = speak(manager, "a")
    manager._handle_stdout_line(json.dumps({"type": "error", "generation": generation, "message": "boom"}))
    assert manager.statuses == [("a", "failed")]


def test_system_voice_fallback_finishes_when_the_voice_process_exits(manager, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    manager._worker_failed = True

    def fallback(text, speed):
        manager._say_process = process

    monkeypatch.setattr(manager, "_speak_fallback", fallback)
    speak(manager, "a")
    assert manager.statuses == [("a", "started")]
    manager._playing_generation = manager._generation
    manager._check_playback()
    assert manager.statuses == [("a", "started")]
    process.poll.return_value = 0
    manager._check_playback()
    assert manager.statuses[-1] == ("a", "finished")


def test_session_acknowledges_and_reports_back_to_the_engine(qapp, monkeypatch):
    monkeypatch.setattr(TtsManager, "start", Mock())
    session = CoachSession()
    try:
        sent: list[dict] = []
        requested: dict = {}
        monkeypatch.setattr(session._process, "send_payload", sent.append)
        monkeypatch.setattr(session._tts, "request_speech", lambda **kw: requested.update(kw))
        session._handle_process_event({"type": "speak_request", "text": "Two decks.", "speech_id": "x1"})
        assert requested["speech_id"] == "x1"
        assert sent == [{"cmd": "speech_status", "speech_id": "x1", "state": "accepted"}]

        session._tts.speechStatus.emit("x1", "finished")
        assert sent[-1] == {"cmd": "speech_status", "speech_id": "x1", "state": "finished"}

        session._muted = True
        session._handle_process_event({"type": "speak_request", "text": "Two decks.", "speech_id": "x2"})
        assert sent[-1] == {"cmd": "speech_status", "speech_id": "x2", "state": "muted"}

        session._muted = False
        session._handle_process_event({"type": "speak_request", "text": "", "speech_id": "x3"})
        assert sent[-1] == {"cmd": "speech_status", "speech_id": "x3", "state": "failed"}
    finally:
        session.shutdown()


def test_pipe_adapter_carries_speech_ids_both_ways():
    adapter = PipeAdapter()
    events: list[dict] = []
    adapter._emit = events.append  # type: ignore[method-assign]
    adapter._coach = SimpleNamespace()
    adapter.emit_speech_request(text="Two decks.", voice_id="v", voice_name="V", speed=1.0, speech_id="x1")
    assert events[-1]["speech_id"] == "x1"
    adapter.emit_speech_request(text="Advice.", voice_id="v", voice_name="V", speed=1.0)
    assert "speech_id" not in events[-1]

    speech_id = adapter.speech_completion.begin()
    adapter._dispatch({"cmd": "speech_status", "speech_id": speech_id, "state": "finished"})
    assert adapter.speech_completion.wait(speech_id, seconds_to_speak=1, accept_timeout=0.1) == "finished"


def test_audio_playback_reports_when_a_clip_ends(tmp_path, monkeypatch):
    import wave

    clip = tmp_path / "clip.wav"
    with wave.open(str(clip), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(b"\0\0" * 2400)  # 0.1 s

    class FakeWinsound:
        SND_FILENAME = SND_ASYNC = SND_NODEFAULT = SND_PURGE = 0

        @staticmethod
        def PlaySound(value, flags) -> None:
            pass

    monkeypatch.setattr(audio, "winsound", FakeWinsound)
    monkeypatch.setattr(audio.AudioPlayback, "_lock", threading.RLock())
    monkeypatch.setattr(audio.AudioPlayback, "_cli_process", None)
    clock = {"now": 100.0}
    monkeypatch.setattr(audio.time, "monotonic", lambda: clock["now"])
    assert audio.AudioPlayback.play_file(str(clip))
    assert audio.AudioPlayback.is_playing()
    clock["now"] += 0.3
    assert audio.AudioPlayback.is_playing()  # length plus a small margin
    clock["now"] += 0.2
    assert not audio.AudioPlayback.is_playing()

    assert audio.AudioPlayback.play_file(str(clip))
    audio.AudioPlayback.stop()
    assert not audio.AudioPlayback.is_playing()

    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(audio.AudioPlayback, "_cli_process", process)
    assert audio.AudioPlayback.is_playing()
    process.poll.return_value = 0
    assert not audio.AudioPlayback.is_playing()
