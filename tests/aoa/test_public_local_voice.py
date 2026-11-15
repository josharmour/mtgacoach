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
    settings["commentary_public_voice"] = "kokoro"
    assert not valid()
    tts._on_stream_chunk(gen, b"\0\0")
    cancelled.assert_called_once()
    assert not tts._pcm.pending and sent[-1]["state"] == "superseded"


def test_missing_late_or_private_engine_projection_cannot_render(booth):
    control, tts, _, service, sent = booth
    control.config = replace(control.config, private_information=False)  # Explicit public-only wire.
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
        {"private": True, "role": None},
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
    service._emit_proposal(
        public,
        replace(control.observation, private_state={}),
        replace(control.config, private_information=False),
    )
    service.delivery(public.utterance_id, "finished")
    assert public.text in caplog.text


@pytest.mark.parametrize("provider", ["kokoro", "chatterbox"])
def test_public_mode_discards_late_private_proposals_for_every_voice(booth, provider):
    control, tts, settings, _, sent = booth
    control.config = replace(control.config, private_information=False)  # Public-only projection.
    settings.update(tts_provider=provider, commentary_public_voice="kokoro")
    spoken = Mock()
    control.spoken = spoken
    item = replace(line(control), text="Synthetic private draw.")
    control.proposal(item.to_wire(), public_only=False)
    assert sent[-1]["state"] == "superseded"
    spoken.assert_not_called()
    tts._public_voice.speak.assert_not_called()


def test_private_line_uses_the_private_local_audio_request(booth, monkeypatch, caplog):
    control, tts, settings, _, _ = booth
    settings.update(tts_provider="kokoro", commentary_public_voice="kokoro")
    control.config = replace(control.config, private_information=True)
    item = replace(line(control), text="A synthetic private deck fact.", utterance_id="sensitive-event-key")
    caplog.set_level("INFO", logger="arenamcp.desktop.commentary_controller")
    control.spoken = Mock()
    request = Mock()
    monkeypatch.setattr(tts, "request_speech", request)
    control.proposal(item.to_wire(), public_only=False)
    payload = request.call_args.kwargs
    assert payload["private"] and payload["provider"] == "kokoro"
    assert payload["text"] == item.text and payload["valid"]()
    control.spoken.assert_not_called()
    control.configure(replace(control.config, mode="actions").to_wire())
    assert not payload["valid"]()
    tts.speechStatus.emit(item.utterance_id, "superseded")
    assert item.utterance_id not in caplog.text and item.text not in caplog.text


@pytest.mark.parametrize("role,index", [("play_by_play", 0), ("color_analyst", 1)])
@pytest.mark.parametrize("selected_pair", [None, "bm_fable|bf_emma"])
def test_real_settings_route_private_proposals_to_local_voice(
    booth, monkeypatch, tmp_path, role, index, selected_pair
):
    from arenamcp.settings import Settings

    control, tts, _, _, sent = booth
    monkeypatch.setattr("arenamcp.settings.SETTINGS_FILE", tmp_path / "prefs.json")
    prefs = Settings()
    for key, value in {
        "autoplay_speech": "commentary",
        "tts_provider": "kokoro",
        "voice": "am_adam",
        "commentary_private_information": True,
        "commentary_kokoro_pair": selected_pair,
    }.items():
        prefs.set(key, value, save=False)
    monkeypatch.setattr("arenamcp.desktop.commentary_controller.get_settings", lambda: prefs)
    control.config = replace(control.config, private_information=True)
    control._catalog = Mock(side_effect=AssertionError("Private mode must not consult a speech server"))
    control.spoken = Mock()
    request = Mock()
    monkeypatch.setattr(tts, "request_speech", request)
    item = replace(line(control, role=role), text="An invented owner-only fact.")
    # Use the normal pipe event route, not a direct call to the settings helper.
    session = SimpleNamespace(_pending_restart=None, _speech=control)
    CoachSession._handle_process_event(
        session,
        {
            "type": "commentary_proposal",
            "utterance": item.to_wire(),
            "public_only": False,
        },
    )
    payload = request.call_args.kwargs
    pair = selected_pair.split("|") if selected_pair else ("am_adam", "af_heart")
    assert payload["voice_id"] == pair[index] and payload["provider"] == "kokoro"
    assert payload["private"] and payload["valid"]()
    tts.speechStatus.emit(item.utterance_id, "started")
    tts.speechStatus.emit(item.utterance_id, "finished")
    assert [s["state"] for s in sent][-2:] == ["started", "finished"]
    control.spoken.assert_not_called()
    control._catalog.assert_not_called()
    assert prefs.get("tts_provider") == "kokoro"


