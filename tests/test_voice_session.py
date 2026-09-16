"""Unit tests for the VoiceSession speech arbiter (pure logic, no audio)."""

from __future__ import annotations

import threading
import time

import pytest

from arenamcp.voice_session import (
    SpeechIdentity,
    SpeechOutcome,
    SpeechPriority,
    SpeechState,
    VoiceSession,
)


class RecordingSink:
    """Duck-typed sink that records speak/stop calls."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stops = 0
        self.speak_kwargs: list[dict] = []

    def speak(self, text: str, blocking: bool = True) -> None:
        self.spoken.append(text)
        self.speak_kwargs_guard(blocking)

    def speak_kwargs_guard(self, blocking: bool) -> None:
        self.speak_calls_blocking = blocking  # type: ignore[attr-defined]

    def stop(self) -> None:
        self.stops += 1


class MinimalSink:
    """Sink with a bare speak(text) only — no blocking, no stop."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


def make_identity(
    session_id: int = 1,
    match_id: str | None = "m1",
    turn_number: int = 5,
    seq: int = 1,
) -> SpeechIdentity:
    return SpeechIdentity(session_id=session_id, match_id=match_id, turn_number=turn_number, seq=seq)


def speak(session: VoiceSession, text: str, priority: str, identity) -> SpeechOutcome:
    return session.speak(text, priority=priority, identity=identity)


# ── Priority preemption matrix ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "incoming", "expected_played"),
    [
        # Higher priority always preempts.
        ("proactive", "advice", True),
        ("proactive", "urgent", True),
        ("proactive", "question", True),
        ("advice", "urgent", True),
        ("advice", "question", True),
        ("urgent", "question", True),
        # Same priority: only newer seq preempts.
        ("advice", "advice", True),
        ("question", "question", True),
        # Lower priority never preempts.
        ("advice", "proactive", False),
        ("urgent", "advice", False),
        ("urgent", "proactive", False),
        ("question", "urgent", False),
    ],
)
def test_preemption_matrix(current: str, incoming: str, expected_played: bool) -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    current_identity = make_identity(seq=10)
    assert speak(session, "first", current, current_identity).played is True

    newer = make_identity(seq=11)
    outcome = speak(session, "second", incoming, newer)

    assert outcome.played is expected_played
    if expected_played:
        assert sink.spoken == ["first", "second"]
        assert session.state == SpeechState.SPEAKING
    else:
        assert sink.spoken == ["first"]
        assert outcome.superseded_by is current_identity


def test_same_priority_older_seq_does_not_preempt() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    assert speak(session, "first", "advice", make_identity(seq=10)).played is True

    outcome = speak(session, "second", "advice", make_identity(seq=9))

    assert outcome.played is False
    assert sink.spoken == ["first"]


def test_identity_none_always_speaks_regardless_of_active_request() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    assert speak(session, "first", "question", make_identity(seq=1)).played is True

    # Legacy path (no identity) must never be stale-dropped or preempted away.
    outcome = speak(session, "legacy", "proactive", None)

    assert outcome.played is True
    assert sink.spoken == ["first", "legacy"]
    assert session.state == SpeechState.SPEAKING
    # The legacy request owns the channel now — but it carries no identity,
    # so the floor identity is unchanged.
    assert speak(session, "again", "urgent", make_identity(seq=2)).played is True


def test_speak_before_any_identity_speaks() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    assert speak(session, "first", "proactive", None).played is True
    assert sink.spoken == ["first"]


def test_request_id_attribute_used_when_seq_absent() -> None:
    """Foreign identity objects expose request_id, not seq — must still arbitrate."""

    class ForeignIdentity:
        def __init__(self, request_id: int) -> None:
            self.session_id = 1
            self.match_id = "m1"
            self.turn_number = 3
            self.request_id = request_id

    sink = RecordingSink()
    session = VoiceSession(sink)
    assert speak(session, "first", "advice", ForeignIdentity(10)).played is True
    assert speak(session, "newer", "advice", ForeignIdentity(11)).played is True
    outcome = speak(session, "older", "advice", ForeignIdentity(5))
    assert outcome.played is False


