"""The desktop offers every American and British English Kokoro voice, starting from the saved one."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arenamcp.kokoro_voices import DEFAULT_VOICE, KOKORO_VOICES, voice_index
from arenamcp.standalone_voice import _PipeVoiceOutput


def test_list_is_every_english_voice_without_duplicates():
    ids = [voice for voice, _label in KOKORO_VOICES]
    assert len(ids) == len(set(ids)) == 28
    assert {"am_adam", "af_sky", "am_onyx", "af_jessica", "af_nova", "bm_george", "bf_emma"} <= set(ids)
    assert all(voice[:2] in {"af", "am", "bf", "bm"} for voice in ids)


def test_list_matches_the_installed_voice_file():
    voices_file = Path.home() / ".cache" / "kokoro" / "voices-v1.0.bin"
    if not voices_file.exists():
        pytest.skip("Kokoro voices file not installed")
    np = pytest.importorskip("numpy")
    english = {name for name in np.load(voices_file).files if name[:2] in {"af", "am", "bf", "bm"}}
    assert english == {voice for voice, _label in KOKORO_VOICES}


def test_pipe_voice_starts_from_saved_voice_and_saves_changes(monkeypatch):
    store = {"voice": "bm_george", "voice_speed": 1.4}
    fake = SimpleNamespace(get=lambda k, d=None: store.get(k, d), set=lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: fake)
    voice = _PipeVoiceOutput(ui=SimpleNamespace())
    assert voice.current_voice == ("bm_george", "George (UK Male)") and voice.speed == 1.4
    assert voice.set_voice("am_adam") == ("am_adam", "Adam (US Male)") and store["voice"] == "am_adam"
    voice.next_voice()
    assert store["voice"] == voice.current_voice[0] != "am_adam"
    with pytest.raises(ValueError):
        voice.set_voice("zz_nobody")


def test_unknown_saved_voice_falls_back_to_adam():
    assert KOKORO_VOICES[voice_index("old_voice")][0] == DEFAULT_VOICE == "am_adam"
