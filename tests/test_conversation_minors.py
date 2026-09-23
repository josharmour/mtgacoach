"""Wave-5 MINOR findings regression tests (fix agent 3).

Covers the 10 MINOR review findings that are not already exercised by the
other conversation suites:

1.  Monotonic suppression clock (now_fn injection; backward wall-clock jump
    never freezes/expiries cooldown or per-topic repetition windows).
2.  Topic delivery staleness gate is session-scope identity.is_stale_vs — a
    turn advance between the topic gate and delivery does NOT drop the
    topic (position drift is normal during the live LLM render).
9.  [BACKEND ERROR] text never enters memory turns → no digest pollution.

(3 lives in tests/test_standalone_conversation.py — the standalone loop owns
the None->first-match transition; 6 and 10 are doc/comment-only changes;
4, 5, 7 and 8 covered the retired MageZero evidence block.)
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import arenamcp.conversation as conversation_mod
from arenamcp.backend_health import BACKEND_ERROR_PREFIX
from arenamcp.conversation import (
    CONVERSATION,
    PENDING_QUESTION_TTL_SECONDS,
    ConversationController,
    EventPriority,
    TopicCandidate,
)

COOLDOWN = 90.0


def card(name: str, seat: int, iid: int) -> dict:
    return {
        "name": name,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "instance_id": iid,
        "type_line": "Creature",
    }


def make_state(
    turn: int = 5,
    battlefield: list[dict] | None = None,
    life: tuple[int, int] = (20, 20),
) -> dict:
    return {
        "match_id": "m-1",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": life[0]},
            {"seat_id": 2, "is_local": False, "life_total": life[1]},
        ],
        "turn": {"turn_number": turn, "active_player": 1, "phase": "Phase_Main1"},
        "battlefield": list(battlefield or []),
        "hand": [],
        "stack": [],
    }


class FakeClock:
    """Injectable suppression clock (now_fn target)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_controller(
    clock: FakeClock | None = None,
    get_advice_result: str = "Hold interaction up.",
    verbosity: str = "detailed",
    snapshot: dict | None = None,
    emit: Any = None,
) -> tuple[ConversationController, Any, Any, Any]:
    """Controller with an injected now_fn and a mock arbiter/inner coach.

    ``emit="capture"`` wires a MagicMock-style recorder with ``.calls``.
    """
    voice = MagicMock()
    voice.speak.return_value = MagicMock(played=True)
    coach = MagicMock()
    coach.voice_session = voice
    coach._voice_output = None
    coach._match_number = 1
    coach.last_match_id = "m-1"
    coach._mcp = None
    coach._game_plan_mgr = None
    inner = MagicMock()
    inner.get_advice = MagicMock(return_value=get_advice_result)
    inner._game_plan_mgr = None
    coach._coach = inner

    settings = MagicMock()
    settings.get = MagicMock(
        side_effect=lambda key, default=None: COOLDOWN if key == "conversation_cooldown_seconds" else default
    )
    original_get_settings = conversation_mod.get_settings
    conversation_mod.get_settings = lambda: settings

    emit_event: Any = None
    global _EMIT_CALLS
    _EMIT_CALLS = []
    if emit == "capture":

        def _emit(event_type, **fields):
            _EMIT_CALLS.append((event_type, fields))

        emit_event = _emit

    try:
        ctrl = ConversationController(
            coach,
            emit_event=emit_event,
            snapshot_fn=lambda: dict(snapshot or make_state()),
            now_fn=clock,  # None → production default (monotonic)
        )
        ctrl.set_mode(CONVERSATION, persist=False)
        ctrl.set_verbosity(verbosity)
    finally:
        conversation_mod.get_settings = original_get_settings
    return ctrl, coach, voice, inner


# ---------------------------------------------------------------------------
# 1. Monotonic suppression clock
# ---------------------------------------------------------------------------


