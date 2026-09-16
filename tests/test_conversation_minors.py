"""Wave-5 MINOR findings regression tests (fix agent 3).

Covers the 10 MINOR review findings that are not already exercised by the
other conversation suites:

1.  Monotonic suppression clock (now_fn injection; backward wall-clock jump
    never freezes/expiries cooldown or per-topic repetition windows).
2.  Topic delivery staleness gate is session-scope identity.is_stale_vs — a
    turn advance between the topic gate and delivery does NOT drop the
    topic (position drift is normal during the live LLM render).
4.  _evidence_signature is a CONTENT fingerprint: an identical re-created
    payload does NOT force a recompute; changed content does.
5.  _refresh_evidence compare-and-set: a concurrent refresh can never regress
    memory.last_evidence to an older snapshot.
7.  Caveat completeness: the two missing clauses mirror mcts_evaluator.py.
8.  Provenance label mapping (payload-style labels, never mislabeled).
9.  [BACKEND ERROR] text never enters memory turns → no digest pollution.

(3 lives in tests/test_standalone_conversation.py — the standalone loop owns
the None->first-match transition; 6 and 10 are doc/comment-only changes.)
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
    UNCALIBRATED_EVIDENCE_CAVEAT,
    ConversationController,
    EventPriority,
    EvidenceBlock,
    TopicCandidate,
    _evidence_signature,
    collect_evidence_block,
    format_evidence_lines,
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
# 4. Evidence signature: content fingerprint
# ---------------------------------------------------------------------------


class FakeBranch:
    def __init__(self, provenance: str) -> None:
        self.score_provenance = provenance


class FakeMctsPayload:
    def __init__(
        self,
        branches: list[str],
        eval_source: str = "MageZero UWTempo v2",
        root: float | None = 0.61,
        traps: list[str] | None = None,
    ) -> None:
        self.branches = [FakeBranch(p) for p in branches]
        self.blunder_traps = [FakeBranch(p) for p in (traps or [])]
        self.eval_source = eval_source
        self.root_win_probability = root


class TestEvidenceContentSignature:
    def test_identical_recreated_payload_does_not_force_recompute(self, monkeypatch) -> None:
        """Two DIFFERENT payload objects with the SAME content produce the
        same signature — the id()-based key is gone."""
        calls = {"n": 0}

        def make_payload():
            # A fresh object every call (a re-created payload).
            return FakeMctsPayload(["neural_afterstate", "prior_only"])

        payloads = {"current": make_payload()}

        def fake_last_payload():
            return payloads["current"]

        monkeypatch.setattr(conversation_mod, "_mcts_last_payload", fake_last_payload, raising=True)
        monkeypatch.setattr(conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True)

        ctrl, _coach, _voice, _inner = make_controller()
        state = uwtempo_like_state()
        ctrl.on_state(state, None, [])
        calls["n"] = 0

        def fake_collect(s):
            calls["n"] += 1
            return EvidenceBlock()

        monkeypatch.setattr(conversation_mod, "collect_evidence_block", fake_collect, raising=True)

        # Swap in a RE-CREATED (different object, identical content) payload.
        payloads["current"] = make_payload()
        ctrl.on_state(state, state, [])
        # No recompute: the content fingerprint is unchanged.
        assert calls["n"] == 0

    def test_changed_payload_content_forces_recompute(self, monkeypatch) -> None:
        payloads = {"current": FakeMctsPayload(["prior_only"])}
        monkeypatch.setattr(conversation_mod, "_mcts_last_payload", lambda: payloads["current"], raising=True)
        monkeypatch.setattr(conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True)

        calls = {"n": 0}

        def fake_collect(s):
            calls["n"] += 1
            return EvidenceBlock()

        monkeypatch.setattr(conversation_mod, "collect_evidence_block", fake_collect, raising=True)

        ctrl, _coach, _voice, _inner = make_controller()
        state = uwtempo_like_state()
        ctrl.on_state(state, None, [])
        assert calls["n"] == 1

        # The payload's CONTENT changes (a branch gains an evaluated row).
        payloads["current"] = FakeMctsPayload(["prior_only", "neural_afterstate"])
        ctrl.on_state(state, state, [])
        assert calls["n"] == 2

    def test_signature_includes_reason_strings(self, monkeypatch) -> None:
        monkeypatch.setattr(conversation_mod, "_mcts_last_payload", lambda: None, raising=True)
        reasons: dict[str, tuple[str | None, str | None]] = {"v": (None, None)}
        monkeypatch.setattr(conversation_mod, "_magezero_reasons", lambda: reasons["v"], raising=True)
        state = uwtempo_like_state()
        base = _evidence_signature(state)
        reasons["v"] = ("served-model-mismatch: x", None)
        changed = _evidence_signature(state)
        assert base != changed


def uwtempo_like_state() -> dict:
    return {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [card("Malcolm, Alluring Scoundrel", 1, 80)],
        "hand": [{"name": "Island"}, {"name": "Spell Pierce"}],
        "turn": {"turn_number": 5, "active_player": 1},
        "match_id": "m-1",
    }


# ---------------------------------------------------------------------------
# 5. _refresh_evidence compare-and-set (TOCTOU)
# ---------------------------------------------------------------------------


class TestRefreshEvidenceCas:
    def test_racing_refresh_never_regresses_last_evidence(self, monkeypatch) -> None:
        """Two threads refresh concurrently for DIFFERENT states; the older
        write loses the compare-and-set and last_evidence ends on the newer
        snapshot — never regressed."""
        states = {
            "old": dict(uwtempo_like_state(), hand=[{"name": "Island"}]),
            "new": dict(uwtempo_like_state(), hand=[{"name": "Island"}, {"name": "Extra"}]),
        }

        blocks = {
            "old": EvidenceBlock(eval_source="old block"),
            "new": EvidenceBlock(eval_source="new block"),
        }

        # Map each state to its block via hand shape.
        def block_for(state):
            return blocks["new"] if len(state.get("hand") or []) >= 2 else blocks["old"]

        monkeypatch.setattr(conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True)
        monkeypatch.setattr(
            conversation_mod,
            "collect_evidence_block",
            lambda s: block_for(s),
            raising=True,
        )

        ctrl, _coach, _voice, _inner = make_controller()
        # The race: A starts refreshing the OLD state (slow collect, prior_sig
        # = None), B refreshes the NEW state and completes while A blocks.
        # A's CAS must then skip its write, so last_evidence stays "new".
        release_a = threading.Event()

        def slow_collect_old(state):
            if len(state.get("hand") or []) < 2:
                release_a.wait(timeout=5.0)
            return block_for(state)

        monkeypatch.setattr(conversation_mod, "collect_evidence_block", slow_collect_old, raising=True)

        thread_a = threading.Thread(target=ctrl._refresh_evidence, args=(states["old"],), daemon=True)
        thread_a.start()

        # B refreshes the NEW state and completes while A is blocked.
        ctrl._refresh_evidence(states["new"])
        assert ctrl.memory.last_evidence is blocks["new"]

        # Unblock A: its stale write must be skipped by the CAS.
        release_a.set()
        thread_a.join(timeout=5)

        assert ctrl.memory.last_evidence is blocks["new"]
        assert ctrl.memory.last_evidence.eval_source == "new block"

    def test_plain_refresh_still_writes_when_no_race(self, monkeypatch) -> None:
        monkeypatch.setattr(
            conversation_mod,
            "collect_evidence_block",
            lambda s: EvidenceBlock(eval_source="solo"),
            raising=True,
        )
        monkeypatch.setattr(conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True)
        ctrl, _coach, _voice, _inner = make_controller()
        ctrl._refresh_evidence(uwtempo_like_state())
        assert ctrl.memory.last_evidence is not None
        assert ctrl.memory.last_evidence.eval_source == "solo"


# ---------------------------------------------------------------------------
# 7. Caveat completeness (mirrors mcts_evaluator.py wording)
# ---------------------------------------------------------------------------


class TestCaveatCompleteness:
    def test_missing_clauses_present(self) -> None:
        # 'treat candidate order as the tactical heuristic ranking, not a
        # neural recommendation' + legal-action preservation — mirrored from
        # mcts_evaluator.py's experimental-evidence line.
        assert "tactical heuristic ranking" in UNCALIBRATED_EVIDENCE_CAVEAT
        assert "not a neural recommendation" in UNCALIBRATED_EVIDENCE_CAVEAT
        assert "preserve legal-action checks" in UNCALIBRATED_EVIDENCE_CAVEAT
        # Core claims unchanged (no strengthening/weakening).
        assert "not calibrated win probabilities" in UNCALIBRATED_EVIDENCE_CAVEAT
        assert "supporting evidence" in UNCALIBRATED_EVIDENCE_CAVEAT
        assert "policy-only preferences" in UNCALIBRATED_EVIDENCE_CAVEAT

    def test_caveat_still_reaches_both_prompt_paths(self) -> None:
        ctrl, coach, _voice, _inner = make_controller()
        ctrl.memory.last_evidence = EvidenceBlock(
            deck_supported=True,
            deck_compatible=True,
            provenance="neural_afterstate",
            evaluated=True,
            uncertainty_reason=UNCALIBRATED_EVIDENCE_CAVEAT,
        )
        augmented = ctrl._augment_question("how am I doing?")
        assert "tactical heuristic ranking" in augmented

        ctrl._last_topics = [
            TopicCandidate(
                key="threat", priority=EventPriority.THREAT, evidence="Opponent revealed Bear (observed)."
            )
        ]
        assert ctrl.speak_topic_if_any() is not None
        question = coach._coach.get_advice.call_args.kwargs.get("question") or ""
        assert "tactical heuristic ranking" in question


# ---------------------------------------------------------------------------
# 8. Provenance label mapping
# ---------------------------------------------------------------------------


class TestProvenanceLabels:
    def _text_for(self, provenance: str, evaluated: bool) -> str:
        ev = EvidenceBlock(
            deck_supported=True,
            deck_compatible=True,
            eval_source="MageZero UWTempo v2",
            provenance=provenance,
            evaluated=evaluated,
            uncertainty_reason=UNCALIBRATED_EVIDENCE_CAVEAT,
        )
        return format_evidence_lines(ev)

    def test_prior_only_maps_to_policy_preference(self) -> None:
        text = self._text_for("prior_only", evaluated=False)
        assert "Provenance: prior_only (policy preference)" in text

    def test_heuristic_lookahead_maps_to_heuristic(self) -> None:
        text = self._text_for("heuristic_lookahead", evaluated=False)
        assert "Provenance: heuristic_lookahead (heuristic)" in text

    def test_unsupported_fallback_maps_to_approx_lookahead(self) -> None:
        text = self._text_for("unsupported_fallback", evaluated=False)
        assert "Provenance: unsupported_fallback (approx lookahead)" in text

    def test_unknown_provenance_degrades_to_policy_preference(self) -> None:
        text = self._text_for("experimental_gizmo", evaluated=False)
        assert "(policy preference, not an evaluated outcome)" in text

    def test_neural_afterstate_without_evaluated_row_never_reads_evaluated(self, monkeypatch) -> None:
        """A neural_afterstate provenance with a heuristic eval_source is NOT
        evaluated (collect_evidence_block rule) — its label must not read as
        an evaluated outcome either."""
        monkeypatch.setattr(conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True)
        payload = FakeMctsPayload(["neural_afterstate"], eval_source="Tactical Heuristic Lookahead")
        monkeypatch.setattr(conversation_mod, "_mcts_last_payload", lambda: payload, raising=True)
        ev = collect_evidence_block(uwtempo_like_state())
        assert ev.evaluated is False
        text = format_evidence_lines(ev)
        assert "(evaluated one-ply afterstates" not in text

    def test_evaluated_row_keeps_evaluated_label(self) -> None:
        text = self._text_for("neural_afterstate", evaluated=True)
        assert "(evaluated one-ply afterstates" in text


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
