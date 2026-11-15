"""Desktop-originated voice selection, engine provenance and stream isolation."""

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.commentary import qwen_public
from arenamcp.commentary.contracts import Evidence, SpeechCategory, Utterance
from arenamcp.commentary.qwen_public_worker import instruction
from arenamcp.commentary.service import CommentaryService
from arenamcp.desktop.coach_session import CoachSession
from arenamcp.desktop.commentary_controller import CommentaryController
from arenamcp.desktop.tts_manager import TtsManager


@pytest.fixture
def booth(qapp, monkeypatch):
    settings = {
        "tts_provider": "kokoro",
        "commentary_public_voice": qwen_public.PROVIDER,
        "autoplay_speech": "commentary",
        "commentary_private_information": False,
        "commentary_persona": "hype",
        "commentary_kokoro_pair": "am_adam|af_heart",
    }
    monkeypatch.setattr("arenamcp.desktop.commentary_controller.get_settings", lambda: settings)
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: settings)
    tts = TtsManager()
    monkeypatch.setattr(tts._public_voice, "warm", Mock())
    monkeypatch.setattr(tts._public_voice, "speak", Mock())
    monkeypatch.setattr(tts, "_should_offload_to_remote", Mock(side_effect=AssertionError))
    monkeypatch.setattr(tts, "_speak_fallback", Mock(side_effect=AssertionError))
    sent = []
    control = CommentaryController(tts, sent.append, lambda text: None)
    control.observe({"match_id": "m", "game_number": 1, "turn_info": {}, "zones": {}, "players": []})
    session = SimpleNamespace(_pending_restart=None, _speech=control)
    service = CommentaryService(
        settings=lambda: settings,
        backend=None,
        emit=lambda event: CoachSession._handle_process_event(session, event),
    )
    yield control, tts, settings, service, sent
    control.close()
    tts.shutdown()


def line(control, uid="play", role="play_by_play"):
    obs = control.observation
    return Utterance(
        uid,
        "One blocker left.",
        SpeechCategory.COMMENTARY,
        obs.epoch,
        time.monotonic() + 25,
        snapshot_id=obs.snapshot_id,
        role=role,
        excitement="electric",
    )


def test_engine_public_projection_routes_typed_roles_and_live_chunk_revocation(booth):
    control, tts, settings, service, sent = booth
    item = line(control)
    service._emit_proposal(item, control.observation, control.config)
    first, request, valid = tts._public_voice.speak.call_args.args
    assert valid() and request == dict(text=item.text, role="play_by_play", excitement="electric", speed=1.5)
    assert tts._last_text == ""
    service._emit_proposal(
        replace(item, utterance_id="analysis", role="color_analyst", anchor_id="play"),
        control.observation,
        control.config,
    )
    tts.speechStatus.emit("play", "started")
    tts.speechStatus.emit("play", "finished")
    gen, analyst, valid = tts._public_voice.speak.call_args.args
    assert gen > first and analyst["role"] == "color_analyst" and analyst["excitement"] == "tense"
    cancelled = Mock()
    tts._public_voice.cancel = cancelled
    settings["commentary_private_information"] = True
    assert not valid()
    tts._on_stream_chunk(gen, b"\0\0")
    cancelled.assert_called_once()
    assert not tts._pcm.pending and sent[-1]["state"] == "superseded"


def test_missing_late_or_private_engine_projection_cannot_render(booth):
    control, tts, _, service, sent = booth
    item = line(control)
    control.proposal(item.to_wire())
    service._emit_proposal(
        replace(item, utterance_id="late"),
        control.observation,
        replace(control.config, private_information=True),
    )
    service._emit_proposal(
        replace(item, utterance_id="state"),
        replace(control.observation, private_state={"hand": ["example"]}),
        control.config,
    )
    ev = Evidence("changed", "hand_changed", 1, item.snapshot_id, visibility="private")
    service._emit_proposal(
        replace(item, utterance_id="events"), replace(control.observation, events=(ev,)), control.config
    )
    assert [result["state"] for result in sent] == ["superseded"] * 4
    tts._public_voice.speak.assert_not_called()


def test_unsaved_preview_releases_pack_after_both_roles(booth, monkeypatch):
    control, tts, settings, _, _ = booth
    options = settings.copy()
    settings["commentary_public_voice"] = "kokoro"
    release = Mock()
    monkeypatch.setattr(tts, "release_public_voice", release)
    control.preview(options)
    control.configure(control.config.to_wire())  # An ordinary settings echo may arrive during preview.
    uid = tts._tracked[1]
    tts.speechStatus.emit(uid, "started")
    tts.speechStatus.emit(uid, "finished")
    release.assert_not_called()
    tts.speechStatus.emit(uid + ":analyst", "started")
    tts.speechStatus.emit(uid + ":analyst", "finished")
    release.assert_called_once()


