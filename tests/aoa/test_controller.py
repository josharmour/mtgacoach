from dataclasses import replace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, Signal

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.headless import deterministic
from arenamcp.desktop import commentary_controller
from arenamcp.desktop.commentary_controller import CommentaryController


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
    monkeypatch.setattr(
        control, "_catalog", lambda _: {"voices": ["atlas", "mira", "default"], "default": "atlas"}
    )
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
    c.configure(CommentaryConfig(mode="actions").to_wire())
    c.speech({"text": "Deck review.", "speech_id": "workflow"})
    c.proposal(line(c).to_wire())
    assert tts.request_speech.call_count == 1
    c.speech({"text": "Manual intervention needed.", "speech_category": "essential"})
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
    assert tts.request_speech.call_args.kwargs["voice_id"] == "am_adam"
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
    c.speech({"text": "Manual intervention needed.", "speech_category": "essential"})
    c.configure(CommentaryConfig(mode="commentary").to_wire())
    assert c.arbiter.active.category.value == "essential"


def test_finished_without_start_is_failed(controller):
    c, tts, sent, state = controller
    c.configure(CommentaryConfig(mode="actions").to_wire())
    c.speech({"text": "Review.", "speech_id": "w"})
    tts.speechStatus.emit("w", "finished")
    assert sent[-1]["state"] == "failed"


def test_preview_bypasses_mute_without_enabling_normal_speech_or_saving(controller):
    c, tts, sent, state = controller
    c.arbiter.set_muted(True)
    options = {"tts_provider": "chatterbox_remote", "chatterbox_voice_override": ""}
    c.preview(options)
    call = tts.request_speech.call_args.kwargs
    assert call["voice_id"] == "server_default"
    assert call["settings_override"] == options
    assert c.arbiter.muted
    uid = call["speech_id"]
    tts.speechStatus.emit(uid, "started")
    tts.speechStatus.emit(uid, "finished")
    assert not c.preview_settings and c.arbiter.muted
    c.proposal(line(c).to_wire())
    assert tts.request_speech.call_count == 1


def test_server_default_ignores_old_pair_and_alerts_use_selected_engine(controller, monkeypatch):
    c, tts, sent, state = controller
    monkeypatch.setattr(
        commentary_controller,
        "get_settings",
        lambda: {
            "tts_provider": "chatterbox_remote",
            "chatterbox_voice": "mira",
            "commentary_chatterbox_analyst": "atlas",
        },
    )
    c.proposal(line(c).to_wire())
    assert tts.request_speech.call_args.kwargs["voice_id"] == "server_default"
    c.speech({"text": "Please intervene.", "speech_category": "essential"})
    assert tts.request_speech.call_args.kwargs["provider"] == "chatterbox_remote"


def test_mode_switch_cancels_advice_and_acknowledges_suppressed_workflow(controller):
    c, tts, sent, state = controller
    c.configure(CommentaryConfig(mode="actions").to_wire())
    c.speech({"text": "Advice", "speech_id": "one"})
    c.configure(CommentaryConfig(mode="commentary").to_wire())
    assert c.arbiter.active is None
    c.speech({"text": "More advice", "speech_id": "two"})
    assert sent[-1] == {"cmd": "speech_status", "speech_id": "two", "state": "superseded"}
    assert tts.request_speech.call_count == 1


@pytest.mark.parametrize("provider", ["kokoro", "chatterbox_local", "chatterbox_remote"])
def test_shared_speed_applies_to_advice_narration_and_preview(controller, monkeypatch, provider):
    c, tts, sent, state = controller
    settings = {"tts_provider": provider, "speech_speed": 1.5, "voice_speed": 1.0, "chatterbox_speed": 0.8}
    monkeypatch.setattr(commentary_controller, "get_settings", lambda: settings)
    c.proposal(line(c).to_wire())
    assert tts.request_speech.call_args.kwargs["speed"] == 1.5
    c.configure(CommentaryConfig(mode="actions").to_wire())
    c.speech({"text": "Play Forest.", "speed": 0.8})
    assert tts.request_speech.call_args.kwargs["speed"] == 1.5
    c.preview(dict(settings, speech_speed=1.75))
    assert tts.request_speech.call_args.kwargs["speed"] == 1.75
    assert settings["speech_speed"] == 1.5


def test_uncategorized_guidance_cannot_bypass_announcers(controller):
    c, tts, sent, state = controller
    c.speech({"text": "Play Forest."})
    c.speech({"text": "Play Forest.", "speech_category": "unknown"})
    tts.request_speech.assert_not_called()


def test_event_call_survives_board_change_but_not_new_game(controller):
    c, tts, sent, state = controller
    first = replace(line(c), event_bound=True)
    c.proposal(first.to_wire())
    valid = tts.request_speech.call_args.kwargs["valid"]
    state["turn_info"] = {"turn_number": 2}
    c.observe(state)
    assert valid()
    state["game_number"] = 2
    c.observe(state)
    assert not valid() and c.arbiter.active is None
