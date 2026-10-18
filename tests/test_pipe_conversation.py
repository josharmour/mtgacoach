"""Tests for pipe-protocol conversation additions (no Qt, no subprocess)."""

from __future__ import annotations

from arenamcp.pipe_adapter import PipeAdapter


class FakeConversation:
    """Stand-in for ConversationController (structural contract only)."""

    def __init__(self, mode: str = "turn_advice") -> None:
        self.mode = mode
        self.set_mode_calls: list[str] = []

    def set_mode(self, mode: str) -> None:
        self.set_mode_calls.append(mode)
        self.mode = mode


class FakeVoiceSession:
    def __init__(self) -> None:
        self.stops: list[str] = []

    def stop_speaking(self, reason: str = "user") -> None:
        self.stops.append(reason)


class FakeVoiceOutput:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


class FakeCoach:
    def __init__(self) -> None:
        self.conversation: FakeConversation | None = None
        self.voice_session: FakeVoiceSession | None = None
        self._voice_output: FakeVoiceOutput | None = None


def make_adapter(coach: FakeCoach | None) -> tuple[PipeAdapter, list[dict]]:
    """Adapter with the stdout queue drained into a plain list."""
    adapter = PipeAdapter()
    events: list[dict] = []
    adapter._emit = events.append  # type: ignore[method-assign]
    adapter._coach = coach
    return adapter, events


def statuses(events: list[dict]) -> dict[str, str]:
    return {e["key"]: e["value"] for e in events if e.get("type") == "status"}


# ── set_mode ─────────────────────────────────────────────────────────────


def test_set_mode_with_controller() -> None:
    coach = FakeCoach()
    coach.conversation = FakeConversation()
    adapter, events = make_adapter(coach)

    adapter._dispatch({"cmd": "set_mode", "mode": "conversation"})

    assert coach.conversation.set_mode_calls == ["conversation"]
    assert statuses(events)["MODE"] == "conversation"


def test_set_mode_without_controller_acks_requested_mode() -> None:
    coach = FakeCoach()
    adapter, events = make_adapter(coach)

    adapter._dispatch({"cmd": "set_mode", "mode": "conversation"})

    assert statuses(events)["MODE"] == "conversation"


def test_set_mode_controller_error_never_raises() -> None:
    coach = FakeCoach()

    class Exploding(FakeConversation):
        def set_mode(self, mode: str) -> None:
            raise RuntimeError("boom")

    coach.conversation = Exploding()
    adapter, events = make_adapter(coach)

    adapter._dispatch({"cmd": "set_mode", "mode": "conversation"})  # must not raise

    assert statuses(events)["MODE"] == "turn_advice"  # reported unchanged


# ── stop_speech ──────────────────────────────────────────────────────────


def test_stop_speech_prefers_voice_session() -> None:
    coach = FakeCoach()
    coach.voice_session = FakeVoiceSession()
    coach._voice_output = FakeVoiceOutput()
    adapter, _ = make_adapter(coach)

    adapter._dispatch({"cmd": "stop_speech"})

    assert coach.voice_session.stops == ["ui"]
    assert coach._voice_output.stops == 0


def test_stop_speech_falls_back_to_voice_output() -> None:
    coach = FakeCoach()
    coach._voice_output = FakeVoiceOutput()
    adapter, _ = make_adapter(coach)

    adapter._dispatch({"cmd": "stop_speech"})

    assert coach._voice_output.stops == 1


def test_stop_speech_with_no_attributes_is_a_noop() -> None:
    coach = FakeCoach()
    adapter, _ = make_adapter(coach)

    adapter._dispatch({"cmd": "stop_speech"})  # must not raise


# ── emit_speech_request payload ──────────────────────────────────────────


def test_speak_request_payload_without_priority_or_identity() -> None:
    adapter, events = make_adapter(None)

    adapter.emit_speech_request(text="hello", voice_id="af_heart", voice_name="Heart", speed=1.0)

    assert events == [
        {
            "type": "speak_request",
            "text": "hello",
            "voice_id": "af_heart",
            "voice_name": "Heart",
            "speed": 1.0,
        }
    ]


def _dispatch_with_payload(adapter, payload):
    """Run one dispatch cycle with a raw payload (test helper)."""
    adapter._dispatch(payload)


def test_set_mode_accepts_text_key_payload():
    """Regression (2026-09-16 Mac): UI send_command transports the value in
    'text'; engine read only 'mode' — every UI mode switch silently no-op'd."""
    coach = FakeCoach()
    coach.conversation = FakeConversation()
    adapter, _ = make_adapter(coach)
    _dispatch_with_payload(adapter, {"cmd": "set_mode", "text": "conversation"})
    assert coach.conversation.mode == "conversation"
    _dispatch_with_payload(adapter, {"cmd": "set_mode", "text": "turn_advice"})
    assert coach.conversation.mode == "turn_advice"
