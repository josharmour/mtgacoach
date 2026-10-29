from dataclasses import replace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, Signal

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.headless import deterministic
from arenamcp.desktop import commentary_controller
from arenamcp.desktop.commentary_controller import CommentaryController
from arenamcp.desktop.commentary_settings import CommentarySettingsDialog


class Tts(QObject):
    speechStatus = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.request_speech = Mock()
        self.stop_speech = Mock()


@pytest.fixture
def controller(qapp, monkeypatch):
    settings = {"tts_provider": "kokoro", "voice": "af_heart", "commentary_kokoro_analyst": "am_onyx"}
    monkeypatch.setattr(commentary_controller, "get_settings", lambda: settings)
    tts = Tts()
    sent = []
    control = CommentaryController(tts, sent.append, lambda text: None)
    control.configure(CommentaryConfig(mode="commentary").to_wire())
    state = {"match_id": "m", "game_number": 1, "turn_info": {}, "zones": {}, "players": []}
    control.observe(state)
    yield control, tts, sent, state
    control.close()


def line(control):
    obs = control.adapter.observe(
        {"match_id": "m", "game_number": 1, "turn_info": {}, "zones": {}, "players": []}
    )
    return deterministic(obs, control.config)[0]


def test_ordered_workflow_protected_and_essential_preempts(controller):
    c, tts, sent, state = controller
    c.speech({"text": "Deck review.", "speech_id": "workflow"})
    c.proposal(line(c).to_wire())
    assert tts.request_speech.call_count == 1
    c.speech({"text": "Manual intervention needed."})
    assert tts.stop_speech.call_count == 1
    assert tts.request_speech.call_args.kwargs["category"] == "essential"
    assert {e["state"] for e in sent if e.get("speech_id") == "workflow"} == {"accepted", "superseded"}


def test_reply_requires_finished_anchor_and_duplicate_is_not_reacknowledged(controller):
    c, tts, sent, state = controller
    first = line(c)
    reply = replace(first, utterance_id="reply", anchor_id=first.utterance_id, role="color_analyst")
    c.proposal(first.to_wire())
    c.proposal(reply.to_wire())
    c.proposal(first.to_wire())
    assert tts.request_speech.call_count == 1
    tts.speechStatus.emit(first.utterance_id, "started")
    tts.speechStatus.emit(first.utterance_id, "finished")
    assert tts.request_speech.call_count == 2
    assert tts.request_speech.call_args.kwargs["voice_id"] == "am_onyx"
    tts.speechStatus.emit(reply.utterance_id, "started")
    tts.speechStatus.emit(reply.utterance_id, "finished")
    tts.speechStatus.emit(first.utterance_id, "finished")
    assert [e["state"] for e in sent if e.get("utterance_id") == first.utterance_id] == [
        "accepted",
        "started",
        "finished",
    ]


def test_expired_facts_after_render_and_new_epoch_never_play(controller):
    c, tts, sent, state = controller
    first = line(c)
    c.proposal(first.to_wire())
    valid = tts.request_speech.call_args.kwargs["valid"]
    state["game_number"] = 2
    c.observe(state)
    assert not valid() and c.arbiter.active is None
    tts.speechStatus.emit(first.utterance_id, "started")
    tts.speechStatus.emit(first.utterance_id, "finished")
    assert [e["state"] for e in sent if e.get("utterance_id") == first.utterance_id] == [
        "accepted",
        "stopped",
    ]


def test_mode_change_clears_routine_but_preserves_essential(controller):
    c, tts, sent, state = controller
    c.configure(CommentaryConfig(mode="actions").to_wire())
    c.speech({"text": "Playing a card.", "speech_category": "routine"})
    c.configure(CommentaryConfig(mode="essential").to_wire())
    assert c.arbiter.active is None
    c.speech({"text": "Playing a card.", "speech_category": "routine"})
    assert tts.request_speech.call_count == 1
    c.speech({"text": "Manual intervention needed."})
    c.configure(CommentaryConfig(mode="commentary").to_wire())
    assert c.arbiter.active.category.value == "essential"


def test_finished_without_start_is_failed(controller):
    c, tts, sent, state = controller
    c.speech({"text": "Review.", "speech_id": "w"})
    tts.speechStatus.emit("w", "finished")
    assert sent[-1]["state"] == "failed"


def test_settings_dialog_preserves_kokoro_and_applies_all_controls(qapp):
    class Settings(dict):
        def set(self, key, value):
            self[key] = value

    settings = Settings(voice="af_bella", voice_speed=1.1)
    session = Mock()
    dialog = CommentarySettingsDialog(settings, session)
    dialog.mode.setCurrentIndex(dialog.mode.findData("commentary"))
    dialog.persona.setCurrentIndex(dialog.persona.findData("pro_tour"))
    dialog.private.setChecked(True)
    assert dialog._apply()
    assert settings["voice"] == "af_bella" and settings["voice_speed"] == 1.1
    assert settings["commentary_persona"] == "pro_tour"
    assert settings["commentary_private_information"] is True
    assert settings["autoplay_speech"] == "commentary"
    session.configure_commentary.assert_called_once()
    dialog.close()
