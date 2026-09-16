"""Tests for the Conversation Mode controller (Wave-2 slice)."""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from arenamcp.backend_health import BACKEND_ERROR_PREFIX
from arenamcp.conversation import (
    CONVERSATION,
    MEMORY_RING_SIZE,
    TURN_ADVICE,
    ConversationController,
    ConversationTurn,
    MatchMemory,
    ResponseIdentity,
)


class FakeVoiceSession:
    """Structural stand-in for voice_session.VoiceSession."""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str, Any]] = []
        self.stops: list[str] = []
        self.lock = threading.Lock()

    def speak(self, text: str, *, priority: str = "proactive", identity: Any = None) -> Any:
        with self.lock:
            self.spoken.append((text, priority, identity))
        return MagicMock()

    def stop_speaking(self, reason: str = "user") -> None:
        with self.lock:
            self.stops.append(reason)


def make_controller(**kwargs: Any) -> tuple[ConversationController, Any, FakeVoiceSession]:
    """Build a controller over a fake coach with stubbed inner get_advice."""
    voice = FakeVoiceSession()
    coach = MagicMock()
    coach.voice_session = voice
    coach._voice_output = None
    coach._match_number = 3
    coach.last_match_id = "m-1"
    coach._mcp = None
    inner = MagicMock()
    inner.get_advice = MagicMock(return_value="Play your lands, then attack.")
    coach._coach = inner
    coach._build_pending_decision_signature = MagicMock(
        side_effect=lambda s: f"{s.get('pending_decision')}|test"
    )

    snapshot = kwargs.pop("snapshot", {"match_id": "m-1", "turn": {"turn_number": 4, "active_player": 1}})
    emit = MagicMock()
    emit.calls = []

    def _emit(event_type, **fields):
        emit.calls.append((event_type, fields))

    emit.side_effect = _emit

    defaults: dict[str, Any] = {
        "coach": coach,
        "emit_event": emit,
        "snapshot_fn": lambda: dict(snapshot),
    }
    defaults.update(kwargs)
    return ConversationController(**defaults), coach, voice


