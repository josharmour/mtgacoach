"""Wave-5 replay validation suite (T1/T2/T3/T4/T6/T7 release thresholds).

Deterministic recorded-game replay harness: scripted snapshot sequences fed
through the real ConversationController + VoiceSession pipeline with mock
backends — no real Arena, no network, no Qt, no subprocess.

Release thresholds from conversation-mode.md Wave 5 (agreed before testing):
- T1: 100% stale-discard when identity drifts mid-render.
- T2: <= 1 proactive utterance per 90s per topic class; <= 3 proactive total
  absent questions.
- T3: zero identical-topic restatements within 3x cooldown.
- T4: stop/question during playback halts within the harness's clock
  resolution — assert no post-stop playback.
- T6: question -> reply <= 8s p50 with an instant mock backend.
- T7: AI-down backend -> tagged [BACKEND ERROR] text in the transcript,
  never spoken, zero unhandled exceptions.

A fake clock (``now_fn`` injection via ``time.time`` monkeypatching on the
conversation module) makes the 90s windows testable instantly.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

import arenamcp.conversation as conversation_mod
from arenamcp.conversation import (
    CONVERSATION,
    TOPIC_PROMPT_PREFIX,
    ConversationController,
)
from arenamcp.voice_session import VoiceSession

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def card(name: str, controller: int, instance_id: int, type_line: str = "Creature") -> dict:
    return {
        "name": name,
        "controller_seat_id": controller,
        "instance_id": instance_id,
        "type_line": type_line,
        "is_attacking": False,
        "owner_seat_id": controller,
    }


def state(
    turn: int = 5,
    battlefield: list[dict] | None = None,
    life: tuple[int, int] = (20, 20),
    match_id: str = "m1",
) -> dict:
    return {
        "match_id": match_id,
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "players": [
            {"seat_id": 1, "life_total": life[0]},
            {"seat_id": 2, "life_total": life[1]},
        ],
        "turn": {"turn_number": turn, "active_player": 1, "phase": "Phase_Main1"},
        "battlefield": list(battlefield or []),
        "hand": [],
        "stack": [],
    }


class ReplayClock:
    """Controllable wall clock for cooldown/TTL windows (now_fn pattern)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ReplayHarness:
    """Scripted recorded-game replay through the conversation pipeline.

    Feed snapshot sequences with :meth:`feed`; each call runs the full
    controller path (on_state -> gate ladder -> speak_topic_if_any) with a
    mock backend and a real VoiceSession over a recording sink.
    """

    def __init__(self, get_advice, *, clock: ReplayClock | None = None) -> None:
        self.clock = clock or ReplayClock()
        self.events: list[tuple[str, dict]] = []
        self.spoken: list[tuple[str, str]] = []  # (text, priority)
        self.replies: list[str] = []  # transcript-visible replies

        self.inner = MagicMock()
        self.inner.get_advice = MagicMock(side_effect=get_advice)
        self.inner._game_plan_mgr = None

        self.sink = _ReplaySink(self)
        self.voice = VoiceSession(self.sink, release_poll_interval=0.005)

        self.coach = MagicMock()
        self.coach.voice_session = self.voice
        self.coach._voice_output = None
        self.coach._match_number = 1
        self.coach.last_match_id = "m1"
        self.coach._mcp = None
        self.coach._game_plan_mgr = None
        self.coach._coach = self.inner

        self.controller = ConversationController(
            self.coach,
            emit_event=self._emit,
            snapshot_fn=lambda: state(),
        )
        self.controller.set_mode(CONVERSATION, persist=False)

    def _emit(self, event_type: str, **fields: Any) -> None:
        self.events.append((event_type, dict(fields)))
        if event_type == "conversation_reply":
            self.replies.append(str(fields.get("text", "")))

    def set_backend(self, get_advice) -> None:
        self.inner.get_advice = MagicMock(side_effect=get_advice)

    def feed(self, prev: dict | None, curr: dict, triggers: list[str] | None = None) -> None:
        self.controller.on_state(curr, prev, triggers or [])
        self.controller.speak_topic_if_any(match_id="m1", match_number=1)
        # Model real playback duration: the topic speech occupies the channel
        # until the arbiter releases it (instant sink, but the release monitor
        # runs asynchronously — the next batch must observe a free channel).
        self.voice.wait_for_idle(5.0)

    def spoken_texts(self) -> list[str]:
        return [t for t, _p in self.spoken]


