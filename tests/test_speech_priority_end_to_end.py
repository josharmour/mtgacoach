"""Conversation speech keeps its priority and identity all the way to the desktop TTS.

Each hop had its own test (PipeAdapter.emit_speech_request, CoachSession's
speak_request forwarding, TtsManager's arbitration), but the engine never sent
the fields: VoiceSession._sink_speak forwarded only blocking/on_complete, so
the desktop rule "a proactive line never displaces a pending question" never
ran. Identity objects (ResponseIdentity) also had no JSON form on the pipe.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from arenamcp.conversation import ResponseIdentity
from arenamcp.desktop.coach_session import CoachSession
from arenamcp.desktop.tts_manager import TtsManager
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone import _PipeVoiceOutput
from arenamcp.voice_session import SpeechIdentity, VoiceSession


class _Inner:
    muted = False
    current_voice = ("af_heart", "Heart")
    speed = 1.0

    def stop(self) -> None:
        pass


def _engine_lines():
    """The engine side: VoiceSession -> pipe voice output -> PipeAdapter, as JSON lines."""
    adapter = PipeAdapter()
    voice = VoiceSession(_PipeVoiceOutput(adapter, _Inner()), release_poll_interval=0.005)

    def lines() -> list[dict]:
        out = []
        while not adapter._write_queue.empty():
            out.append(json.loads(adapter._write_queue.get_nowait()))
        return out

    return voice, lines


def _identity(request_id: int, session_id: int = 3) -> ResponseIdentity:
    return ResponseIdentity(
        session_id=session_id,
        match_id="m1",
        match_number=1,
        turn_number=6,
        active_player=1,
        decision_sig=None,
        mode="conversation",
        request_id=request_id,
    )


def test_voice_session_speech_carries_priority_and_identity_onto_the_pipe():
    voice, lines = _engine_lines()

    assert voice.speak("Block with the wall.", priority="question", identity=_identity(7)).played

    (event,) = lines()
    assert event["type"] == "speak_request" and event["text"] == "Block with the wall."
    assert event["priority"] == "question"
    assert event["identity"] == {
        "session_id": 3,
        "match_id": "m1",
        "turn_number": 6,
        "request_id": 7,
        "mode": "conversation",
    }


def test_a_speech_identity_dataclass_becomes_a_json_object():
    voice, lines = _engine_lines()

    voice.speak("Hold up mana.", priority="urgent", identity=SpeechIdentity(2, None, 4, 9))

    (event,) = lines()
    assert event["priority"] == "urgent"
    assert event["identity"] == {"session_id": 2, "match_id": None, "turn_number": 4, "seq": 9}


def test_legacy_speech_without_identity_stays_legacy():
    voice, lines = _engine_lines()

    voice.speak("Play a land.", priority="advice")

    (event,) = lines()
    assert event["priority"] == "advice" and "identity" not in event


def test_a_pending_question_is_not_displaced_by_proactive_speech_on_the_desktop(qapp, monkeypatch):
    voice, lines = _engine_lines()
    monkeypatch.setattr(TtsManager, "start", Mock())
    monkeypatch.setattr(TtsManager, "is_running", property(lambda self: True))
    session = CoachSession()
    try:
        tts = session._tts
        monkeypatch.setattr(tts, "_dispatch_pending", Mock())  # the worker is busy: requests wait
        monkeypatch.setattr(tts, "_stop_playback", Mock())
        monkeypatch.setattr(tts, "_stop_say", Mock())

        voice.speak("Your answer: chump block.", priority="question", identity=_identity(7))
        assert voice.wait_for_idle(2.0)  # the pipe sink hands off at once
        voice.speak("Fun fact about this card.", priority="proactive", identity=_identity(8))
        for event in lines():
            session._handle_process_event(event)

        assert tts._pending_request["text"] == "Your answer: chump block."
        assert tts._pending_request["priority"] == "question"
    finally:
        session.shutdown()
