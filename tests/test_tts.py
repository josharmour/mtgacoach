"""Kokoro speech: one voice for every line length, and a short lead-in."""

from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from arenamcp import tts
from arenamcp.tts import STYLE_TOKENS, KokoroTTS, VoiceOutput

SHORT = "Passing now."
LONG = " ".join(["Attack with the Beast and hold up two mana for Teyo"] * 4)  # 44 words


def _kokoro_with_fake_model():
    """The real kokoro_onnx Kokoro.create, with a session that records the style it gets."""
    kokoro_onnx = pytest.importorskip("kokoro_onnx")
    model = kokoro_onnx.Kokoro.__new__(kokoro_onnx.Kokoro)
    # Row i of the voice is filled with i, so a style names the row it came from.
    pack = np.repeat(np.arange(510, dtype=np.float32)[:, None, None], 256, axis=2)
    model.voices = {"am_fenrir": pack}
    model.tokenizer = SimpleNamespace(phonemize=lambda text, lang: text, tokenize=lambda ph: [1] * len(ph))
    model.styles = []

    def run(_outputs, inputs):
        model.styles.append(np.asarray(inputs["style"]))
        return [np.sin(np.linspace(0, 200, 4800, dtype=np.float32))]

    model.sess = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name="tokens")], run=run)
    model._tokens_input = "tokens"
    model._input_dtypes = {
        "tokens": np.dtype(np.int64),
        "style": np.dtype(np.float32),
        "speed": np.dtype(np.float32),
    }
    model.has_timings = False
    model._stops = frozenset()
    model._spaces = frozenset()
    return model


def test_short_and_long_lines_use_the_same_style_row():
    model = _kokoro_with_fake_model()
    engine = KokoroTTS(voice="am_fenrir", speed=1.6)
    engine._kokoro = model

    engine.synthesize(SHORT)
    engine.synthesize(LONG)

    # kokoro_onnx would have used row len(tokens): 12 for the short line, ~200 for the long one.
    assert len(model.styles) == 2
    for style in model.styles:
        assert style.shape == (1, 256)
        assert np.all(style == STYLE_TOKENS)


def test_inaudible_noise_before_the_first_word_is_cut():
    rate = 24000
    hiss = np.full(int(rate * 0.15), 0.3 * 10 ** (-70 / 20), dtype=np.float32)  # 150 ms at -70 dB
    word = 0.3 * np.sin(np.linspace(0, 400, rate // 2, dtype=np.float32))

    trimmed = tts._trim_leading_quiet(np.concatenate([hiss, word]), rate)

    assert len(trimmed) == len(word) + int(rate * 0.02)  # 20 ms of the hiss kept
    assert len(tts._trim_leading_quiet(word, rate)) == len(word)
    assert len(tts._trim_leading_quiet(np.zeros(100, dtype=np.float32), rate)) == 100


def test_unknown_voice_is_reported_by_kokoro():
    model = _kokoro_with_fake_model()
    engine = KokoroTTS(voice="zz_nobody")
    engine._kokoro = model

    with pytest.raises((AssertionError, ValueError), match="zz_nobody"):
        engine.synthesize(SHORT)


class _FakeEngine:
    def synthesize(self, _text):
        return np.full(2400, 0.5, dtype=np.float32), 24000


@pytest.fixture
def voice(monkeypatch):
    store = {"voice": "am_fenrir", "voice_speed": 1.6}
    fake = SimpleNamespace(get=lambda k, d=None: store.get(k, d), set=lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: fake)
    out = VoiceOutput()
    out._tts_engine = _FakeEngine()
    return out


def test_rendered_wav_starts_with_only_the_lead_in(voice):
    path, duration = voice.render_to_wav_file("Passing.")
    try:
        with wave.open(path, "rb") as clip:
            rate = clip.getframerate()
            samples = np.frombuffer(clip.readframes(clip.getnframes()), dtype=np.int16)
    finally:
        Path(path).unlink()

    lead = int(rate * 0.1)  # was 0.5 s of silence before every line
    assert np.all(samples[:lead] == 0)
    assert np.all(samples[lead:] != 0)
    assert duration == pytest.approx(0.1 + 0.1)


def test_direct_playback_uses_the_same_lead_in(voice, monkeypatch):
    played = []
    monkeypatch.setattr(tts, "_USE_WINSOUND", False)
    monkeypatch.setattr(
        tts,
        "sd",
        SimpleNamespace(
            play=lambda s, rate, device=None: played.append(s), wait=lambda: None, stop=lambda: None
        ),
    )

    voice.speak("Passing.")

    (samples,) = played
    lead = int(24000 * 0.1)
    assert len(samples) == lead + 2400
    assert np.all(samples[:lead] == 0) and np.all(samples[lead:] == 0.5)
