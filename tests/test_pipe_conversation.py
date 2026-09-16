"""Tests for pipe-protocol conversation additions (no Qt, no subprocess)."""

from __future__ import annotations

from typing import Any

from arenamcp.pipe_adapter import PipeAdapter


class FakeConversation:
    """Stand-in for ConversationController (structural contract only)."""

    def __init__(self, mode: str = "turn_advice") -> None:
        self.mode = mode
        self.verbosity = "balanced"
        self.set_mode_calls: list[str] = []
        self.set_verbosity_calls: list[str] = []
        self.question_calls: list[tuple[str, str]] = []

    def set_mode(self, mode: str) -> None:
        self.set_mode_calls.append(mode)
        self.mode = mode

    def set_verbosity(self, verbosity: str) -> None:
        self.set_verbosity_calls.append(verbosity)
        self.verbosity = verbosity

    def on_user_question(self, text: str, source: str = "typed") -> int:
        self.question_calls.append((text, source))
        return 1


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
        self._coach: Any = None
        self._mcp: Any = None
        self.speak_advice_calls: list[str] = []

    def _inject_library_summary_if_needed(self, game_state: dict) -> None:
        return None

    def speak_advice(self, text: str, blocking: bool = True) -> None:  # pragma: no cover
        self.speak_advice_calls.append(text)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    def get_advice(self, game_state: dict, question: str = "") -> str:
        self.calls.append((game_state, question))
        return "legacy answer"


class FakeMcp:
    def get_game_state(self) -> dict:
        return {"turn": {"turn_number": 3}}


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


# ── set_verbosity ────────────────────────────────────────────────────────


def test_set_verbosity_with_controller() -> None:
    coach = FakeCoach()
    coach.conversation = FakeConversation()
    adapter, events = make_adapter(coach)

    adapter._dispatch({"cmd": "set_verbosity", "verbosity": "detailed"})

    assert coach.conversation.set_verbosity_calls == ["detailed"]
    assert statuses(events)["VERBOSITY"] == "detailed"


def test_set_verbosity_without_controller_acks() -> None:
    coach = FakeCoach()
    adapter, events = make_adapter(coach)

    adapter._dispatch({"cmd": "set_verbosity", "verbosity": "detailed"})

    assert statuses(events)["VERBOSITY"] == "detailed"


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


# ── _handle_chat routing ─────────────────────────────────────────────────


def test_chat_routes_to_conversation_controller_in_conversation_mode() -> None:
    coach = FakeCoach()
    coach.conversation = FakeConversation(mode="conversation")
    adapter, _ = make_adapter(coach)

    adapter._handle_chat("what's my board state?")

    assert coach.conversation.question_calls == [("what's my board state?", "typed")]
    # Legacy one-shot path never ran.
    assert coach.speak_advice_calls == []


def test_chat_uses_legacy_path_in_turn_advice_mode() -> None:
    coach = FakeCoach()
    coach.conversation = FakeConversation(mode="turn_advice")
    coach._coach = FakeBackend()
    coach._mcp = FakeMcp()
    adapter, _ = make_adapter(coach)
    advice_calls: list[tuple[str, str]] = []
    adapter.advice = lambda text, seat: advice_calls.append((text, seat))  # type: ignore[method-assign]

    adapter._handle_chat("what's my board state?")

    assert coach.conversation.question_calls == []
    assert coach._coach.calls and coach._coach.calls[0][1] == "what's my board state?"
    assert advice_calls == [("legacy answer", "CHAT")]
    assert coach.speak_advice_calls == ["legacy answer"]


def test_chat_uses_legacy_path_when_controller_absent() -> None:
    coach = FakeCoach()
    coach._coach = FakeBackend()
    coach._mcp = FakeMcp()
    adapter, _ = make_adapter(coach)
    advice_calls: list[tuple[str, str]] = []
    adapter.advice = lambda text, seat: advice_calls.append((text, seat))  # type: ignore[method-assign]

    adapter._handle_chat("hello")

    assert coach._coach.calls and coach._coach.calls[0][1] == "hello"
    assert advice_calls == [("legacy answer", "CHAT")]


def test_chat_slash_commands_still_handled_before_conversation_routing() -> None:
    coach = FakeCoach()
    coach.conversation = FakeConversation(mode="conversation")
    adapter, events = make_adapter(coach)

    adapter._handle_chat("/assess")

    # Slash command short-circuits to game assessment (which logs its path),
    # never reaching the conversation controller.
    assert coach.conversation.question_calls == []


# ── emit_speech_request payload ──────────────────────────────────────────


def test_speak_request_payload_without_priority_or_identity() -> None:
    adapter, events = make_adapter(None)

    adapter.emit_speech_request(
        text="hello", voice_id="af_heart", voice_name="Heart", speed=1.0
    )

    assert events == [
        {
            "type": "speak_request",
            "text": "hello",
            "voice_id": "af_heart",
            "voice_name": "Heart",
            "speed": 1.0,
        }
    ]


def test_speak_request_payload_with_priority_and_identity() -> None:
    adapter, events = make_adapter(None)
    identity = {"session_id": 1, "match_id": "m1", "turn_number": 4, "request_id": 7, "mode": "conversation"}

    adapter.emit_speech_request(
        text="hello",
        voice_id="af_heart",
        voice_name="Heart",
        speed=1.0,
        priority="question",
        identity=identity,
    )

    assert events == [
        {
            "type": "speak_request",
            "text": "hello",
            "voice_id": "af_heart",
            "voice_name": "Heart",
            "speed": 1.0,
            "priority": "question",
            "identity": identity,
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


def test_set_verbosity_accepts_text_key_payload():
    coach = FakeCoach()
    coach.conversation = FakeConversation()
    adapter, _ = make_adapter(coach)
    _dispatch_with_payload(adapter, {"cmd": "set_verbosity", "text": "quiet"})
    assert coach.conversation.verbosity == "quiet"
