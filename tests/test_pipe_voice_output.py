from __future__ import annotations

from arenamcp.standalone import _PipeVoiceOutput


class _DummyUI:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.stops = 0

    def emit_speech_request(self, **kwargs) -> None:
        self.requests.append(kwargs)

    def emit_speech_stop(self) -> None:
        self.stops += 1


class _DummyInner:
    def __init__(self, muted: bool = False) -> None:
        self.muted = muted
        self.current_voice = ("af_sarah", "Sarah")
        self.speed = 1.2
        self.stop_calls = 0
        self.speak_calls: list[tuple[str, bool]] = []

    def _clean_text(self, text: str) -> str:
        return text.strip().replace("**", "")

    def stop(self) -> None:
        self.stop_calls += 1

    def speak(self, text: str, blocking: bool = True) -> None:
        self.speak_calls.append((text, blocking))


def test_pipe_voice_output_emits_desktop_speech_request() -> None:
    ui = _DummyUI()
    inner = _DummyInner()
    voice = _PipeVoiceOutput(ui, inner)

    voice.speak("  **hello**  ", blocking=False)

    assert inner.stop_calls == 1
    assert inner.speak_calls == []
    assert ui.requests == [
        {
            "text": "hello",
            "voice_id": "af_sarah",
            "voice_name": "Sarah",
            "speed": 1.2,
        }
    ]


def test_pipe_voice_output_skips_muted_requests() -> None:
    ui = _DummyUI()
    inner = _DummyInner(muted=True)
    voice = _PipeVoiceOutput(ui, inner)

    voice.speak("hello")

    assert inner.stop_calls == 0
    assert ui.requests == []
