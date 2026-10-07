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


def test_older_turn_same_session_still_speaks() -> None:
    """Session-scope staleness (live reports 2026-09-16 15:41 + 21:31): the
    floor's turn advances with every ~12s advice utterance, so commentary
    rendered for turn N reaching the arbiter during turn N+1 must STILL
    speak — turn regression vs the floor is the normal case, not staleness.
    (Rank preemption is separate: a proactive request while advice OWNS the
    channel still cancels — clear the channel first, as happens live when
    advice audio completes before the topic render reaches the arbiter.)"""
    sink = RecordingSink()
    session = VoiceSession(sink)
    floor = make_identity(seq=10, turn_number=9)
    assert speak(session, "floor", "advice", floor).played is True
    session.stop_speaking(reason="audio-complete")

    older_turn = make_identity(seq=50, turn_number=5)
    outcome = speak(session, "booth", "proactive", older_turn)
    assert outcome.state == SpeechState.SPEAKING
    assert outcome.played is True
    assert sink.spoken == ["floor", "booth"]


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
    # Teardown: the sink still reports speaking, so without a stop its release monitor
    # polls on (for up to 300 s) into later tests.
    session.stop_speaking()


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
    session.stop_speaking()  # teardown: end the still-speaking utterance's release monitor


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
    # Teardown: both sinks still report speaking; end their release monitors.
    session.stop_speaking()
    session2.stop_speaking()


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
    # polling is_speaking()==True until its 300s deadline (before the
    # deadline fix it polled forever). That thread (a) burned wall clock in
    # later full-suite runs and (b) made test_turn_drop_resets_conversation
    # order-dependent — its time.sleep() ticks inside a monkeypatched loop
    # stopped that test's coaching loop early (the monitor now sleeps via the
    # module-local voice_session._sleep). stop_speaking() bumps the channel
    # token, so the monitor exits immediately.
    session.stop_speaking()


class _FakeClock:
    """Monotonic stand-in advanced only by the patched module-local sleep."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class _StuckSink:
    """A sink whose is_speaking() never turns False (wedged audio device)."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.polls = 0

    def speak(self, text: str, blocking: bool = True) -> None:
        self.spoken.append(text)

    def is_speaking(self) -> bool:
        self.polls += 1
        return True


def _wait_real(predicate, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` on the real clock (the session's clock may be fake)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_release_monitor_honours_deadline_while_sink_keeps_speaking() -> None:
    """I6: the is_speaking() branch `continue`d past the deadline check, so a
    sink stuck reporting speaking held the channel (and a polling daemon
    thread) forever. The monitor must release at release_max_wait."""
    clock = _FakeClock()
    sink = _StuckSink()
    # The fake sleep belongs to this session only: monitors other tests left running keep
    # the real one (patching the module global would let them spin on this clock).
    session = VoiceSession(
        sink, now=clock.now, release_poll_interval=0.05, release_max_wait=1.0, sleep=clock.sleep
    )
    try:
        assert speak(session, "stuck", "urgent", make_identity(seq=1)).played is True

        assert _wait_real(lambda: session.state == SpeechState.IDLE), (
            f"channel never released: polls={sink.polls}, fake clock advanced {clock.t - 1000.0:.2f}s"
        )
        # It polled while speaking (did not release on the first probe) and
        # released at the deadline, not long after it.
        assert sink.polls > 1
        assert 1.0 <= clock.t - 1000.0 <= 1.0 + 0.05 + 1e-9
        # A lower-priority request can speak again once the channel is free.
        assert speak(session, "next", "proactive", make_identity(seq=2)).played is True
    finally:
        session.stop_speaking()


def test_release_monitor_still_polls_until_deadline_when_sink_finishes() -> None:
    """The deadline must not shorten a normal utterance: a sink that stops
    speaking before release_max_wait releases on that probe, not earlier."""
    clock = _FakeClock()
    sink = _StuckSink()
    finish_after = 5

    def sleep_then_maybe_finish(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.sleeps) >= finish_after:
            sink.is_speaking = lambda: False  # type: ignore[method-assign]

    session = VoiceSession(
        sink, now=clock.now, release_poll_interval=0.05, release_max_wait=300.0, sleep=sleep_then_maybe_finish
    )
    try:
        assert speak(session, "utterance", "urgent", make_identity(seq=1)).played is True
        assert _wait_real(lambda: session.state == SpeechState.IDLE)
        assert len(clock.sleeps) == finish_after
        assert clock.t - 1000.0 < 300.0
    finally:
        session.stop_speaking()


def test_an_injected_sleep_reaches_only_its_own_session() -> None:
    """Review 2026-10-07: the I6 tests patched the module-global ``_sleep``, which
    release monitors left running by earlier tests also read; under load those
    spun on the fake clock ('assert 166509 == 5'). The sleep is now injected per
    session: another session's monitor keeps the real one."""
    leaked_sink = CompletingSink()
    leaked = VoiceSession(leaked_sink, release_poll_interval=0.001)  # left speaking, as a leak would be
    clock = _FakeClock()
    sink = _StuckSink()
    session = VoiceSession(
        sink, now=clock.now, release_poll_interval=0.05, release_max_wait=1.0, sleep=clock.sleep
    )
    try:
        assert speak(leaked, "left speaking", "urgent", make_identity(seq=1)).played is True
        assert speak(session, "stuck", "urgent", make_identity(seq=1)).played is True
        assert _wait_real(lambda: session.state == SpeechState.IDLE)
        threading.Event().wait(0.03)  # the other monitor keeps polling meanwhile
        assert leaked.state == SpeechState.SPEAKING
        assert set(clock.sleeps) == {0.05}  # only this session's polls
        assert 1.0 <= clock.t - 1000.0 <= 1.0 + 0.05 + 1e-9
    finally:
        session.stop_speaking()
        leaked.stop_speaking()


def test_release_monitor_does_not_use_global_time_sleep(monkeypatch) -> None:
    """Other tests patch the GLOBAL time.sleep (via ``<module>.time.sleep``);
    a release monitor outliving its own test must not tick their fakes. The
    arbiter sleeps through the module-local ``voice_session._sleep``."""
    real_sleep = time.sleep
    callers: list[str] = []

    def recording_sleep(seconds: float) -> None:
        callers.append(threading.current_thread().name)
        real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", recording_sleep)
    sink = CompletingSink()
    session = VoiceSession(sink, release_poll_interval=0.005)
    try:
        assert speak(session, "utterance", "urgent", make_identity(seq=1)).played is True
        threading.Event().wait(0.03)  # let the monitor poll a few times
        sink._speaking = False
        assert session.wait_for_idle(5.0) is True
    finally:
        session.stop_speaking()
    assert "voice-session-release" not in callers
    assert threading.current_thread().name not in callers  # wait_for_idle too


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