# ── Staleness ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("floor_kwargs", "incoming_kwargs"),
    [
        # Older session.
        ({"session_id": 2}, {"session_id": 1}),
        # Same session, different match.
        ({"session_id": 1, "match_id": "m2"}, {"session_id": 1, "match_id": "m1"}),
        # Same session + match, older turn.
        ({"session_id": 1, "turn_number": 6}, {"session_id": 1, "turn_number": 5}),
    ],
)
def test_stale_requests_cancelled(floor_kwargs: dict, incoming_kwargs: dict) -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    floor = make_identity(seq=10, **floor_kwargs)
    assert speak(session, "floor", "advice", floor).played is True

    stale = make_identity(seq=50, **incoming_kwargs)
    outcome = speak(session, "stale", "question", stale)

    assert outcome.state == SpeechState.CANCELLED
    assert outcome.played is False
    assert sink.spoken == ["floor"]  # audio never touched


def test_newer_session_not_stale() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    assert speak(session, "old", "advice", make_identity(session_id=1)).played is True
    assert speak(session, "new", "advice", make_identity(session_id=2, seq=2)).played is True
    assert sink.spoken == ["old", "new"]


def test_cancel_obsolete_advances_floor() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    # No active request — only the floor matters here.
    session.cancel_obsolete(make_identity(session_id=3, match_id="m9", turn_number=8))

    outcome = speak(session, "old", "question", make_identity(session_id=2, seq=99))

    assert outcome.played is False
    assert sink.spoken == []
    assert session.state == SpeechState.IDLE


def test_identity_none_never_stale_even_after_cancel_obsolete() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    session.cancel_obsolete(make_identity(session_id=9))
    assert speak(session, "legacy", "proactive", None).played is True
    assert sink.spoken == ["legacy"]


# ── stop_speaking / state / listeners ────────────────────────────────────


def test_stop_speaking_silences_sink_and_resets_state() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    speak(session, "hello", "advice", make_identity())
    assert session.state == SpeechState.SPEAKING

    session.stop_speaking("ui")

    assert sink.stops == 1
    assert session.state == SpeechState.IDLE


def test_stop_speaking_allows_lower_priority_to_speak_afterwards() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    speak(session, "question", "question", make_identity(seq=1))
    session.stop_speaking()
    assert speak(session, "proactive", "proactive", make_identity(seq=2)).played is True
    assert sink.spoken == ["question", "proactive"]


def test_listener_receives_state_transitions() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    seen: list[str] = []
    session.add_listener(seen.append)

    speak(session, "a", "advice", make_identity())
    session.stop_speaking()

    assert seen == ["speaking", "idle"]


def test_listener_exceptions_do_not_break_arbiter() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)

    def boom(_state: str) -> None:
        raise RuntimeError("listener boom")

    session.add_listener(boom)
    assert speak(session, "a", "advice", make_identity()).played is True
    assert sink.spoken == ["a"]


# ── C1 arbiter lifecycle: completion release + namespace safety ──────────


