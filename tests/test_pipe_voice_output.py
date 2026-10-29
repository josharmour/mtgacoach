"""_PipeVoiceOutput: the coach process's voice in pipe mode; the desktop app speaks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arenamcp.standalone_voice import _PipeVoiceOutput


class _DummyUI:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.stops = 0

    def emit_speech_request(self, **kwargs) -> None:
        self.requests.append(kwargs)

    def emit_speech_stop(self) -> None:
        self.stops += 1


@pytest.fixture
def store(monkeypatch):
    store = {"voice": "bm_george", "voice_speed": 1.4}
    fake = SimpleNamespace(get=lambda k, d=None: store.get(k, d), set=lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: fake)
    return store


def test_speech_goes_to_the_desktop_cleaned_with_the_saved_voice(store) -> None:
    ui = _DummyUI()
    voice = _PipeVoiceOutput(ui)

    voice.speak("  **Attack** with `Bear` [TAG] now...  ")
    voice.speak("**[TAG]**")  # nothing left to say once cleaned

    assert ui.requests == [
        {
            "text": "Attack with Bear  now",
            "voice_id": "bm_george",
            "voice_name": "George (UK Male)",
            "speed": 1.4,
            "category": "workflow",
        }
    ]
    voice.stop()
    assert ui.stops == 1


def test_muted_voice_sends_nothing(store) -> None:
    ui = _DummyUI()
    voice = _PipeVoiceOutput(ui)

    assert voice.toggle_mute() is True and voice.muted
    voice.speak("hello")
    assert ui.requests == []
    assert voice.toggle_mute() is False


def test_starts_from_the_saved_voice_and_saves_changes(store) -> None:
    voice = _PipeVoiceOutput(ui=SimpleNamespace())
    assert voice.current_voice == ("bm_george", "George (UK Male)") and voice.speed == 1.4
    assert voice.set_voice("am_adam") == ("am_adam", "Adam (US Male)") and store["voice"] == "am_adam"
    voice.next_voice()
    assert store["voice"] == voice.current_voice[0] != "am_adam"
    with pytest.raises(ValueError):
        voice.set_voice("zz_nobody")
    assert voice.set_speed(9.0) == 2.0 and store["voice_speed"] == 2.0
    assert voice.cycle_speed() == 1.0 and store["voice_speed"] == 1.0  # off-preset speed restarts at 1.0
    assert voice.cycle_speed() == 1.2


def test_saved_global_mute_survives_voice_initialization(store):
    store["muted"] = True
    ui = _DummyUI()
    voice = _PipeVoiceOutput(ui)
    voice.speak("Do not speak while muted")
    assert voice.muted and not ui.requests
    assert voice.toggle_mute() is False
    assert store["muted"] is False