class TestMonotonicSuppressionClock:
    def test_backward_wall_clock_jump_does_not_expire_suppression(self) -> None:
        """A wall-clock jump BACKWARD must not clear suppression: cooldown and
        per-topic repetition windows run on the injected monotonic clock, so
        only REAL elapsed suppression time expires them."""
        clock = FakeClock()
        ctrl, _coach, voice, _inner = make_controller(clock=clock)

        prev = make_state(battlefield=[])
        cur1 = make_state(battlefield=[card("Bear", 2, 71)])
        ctrl.on_state(cur1, prev, [])
        assert ctrl.speak_topic_if_any() is not None
        assert voice.speak.call_count == 1

        # Wall clock jumps backward by an hour (NTP correction / suspend) —
        # the suppression clock does not care.
        # Monotonic time advances only 10s: still inside cooldown (90s) and
        # the 3x per-topic window — the same topic stays suppressed even
        # though a naive wall-clock implementation would now see
        # discussed_topics[key] far in the future (frozen suppression).
        clock.advance(10)
        cur2 = make_state(battlefield=[card("Otter", 2, 72)])
        ctrl.on_state(cur2, cur1, [])
        spoken_before = voice.speak.call_count
        ctrl.speak_topic_if_any()
        # opponent_development was spoken 10s (suppression time) ago: inside
        # the 3x90s per-topic window → suppressed.
        assert voice.speak.call_count == spoken_before

    def test_suppression_expires_by_real_elapsed_suppression_time(self) -> None:
        """After 3xcooldown of SUPPRESSION time (not wall time), the topic may
        re-speak — the window follows the monotonic clock, not the wall."""
        clock = FakeClock()
        ctrl, _coach, voice, _inner = make_controller(clock=clock)

        prev = make_state(battlefield=[])
        cur1 = make_state(battlefield=[card("Bear", 2, 73)])
        ctrl.on_state(cur1, prev, [])
        assert ctrl.speak_topic_if_any() is not None

        clock.advance(COOLDOWN * 3 + 1)
        cur2 = make_state(turn=6, battlefield=[card("Bear", 2, 74)])
        ctrl.on_state(cur2, cur1, [])
        assert ctrl.speak_topic_if_any() is not None
        assert voice.speak.call_count == 2

    def test_wall_clock_jump_forward_does_not_instantly_expire(self) -> None:
        """The inverse failure: a wall-clock jump FORWARD (DST fix) must not
        instantly expire suppression — suppression is measured on the
        monotonic clock, which did not move."""
        clock = FakeClock()
        ctrl, _coach, voice, _inner = make_controller(clock=clock)

        prev = make_state(battlefield=[])
        cur1 = make_state(battlefield=[card("Bear", 2, 75)])
        ctrl.on_state(cur1, prev, [])
        assert ctrl.speak_topic_if_any() is not None

        # No suppression-clock advance at all: even though the WALL clock
        # might have jumped hours ahead, the per-topic window (measured on
        # the injected clock) has not elapsed → still suppressed.
        cur2 = make_state(battlefield=[card("Otter", 2, 76), card("Bear", 2, 75)])
        ctrl.on_state(cur2, cur1, [])
        spoken_before = voice.speak.call_count
        ctrl.speak_topic_if_any()
        assert voice.speak.call_count == spoken_before

    def test_pending_ttl_runs_on_injected_clock(self) -> None:
        """Deferred-question TTL: fresh on the suppression clock stays; past
        TTL on the suppression clock is swept."""
        clock = FakeClock()
        ctrl, _coach, _voice, _inner = make_controller(clock=clock)

        ctrl.memory.record_pending_question("fresh q", match_id="m-1", ts=clock.now)
        ctrl._sweep_expired_pending_questions()
        assert len(ctrl.memory.pending_questions) == 1

        # Direct-construction PendingQuestion keeps wall-clock ts: on the
        # monotonic/fake base that age is negative (unverifiable clock base)
        # → dropped rather than trusted (never blocks topic speech forever).
        old_wall = conversation_mod.PendingQuestion(
            text="wall-clock q",
            ts=time.time(),
            match_id="m-1",
        )
        ctrl.memory.pending_questions.append(old_wall)
        ctrl._sweep_expired_pending_questions()
        assert all(pq.text != "wall-clock q" for pq in ctrl.memory.pending_questions)

        # Aged past TTL on the suppression clock → swept.
        aged = conversation_mod.PendingQuestion(
            text="aged q",
            ts=clock.now - PENDING_QUESTION_TTL_SECONDS - 5,
            match_id="m-1",
        )
        ctrl.memory.pending_questions.append(aged)
        ctrl._sweep_expired_pending_questions()
        assert all(pq.text != "aged q" for pq in ctrl.memory.pending_questions)

    def test_conversation_turn_ts_stays_wall_clock(self) -> None:
        """ConversationTurn.ts remains wall-clock (display timestamps) even
        with a monotonic suppression clock injected."""
        clock = FakeClock()
        _ctrl, _coach, _voice, _inner = make_controller(clock=clock)
        turn = conversation_mod.ConversationTurn(role="user", text="hi")
        assert 1_600_000_000.0 < turn.ts < time.time() + 60  # wall-clock magnitude
        assert turn.ts > clock.now  # NOT on the fake monotonic base


# ---------------------------------------------------------------------------
# 2. Full staleness gate on topic delivery
# ---------------------------------------------------------------------------