def test_turning_off_announcers_while_projecting_prevents_a_late_backend_call():
    import threading

    from tests.aoa.test_runtime import snapshot

    from arenamcp.backends.proxy import ProxyBackend

    entered, release, closed = (threading.Event() for _ in range(3))
    calls = []
    settings = {"autoplay_speech": "commentary", "commentary_private_information": True}

    class Backend(ProxyBackend):
        def __init__(self):
            pass

        def complete(self, system, user, *args, **kwargs):
            calls.append(user)
            return "{}"

    class Client:
        def start(self, config):
            pass

        def close(self):
            closed.set()

        def delivered(self, *args):
            pass

        def generate(self, observation, config, rpc, cancelled):
            rpc("Synthetic public or private observation", {"scope": observation.private_state}, False)
            return {"utterances": []}

    service = CommentaryService(
        settings=lambda: settings, backend=Backend, emit=lambda _: None, client_factory=Client
    )
    observe = service.adapter.observe

    def gated(snapshot, private, **kwargs):
        row = observe(snapshot, private, **kwargs)
        if private:
            entered.set()
            assert release.wait(3)
            return replace(row, private_state={**row.private_state, "secret": "private-test-value"})
        return row

    service.adapter.observe = gated
    try:
        service.offer(snapshot())
        assert entered.wait(3)
        settings["autoplay_speech"] = "actions"
        service.configure()
        release.set()
        assert closed.wait(3)
        assert all("private-test-value" not in call for call in calls)
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize("result", ["heard", "private", "interrupted", "changed", "expired", "revoked"])
def test_waiting_lead_cannot_be_replaced_by_its_own_analyst(booth, monkeypatch, result):
    control, tts, settings, service, _ = booth
    private = result in {"private", "revoked"}
    settings.update(commentary_private_information=private, commentary_public_voice="kokoro")
    config = replace(control.config, private_information=private)
    service.config = control.config = config
    control.send = lambda d: (
        service.delivery(d["utterance_id"], d["state"], d.get("reason", ""))
        if d["cmd"] == "commentary_delivery"
        else None
    )
    render = Mock()
    monkeypatch.setattr(tts, "request_speech", render)
    control.observe({"match_id": "m", "game_number": 1, "zones": {}, "turn_info": {}, "players": []})
    obs = control.observation
    advice = replace(line(control), utterance_id="advice", category=SpeechCategory.ADVICE)
    control.arbiter.submit(advice)
    tts.speechStatus.emit(advice.utterance_id, "started")
    lead = replace(line(control), utterance_id="lead", event_bound=True, evidence_ids=("event",))
    tail = replace(line(control, "color_analyst"), utterance_id="tail", anchor_id="lead")
    for item in (lead, tail):
        service.proposals[item.utterance_id] = item
        service.inflight[item.utterance_id] = time.monotonic() + 60
        service._emit_proposal(item, obs, config)

    assert control.arbiter.pending and control.arbiter.pending.utterance_id == "lead"
    assert service.waiting_for_anchor == {"tail"}
    assert "tail" not in control.arbiter._states
    assert render.call_count == 1
    tts.speechStatus.emit(advice.utterance_id, "finished")
    tts.speechStatus.emit(lead.utterance_id, "started")
    assert render.call_count == 2 and not control.arbiter.pending
    if result == "changed":
        control.observation = replace(obs, snapshot_id="changed")
    if result == "expired":
        service.proposals["tail"] = replace(tail, expires_at=time.monotonic() - 1)
    if result == "revoked":
        settings["autoplay_speech"] = "actions"
        service.configure()
    tts.speechStatus.emit(lead.utterance_id, "stopped" if result == "interrupted" else "finished")
    assert not service.waiting_for_anchor
    if result in {"heard", "private"}:
        assert control.arbiter.active.utterance_id == "tail" and render.call_count == 3
        assert render.call_args.kwargs.get("private", False) is private
        tts.speechStatus.emit(tail.utterance_id, "started")
        tts.speechStatus.emit(tail.utterance_id, "finished")
        assert control.arbiter.active is None
        assert list(service.heard) == [lead.text, tail.text]
    else:
        assert render.call_count == 2 and "tail" not in service.proposals