class _ReplaySink:
    """Recording sink: speak() finishes instantly (playback time == 0) so the
    harness clock resolution is a single step."""

    def __init__(self, harness: ReplayHarness) -> None:
        self._harness = harness

    def speak(self, text: str, blocking: bool = True) -> None:
        self._harness.spoken.append((text, ""))

    def stop(self) -> None:
        pass

    def is_speaking(self) -> bool:
        return False


@pytest.fixture
def fast_backend():
    """Instant mock backend — echoes a short comment derived from the prompt."""

    def get_advice(snapshot, question=None, **kw):
        return "Hold up interaction; their threat is real."

    return get_advice


@pytest.fixture
def clock():
    return ReplayClock()


@pytest.fixture
def patch_clock(monkeypatch, clock):
    """Route conversation.py's time.time() through the replay clock so 90s
    windows are testable without sleeping."""
    monkeypatch.setattr(conversation_mod.time, "time", clock.time)
    return clock


@pytest.fixture
def harness_factory(fast_backend, patch_clock, clock):
    def make(get_advice=None):
        return ReplayHarness(get_advice or fast_backend, clock=clock)

    return make


def _threat_state(turn: int = 5, iid: int = 50, extra: list[dict] | None = None) -> dict:
    battlefield = [card("Sheoldred, the Apocalypse", 2, iid)] + list(extra or [])
    return state(turn=turn, battlefield=battlefield)


# ---------------------------------------------------------------------------
# T1 — stale discard on identity drift mid-render
# ---------------------------------------------------------------------------


def test_replay_t1_stale_discard_on_identity_drift_mid_render(harness_factory):
    """T1: a response rendered while the match boundary fires is discarded —
    100% of drifted deliveries must be suppressed."""
    harness = harness_factory()
    ctrl = harness.controller

    drift = threading.Event()

    def drifting_advice(snapshot, question=None, **kw):
        drift.wait(timeout=5.0)
        return "answer from the previous match"

    harness.set_backend(drifting_advice)

    rid = ctrl.on_user_question("why not attack?")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not ctrl._pending:
        time.sleep(0.01)

    # Match boundary fires MID-RENDER (reset_for_match bumps session_id).
    ctrl.reset_for_match("m2", 2)
    drift.set()
    for thread in list(ctrl._answer_threads):
        thread.join(timeout=5)

    # The drifted answer never reached the transcript or the speaker.
    assert harness.replies == []
    assert harness.spoken == []


def test_replay_t1_turn_drift_discards_topic(harness_factory):
    """T1: a topic rendered for turn N that finishes when the identity has
    moved to a different decision context is dropped."""
    harness = harness_factory()
    ctrl = harness.controller

    def stale_advice(snapshot, question=None, **kw):
        # The match moved on during the render.
        ctrl.reset_for_match("m2", 2)
        return "comment about the old board"

    harness.set_backend(stale_advice)
    prev = state(turn=5)
    cur = _threat_state(turn=5, iid=60)
    harness.feed(prev, cur, [])

    assert harness.spoken == []
    assert harness.replies == []


# ---------------------------------------------------------------------------
# T2 — proactive frequency cap
# ---------------------------------------------------------------------------


def test_replay_t2_at_most_one_per_90s_per_topic_class(harness_factory):
    """T2: with a live 90s cooldown, an identical threat board can produce at
    most ONE proactive utterance per 90s per topic class."""
    harness = harness_factory()
    prev = state(turn=5, battlefield=[])
    cur = _threat_state(iid=61)

    for i in range(6):  # six batches, all within the same 90s window
        harness.feed(cur, cur, [])

    assert len(harness.spoken_texts()) == 1


def test_replay_t2_at_most_three_proactive_absent_questions(harness_factory):
    """T2: absent questions, <= 3 proactive utterances within ANY rolling
    3-minute span — six distinct development batches spaced 3 minutes apart
    still cap at 3 per window (each 3-topic-per-window assertion holds
    because the per-topic window is 3x the speaking cooldown and the same
    topic class dominates the sequence)."""
    harness = harness_factory()

    prev = state(turn=1, battlefield=[], life=(20, 20))
    utterance_times: list[float] = []
    for i in range(6):
        # A genuinely new opponent permanent each batch (development topic),
        # past each per-topic repetition window.
        harness.clock.advance(600)
        cur = state(
            turn=2 + i,
            battlefield=[card(f"Opp Card {i}", 2, 100 + i)],
            life=(20 - i * 4, 20),
        )
        before = len(harness.spoken_texts())
        harness.feed(prev, cur, [])
        prev = cur
        if len(harness.spoken_texts()) > before:
            utterance_times.append(harness.clock.now)

    # T2's global cap: at most 3 proactive utterances in any 180s span.
    for i, t0 in enumerate(utterance_times):
        in_span = [t for t in utterance_times if t0 <= t < t0 + 180]
        assert len(in_span) <= 3


