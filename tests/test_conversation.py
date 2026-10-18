"""Tests for the Conversation Mode controller: modes, identity, memory, match resets."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

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
    defaults: dict[str, Any] = {"coach": coach, "snapshot_fn": lambda: dict(snapshot)}
    defaults.update(kwargs)
    return ConversationController(**defaults), coach, voice


# ---------------------------------------------------------------------------
# Constants + dataclasses
# ---------------------------------------------------------------------------


class TestConstants:
    def test_mode_strings(self) -> None:
        assert TURN_ADVICE == "turn_advice"
        assert CONVERSATION == "conversation"


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
        d = ResponseIdentity(1, "m", 3, 2, None, None, TURN_ADVICE, 4)
        assert a.is_stale_vs(d)

    def test_is_stale_vs_ignores_position_fields(self) -> None:
        # Session-scope staleness (live-test fix 2026-09-16): turn/player/
        # decision drift during the multi-second LLM render must NOT drop
        # conversation speech — only session (mode/match) changes do.
        a = ResponseIdentity(1, "m", 1, 2, 1, "sig-a", TURN_ADVICE, 1)
        same_session = ResponseIdentity(1, "m", 1, 9, 2, "sig-b", TURN_ADVICE, 2)
        assert not a.is_stale_vs(same_session)


class TestMatchMemory:
    def test_ring_trim(self) -> None:
        mem = MatchMemory()
        for i in range(MEMORY_RING_SIZE + 5):
            mem.append(ConversationTurn(role="state", text=f"t{i}"))
        assert len(mem.turns) == MEMORY_RING_SIZE
        assert mem.turns[-1].text == f"t{MEMORY_RING_SIZE + 4}"
        assert mem.turns[0].text == "t5"

    def test_wave3_fields_default_unspoken_and_empty(self) -> None:
        mem = MatchMemory()
        assert mem.last_proactive_ts is None
        assert mem.plan_summary_prev == ""


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


class TestMode:
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

    def test_topic_rendered_across_a_mode_change_is_dropped(self, monkeypatch) -> None:
        monkeypatch.setattr("arenamcp.conversation.get_settings", lambda: MagicMock(set=lambda k, v: None))
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
        voice.spoken.clear()

        # The mode flips MID-RENDER, so the post-render staleness check fires.
        original_render = ctrl._render_topic

        def render_then_flip(topic):
            ctrl.set_mode(TURN_ADVICE)
            return original_render(topic)

        ctrl._render_topic = render_then_flip  # type: ignore[method-assign]
        assert ctrl._speak_topic_if_any(None, 0) is None
        assert voice.spoken == []
        assert ctrl.memory.discussed_topics == {}


# ---------------------------------------------------------------------------
# Match reset + identity
# ---------------------------------------------------------------------------


class TestMatchResetAndIdentity:
    def test_reset_for_match_clears_memory_and_bumps_session(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.append(ConversationTurn(role="coach", text="hi"))
        before = ctrl.current_identity()
        ctrl.reset_for_match("m-2", 5)
        assert len(ctrl.memory.turns) == 0
        after = ctrl.current_identity()
        assert after.session_id == before.session_id + 1
        assert after.match_id == "m-1"  # snapshot still reports the old match id

    def test_reset_for_match_clears_wave3_fields(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.last_proactive_ts = 999.0
        ctrl.memory.discussed_topics["threat"] = 123.0
        ctrl.memory.plan_summary = "old plan"
        ctrl.memory.plan_summary_prev = "older plan"
        ctrl._last_topics = [MagicMock()]

        ctrl.reset_for_match("m-2", 5)

        assert ctrl.memory.last_proactive_ts is None
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
        ctrl = ConversationController(coach=coach, snapshot_fn=None)
        ident = ctrl.current_identity()
        assert ident.match_id is None
        assert ident.match_number == 0
        assert ident.turn_number == 0
        assert ident.active_player is None
        assert ident.decision_sig is None
        assert ident.request_id == 0


# ---------------------------------------------------------------------------
# on_state (memory + topic selection, no speech)
# ---------------------------------------------------------------------------


class TestOnState:
    def test_records_memory_no_speech(self, monkeypatch) -> None:
        ctrl, coach, voice = make_controller()
        ctrl.on_state({"turn": {"turn_number": 1}}, None, ["state_shift"])
        assert [t.role for t in ctrl.memory.turns] == ["state"]
        assert voice.spoken == []

    def test_no_triggers_no_memory(self, monkeypatch) -> None:
        ctrl, _, _ = make_controller()
        ctrl.on_state({}, None, [])
        assert ctrl.memory.turns == []


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


class TestMatchOpenerBoundaries:
    """The opener belongs ONLY to match START (match_id is not None).
    A match-END boundary (match_id -> None) must reset silently —
    live report 2026-09-16 10:09 heard 'Match underway' as the match ended."""

    def _controller_in_conversation(self):
        coach = MagicMock()
        coach._voice_output = MagicMock()
        ctrl = ConversationController(coach)
        ctrl.set_mode(CONVERSATION, persist=False)
        return ctrl, coach._voice_output

    def test_match_start_boundary_speaks(self):
        ctrl, vo = self._controller_in_conversation()
        ctrl.reset_for_match("m-new", 2, match_start=True)
        vo.speak.assert_called_once()

    def test_match_end_boundary_is_silent(self):
        ctrl, vo = self._controller_in_conversation()
        ctrl.reset_for_match(None, 2, match_start=False)
        vo.speak.assert_not_called()

    def test_match_end_still_resets_memory(self):
        ctrl, vo = self._controller_in_conversation()
        ctrl.memory.plan_summary = "stale plan"
        ctrl.reset_for_match(None, 2, match_start=False)
        assert ctrl.memory.plan_summary == ""
        assert ctrl.memory.turns == []