def test_real_grounded_opening_waits_behind_advice_then_delivers_both_roles_once(booth, monkeypatch):
    from test_narration_context import snapshot

    control, tts, settings, service, _ = booth
    settings.update(commentary_private_information=True, commentary_public_voice="kokoro")
    cfg = replace(control.config, private_information=True)
    service.config = control.config = cfg
    state = snapshot()
    service.latest = state
    control.observe(state)
    obs = service.adapter.observe(state, True)
    service.pending_events = {e.event_id: e for e in obs.events}
    feedback = []

    def report(pkt):
        if pkt["cmd"] == "commentary_delivery":
            feedback.append((pkt["utterance_id"], pkt["state"]))
            service.delivery(pkt["utterance_id"], pkt["state"], pkt.get("reason", ""))

    session = SimpleNamespace(_pending_restart=None, _speech=control, commentaryStatusChanged=Mock())
    service.emit = lambda evt: CoachSession._handle_process_event(session, evt)
    control.send = report
    render = Mock()
    monkeypatch.setattr(tts, "request_speech", render)
    advice = replace(line(control), utterance_id="first-advice", category=SpeechCategory.ADVICE)
    control.arbiter.submit(advice)
    tts.speechStatus.emit(advice.utterance_id, "started")
    service._fallback(obs, cfg, "opening_deck_overview")
    lead = control.arbiter.pending
    assert lead and lead.role == "play_by_play"
    (tail_id,) = service.waiting_for_anchor
    assert service.proposals[tail_id].anchor_id == lead.utterance_id
    tts.speechStatus.emit(advice.utterance_id, "finished")
    tts.speechStatus.emit(lead.utterance_id, "started")
    assert render.call_count == 2 and control.arbiter.pending is None
    tts.speechStatus.emit(lead.utterance_id, "finished")
    assert control.arbiter.active.utterance_id == tail_id and render.call_count == 3
    tts.speechStatus.emit(tail_id, "started")
    tts.speechStatus.emit(tail_id, "finished")
    assert [u for u, state in feedback if state == "finished"] == [lead.utterance_id, tail_id]
    assert next(iter(service.openings.values()))["finished"] == {"play_by_play", "color_analyst"}


def test_selected_hosted_voice_also_renders_player_perspective(booth, monkeypatch):
    control, tts, settings, _, _ = booth
    settings.update(
        tts_provider="chatterbox_remote",
        _commentary_voice_catalog={"voices": ["atlas", "mira"], "default": "atlas"},
    )
    hosted = Mock()
    monkeypatch.setattr(tts, "_request_chatterbox_speech", hosted)
    item = replace(line(control, role="color_analyst"), text="A known card in your hand.")
    control.proposal(item.to_wire(), public_only=False)
    assert hosted.call_args.kwargs["text"] == item.text
    assert hosted.call_args.kwargs["chatterbox_voice"] == "mira"
    tts._should_offload_to_remote.assert_not_called()