def test_preview_both_public_voices_and_local_failure(booth):
    control, tts, settings, _, _ = booth
    control.preview(settings.copy())
    gen, request, _ = tts._public_voice.speak.call_args.args
    assert request["role"] == "play_by_play"
    uid = tts._tracked[1]
    tts.speechStatus.emit(uid, "started")
    tts.speechStatus.emit(uid, "finished")
    gen, request, _ = tts._public_voice.speak.call_args.args
    assert request["role"] == "color_analyst"
    tts._public_voice_failed(gen)
    assert control.arbiter.active is None and control.arbiter._states[uid + ":analyst"] == "failed"
    tts._speak_fallback.assert_not_called()
    tts._should_offload_to_remote.assert_not_called()


@pytest.mark.parametrize(
    "route",
    [
        {"private": True},
        {"category": "essential"},
        {"provider": None},
        {"voice_id": "Serena"},
        {"category": "workflow", "speech_id": "preview:forged"},
    ],
)
def test_manager_rejects_non_public_categories_before_worker(booth, route):
    _, tts, settings, _, _ = booth
    options = dict(
        text="synthetic",
        voice_id="Ryan",
        voice_name="Ryan",
        speed=1,
        provider=qwen_public.PROVIDER,
        category="commentary",
        role="play_by_play",
        speech_id="test",
        valid=lambda: True,
    )
    if route.get("provider", "") is None:
        settings["tts_provider"] = qwen_public.PROVIDER  # A global setting cannot opt ordinary speech in.
    tts.request_speech(**dict(options, **route))
    tts._public_voice.speak.assert_not_called()
    assert tts._last_text == ""


def test_control_grammar_has_distinct_role_energy_and_pace():
    assert instruction("play_by_play", "electric", 1) != instruction("play_by_play", "calm", 1)
    assert instruction("color_analyst", "calm", 1) != instruction("play_by_play", "calm", 1)
    assert "225 words per minute" in instruction("play_by_play", "calm", 1.5)


def test_worker_stop_drops_stale_chunks_and_recovers(qapp, monkeypatch, tmp_path):
    import sys

    from PySide6.QtCore import QEventLoop, QTimer

    from arenamcp.desktop.qwen_public_stream import PublicVoiceStream

    script = tmp_path / "synthetic-worker.py"
    script.write_text(
        "import json, sys, time\n"
        'print(json.dumps({"event":"ready"}), flush=True)\n'
        "for row in sys.stdin:\n"
        '    if json.loads(row)["text"] == "cancel": time.sleep(5)\n'
        '    print(json.dumps({"event":"pcm", "data":"AQA="}), flush=True)\n'
        '    print(json.dumps({"event":"done"}), flush=True)\n'
    )
    monkeypatch.setattr(qwen_public, "command", lambda: [sys.executable, "-I", "-B", "-u", str(script)])
    stream = PublicVoiceStream(None)
    received, failed = [], []
    loop = QEventLoop()
    stream.chunk.connect(lambda g, chunk: received.append((g, chunk)))
    stream.done.connect(lambda g: loop.quit())
    stream.failed.connect(failed.append)
    stream.speak(1, {"text": "cancel"}, lambda: True)
    wrapper = stream.process

    def replace_slow_request():
        if not stream.rendering:
            QTimer.singleShot(10, replace_slow_request)
            return
        stream.cancel()
        stream.speak(2, {"text": "synthetic"}, lambda: True)

    QTimer.singleShot(0, replace_slow_request)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timeout.start(5000)
    loop.exec()
    assert received == [(2, b"\1\0")] and not failed and stream.ready
    assert stream.process is wrapper  # Native child changed; the Qt signal receiver remained alive.
    stream.speak(3, {"text": "obsolete"}, lambda: False)
    assert failed == [3] and not stream.ready
    timeout.stop()
    stream.shutdown()
    assert wrapper.state() == wrapper.ProcessState.NotRunning
    qapp.processEvents()


def test_private_projection_never_writes_the_line_to_the_engine_log(booth, caplog):
    control, _, _, service, _ = booth
    caplog.set_level("INFO", logger="arenamcp.commentary.service")
    item = replace(line(control), text="Synthetic hidden library entry.")
    service.proposals[item.utterance_id] = item
    service._emit_proposal(item, control.observation, replace(control.config, private_information=True))
    service.delivery(item.utterance_id, "finished")
    assert item.text not in caplog.text
    public = replace(item, utterance_id="public-trace", text="Public combat finished.")
    service.proposals[public.utterance_id] = public
    service._emit_proposal(public, control.observation, control.config)
    service.delivery(public.utterance_id, "finished")
    assert public.text in caplog.text


@pytest.mark.parametrize("provider", ["kokoro", "chatterbox"])
def test_public_mode_discards_late_private_proposals_for_every_voice(booth, provider):
    control, tts, settings, _, sent = booth
    settings.update(tts_provider=provider, commentary_public_voice="kokoro")
    spoken = Mock()
    control.spoken = spoken
    item = replace(line(control), text="Synthetic private draw.")
    control.proposal(item.to_wire(), public_only=False)
    assert sent[-1]["state"] == "superseded"
    spoken.assert_not_called()
    tts._public_voice.speak.assert_not_called()