class CompletingSink:
    """Sink exposing a callable is_speaking probe (duck-typed VoiceOutput)."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self._speaking = False

    def speak(self, text: str, blocking: bool = True) -> None:
        self.spoken.append(text)
        self._speaking = True

    def stop(self) -> None:
        self._speaking = False

    def is_speaking(self) -> bool:
        return self._speaking


def test_channel_released_after_sink_completion() -> None:
    """After the sink finishes speaking, the arbiter must return to IDLE so
    lower-priority speech can speak again (C1a)."""
    sink = CompletingSink()
    session = VoiceSession(sink, release_poll_interval=0.005)
    assert speak(session, "utterance", "urgent", make_identity(seq=1)).played is True
    assert session.state == SpeechState.SPEAKING

    # Simulate playback finishing.
    sink._speaking = False

    deadline = time.monotonic() + 5.0
    while session.state != SpeechState.IDLE and time.monotonic() < deadline:
        time.sleep(0.01)

    assert session.state == SpeechState.IDLE

    # A lower-priority proactive request can now speak.
    assert speak(session, "proactive topic", "proactive", make_identity(seq=2)).played is True


def test_urgent_advice_after_question_answer_not_cancelled() -> None:
    """The repro from the review: after a question answer completes, a later
    urgent advice request must NOT be cancelled (C1c)."""
    sink = CompletingSink()
    session = VoiceSession(sink, release_poll_interval=0.005)

    # 1. A question (conversation answer, request_id namespace) speaks.
    class AnswerIdentity:
        def __init__(self, request_id: int) -> None:
            self.session_id = 1
            self.match_id = "m1"
            self.turn_number = 4
            self.request_id = request_id

    assert speak(session, "answer", "question", AnswerIdentity(1)).played is True

    # 2. The answer completes → channel released.
    sink._speaking = False
    assert session.wait_for_idle(5.0) is True

    # 3. Urgent advice (SpeechIdentity seq namespace) after the answer —
    #    previously cancelled because _active was never cleared.
    outcome = speak(session, "urgent advice", "urgent", make_identity(seq=2))
    assert outcome.played is True
    assert sink.spoken == ["answer", "urgent advice"]


def test_seq_comparison_only_within_same_identity_class() -> None:
    """seq (SpeechIdentity) and request_id (ResponseIdentity) are unrelated
    counters — same-priority supersession must never compare across them
    (C1b)."""
    sink = CompletingSink()
    session = VoiceSession(sink)

    class AnswerIdentity:
        def __init__(self, request_id: int) -> None:
            self.session_id = 1
            self.match_id = "m1"
            self.turn_number = 5
            self.request_id = request_id

    # A request_id-namespace identity owns the channel at "urgent" rank with
    # a HUGE request id. A seq-namespace identity with a small seq must NOT
    # be cancelled by cross-namespace comparison (old behavior: 2 <= 10**9).
    assert speak(session, "answer", "urgent", AnswerIdentity(10**9)).played is True
    outcome = speak(session, "topic", "urgent", make_identity(seq=2))
    assert outcome.played is True

    # And the inverse: a request_id-namespace request must not be cancelled
    # by a huge seq on the channel.
    session2_sink = CompletingSink()
    session2 = VoiceSession(session2_sink)
    assert speak(session2, "topic", "urgent", make_identity(seq=10**9)).played is True
    outcome2 = speak(session2, "answer", "urgent", AnswerIdentity(2))
    assert outcome2.played is True


def test_completion_release_does_not_clobber_newer_request() -> None:
    """A late completion from an old utterance must not clear the channel of
    a newer request (token guard)."""
    sink = CompletingSink()
    session = VoiceSession(sink, release_poll_interval=0.005)
    assert speak(session, "first", "urgent", make_identity(seq=1)).played is True

    # Second request takes the channel (preempts).
    assert speak(session, "second", "question", make_identity(seq=2)).played is True
    first_state = session.state
    assert first_state == SpeechState.SPEAKING

    # The first utterance's sink finishes (its monitor sees not-speaking, but
    # its token is stale) — the second request must keep the channel.
    sink._speaking = True  # second utterance is speaking
    time.sleep(0.05)
    assert session.state == SpeechState.SPEAKING

    # Teardown hygiene: without a stop, the release-monitor daemon keeps
    # polling is_speaking()==True until its 300s deadline. That spinning
    # thread (a) burns ~200s of wall clock in later full-suite runs and
    # (b) made test_turn_drop_resets_conversation order-dependent — its
    # time.sleep() ticks inside a monkeypatched loop stopped that test's
    # coaching loop early. stop_speaking() bumps the channel token, so the
    # monitor exits immediately.
    session.stop_speaking()


# ── Sink-shape robustness ────────────────────────────────────────────────


def test_minimal_sink_without_stop_or_blocking() -> None:
    sink = MinimalSink()
    session = VoiceSession(sink)
    assert speak(session, "hello", "urgent", None).played is True
    assert sink.spoken == ["hello"]
    session.stop_speaking()  # must not raise


def test_priority_coercion_accepts_strings_and_enums() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    speak(session, "a", "question", make_identity(seq=1))
    # Unknown priority falls back to PROACTIVE → cannot preempt QUESTION.
    assert speak(session, "b", "bogus", make_identity(seq=2)).played is False
    assert speak(session, "c", SpeechPriority.QUESTION, make_identity(seq=3)).played is True


# ── Thread safety smoke ──────────────────────────────────────────────────


def test_concurrent_speaks_are_serialized_safely() -> None:
    sink = RecordingSink()
    session = VoiceSession(sink)
    outcomes: list[SpeechOutcome] = []
    lock = threading.Lock()

    def worker(seq: int) -> None:
        outcome = speak(session, f"t{seq}", "advice", make_identity(seq=seq))
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 21)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one of the 20 same-priority requests wins the channel (the
    # highest seq, whichever lands last among the races); the rest are
    # superseded, not lost.
    played = [o for o in outcomes if o.played]
    assert len(played) >= 1
    assert len(outcomes) == 20
    assert session.state == SpeechState.SPEAKING

    session.stop_speaking()
    assert session.state == SpeechState.IDLE
    assert sink.stops >= 1