def wait_thread(controller: ConversationController, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    for thread in list(controller._answer_threads):
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    assert all(not t.is_alive() for t in controller._answer_threads), "answer thread still running"


# ---------------------------------------------------------------------------
# Constants + dataclasses
# ---------------------------------------------------------------------------


class TestConstants:
    def test_mode_strings(self) -> None:
        assert TURN_ADVICE == "turn_advice"
        assert CONVERSATION == "conversation"

    def test_event_priority_mapping(self) -> None:
        from arenamcp.conversation import (
            EVENT_TO_SPEECH_PRIORITY,
            EventPriority,
        )

        assert EventPriority.USER_QUESTION == 100
        assert EventPriority.URGENT_DECISION == 90
        assert EventPriority.THREAT == 80
        assert EventPriority.STATE_SHIFT == 60
        assert EventPriority.TURN_CONTEXT == 40
        assert EventPriority.FILLER == 10
        assert EVENT_TO_SPEECH_PRIORITY[EventPriority.USER_QUESTION] == "question"
        assert EVENT_TO_SPEECH_PRIORITY[EventPriority.URGENT_DECISION] == "urgent"
        assert EVENT_TO_SPEECH_PRIORITY[EventPriority.THREAT] == "urgent"
        assert EVENT_TO_SPEECH_PRIORITY[EventPriority.TURN_CONTEXT] == "advice"
        assert EVENT_TO_SPEECH_PRIORITY[EventPriority.STATE_SHIFT] == "proactive"
        assert EVENT_TO_SPEECH_PRIORITY[EventPriority.FILLER] == "proactive"


class TestResponseIdentity:
    def test_payload_is_five_field_subset(self) -> None:
        ident = ResponseIdentity(
            session_id=2,
            match_id="abc",
            match_number=3,
            turn_number=7,
            active_player=1,
            decision_sig="sig",
            mode=CONVERSATION,
            request_id=11,
        )
        assert ident.to_payload() == {
            "session_id": 2,
            "match_id": "abc",
            "turn_number": 7,
            "request_id": 11,
            "mode": "conversation",
        }

    def test_is_stale_vs_none_and_fields(self) -> None:
        a = ResponseIdentity(1, "m", 1, 2, None, None, TURN_ADVICE, 1)
        assert not a.is_stale_vs(None)
        b = ResponseIdentity(2, "m", 1, 2, None, None, TURN_ADVICE, 2)
        assert a.is_stale_vs(b)
        c = ResponseIdentity(1, "other", 1, 2, None, None, TURN_ADVICE, 3)
        assert a.is_stale_vs(c)
        d = ResponseIdentity(1, "m", 1, 3, None, None, TURN_ADVICE, 4)
        assert a.is_stale_vs(d)


class TestMatchMemory:
    def test_ring_trim(self) -> None:
        mem = MatchMemory()
        for i in range(MEMORY_RING_SIZE + 5):
            mem.append(ConversationTurn(role="user", text=f"q{i}"))
        assert len(mem.turns) == MEMORY_RING_SIZE
        assert mem.turns[-1].text == f"q{MEMORY_RING_SIZE + 4}"
        assert mem.turns[0].text == "q5"

    def test_wave3_fields_default_zero_and_empty(self) -> None:
        mem = MatchMemory()
        assert mem.last_proactive_ts == 0.0
        assert mem.pending_questions == []
        assert mem.plan_summary_prev == ""

    def test_record_pending_question_caps_at_five(self) -> None:
        from arenamcp.conversation import PENDING_QUESTION_CAP

        mem = MatchMemory()
        for i in range(PENDING_QUESTION_CAP + 3):
            mem.record_pending_question(f"q{i}", match_id="m-1")
        assert len(mem.pending_questions) == PENDING_QUESTION_CAP
        # Newest kept (oldest dropped from the front).
        assert mem.pending_questions[-1].text == f"q{PENDING_QUESTION_CAP + 2}"
        assert mem.pending_questions[0].text == "q3"


# ---------------------------------------------------------------------------
# Mode / verbosity
# ---------------------------------------------------------------------------


class TestModeAndVerbosity:
    def test_set_mode_persists_and_bumps_session(self, monkeypatch) -> None:
        stored: dict[str, Any] = {}
        monkeypatch.setattr(
            "arenamcp.conversation.get_settings",
            lambda: MagicMock(set=lambda k, v: stored.update({k: v})),
        )
        ctrl, coach, voice = make_controller()
        before = ctrl.current_identity()
        ctrl.set_mode(CONVERSATION)
        assert stored["conversation_mode"] == CONVERSATION
        assert ctrl.mode == CONVERSATION
        voice.stop_speaking_calls = voice.stops
        assert "mode_change" in voice.stops
        assert ctrl.current_identity().session_id == before.session_id + 1

    def test_set_mode_invalid_rejected(self) -> None:
        ctrl, _, _ = make_controller()
        with pytest.raises(ValueError):
            ctrl.set_mode("banana")
        assert ctrl.mode == TURN_ADVICE

    def test_set_mode_no_persist(self, monkeypatch) -> None:
        calls: list[tuple] = []

        class Boom:
            @staticmethod
            def set(key, value):
                calls.append((key, value))
                raise RuntimeError("no persist")

        monkeypatch.setattr("arenamcp.conversation.get_settings", lambda: Boom())
        ctrl, _, _ = make_controller()
        # persist=False skips the settings write entirely
        ctrl.set_mode(CONVERSATION, persist=False)
        assert calls == []
        assert ctrl.mode == CONVERSATION

    def test_set_verbosity_persists(self, monkeypatch) -> None:
        stored: dict[str, Any] = {}
        monkeypatch.setattr(
            "arenamcp.conversation.get_settings",
            lambda: MagicMock(set=lambda k, v: stored.update({k: v})),
        )
        ctrl, _, _ = make_controller()
        ctrl.set_verbosity("quiet")
        assert stored["conversation_verbosity"] == "quiet"
        assert ctrl.verbosity == "quiet"
        with pytest.raises(ValueError):
            ctrl.set_verbosity("shouting")


# ---------------------------------------------------------------------------
# User question happy path
# ---------------------------------------------------------------------------


class TestUserQuestion:
    def test_happy_path_emits_reply_records_turn_speaks(self, monkeypatch) -> None:
        ctrl, coach, voice = make_controller()
        rid = ctrl.on_user_question("Why not attack?")
        assert rid == 1
        wait_thread(ctrl)

        reply_events = [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"]
        assert len(reply_events) == 1
        text, fields = reply_events[0]
        assert fields["text"] == "Play your lands, then attack."
        assert fields["identity"]["mode"] == TURN_ADVICE
        assert fields["identity"]["request_id"] == rid
        assert fields["identity"]["match_id"] == "m-1"

        # coach turn recorded in memory after the user turn
        roles = [t.role for t in ctrl.memory.turns]
        assert roles == ["user", "coach"]
        assert ctrl.memory.turns[1].text == "Play your lands, then attack."

        # speech went through the arbiter with question priority
        assert len(voice.spoken) == 1
        spoken_text, priority, identity = voice.spoken[0]
        assert spoken_text == "Play your lands, then attack."
        assert priority == "question"
        assert identity is not None and identity.request_id == rid

        # thinking + idle status events emitted
        states = [f["state"] for c, f in ctrl._emit_event.calls if c == "conversation_status"]
        assert "thinking" in states and "idle" in states

        # question was augmented with conversation digest
        kwargs = coach._coach.get_advice.call_args.kwargs
        assert kwargs["question"].startswith("Recent conversation:")
        assert "User question: Why not attack?" in kwargs["question"]

    def test_fallback_voice_output_when_no_session(self, monkeypatch) -> None:
        ctrl, coach, voice = make_controller()
        coach.voice_session = None
        fallback = MagicMock()
        coach._voice_output = fallback
        ctrl.on_user_question("hello")
        wait_thread(ctrl)
        fallback.speak.assert_called_once_with("Play your lands, then attack.")
        assert voice.spoken == []

    def test_empty_question_ignored(self) -> None:
        ctrl, _, _ = make_controller()
        assert ctrl.on_user_question("   ") == 0
        wait_thread(ctrl)
        assert [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"] == []


# ---------------------------------------------------------------------------
# Staleness / cancellation
# ---------------------------------------------------------------------------


class TestStaleness:
    def _block_until_pending(self, ctrl: ConversationController, count: int) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with ctrl._lock:
                if len(ctrl._pending) >= count:
                    return
            time.sleep(0.01)
        raise AssertionError("request never reached pending set")

    def test_superseded_request_discarded(self, monkeypatch) -> None:
        gate = threading.Event()

        def slow_advice(snapshot, question=None, **kw):
            gate.wait(timeout=5.0)
            return "late answer"

        ctrl, coach, voice = make_controller()
        coach._coach.get_advice.side_effect = slow_advice

        first = ctrl.on_user_question("first")
        self._block_until_pending(ctrl, 1)
        second = ctrl.on_user_question("second")
        assert second > first

        # second request still pending; release both
        gate.set()
        wait_thread(ctrl)

        reply_events = [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"]
        assert [f["text"] for _, f in reply_events] == ["late answer"]
        assert voice.spoken and voice.spoken[0][0] == "late answer"

    def test_session_id_change_discards(self, monkeypatch) -> None:
        gate = threading.Event()

        def slow_advice(snapshot, question=None, **kw):
            gate.wait(timeout=5.0)
            return "stale reply"

        ctrl, coach, voice = make_controller()
        coach._coach.get_advice.side_effect = slow_advice
        monkeypatch.setattr(
            "arenamcp.conversation.get_settings",
            lambda: MagicMock(set=lambda k, v: None),
        )

        ctrl.on_user_question("hello")
        self._block_until_pending(ctrl, 1)
        ctrl.set_mode(CONVERSATION)  # bumps session_id + clears pending
        gate.set()
        wait_thread(ctrl)

        assert [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"] == []
        assert voice.spoken == []

    def test_match_change_discards(self, monkeypatch) -> None:
        gate = threading.Event()

        def slow_advice(snapshot, question=None, **kw):
            gate.wait(timeout=5.0)
            return "wrong match"

        ctrl, coach, voice = make_controller()
        coach._coach.get_advice.side_effect = slow_advice
        ctrl.on_user_question("hello")
        self._block_until_pending(ctrl, 1)
        ctrl.reset_for_match("m-2", 4)  # bumps session_id, clears pending
        gate.set()
        wait_thread(ctrl)
        assert [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"] == []
        assert voice.spoken == []

    def test_cancel_pending_discards_in_flight(self, monkeypatch) -> None:
        gate = threading.Event()

        def slow_advice(snapshot, question=None, **kw):
            gate.wait(timeout=5.0)
            return "cancelled answer"

        ctrl, coach, voice = make_controller()
        coach._coach.get_advice.side_effect = slow_advice
        ctrl.on_user_question("hello")
        self._block_until_pending(ctrl, 1)
        ctrl.cancel_pending()
        gate.set()
        wait_thread(ctrl)
        assert [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"] == []
        assert voice.spoken == []

    def test_cancel_pending_by_stop_speech(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ctrl._pending[42] = ctrl.current_identity(request_id=42)
        ctrl.cancel_pending()
        assert 42 not in ctrl._pending


# ---------------------------------------------------------------------------
# Backend error path
# ---------------------------------------------------------------------------


class TestBackendError:
    def test_raising_backend_emitted_not_spoken(self, monkeypatch) -> None:
        ctrl, coach, voice = make_controller()
        coach._coach.get_advice.side_effect = RuntimeError("backend down")
        ctrl.on_user_question("hello")
        wait_thread(ctrl)

        reply_events = [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"]
        assert len(reply_events) == 1
        _, fields = reply_events[0]
        assert fields["text"].startswith(BACKEND_ERROR_PREFIX)
        assert fields["identity"]["request_id"] == 1
        assert voice.spoken == []
        # Wave 5: backend-error text is transcript-only — it is NOT appended
        # to memory turns (must never leak into later prompt digests).
        roles = [t.role for t in ctrl.memory.turns]
        assert roles == ["user"]

    def test_tagged_text_returned_not_spoken(self, monkeypatch) -> None:
        ctrl, coach, voice = make_controller()
        coach._coach.get_advice.return_value = f"{BACKEND_ERROR_PREFIX} backend timed out"
        ctrl.on_user_question("hello")
        wait_thread(ctrl)

        reply_events = [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"]
        assert len(reply_events) == 1
        _, fields = reply_events[0]
        assert fields["text"].startswith(BACKEND_ERROR_PREFIX)
        assert voice.spoken == []

    def test_strip_tags_not_applied_to_display(self, monkeypatch) -> None:
        # Display keeps the tag; only speech would strip it (and errors are
        # never spoken at all).
        ctrl, coach, _ = make_controller()
        coach._coach.get_advice.return_value = f"{BACKEND_ERROR_PREFIX} x"
        ctrl.on_user_question("q")
        wait_thread(ctrl)
        _, fields = [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"][0]
        assert fields["text"].startswith(BACKEND_ERROR_PREFIX)


# ---------------------------------------------------------------------------
# Match reset + identity
# ---------------------------------------------------------------------------


class TestMatchResetAndIdentity:
    def test_reset_for_match_clears_memory_and_bumps_session(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.append(ConversationTurn(role="user", text="hi"))
        before = ctrl.current_identity()
        ctrl.reset_for_match("m-2", 5)
        assert len(ctrl.memory.turns) == 0
        after = ctrl.current_identity()
        assert after.session_id == before.session_id + 1
        assert after.match_id == "m-1"  # snapshot still reports the old match id

    def test_reset_for_match_clears_wave3_fields(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.last_proactive_ts = 999.0
        ctrl.memory.record_pending_question("stale question", match_id="m-1")
        ctrl.memory.discussed_topics["threat"] = 123.0
        ctrl.memory.plan_summary = "old plan"
        ctrl.memory.plan_summary_prev = "older plan"
        ctrl._last_topics = [MagicMock()]

        ctrl.reset_for_match("m-2", 5)

        assert ctrl.memory.last_proactive_ts == 0.0
        assert ctrl.memory.pending_questions == []
        assert ctrl.memory.discussed_topics == {}
        assert ctrl.memory.plan_summary == ""
        assert ctrl.memory.plan_summary_prev == ""
        assert ctrl._last_topics == []

    def test_current_identity_from_snapshot(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ident = ctrl.current_identity()
        assert ident.match_id == "m-1"
        assert ident.match_number == 3
        assert ident.turn_number == 4
        assert ident.active_player == 1
        assert ident.decision_sig is None

    def test_current_identity_decision_sig(self, monkeypatch) -> None:
        snap = {
            "match_id": "m-1",
            "pending_decision": "Select Cards",
            "turn": {"turn_number": 2, "active_player": 1},
        }
        ctrl, coach, _ = make_controller(snapshot=snap)
        sig = ctrl.current_identity().decision_sig
        assert sig is not None and "Select Cards" in sig

    def test_current_identity_graceful_with_missing_attrs(self, monkeypatch) -> None:
        coach = object()  # no attributes at all
        ctrl = ConversationController(coach=coach, emit_event=None, snapshot_fn=None)
        ident = ctrl.current_identity()
        assert ident.match_id is None
        assert ident.match_number == 0
        assert ident.turn_number == 0
        assert ident.active_player is None
        assert ident.decision_sig is None
        assert ident.request_id == 0


# ---------------------------------------------------------------------------
# on_state (memory-only slice)
# ---------------------------------------------------------------------------


class TestOnState:
    def test_records_memory_no_speech(self, monkeypatch) -> None:
        ctrl, coach, voice = make_controller()
        ctrl.on_state({"turn": {"turn_number": 1}}, None, ["state_shift"])
        wait_thread(ctrl)
        assert [t.role for t in ctrl.memory.turns] == ["state"]
        assert voice.spoken == []
        replies = [c for c in ctrl._emit_event.calls if c[0] == "conversation_reply"]
        assert replies == []

    def test_no_triggers_no_memory(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ctrl.on_state({}, None, [])
        assert ctrl.memory.turns == []


# ---------------------------------------------------------------------------
# Augmentation
# ---------------------------------------------------------------------------


class TestAugmentQuestion:
    def test_digest_includes_recent_turns(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.append(ConversationTurn(role="user", text="what changed?"))
        ctrl.memory.append(ConversationTurn(role="coach", text="you gained board control"))
        augmented = ctrl._augment_question("why not attack?")
        assert "user: what changed?" in augmented
        assert "coach: you gained board control" in augmented
        assert "User question: why not attack?" in augmented

    def test_no_history_returns_raw(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        assert ctrl._augment_question("just this") == "just this"



class TestWave4EvidenceAugment:
    """Wave 4: evidence block is attached to the question prompt."""

    def test_supported_evidence_included_with_caveat(self) -> None:
        from arenamcp.conversation import EvidenceBlock

        ctrl, _, _ = make_controller()
        ctrl.memory.last_evidence = EvidenceBlock(
            model_id="uwtempo/ver2",
            deck_supported=True,
            deck_compatible=True,
            similarity=0.9,
            eval_source="MageZero UWTempo v2",
        )
        augmented = ctrl._augment_question("how is my board?")
        assert "MageZero evidence:" in augmented
        assert "uwtempo/ver2" in augmented
        assert "Deck support: supported" in augmented
        # Uncertainty sentence rides along via prompt on rendered turns; the
        # evidence block itself only adds it when provenance is present.

    def test_unavailable_evidence_still_answers(self) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.append(ConversationTurn(role="user", text="earlier q"))
        ctrl.memory.last_evidence = None
        augmented = ctrl._augment_question("why not attack?")
        assert "User question: why not attack?" in augmented
        assert "MageZero evidence is unavailable" in augmented

    def test_no_history_no_evidence_returns_raw(self) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.last_evidence = None
        assert ctrl._augment_question("just this") == "just this"


# ---------------------------------------------------------------------------
# Wave 5 — M7 question-path grounding
# ---------------------------------------------------------------------------


class TestQuestionPathGrounding:
    def test_augmented_question_carries_evidence_instructions(self) -> None:
        """M7 / PRODUCT D4: whenever evidence is present, the augmented
        question must carry the no-win-probability/one-ply/policy rules —
        otherwise a direct 'what's my win chance?' can parrot the
        uncalibrated score."""
        from arenamcp.conversation import EvidenceBlock

        ctrl, _, _ = make_controller()
        ctrl.memory.last_evidence = EvidenceBlock(
            model_id="uwtempo/ver2",
            deck_supported=True,
            deck_compatible=True,
            similarity=0.9,
            eval_source="MageZero UWTempo v2",
        )
        augmented = ctrl._augment_question("what's my win chance?")
        assert "never state or imply a win probability" in augmented
        assert "one-ply" in augmented
        assert "evaluated outcome" in augmented
        assert "MageZero evidence:" in augmented

    def test_augmented_question_with_history_also_carries_instructions(self) -> None:
        from arenamcp.conversation import EvidenceBlock

        ctrl, _, _ = make_controller()
        ctrl.memory.append(ConversationTurn(role="user", text="earlier"))
        ctrl.memory.last_evidence = EvidenceBlock(model_id="m", provenance="prior_only")
        augmented = ctrl._augment_question("explain the board")
        assert "never state or imply a win probability" in augmented
        assert "Recent conversation:" in augmented

    def test_mock_backend_receives_instruction_line(self, monkeypatch) -> None:
        """End-to-end through the answer thread (PRODUCT reviewer spec D4):
        the mock backend's echoed question carries the instruction when
        evidence exists."""
        from arenamcp.conversation import EvidenceBlock

        ctrl, coach, _ = make_controller()
        ctrl.memory.last_evidence = EvidenceBlock(model_id="m", provenance="prior_only")
        ctrl.on_user_question("what's my win chance?")
        wait_thread(ctrl)
        kwargs = coach._coach.get_advice.call_args.kwargs
        assert "never state or imply a win probability" in kwargs["question"]
        assert "MageZero evidence:" in kwargs["question"]

    def test_no_evidence_raw_question_has_no_instruction(self) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.last_evidence = None
        assert ctrl._augment_question("just this") == "just this"


# ---------------------------------------------------------------------------
# Wave 5 — M6 status lifecycle
# ---------------------------------------------------------------------------


class TestStatusLifecycle:
    def _states(self, ctrl: ConversationController) -> list[str]:
        return [
            f.get("state")
            for c, f in ctrl._emit_event.calls
            if c == "conversation_status"
        ]

    def test_convo_state_idle_on_stale_delivery(self, monkeypatch) -> None:
        gate = threading.Event()

        def slow_advice(snapshot, question=None, **kw):
            gate.wait(timeout=5.0)
            return "stale reply"

        ctrl, coach, voice = make_controller()
        coach._coach.get_advice.side_effect = slow_advice
        monkeypatch.setattr(
            "arenamcp.conversation.get_settings",
            lambda: MagicMock(set=lambda k, v: None),
        )
        ctrl.on_user_question("hello")
        TestStaleness()._block_until_pending(ctrl, 1)
        ctrl.cancel_pending()  # stop during render
        gate.set()
        wait_thread(ctrl)

        assert self._states(ctrl)[-1] == "idle"

    def test_convo_state_idle_after_backend_topic(self, monkeypatch) -> None:
        # persist=True would write 'conversation' into the REAL
        # ~/.arenamcp/settings.json AND the process-wide singleton — the
        # exact pollution that made test_mode_button_clicks_send_set_mode
        # order-dependent (panel read conversation_mode='conversation' and
        # toggled to turn_advice). Isolate like the other persist tests.
        monkeypatch.setattr(
            "arenamcp.conversation.get_settings",
            lambda: MagicMock(set=lambda k, v: None),
        )
        ctrl, coach, voice = make_controller()
        coach._coach.get_advice.return_value = "[BACKEND ERROR] gateway down"
        ctrl.set_mode(CONVERSATION)
        prev = {"turn": {"turn_number": 1}, "players": [], "battlefield": [], "hand": []}
        cur = {
            "turn": {"turn_number": 1},
            "players": [],
            "battlefield": [{"name": "Bear", "controller_seat_id": 2, "instance_id": 9}],
            "hand": [],
            "local_seat_id": 1,
            "opponent_seat_id": 2,
        }
        ctrl.on_state(cur, prev, [])
        assert ctrl.speak_topic_if_any() is None
        assert self._states(ctrl)[-1] == "idle"

    def test_convo_state_idle_after_stale_topic(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "arenamcp.conversation.get_settings",
            lambda: MagicMock(set=lambda k, v: None),
        )
        ctrl, coach, voice = make_controller()
        ctrl.set_mode(CONVERSATION)
        prev = {"turn": {"turn_number": 1}, "players": [], "battlefield": [], "hand": []}
        cur = {
            "turn": {"turn_number": 1},
            "players": [],
            "battlefield": [{"name": "Bear", "controller_seat_id": 2, "instance_id": 9}],
            "hand": [],
            "local_seat_id": 1,
            "opponent_seat_id": 2,
        }
        ctrl.on_state(cur, prev, [])

        # Simulate the mode flipping MID-RENDER: the render side effect flips
        # the mode, so the post-render staleness check fires.
        original_render = ctrl._render_topic

        def render_then_flip(topic):
            ctrl.set_mode(TURN_ADVICE)
            return original_render(topic)

        ctrl._render_topic = render_then_flip  # type: ignore[method-assign]
        assert ctrl._speak_topic_if_any(None, 0) is None
        assert self._states(ctrl)[-1] == "idle"

    def test_convo_state_idle_when_cancel_pending_had_pending(self) -> None:
        ctrl, _, _ = make_controller()
        ctrl._pending[7] = ctrl.current_identity(request_id=7)
        ctrl.cancel_pending()
        assert self._states(ctrl)[-1] == "idle"

    def test_convo_state_idle_when_set_mode_had_pending(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "arenamcp.conversation.get_settings",
            lambda: MagicMock(set=lambda k, v: None),
        )
        ctrl, _, _ = make_controller()
        ctrl._pending[7] = ctrl.current_identity(request_id=7)
        ctrl.set_mode(TURN_ADVICE)
        assert self._states(ctrl)[-1] == "idle"


class TestMatchOpener:
    """Match-start handshake (user request 2026-09-16): conversation mode
    announces audibly at every match boundary; turn advice stays silent."""

    def _controller(self, mode: str) -> tuple[ConversationController, MagicMock]:
        coach = MagicMock()
        coach.voice_session = MagicMock()
        coach._voice_output = MagicMock()
        ctrl = ConversationController(coach)
        if mode != TURN_ADVICE:
            ctrl.set_mode(mode, persist=False)
        return ctrl, coach._voice_output

    def test_opener_spoken_in_conversation_mode(self):
        ctrl, vo = self._controller(CONVERSATION)
        ctrl.reset_for_match("m1", 1)
        text = vo.speak.call_args[0][0]
        assert text.startswith("Conversation mode. Match underway.")

    def test_no_opener_in_turn_advice_mode(self):
        ctrl, vo = self._controller(TURN_ADVICE)
        ctrl.reset_for_match("m1", 1)
        vo.speak.assert_not_called()

    def test_opener_includes_plan_summary_when_seeded(self):
        ctrl, vo = self._controller(CONVERSATION)
        ctrl.memory.plan_summary = "Elf swarm into Overrun."
        ctrl.reset_for_match("m1", 1)
        text = vo.speak.call_args[0][0]
        assert "Elf swarm" in text

    def test_opener_falls_back_without_plan(self):
        ctrl, vo = self._controller(CONVERSATION)
        ctrl.reset_for_match("m1", 1)
        text = vo.speak.call_args[0][0]
        assert "call the swings" in text

    def test_opener_silent_when_no_voice_output(self):
        coach = MagicMock()
        coach._voice_output = None
        ctrl = ConversationController(coach)
        ctrl.set_mode(CONVERSATION, persist=False)
        ctrl.reset_for_match("m1", 1)  # must not raise