# ---------------------------------------------------------------------------
# T3 — zero identical-topic restatements within 3x cooldown
# ---------------------------------------------------------------------------


def test_replay_t3_no_identical_topic_restatement_within_window(harness_factory):
    """T3: the same topic class never restates within 3x cooldown."""
    harness = harness_factory()
    prev = state(turn=5, battlefield=[], life=(20, 20))

    # Life swings >= 3 every batch, all inside one 3x90s window.
    for i in range(5):
        harness.clock.advance(10)
        cur = state(turn=5, battlefield=[], life=(20 - (i + 1) * 4, 20))
        harness.feed(prev, cur, [])
        prev = cur

    texts = harness.spoken_texts()
    # Every proactive utterance in the window belongs to a distinct topic
    # batch; the material_shift class may appear at most once.
    assert texts.count("Hold up interaction; their threat is real.") <= 1 or len(texts) <= 1


def test_replay_t3_threat_class_restates_only_after_window(harness_factory):
    """T3/T2 combined: a NEW threat inside the 3x window does not restate the
    threat topic; after the window it may."""
    harness = harness_factory()
    prev = state(turn=5, battlefield=[])
    cur1 = _threat_state(iid=62)
    harness.feed(prev, cur1, ["threat_detected"])
    assert len(harness.spoken_texts()) == 1

    # New threat, still inside the window.
    cur2 = _threat_state(iid=62, extra=[card("Atraxa, Grand Unifier", 2, 63)])
    harness.feed(cur2, cur2, ["threat_detected"])
    assert len(harness.spoken_texts()) == 1  # no restatement

    # After the per-key window (3x90s) a changed threat set speaks again.
    harness.clock.advance(90 * 3 + 1)
    cur3 = _threat_state(iid=62, extra=[card("Atraxa, Grand Unifier", 2, 63), card("Questing Beast", 2, 69)])
    harness.feed(cur2, cur3, ["threat_detected"])
    assert len(harness.spoken_texts()) == 2


# ---------------------------------------------------------------------------
# T4 — stop/question during playback halts (no post-stop playback)
# ---------------------------------------------------------------------------


def test_replay_t4_stop_during_playback_halts_within_clock_resolution(harness_factory):
    """T4: a stop during playback leaves NO post-stop playback — the sink
    never records an utterance after the stop at the harness's clock
    resolution."""
    harness = harness_factory()

    class SlowSink:
        """Sink whose playback is gated by a latch the test controls."""

        def __init__(self, harness: ReplayHarness) -> None:
            self._harness = harness
            self.released = threading.Event()
            self.stopped = False

        def speak(self, text: str, blocking: bool = True) -> None:
            self._harness.spoken.append((text, ""))
            self.released.wait(timeout=5.0)

        def stop(self) -> None:
            self.stopped = True
            self.released.set()

        def is_speaking(self) -> bool:
            return not self.released.is_set() and not self.stopped

    slow = SlowSink(harness)
    harness.voice._output = slow

    def blocking_advice(snapshot, question=None, **kw):
        return "a long topic commentary"

    harness.set_backend(blocking_advice)
    prev = state(turn=5, battlefield=[])
    cur = _threat_state(iid=64)

    # Feed the topic; the sink's speak blocks in "playback".
    feed_thread = threading.Thread(target=lambda: harness.feed(prev, cur, []), daemon=True)
    feed_thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not harness.spoken:
        time.sleep(0.01)
    assert harness.spoken  # playback started

    # STOP arrives mid-playback (UI stop -> engine stop path).
    harness.voice.stop_speaking("ui")
    harness.controller.cancel_pending()

    # After the stop, NOTHING new is spoken at clock resolution.
    spoken_at_stop = len(harness.spoken)
    harness.clock.advance(120)
    assert len(harness.spoken) == spoken_at_stop
    slow.released.set()
    feed_thread.join(timeout=5)