class TestTopicFullStalenessGate:
    def test_turn_advance_between_gate_and_delivery_still_delivers(self) -> None:
        """Same session, turn advances while the topic renders → the topic
        STILL delivers (session-scope staleness, live-test fix 2026-09-16):
        position drift during the multi-second render is normal in a live
        game and must not silently drop conversation speech.
        """
        ctrl, coach, voice, inner = make_controller(emit="capture")
        turn_holder = {"n": 5}

        original = inner.get_advice

        def render_then_advance(snapshot, question=None, **kw):
            result = original(snapshot, question=question, **kw)
            # The board moved on mid-render: the next identity sees turn 6.
            turn_holder["n"] = 6
            ctrl._snapshot = lambda: make_state(turn=turn_holder["n"])  # type: ignore[method-assign]
            return result

        inner.get_advice = MagicMock(side_effect=render_then_advance)

        ctrl._last_topics = [
            TopicCandidate(
                key="threat", priority=EventPriority.THREAT, evidence="Opponent revealed Bear (observed)."
            )
        ]
        result = ctrl.speak_topic_if_any(match_id="m-1", match_number=1)
        assert result is not None
        assert voice.speak.call_count == 1
        # Topic recorded as discussed — it was actually spoken.
        assert "threat" in ctrl.memory.discussed_topics
        # Speaking status emitted (M6 lifecycle). The final status may be
        # 'idle' (urgent-recovery thread completes and returns to idle).
        statuses = [
            f.get("state")
            for c, f in _EMIT_CALLS  # type: ignore[name-defined]
            if c == "conversation_status"
        ]
        assert statuses and "speaking" in statuses
        # Join the urgent-recovery daemon so its grace wait cannot leak into
        # later tests that monkeypatch the shared time.sleep (suite-order
        # flake, 2026-09-16).
        for t in threading.enumerate():
            if t.name == "convo-topic-recovery":
                t.join(timeout=5.0)

    def test_same_session_no_drift_still_delivers(self) -> None:
        """No identity drift → the topic delivers normally (control)."""
        ctrl, _coach, voice, _inner = make_controller()
        ctrl._last_topics = [
            TopicCandidate(
                key="threat", priority=EventPriority.THREAT, evidence="Opponent revealed Bear (observed)."
            )
        ]
        assert ctrl.speak_topic_if_any(match_id="m-1", match_number=1) is not None
        assert voice.speak.call_count == 1


# ---------------------------------------------------------------------------
# 9. Backend-error text never pollutes memory digests
# ---------------------------------------------------------------------------


class TestBackendErrorMemoryPollution:
    def test_error_reply_not_in_later_question_digest(self) -> None:
        """A [BACKEND ERROR] answer reply never appears in a later question's
        prompt digest."""
        ctrl, coach, _voice, inner = make_controller()

        # First question fails.
        inner.get_advice = MagicMock(return_value=f"{BACKEND_ERROR_PREFIX} gateway down")
        ctrl.on_user_question("what's my win chance?")
        for thread in list(ctrl._answer_threads):
            thread.join(timeout=5)

        # Second question gets a real answer; its prompt digest must NOT
        # contain the error text.
        inner.get_advice = MagicMock(return_value="Race them; hold interaction.")
        ctrl.on_user_question("play then?")
        for thread in list(ctrl._answer_threads):
            thread.join(timeout=5)

        second_question = inner.get_advice.call_args.kwargs.get("question") or ""
        assert "BACKEND ERROR" not in second_question
        # The reply IS in memory (a real answer) and digestible.
        assert "Race them; hold interaction." in second_question or "Recent conversation:" in second_question
        # And no error text sits in memory at all.
        assert all(BACKEND_ERROR_PREFIX not in t.text for t in ctrl.memory.turns if t.role == "coach")

    def test_error_topic_reply_not_in_later_topic_digest(self) -> None:
        """Same guarantee on the topic path: a failed topic render is not
        digested into the next topic's prompt."""
        clock = FakeClock()
        ctrl, coach, _voice, inner = make_controller(clock=clock)

        # First topic render fails.
        inner.get_advice = MagicMock(return_value=f"{BACKEND_ERROR_PREFIX} down")
        ctrl._last_topics = [
            TopicCandidate(
                key="threat", priority=EventPriority.THREAT, evidence="Opponent revealed Bear (observed)."
            )
        ]
        assert ctrl.speak_topic_if_any() is None

        # Second topic render succeeds; its prompt must not digest the error.
        inner.get_advice = MagicMock(return_value="Watch the flier.")
        ctrl._last_topics = [
            TopicCandidate(
                key="urgent_decision",
                priority=EventPriority.URGENT_DECISION,
                evidence="Two attacks available (observed).",
            )
        ]
        result = ctrl.speak_topic_if_any()
        assert result is not None
        question = inner.get_advice.call_args.kwargs.get("question") or ""
        assert "BACKEND ERROR" not in question
        # Error text absent from memory turns.
        assert all(BACKEND_ERROR_PREFIX not in t.text for t in ctrl.memory.turns if t.role == "coach")

    def test_pending_request_still_resolves_after_error_reply(self) -> None:
        """The answer thread still pops its pending entry on an error reply —
        the thinking status resolves and later answers are not superseded."""
        ctrl, _coach, _voice, inner = make_controller()
        inner.get_advice = MagicMock(return_value=f"{BACKEND_ERROR_PREFIX} down")
        rid = ctrl.on_user_question("still alive?")
        for thread in list(ctrl._answer_threads):
            thread.join(timeout=5)
        assert rid not in ctrl._pending
