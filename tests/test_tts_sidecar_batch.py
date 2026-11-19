"""Focused regression & edge-case tests for TTS-1, TTS-2, and TTS-3."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import time
import types
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient


def _write_test_wav(path: Path, value: int = 16000, frames: int = 240, rate: int = 24000) -> None:
    samples = np.full(frames, value, dtype=np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def _read_pcm_i16(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# TTS-1: Volume clamping, settings persistence, backends, session, UI
# ---------------------------------------------------------------------------


def test_clamp_voice_volume_bounds_and_invalid_inputs() -> None:
    from arenamcp.settings import clamp_voice_volume

    assert clamp_voice_volume(0.0) == 0.0
    assert clamp_voice_volume(0.5) == 0.5
    assert clamp_voice_volume(1.0) == 1.0
    assert clamp_voice_volume(-0.4) == 0.0
    assert clamp_voice_volume(1.8) == 1.0
    assert clamp_voice_volume("0.35") == pytest.approx(0.35)
    for bad in (None, True, False, "bad", float("nan"), float("inf"), float("-inf")):
        assert clamp_voice_volume(bad, default=0.75) == 0.75


def test_settings_persists_and_clamps_voice_volume(tmp_path: Path, monkeypatch) -> None:
    from arenamcp import settings as settings_mod

    monkeypatch.setattr(settings_mod, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")

    s = settings_mod.Settings()
    assert s.get("voice_volume") == 1.0
    s.set("voice_volume", 0.42)

    reloaded = settings_mod.Settings()
    assert reloaded.get("voice_volume") == pytest.approx(0.42)

    reloaded.set("voice_volume", 2.5)
    assert reloaded.get("voice_volume") == 1.0
    reloaded.set("voice_volume", -1.0)
    assert reloaded.get("voice_volume") == 0.0


def test_pcm_stream_player_applies_volume_to_qaudio_sink(monkeypatch) -> None:
    _app()
    from PySide6 import QtMultimedia

    from arenamcp.desktop import pcm_stream

    sink = MagicMock()
    sink.error.return_value = QtMultimedia.QAudio.NoError
    sink.bytesFree.return_value = 9600
    sink.start.return_value = MagicMock(write=lambda b: len(b))
    monkeypatch.setattr(QtMultimedia, "QAudioSink", MagicMock(return_value=sink))
    monkeypatch.setattr(
        QtMultimedia.QMediaDevices,
        "defaultAudioOutput",
        staticmethod(lambda: MagicMock(isNull=lambda: False)),
    )

    player = pcm_stream.PcmStreamPlayer()
    assert player.set_volume(0.35) == pytest.approx(0.35)
    assert player.set_volume(True) == pytest.approx(0.35)  # invalid keeps prior
    player.append(b"\x01\x00" * 16)
    sink.setVolume.assert_called_with(pytest.approx(0.35))

    assert player.set_volume(0.65) == pytest.approx(0.65)
    sink.setVolume.assert_called_with(pytest.approx(0.65))


def test_audio_playback_scales_below_unit_and_keeps_exact_args_at_unit(tmp_path: Path, monkeypatch) -> None:
    from arenamcp.desktop import audio

    wav = tmp_path / "clip.wav"
    _write_test_wav(wav, value=16000)
    AudioPlayback = audio.AudioPlayback
    AudioPlayback.set_volume(1.0)

    cmds: list[list[str]] = []

    class FakeProc:
        def poll(self):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr("shutil.which", lambda p: f"/usr/bin/{p}")
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **_kw: cmds.append(cmd) or FakeProc())

    # macOS afplay: exact cmd at 1.0 vs -v at 0.4
    monkeypatch.setattr(audio, "winsound", None)
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    assert AudioPlayback.play_file(str(wav), volume=1.0) is True
    assert cmds[-1] == ["afplay", str(wav.resolve())]
    assert AudioPlayback.play_file(str(wav), volume=0.4) is True
    assert cmds[-1] == ["afplay", "-v", "0.400", str(wav.resolve())]

    # Linux CLI: paplay --volume and aplay temp scaled WAV cleanup
    monkeypatch.setattr(audio.sys, "platform", "linux")
    monkeypatch.setattr(audio, "sd", None)
    monkeypatch.setattr(audio, "sf", None)
    assert AudioPlayback._play_via_cli_unlocked(wav, ("paplay",), volume=0.5) is True
    assert cmds[-1] == ["paplay", "--volume=32768", str(wav)]

    assert AudioPlayback._play_via_cli_unlocked(wav, ("play",), volume=0.25) is True
    assert cmds[-1] == ["play", "-v", "0.250", str(wav)]

    assert AudioPlayback._play_via_cli_unlocked(wav, ("aplay",), volume=0.5) is True
    scaled_path = Path(cmds[-1][1])
    assert scaled_path != wav and scaled_path.exists()
    assert int(np.max(np.abs(_read_pcm_i16(scaled_path)))) == pytest.approx(8000, abs=2)
    AudioPlayback.stop()
    assert not scaled_path.exists()

    # Linux sounddevice scaling vs unit pass-through
    played_arrays: list[np.ndarray] = []
    monkeypatch.setattr(
        audio,
        "sf",
        SimpleNamespace(read=lambda _p, dtype="float32": (np.ones(100, dtype=np.float32), 24000)),
    )
    monkeypatch.setattr(
        audio,
        "sd",
        SimpleNamespace(play=lambda arr, _fs: played_arrays.append(arr), stop=lambda: None),
    )
    assert AudioPlayback.play_file(str(wav), volume=0.25) is True
    assert float(np.max(played_arrays[-1])) == pytest.approx(0.25)
    assert AudioPlayback.play_file(str(wav), volume=1.0) is True
    assert float(np.max(played_arrays[-1])) == pytest.approx(1.0)

    # winsound branch: temp WAV when < 1.0, exact original path at 1.0
    ws_calls: list[str] = []
    fake_ws = SimpleNamespace(
        SND_FILENAME=1,
        SND_ASYNC=2,
        SND_NODEFAULT=4,
        SND_PURGE=8,
        PlaySound=lambda target, _flags: ws_calls.append(target) if target else None,
    )
    monkeypatch.setattr(audio, "winsound", fake_ws)
    assert AudioPlayback.play_file(str(wav), volume=1.0) is True
    assert ws_calls[-1] == str(wav.resolve())
    assert AudioPlayback._scaled_tmp_path is None

    assert AudioPlayback.play_file(str(wav), volume=0.25) is True
    ws_scaled = Path(ws_calls[-1])
    assert ws_scaled != wav.resolve() and ws_scaled.exists()
    assert int(np.max(np.abs(_read_pcm_i16(ws_scaled)))) == pytest.approx(4000, abs=2)
    AudioPlayback.stop()
    assert not ws_scaled.exists()


def test_samples_to_wav_bytes_and_voice_output_volume_and_render_only(tmp_path: Path, monkeypatch) -> None:
    from arenamcp import settings as settings_mod
    from arenamcp import tts

    monkeypatch.setattr(settings_mod, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    s = settings_mod.Settings()
    s.set("voice_volume", 0.25)
    monkeypatch.setattr(settings_mod, "_settings", s)

    samples = np.full(100, 0.8, dtype=np.float32)
    full_wav = tts._samples_to_wav_bytes(samples, 24000, volume=1.0)
    quarter_wav = tts._samples_to_wav_bytes(samples, 24000, volume=0.25)
    zero_wav = tts._samples_to_wav_bytes(samples, 24000, volume=0.0)

    with wave.open(io.BytesIO(full_wav), "rb") as wf:
        full_i16 = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    with wave.open(io.BytesIO(quarter_wav), "rb") as wf:
        quarter_i16 = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    with wave.open(io.BytesIO(zero_wav), "rb") as wf:
        zero_i16 = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    assert int(np.max(quarter_i16)) == pytest.approx(int(np.max(full_i16)) // 4, abs=3)
    assert int(np.max(np.abs(zero_i16))) == 0

    render_voice = tts.VoiceOutput(render_only=True)
    assert render_voice.volume == 1.0  # parent owns scaling

    live_voice = tts.VoiceOutput(render_only=False)
    assert live_voice.volume == pytest.approx(0.25)
    assert live_voice.set_volume(0.55) == pytest.approx(0.55)
    assert s.get("voice_volume") == pytest.approx(0.55)

    captured_scripts: list[str] = []
    monkeypatch.setattr(tts.os, "name", "nt")
    monkeypatch.setattr(
        tts.subprocess,
        "run",
        lambda cmd, **_kw: captured_scripts.append(cmd[-1]) or SimpleNamespace(returncode=0),
    )
    assert live_voice._try_windows_tts_fallback("Test", blocking=True) is True
    assert "$s.Volume = 55;" in captured_scripts[-1]


def test_coach_session_pipe_adapter_and_preview_volume(tmp_path: Path, monkeypatch, caplog) -> None:
    _app()
    import logging

    from arenamcp import settings as settings_mod
    from arenamcp.desktop import coach_session
    from arenamcp.pipe_adapter import PipeAdapter
    from arenamcp.standalone_voice import _PipeVoiceOutput

    monkeypatch.setattr(settings_mod, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    s = settings_mod.Settings()
    monkeypatch.setattr(settings_mod, "_settings", s)

    session = coach_session.CoachSession()
    sent_payloads: list[dict] = []
    emitted_volumes: list[float] = []
    monkeypatch.setattr(session._process, "send_payload", sent_payloads.append)
    session.volumeChanged.connect(emitted_volumes.append)

    assert session.set_speech_volume(0.4, persist=False) == pytest.approx(0.4)
    assert session.voice_volume == pytest.approx(0.4)
    assert sent_payloads[-1] == {"cmd": "sync_voice_preferences", "voice_volume": pytest.approx(0.4)}
    assert emitted_volumes[-1] == pytest.approx(0.4)
    assert s.get("voice_volume") == 1.0  # persist=False did not overwrite settings

    assert session.set_speech_volume(0.6, persist=True) == pytest.approx(0.6)
    assert s.get("voice_volume") == pytest.approx(0.6)

    # Per-generation preview override does not mutate TtsManager's baseline volume
    session._tts._worker_failed = False
    monkeypatch.setattr(session._tts, "start", lambda: None)
    session._tts._process = SimpleNamespace(state=lambda: 2)
    session._tts._ready = False
    session._tts.request_speech(
        text="Preview line",
        voice_id="am_adam",
        voice_name="Adam",
        speed=1.0,
        settings_override={"tts_provider": "kokoro", "voice_volume": 0.2},
    )
    assert session._tts.volume == pytest.approx(0.6)
    assert session._tts._volume_for_generation(session._tts._generation) == pytest.approx(0.2)

    preview_calls: list[dict] = []
    monkeypatch.setattr(session._speech, "preview", preview_calls.append)
    session.preview_voice()
    assert "voice_volume" in preview_calls[-1]
    session._tts._process = None
    session.shutdown()

    # PipeAdapter syncs voice_volume cleanly against real _PipeVoiceOutput and minimal stubs
    pipe_voice = _PipeVoiceOutput(ui=SimpleNamespace())
    fake_coach = SimpleNamespace(settings=s, _voice_output=pipe_voice)
    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._coach = fake_coach
    adapter.status = MagicMock()
    with caplog.at_level(logging.WARNING):
        adapter._handle_sync_voice_preferences({"voice_volume": 0.35})
    assert s.get("voice_volume") == pytest.approx(0.35)
    assert pipe_voice.volume == pytest.approx(0.35)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    minimal_voice = SimpleNamespace(current_voice=("am_adam", "Adam"))
    fake_coach._voice_output = minimal_voice
    with caplog.at_level(logging.WARNING):
        adapter._handle_sync_voice_preferences({"voice_volume": 0.5})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_compact_coach_volume_slider_sync_and_width(tmp_path: Path, monkeypatch) -> None:
    _app()
    from arenamcp import settings as settings_mod
    from arenamcp.desktop import coach_session, compact_coach

    monkeypatch.setattr(settings_mod, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    s = settings_mod.Settings()
    s.set("voice_volume", 0.75)
    monkeypatch.setattr(settings_mod, "_settings", s)

    session = coach_session.CoachSession()
    monkeypatch.setattr(session._process, "send_payload", lambda _p: None)
    panel = compact_coach.CompactCoachPanel(session)
    assert panel.volume_slider.value() == 75
    assert panel.volume_value_label.text() == "75%"
    assert panel.volume_control.minimumSizeHint().width() <= 300

    panel.volume_slider.setValue(45)
    assert panel.volume_value_label.text() == "45%"
    assert s.get("voice_volume") == pytest.approx(0.45)

    # External volume update syncs slider without looping back
    spy = MagicMock(side_effect=session.set_speech_volume)
    monkeypatch.setattr(session, "set_speech_volume", spy)
    session.volumeChanged.emit(0.8)
    assert panel.volume_slider.value() == 80
    assert panel.volume_value_label.text() == "80%"
    spy.assert_not_called()

    panel.resize(320, 680)
    panel.show()
    _app().processEvents()
    assert panel.width() <= 320
    panel.close()
    session.shutdown()


def test_commentary_settings_dialog_volume_save_preview_cancel() -> None:
    _app()
    from PySide6.QtWidgets import QLineEdit

    from arenamcp.desktop import commentary_settings as cs

    class MemSettings(dict):
        def get(self, key, default=None):
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

    base = {
        "tts_provider": "kokoro",
        "voice": "am_adam",
        "voice_speed": 1.5,
        "speech_speed": 1.5,
        "voice_volume": 0.85,
        "muted": False,
        "autoplay_speech": "actions",
    }

    # Cancel makes zero session calls and leaves settings intact
    s_cancel = MemSettings(base)
    session_cancel = MagicMock()
    dlg_cancel = cs.CommentarySettingsDialog(s_cancel, session_cancel)
    assert not dlg_cancel.findChildren(QLineEdit)
    assert dlg_cancel.volume.value() == 85
    dlg_cancel.volume.setValue(30)
    dlg_cancel.reject()
    assert s_cancel["voice_volume"] == 0.85
    assert not session_cancel.mock_calls

    # Preview sends voice_volume in options without saving or calling set_speech_volume
    s_preview = MemSettings(base)
    session_preview = MagicMock()
    dlg_preview = cs.CommentarySettingsDialog(s_preview, session_preview)
    dlg_preview.volume.setValue(40)
    dlg_preview._preview()
    assert s_preview["voice_volume"] == 0.85
    session_preview.set_speech_volume.assert_not_called()
    preview_opts = session_preview.preview_voice.call_args.args[0]
    assert preview_opts["voice_volume"] == pytest.approx(0.4)

    # Save persists voice_volume and pushes live update with persist=False
    assert dlg_preview._apply() is True
    assert s_preview["voice_volume"] == pytest.approx(0.4)
    session_preview.set_speech_volume.assert_called_once_with(0.4, persist=False)


# ---------------------------------------------------------------------------
# TTS-2: Kokoro expressive blending, composite voices, advice excitement
# ---------------------------------------------------------------------------


def test_kokoro_expressive_blending_composite_and_normal_invariance(tmp_path: Path) -> None:
    from arenamcp import tts

    def make_pack(val: float) -> np.ndarray:
        return np.full((510, 1, 256), val, dtype=np.float32)

    packs = {
        "am_adam": make_pack(1.0),
        "am_fenrir": make_pack(2.0),
        "am_michael": make_pack(0.0),
        "am_santa": make_pack(1.5),
        "af_bella": make_pack(3.0),
        "af_sky": make_pack(5.0),
    }
    fake_kokoro = SimpleNamespace(
        voices=packs,
        get_voice_style=lambda vid: packs[vid],
    )

    # Normal excitement returns exact base vector (STYLE_TOKENS invariance)
    engine = tts.KokoroTTS(voice="am_adam", excitement="normal", expressive_blend=True)
    engine._kokoro = fake_kokoro
    normal_style = engine._voice_style()
    assert normal_style.shape == packs["am_adam"].shape
    assert np.allclose(normal_style, 1.0)

    # Tense = 0.75 * am_adam(1.0) + 0.25 * am_fenrir(2.0) = 1.25
    tense_engine = engine.for_delivery("am_adam", 1.1, excitement="tense")
    assert np.allclose(tense_engine._voice_style(), 1.25)

    # Electric = 0.60 * am_adam(1.0) + 0.40 * am_fenrir(2.0) = 1.40
    electric_engine = engine.for_delivery("am_adam", 1.1, excitement="electric")
    assert electric_engine._voice_style().dtype == np.float32
    assert np.allclose(electric_engine._voice_style(), 1.40)

    # Calm = 0.75 * am_adam(1.0) + 0.25 * am_michael(0.0) = 0.75
    calm_engine = engine.for_delivery("am_adam", 1.0, excitement="calm")
    assert np.allclose(calm_engine._voice_style(), 0.75)

    # Prefix fallback when voice not in explicit dict (am_santa -> am_fenrir)
    santa_electric = engine.for_delivery("am_santa", 1.0, excitement="electric")
    assert np.allclose(santa_electric._voice_style(), 0.60 * 1.5 + 0.40 * 2.0)

    # Negative controls: disabled blend + missing partner degrades safely to base
    disabled = tts.KokoroTTS(voice="am_adam", excitement="electric", expressive_blend=False)
    disabled._kokoro = fake_kokoro
    assert np.allclose(disabled._voice_style(), 1.0)

    single_only = tts.KokoroTTS(voice="am_adam", excitement="electric", expressive_blend=True)
    single_only._kokoro = SimpleNamespace(
        voices={"am_adam": packs["am_adam"]},
        get_voice_style=lambda _vid: packs["am_adam"],
    )
    assert np.allclose(single_only._voice_style(), 1.0)

    # Composite voice syntax "af_bella+af_sky:0.35" -> 0.65*3 + 0.35*5 = 3.70
    composite = tts.KokoroTTS(voice="af_bella+af_sky:0.35", excitement="normal", expressive_blend=True)
    composite._kokoro = fake_kokoro
    comp_style = composite._voice_style()
    assert comp_style.dtype == np.float32
    assert np.allclose(comp_style, 3.70)

    # VoiceOutput.set_voice accepts composite base in VOICES and rejects unknown base
    vo = tts.VoiceOutput(render_only=True)
    assert vo.set_voice("af_bella+af_sky:0.35") == "af_bella+af_sky:0.35"
    assert vo.set_voice("not_a_real_voice") == "af_bella+af_sky:0.35"


def test_infer_advice_excitement_and_forwarding(tmp_path: Path, monkeypatch) -> None:
    _app()
    from arenamcp.commentary.delivery import infer_advice_excitement
    from arenamcp.desktop import tts_manager, tts_worker
    from arenamcp.tts import VoiceOutput

    assert infer_advice_excitement("Swing with everything for lethal!") == "electric"
    assert infer_advice_excitement("Watch out, they hold up counterspell mana.") == "tense"
    assert infer_advice_excitement("No attacks, pass the turn.") == "calm"
    assert infer_advice_excitement("Play Forest and cast Llanowar Elves.") == "normal"

    # TtsManager infers excitement for non-commentary Kokoro actions when normal
    mgr = tts_manager.TtsManager()
    monkeypatch.setattr(mgr, "start", lambda: None)
    mgr._process = SimpleNamespace(state=lambda: 2)
    mgr._ready = False
    mgr.request_speech(
        text="Swing all for lethal.",
        voice_id="am_adam",
        voice_name="Adam",
        speed=1.0,
        category="actions",
        provider="kokoro",
        excitement="normal",
    )
    assert mgr._pending_request["excitement"] == "electric"

    # Does not overwrite explicit non-normal excitement
    mgr.request_speech(
        text="Play Forest.",
        voice_id="am_adam",
        voice_name="Adam",
        speed=1.0,
        category="actions",
        provider="kokoro",
        excitement="calm",
    )
    assert mgr._pending_request["excitement"] == "calm"

    # tts_worker forwards excitement to VoiceOutput.render_to_wav_file
    received_excitements: list[str] = []

    def fake_render(self, _text, excitement="normal"):
        received_excitements.append(excitement)
        wav = tmp_path / "w.wav"
        wav.touch()
        return str(wav), 0.5

    monkeypatch.setattr(VoiceOutput, "render_to_wav_file", fake_render)
    monkeypatch.setattr(VoiceOutput, "warmup", lambda _self: None)
    monkeypatch.setattr(tts_worker, "_lower_priority", lambda: None)
    monkeypatch.setattr(tts_worker, "_configure_logging", lambda: None)
    events: list[dict] = []
    monkeypatch.setattr(tts_worker, "_emit", events.append)
    cmds = [
        {"cmd": "render", "generation": 1, "text": "Lethal!", "excitement": "electric"},
        {"cmd": "shutdown"},
    ]
    monkeypatch.setattr(tts_worker.sys, "stdin", io.StringIO("\n".join(map(json.dumps, cmds))))
    assert tts_worker.main() == 0
    assert received_excitements == ["normal", "electric"]  # warmup + command render


def test_voice_output_loads_kokoro_once_without_warmup(monkeypatch) -> None:
    from arenamcp import tts

    def make_pack(val: float) -> np.ndarray:
        return np.full((510, 1, 256), val, dtype=np.float32)

    packs = {
        "am_adam": make_pack(1.0),
        "am_fenrir": make_pack(2.0),
    }
    load_count = 0

    def fake_ensure_loaded(self):
        nonlocal load_count
        if self._kokoro is not None:
            return
        load_count += 1
        self._kokoro = SimpleNamespace(
            voices=packs,
            get_voice_style=lambda vid: packs[vid],
            create=lambda text, **_kw: (np.ones(2400, dtype=np.float32), 24000),
        )

    monkeypatch.setattr(tts.KokoroTTS, "_ensure_model_loaded", fake_ensure_loaded)
    vo = tts.VoiceOutput(render_only=True)
    for exc in ("normal", "tense", "electric"):
        rendered = vo.render_to_wav_file("Test line", excitement=exc)
        assert rendered is not None
        Path(rendered[0]).unlink(missing_ok=True)
    assert load_count == 1
    assert vo._tts_engine._kokoro is not None

    # Negative control: missing model files inside _ensure_model_loaded cleanly returns None
    vo_missing = tts.VoiceOutput(render_only=True)

    def fake_missing(self):
        raise FileNotFoundError("missing kokoro")

    monkeypatch.setattr(tts.KokoroTTS, "_ensure_model_loaded", fake_missing)
    assert vo_missing.render_to_wav_file("Test line", excitement="electric") is None


# ---------------------------------------------------------------------------
# TTS-3: hosted_base_url, keep-alive, Chatterbox->Kokoro fallback, gateway
# ---------------------------------------------------------------------------


def test_hosted_base_url_and_keepalive_session(monkeypatch) -> None:
    from arenamcp import chatterbox_remote as cb

    monkeypatch.delenv("ARENAMCP_CHATTERBOX_URL", raising=False)
    assert cb.hosted_base_url() == cb.HOSTED_BASE
    assert cb.hosted_base_url("http://198.51.100.10:18767/") == "http://198.51.100.10:18767/v1/chatterbox"
    assert (
        cb.hosted_base_url("http://198.51.100.10:18767/v1/chatterbox/speech")
        == "http://198.51.100.10:18767/v1/chatterbox"
    )

    monkeypatch.setenv("ARENAMCP_CHATTERBOX_URL", "https://198.51.100.10:18767/v1/chatterbox/voices")
    assert cb.hosted_base_url() == "https://198.51.100.10:18767/v1/chatterbox"

    for bad in (
        "ftp://198.51.100.10/v1/chatterbox",
        "https://user:pass@198.51.100.10/v1/chatterbox",
        "https://198.51.100.10/v1/chatterbox?foo=1",
        "https://198.51.100.10:99999/v1/chatterbox",
    ):
        with pytest.raises(cb.ChatterboxError):
            cb.hosted_base_url(bad)

    # Keep-alive requests.Session reused when opener is None
    posted_urls: list[str] = []

    class FakeResp:
        def __init__(self, status_code=200, chunks=(b"\x01\x00\x02\x00",), ctype="audio/pcm", rate="24000"):
            self.status_code = status_code
            self._chunks = chunks
            self.headers = {"Content-Type": ctype, "X-Sample-Rate": rate}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size=9600):
            return iter(self._chunks)

    fake_session = SimpleNamespace(post=lambda url, **_kw: posted_urls.append(url) or FakeResp())
    monkeypatch.setattr(cb, "_http_session", lambda: fake_session)
    monkeypatch.delenv("ARENAMCP_CHATTERBOX_URL", raising=False)
    out = b"".join(cb.stream_hosted(api_key="test-key", text="Hello", base_url="http://198.51.100.10:18767"))
    assert out == b"\x01\x00\x02\x00"
    assert posted_urls == ["http://198.51.100.10:18767/v1/chatterbox/speech"]

    # Auth error and 503 error mapping on keep-alive session path
    monkeypatch.setattr(
        cb,
        "_http_session",
        lambda: SimpleNamespace(post=lambda *_a, **_kw: FakeResp(status_code=401)),
    )
    with pytest.raises(cb.ChatterboxError, match="Sign in"):
        list(cb.stream_hosted(api_key="test-key", text="Hello"))

    monkeypatch.setattr(
        cb,
        "_http_session",
        lambda: SimpleNamespace(post=lambda *_a, **_kw: FakeResp(status_code=503)),
    )
    with pytest.raises(cb.ChatterboxError, match="busy"):
        list(cb.stream_hosted(api_key="test-key", text="Hello"))


def test_tts_manager_chatterbox_fallback_and_negative_controls(tmp_path: Path, monkeypatch) -> None:
    _app()
    from arenamcp import settings as settings_mod
    from arenamcp.desktop import tts_manager

    monkeypatch.setattr(settings_mod, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    s = settings_mod.Settings()
    s.set("tts_provider", "chatterbox_remote")
    s.set("chatterbox_fallback_to_kokoro", True)
    monkeypatch.setattr(settings_mod, "_settings", s)

    mgr = tts_manager.TtsManager()
    statuses: list[str] = []
    errors: list[str] = []
    speech_events: list[tuple[str, str]] = []
    mgr.status_line.connect(statuses.append)
    mgr.error_line.connect(errors.append)
    mgr.speechStatus.connect(lambda sid, st: speech_events.append((sid, st)))
    monkeypatch.setattr(mgr, "start", lambda: None)
    dispatched: list[dict] = []

    def fake_dispatch():
        dispatched.append(dict(mgr._pending_request))

    monkeypatch.setattr(mgr, "_dispatch_pending", fake_dispatch)

    # 1. Public Chatterbox failure falls back to Kokoro cleanly without changing tts_provider
    mgr._selected_provider = "chatterbox_remote"
    mgr._generation = 1
    mgr._remote_meta[1] = (False, "tense", True)
    mgr._on_remote_failed(1, "Watch out.", "am_adam", "Adam", 1.2, "503")
    assert s.get("tts_provider") == "chatterbox_remote"
    assert any("falling back to local Kokoro" in st for st in statuses)
    assert dispatched[-1]["cmd"] == "render"
    assert dispatched[-1]["excitement"] == "tense"
    assert "private" not in dispatched[-1]

    # 2. Private fallback succeeds only when private_render_ready is True
    dispatched.clear()
    mgr._process = SimpleNamespace(
        state=lambda: 2,
        write=lambda _b: None,
        closeWriteChannel=lambda: None,
        terminate=lambda: None,
        waitForFinished=lambda _ms=1500: True,
        deleteLater=lambda: None,
    )
    mgr._private_render_supported = True
    mgr._ready = True
    assert mgr.private_render_ready is True
    mgr._generation = 2
    mgr._last_text = ""
    mgr._remote_meta[2] = (True, "normal", True)
    mgr._on_remote_failed(2, "Secret card.", "am_adam", "Adam", 1.0, "timeout")
    assert dispatched[-1]["private"] is True
    assert mgr._last_text == ""  # private text never stored for say fallback

    # 3. Negative control: private=True and private_render_ready=False MUST NOT fallback
    dispatched.clear()
    mgr._private_render_supported = False
    mgr._generation = 3
    mgr._tracked = (3, "priv-3")
    mgr._remote_meta[3] = (True, "normal", True)
    mgr._on_remote_failed(3, "Secret card.", "am_adam", "Adam", 1.0, "timeout")
    assert dispatched == []
    assert ("priv-3", "failed") in speech_events

    # 4. Negative control: busy_local=True MUST NOT fallback
    dispatched.clear()
    mgr._private_render_supported = True
    mgr._generation = 4
    mgr._tracked = (4, "busy-4")
    mgr._remote_meta[4] = (False, "normal", True)
    mgr._on_remote_failed(4, "Line", "am_adam", "Adam", 1.0, "busy", busy_local=True)
    assert dispatched == []
    assert ("busy-4", "failed") in speech_events

    # 5. Negative control: chatterbox_fallback_to_kokoro=False MUST NOT fallback
    dispatched.clear()
    mgr._generation = 5
    mgr._tracked = (5, "nofb-5")
    mgr._remote_meta[5] = (False, "normal", False)
    mgr._on_remote_failed(5, "Line", "am_adam", "Adam", 1.0, "timeout")
    assert dispatched == []
    assert ("nofb-5", "failed") in speech_events


def test_gateway_auth_cache_clause_segments_busy_wait_and_build_engine(monkeypatch) -> None:
    from gateway.chatterbox_service import build_engine, create_app, segments

    # Clause-aware segments: short first chunk, reconstructable text
    line = "Cast Llanowar Elves first, then hold open mana for removal on their attack step."
    chunks = list(segments(line, limit=100, first_limit=64))
    assert len(chunks[0]) <= 64
    assert " ".join(chunks) == line
    assert all(len(c) <= 100 for c in chunks)

    # Auth cache: 200 hits upstream once; 401 never cached
    auth_calls = 0

    def auth_handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        auth_calls += 1
        if request.headers.get("authorization") == "Bearer good-key":
            return httpx.Response(200, json={"data": [{"id": "current"}]})
        return httpx.Response(401, json={})

    # Turbo engine WITHOUT excitement param validates signature adaptation
    class TurboOnlyEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def _synthesize_pcm(self, text, *, rate=1.0, voice="atlas", exaggeration=0.5):
            yield np.zeros(240, dtype=np.float32), 24000

    app = create_app(
        TurboOnlyEngine(),
        transport=httpx.MockTransport(auth_handler),
        model_name="turbo",
        dtype_name="bfloat16",
        auth_cache_ttl=60.0,
        busy_wait_s=0.1,
    )
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health == {
            "status": "ready",
            "model": "turbo",
            "dtype": "bfloat16",
            "streaming": "segments",
        }
        assert (
            client.get("/v1/chatterbox/voices", headers={"Authorization": "Bearer good-key"}).status_code
            == 200
        )
        assert (
            client.get("/v1/chatterbox/voices", headers={"Authorization": "Bearer good-key"}).status_code
            == 200
        )
        assert auth_calls == 1

        # Negative control: 401 is NEVER cached
        assert (
            client.get("/v1/chatterbox/voices", headers={"Authorization": "Bearer bad-key"}).status_code
            == 401
        )
        assert (
            client.get("/v1/chatterbox/voices", headers={"Authorization": "Bearer bad-key"}).status_code
            == 401
        )
        assert auth_calls == 3

        # Excitement=electric works even though TurboOnlyEngine lacks excitement arg
        resp = client.post(
            "/v1/chatterbox/speech",
            json={"input": "Hello.", "excitement": "electric"},
            headers={"Authorization": "Bearer good-key"},
        )
        assert resp.status_code == 200

    # Bounded busy lock wait: succeeds when released within busy_wait_s, 503 if held
    def slow_synth(text, **kwargs):
        time.sleep(0.12)
        yield np.zeros(240, dtype=np.float32), 24000

    slow_engine = Mock()
    slow_engine._synthesize_pcm.side_effect = slow_synth
    app_wait = create_app(
        slow_engine,
        transport=httpx.MockTransport(auth_handler),
        busy_wait_s=0.6,
    )

    async def call_speech(app_target, *, disconnect_immediately: bool = False) -> int:
        status_code = 0
        body = json.dumps({"input": "Hello."}).encode()
        sent = False
        done_event = asyncio.Event()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnect_immediately:
                return {"type": "http.disconnect"}
            await done_event.wait()
            return {"type": "http.disconnect"}

        async def send(event):
            nonlocal status_code
            if event["type"] == "http.response.start":
                status_code = event["status"]
            elif event["type"] == "http.response.body" and not event.get("more_body", False):
                done_event.set()

        await app_target(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/v1/chatterbox/speech",
                "query_string": b"",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"authorization", b"Bearer good-key"),
                ],
            },
            receive,
            send,
        )
        return status_code

    async def run_overlap() -> list[int]:
        t1 = asyncio.create_task(call_speech(app_wait))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(call_speech(app_wait))
        t3 = asyncio.create_task(call_speech(app_wait, disconnect_immediately=True))
        return list(await asyncio.gather(t1, t2, t3))

    assert asyncio.run(run_overlap()) == [200, 200, 499]

    # Negative control: busy timeout exceeded -> 503
    held_started = threading.Event()
    release_held = threading.Event()

    def held_synth(text, **kwargs):
        held_started.set()
        release_held.wait(timeout=2.0)
        yield np.zeros(240, dtype=np.float32), 24000

    held_engine = Mock()
    held_engine._synthesize_pcm.side_effect = held_synth
    app_timeout = create_app(
        held_engine,
        transport=httpx.MockTransport(auth_handler),
        busy_wait_s=0.05,
    )
    t_bg = threading.Thread(
        target=lambda: TestClient(app_timeout).post(
            "/v1/chatterbox/speech",
            json={"input": "Hold."},
            headers={"Authorization": "Bearer good-key"},
        ),
        daemon=True,
    )
    t_bg.start()
    assert held_started.wait(timeout=2.0)
    try:
        r_busy = TestClient(app_timeout).post(
            "/v1/chatterbox/speech",
            json={"input": "Too soon."},
            headers={"Authorization": "Bearer good-key"},
        )
        assert r_busy.status_code == 503
        assert r_busy.headers.get("retry-after") == "2"
    finally:
        release_held.set()
        t_bg.join(timeout=2.0)

    # build_engine defaults and CPU dtype override
    created: list[dict] = []

    class FakeChatterbox:
        def __init__(self, **kwargs):
            created.append(kwargs)

    class FakeHybrid:
        def __init__(self, **kwargs):
            created.append(kwargs)

    fake_aoa_pkg = types.ModuleType("arenaonair")
    fake_cb_mod = types.ModuleType("arenaonair.platform.tts_chatterbox")
    fake_cb_mod.ChatterboxEngine = FakeChatterbox
    fake_tts_mod = types.ModuleType("arenaonair.tts_server")
    fake_tts_mod.HybridSpeechEngine = FakeHybrid
    fake_plat_mod = types.ModuleType("arenaonair.platform")
    monkeypatch.setitem(sys.modules, "arenaonair", fake_aoa_pkg)
    monkeypatch.setitem(sys.modules, "arenaonair.platform", fake_plat_mod)
    monkeypatch.setitem(sys.modules, "arenaonair.platform.tts_chatterbox", fake_cb_mod)
    monkeypatch.setitem(sys.modules, "arenaonair.tts_server", fake_tts_mod)

    monkeypatch.delenv("TTS_MODEL", raising=False)
    monkeypatch.delenv("TTS_DTYPE", raising=False)
    monkeypatch.delenv("TTS_DEVICE", raising=False)
    _eng, mode, dt = build_engine()
    assert (mode, dt) == ("turbo", "bfloat16")
    assert created[-1] == {"device": "cuda", "model_variant": "turbo", "dtype": "bfloat16"}

    _eng2, mode2, dt2 = build_engine(model_mode="turbo", device="cpu", dtype="bfloat16")
    assert (mode2, dt2) == ("turbo", "none")
    assert created[-1]["dtype"] is None

    _eng3, mode3, dt3 = build_engine(model_mode="hybrid", device="cuda", dtype="bfloat16")
    assert (mode3, dt3) == ("hybrid", "bfloat16")
    assert "factory" in created[-1]