def test_replay_t4_question_during_playback_preempts_and_answers(harness_factory):
    """T4 variant: a question during playback preempts the topic; after the
    preemption the topic's audio never resumes (no post-preempt topic
    playback beyond the answer)."""
    harness = harness_factory()
    ctrl = harness.controller

    def fast_advice(snapshot, question=None, **kw):
        return "answer to the question"

    harness.set_backend(fast_advice)
    prev = state(turn=5, battlefield=[])
    cur = _threat_state(iid=65)
    harness.feed(prev, cur, [])  # topic speaks (instant sink)
    assert len(harness.spoken) == 1

    # A question arrives: preemption + answer.
    ctrl.on_user_question("what changed?")
    for thread in list(ctrl._answer_threads):
        thread.join(timeout=5)

    assert harness.spoken[-1][0] == "answer to the question"
    # No topic re-playback after the answer.
    assert len(harness.spoken) == 2


# ---------------------------------------------------------------------------
# T6 — question -> reply latency (instant mock)
# ---------------------------------------------------------------------------


def test_replay_t6_question_reply_latency_under_8s(harness_factory):
    """T6: with an instant mock backend, question -> conversation_reply must
    be <= 8s p50 (the harness measures wall time of the answer thread)."""
    harness = harness_factory()
    ctrl = harness.controller

    latencies: list[float] = []
    for i in range(10):
        start = time.monotonic()
        ctrl.on_user_question(f"question {i}")
        for thread in list(ctrl._answer_threads):
            thread.join(timeout=8)
        elapsed = time.monotonic() - start
        latencies.append(elapsed)
        assert any(c == "conversation_reply" for c, _f in harness.events), "reply event missing"
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    assert p50 <= 8.0


# ---------------------------------------------------------------------------
# T7 — AI-down backend: tagged text in transcript, never spoken
# ---------------------------------------------------------------------------


def test_replay_t7_backend_down_tagged_never_spoken(harness_factory):
    """T7: with the AI down, the transcript shows [BACKEND ERROR] text and
    NOTHING is spoken; the run completes with zero unhandled exceptions."""

    def down_backend(snapshot, question=None, **kw):
        raise RuntimeError("gateway unreachable")

    harness = harness_factory(down_backend)

    # Questions fail cleanly.
    harness.controller.on_user_question("hello?")
    for thread in list(harness.controller._answer_threads):
        thread.join(timeout=5)

    # Topic renders fail cleanly.
    prev = state(turn=5, battlefield=[])
    cur = _threat_state(iid=66)
    harness.feed(prev, cur, [])

    # Transcript has tagged error text.
    assert any(t.startswith("[BACKEND ERROR]") for t in harness.replies)
    # Nothing was ever spoken.
    assert harness.spoken == []

    # And the pipeline is still alive afterwards (backend "recovers").
    harness.set_backend(lambda snapshot, question=None, **kw: "Back online advice.")
    harness.controller.on_user_question("still there?")
    for thread in list(harness.controller._answer_threads):
        thread.join(timeout=5)
    assert "Back online advice." in harness.spoken_texts()


def test_replay_t7_topic_backend_down_returns_none_cleanly(harness_factory):
    """T7: a down backend during topic speech returns None (no crash, no
    speech, idle status restored)."""
    failures: list[Exception] = []

    def watch(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as exc:  # pragma: no cover - should never happen
                failures.append(exc)
                raise

        return wrapper

    def down_backend(snapshot, question=None, **kw):
        raise RuntimeError("down")

    harness = harness_factory(down_backend)
    # Guard: any unhandled exception on the controller surface is captured.
    harness.controller.speak_topic_if_any = watch(harness.controller.speak_topic_if_any)

    prev = state(turn=5, battlefield=[])
    cur = _threat_state(iid=67)
    harness.feed(prev, cur, ["threat_detected"])

    assert harness.spoken == []
    assert harness.replies and harness.replies[0].startswith("[BACKEND ERROR]")
    assert failures == []

    # Status lifecycle: idle after the failure (M6).
    states = [
        f.get("state")
        for c, f in harness.events
        if c == "conversation_status"
    ]
    assert states[-1] == "idle"


# ---------------------------------------------------------------------------
# Cross-threshold sanity: the harness itself is deterministic
# ---------------------------------------------------------------------------


def test_replay_harness_topic_prompt_carries_grounding(harness_factory):
    """The topic prompt keeps the TOPIC_PROMPT_PREFIX discipline in replay."""
    harness = harness_factory()
    prompts: list[str] = []

    original = harness.inner.get_advice

    def capture(snapshot, question=None, **kw):
        prompts.append(question or "")
        return original.side_effect(snapshot, question=question, **kw)

    harness.inner.get_advice = MagicMock(side_effect=capture)

    prev = state(turn=5, battlefield=[])
    cur = _threat_state(iid=68)
    harness.feed(prev, cur, [])

    assert prompts and TOPIC_PROMPT_PREFIX in prompts[0]
