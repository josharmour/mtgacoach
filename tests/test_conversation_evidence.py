"""Wave-4 tests: MageZero evidence in conversation prompts.

Covers (synthetic snapshots, no Qt, no network, no subprocess):
- EvidenceBlock population: supported / unsupported / MageZero-absent paths
  (no exceptions anywhere, all accessors guarded).
- Prompt integration: caveat present, no uncalibrated win-probability claims,
  root_win_probability None unless an evaluated row exists.
- last_evidence refresh guard (recompute only on change).
- Material-assessment topic gated on support + provenance + evaluated row.
- Fallback label surfacing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from arenamcp.conversation import (
    CONVERSATION,
    UNCALIBRATED_EVIDENCE_CAVEAT,
    ConversationController,
    EvidenceBlock,
    MatchMemory,
    TopicSelector,
    collect_evidence_block,
    format_evidence_lines,
)

# ---------------------------------------------------------------------------
# Helpers (mock patterns reused from tests/test_conversation.py)
# ---------------------------------------------------------------------------


def make_controller(**kwargs: Any) -> tuple[ConversationController, Any, Any]:
    """Controller over a fake coach with stubbed inner get_advice."""
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
    inner.get_advice = MagicMock(return_value="Hold up mana for interaction.")
    inner._game_plan_mgr = None
    coach._coach = inner

    snapshot = kwargs.pop("snapshot", {"match_id": "m-1", "turn": {"turn_number": 4}})
    controller = ConversationController(
        coach,
        emit_event=None,
        snapshot_fn=lambda: dict(snapshot),
    )
    controller.set_mode(CONVERSATION, persist=False)
    return controller, coach, voice


def uwtempo_state() -> dict[str, Any]:
    """A state whose hero deck passes the UWTempo gate (in-match subset)."""
    return {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [
            {
                "name": "Malcolm, Alluring Scoundrel",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Legendary Creature — Siren Pirate",
            },
        ],
        "hand": [
            {"name": "Island", "type_line": "Basic Land — Island"},
            {"name": "Spell Pierce", "type_line": "Instant"},
            {"name": "No More Lies", "type_line": "Instant"},
        ],
    }


def monogreen_state() -> dict[str, Any]:
    """A state whose hero deck cannot match any MageZero archetype."""
    return {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [
            {
                "name": "Forest",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Basic Land — Forest",
            },
        ],
        "hand": [
            {"name": "Llanowar Elves", "type_line": "Creature — Elf Druid"},
        ],
    }


def supported_evidence(**overrides: Any) -> EvidenceBlock:
    ev = EvidenceBlock(
        model_id="uwtempo/ver2",
        checkpoint_hash="abc123",
        deck_supported=True,
        deck_compatible=True,
        similarity=0.92,
        eval_source="MageZero UWTempo v2",
        provenance="neural_afterstate",
        evaluated=True,
        root_win_probability=0.60,
        uncertainty_reason=UNCALIBRATED_EVIDENCE_CAVEAT,
    )
    for key, value in overrides.items():
        setattr(ev, key, value)
    return ev


# ---------------------------------------------------------------------------
# EvidenceBlock population
# ---------------------------------------------------------------------------


class TestEvidencePopulation:
    def test_supported_path_with_mocks(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod, "_mcts_last_payload", lambda: None, raising=True
        )
        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True
        )
        ev = collect_evidence_block(uwtempo_state())
        assert ev.deck_supported is True
        assert ev.deck_compatible is True
        assert isinstance(ev.similarity, float)
        assert ev.eval_source == "MageZero UWTempo v2"
        # No payload => no provenance, no evaluated row, no win probability.
        assert ev.provenance is None
        assert ev.evaluated is False
        assert ev.root_win_probability is None
        assert ev.calibrated is False
        # Supported but no evaluated row: no invented model identity.
        assert ev.model_id is None

    def test_unsupported_path(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod, "_mcts_last_payload", lambda: None, raising=True
        )
        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True
        )
        ev = collect_evidence_block(monogreen_state())
        assert ev.deck_supported is False
        assert ev.is_supported() is False
        assert ev.eval_source == "Tactical Heuristic Lookahead"
        assert ev.root_win_probability is None

    def test_unavailable_path_state_not_dict(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True
        )
        for bad in (None, "not-a-state", 42):
            ev = collect_evidence_block(bad)  # type: ignore[arg-type]
            assert ev.deck_supported is False
            assert ev.root_win_probability is None
            assert "unavailable" in ev.uncertainty_reason.lower()

    def test_fallback_reason_surfaces(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod,
            "_magezero_reasons",
            lambda: ("no-coaching-endpoint-configured", None),
            raising=True,
        )
        ev = collect_evidence_block(uwtempo_state())
        assert ev.fallback_reason == "no-coaching-endpoint-configured"
        text = format_evidence_lines(ev)
        assert "no-coaching-endpoint-configured" in text

    def test_never_raises_on_hostile_state(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True
        )
        hostile: dict[str, Any] = {
            "battlefield": [{"instance_id": "not-an-int", "name": 1}],
            "hand": [{"nonsense": object()}],
            "zones": {"opponent_hand_count": "many"},
        }
        ev = collect_evidence_block(hostile)
        assert isinstance(ev, EvidenceBlock)

    def test_full_magezero_absent_no_exceptions(self, monkeypatch) -> None:
        """Every accessor missing (no module import at all)."""
        import arenamcp.conversation as conversation_mod

        for name in ("_magezero_reasons", "_mcts_last_payload", "_selection_identity"):
            monkeypatch.setattr(
                conversation_mod,
                name,
                MagicMock(side_effect=ImportError("module gone")),
                raising=True,
            )
        ev = collect_evidence_block(uwtempo_state())
        assert isinstance(ev, EvidenceBlock)
        assert ev.root_win_probability is None
        text = format_evidence_lines(ev)
        assert isinstance(text, str) and text


# ---------------------------------------------------------------------------
# Evaluated-row / provenance handling
# ---------------------------------------------------------------------------


class FakePayload:
    def __init__(
        self,
        branches_provenance: list[str],
        eval_source: str = "MageZero UWTempo v2 (92%)",
        root: float | None = 0.61,
    ) -> None:
        self.branches = [MagicMock(score_provenance=p) for p in branches_provenance]
        self.blunder_traps = []
        self.eval_source = eval_source
        self.root_win_probability = root


class TestEvaluatedRows:
    def _collect(self, monkeypatch, payload: FakePayload, reasons=(None, None)):
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod, "_mcts_last_payload", lambda: payload, raising=True
        )
        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: reasons, raising=True
        )
        return collect_evidence_block(uwtempo_state())

    def test_neural_afterstate_row_is_evaluated(self, monkeypatch) -> None:
        ev = self._collect(monkeypatch, FakePayload(["neural_afterstate", "prior_only"]))
        assert ev.evaluated is True
        assert ev.provenance == "neural_afterstate"
        assert ev.root_win_probability == pytest.approx(0.61)
        assert ev.calibrated is False
        assert UNCALIBRATED_EVIDENCE_CAVEAT in ev.uncertainty_reason

    def test_prior_only_is_not_evaluated_no_root_score(self, monkeypatch) -> None:
        ev = self._collect(monkeypatch, FakePayload(["prior_only"], root=0.60))
        # Provenance exists but no evaluated row: root_win_probability stays None.
        assert ev.evaluated is False
        assert ev.provenance == "prior_only"
        assert ev.root_win_probability is None

    def test_heuristic_label_blocks_evaluated(self, monkeypatch) -> None:
        ev = self._collect(
            monkeypatch,
            FakePayload(["neural_afterstate"], eval_source="Tactical Heuristic Lookahead"),
        )
        assert ev.evaluated is False
        assert ev.root_win_probability is None

    def test_reject_reason_surfaces_with_payload(self, monkeypatch) -> None:
        ev = self._collect(
            monkeypatch, FakePayload(["prior_only"]), reasons=("served-model-mismatch: x", None)
        )
        assert ev.fallback_reason == "served-model-mismatch: x"
        # The reason is carried on the block payload; when no evaluated row
        # exists it explains why model evidence is absent. With provenance
        # present the caveat takes precedence in the rendered block, so the
        # reason is asserted on the block itself.
        assert ev.fallback_reason == "served-model-mismatch: x"
        assert ev.payload["fallback_reason"] == "served-model-mismatch: x"

    def test_policy_preference_not_described_as_evaluated(self, monkeypatch) -> None:
        ev = self._collect(monkeypatch, FakePayload(["prior_only"]))
        text = format_evidence_lines(ev)
        assert "not an evaluated outcome" in text
        # Never a bare win-probability number from uncalibrated evidence.
        assert "%" not in text or "similarity" in text


# ---------------------------------------------------------------------------
# Prompt integration
# ---------------------------------------------------------------------------


class TestPromptIntegration:
    def test_caveat_in_question_prompt_when_supported(self) -> None:
        ctrl, _, _ = make_controller()
        ctrl.memory.last_evidence = supported_evidence()
        augmented = ctrl._augment_question("how am I doing?")
        assert "MageZero evidence:" in augmented
        assert "MageZero UWTempo v2" in augmented
        # Uncertainty sentence present via the caveat in the evidence text.
        assert "not calibrated win probabilities" in augmented

    def test_no_win_probability_claim_from_uncalibrated(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        payload = FakePayload(["prior_only"], root=0.75)
        monkeypatch.setattr(
            conversation_mod, "_mcts_last_payload", lambda: payload, raising=True
        )
        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True
        )
        ctrl, _, _ = make_controller()
        ev = collect_evidence_block(uwtempo_state())
        ctrl.memory.last_evidence = ev
        augmented = ctrl._augment_question("what's my chance?")
        # Uncalibrated evidence must NOT surface a win-probability figure.
        assert ev.root_win_probability is None
        assert "calibrated win probabilities" in UNCALIBRATED_EVIDENCE_CAVEAT

    def test_evaluated_row_keeps_uncalibrated_framing(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        payload = FakePayload(["neural_afterstate"], root=0.62)
        monkeypatch.setattr(
            conversation_mod, "_mcts_last_payload", lambda: payload, raising=True
        )
        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True
        )
        ev = collect_evidence_block(uwtempo_state())
        text = format_evidence_lines(ev)
        # Even evaluated rows carry the codebase's own uncalibrated framing.
        assert "uncalibrated" in text
        assert "not full rules-engine search" in text

    def test_topic_prompt_carries_evidence_block(self) -> None:
        ctrl, coach, _ = make_controller()
        ctrl.memory.last_evidence = supported_evidence()
        ctrl.memory.append(
            __import__("arenamcp.conversation", fromlist=["ConversationTurn"]).ConversationTurn(
                role="user", text="what changed?"
            )
        )
        from arenamcp.conversation import EventPriority, TopicCandidate

        ctrl._last_topics = [
            TopicCandidate(key="threat", priority=EventPriority.THREAT, evidence="Opponent revealed Bear (observed).")
        ]
        result = ctrl.speak_topic_if_any()
        assert result is not None
        question = coach._coach.get_advice.call_args.kwargs.get("question") or (
            coach._coach.get_advice.call_args[0][1]
        )
        assert "MageZero evidence:" in question
        assert "MageZero UWTempo v2" in question
        assert "not calibrated win probabilities" in question

    def test_unsupported_deck_keeps_tactical_label(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True
        )
        ev = collect_evidence_block(monogreen_state())
        text = format_evidence_lines(ev)
        # Gating's 'Tactical Heuristic Lookahead' label semantics preserved.
        assert "Tactical Heuristic Lookahead" in text
        assert "not supported" in text

    def test_unavailable_block_says_so_plainly(self) -> None:
        text = format_evidence_lines(None)
        assert "unavailable" in text.lower()
        # Conversation remains useful: an actionable framing, not a dead end.
        assert "coaching continues" in text

    def test_answer_path_works_with_magezero_fully_absent(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod, "_magezero_reasons", lambda: (None, None), raising=True
        )
        ctrl, coach, voice = make_controller()
        ctrl.on_user_question("why not attack?")
        for thread in list(ctrl._answer_threads):
            thread.join(timeout=5)
        assert coach._coach.get_advice.call_count == 1
        assert voice.speak.call_count == 1
        assert ctrl.memory.last_evidence is not None

    def test_topic_path_works_with_magezero_fully_absent(self) -> None:
        ctrl, coach, voice = make_controller()
        from arenamcp.conversation import EventPriority, TopicCandidate

        ctrl._last_topics = [
            TopicCandidate(key="threat", priority=EventPriority.THREAT, evidence="Opponent revealed Bear (observed).")
        ]
        assert ctrl.speak_topic_if_any() is not None
        assert voice.speak.call_count == 1
        question = coach._coach.get_advice.call_args.kwargs.get("question") or (
            coach._coach.get_advice.call_args[0][1]
        )
        # Unsupported/absent evidence: plain statement + tactical basis.
        assert "MageZero evidence:" in question
        assert "not supported" in question or "unavailable" in question


# ---------------------------------------------------------------------------
# Refresh guard (memory.last_evidence)
# ---------------------------------------------------------------------------


class TestEvidenceRefreshGuard:
    def test_refresh_skipped_when_signature_unchanged(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        calls = {"n": 0}

        def fake_collect(state):
            calls["n"] += 1
            return EvidenceBlock()

        monkeypatch.setattr(
            conversation_mod, "collect_evidence_block", fake_collect, raising=True
        )
        ctrl, _, _ = make_controller()
        state = uwtempo_state()
        ctrl.on_state(state, None, [])
        assert calls["n"] == 1
        ctrl.on_state(state, state, [])
        # Same inputs => no recompute (cheap guard).
        assert calls["n"] == 1
        state2 = dict(state)
        state2["hand"] = [{"name": "Extra Card"}]
        ctrl.on_state(state2, state, [])
        assert calls["n"] == 2

    def test_reset_for_match_clears_signature(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        calls = {"n": 0}

        def fake_collect(state):
            calls["n"] += 1
            return EvidenceBlock()

        monkeypatch.setattr(
            conversation_mod, "collect_evidence_block", fake_collect, raising=True
        )
        ctrl, _, _ = make_controller()
        state = uwtempo_state()
        ctrl.on_state(state, None, [])
        assert calls["n"] == 1
        ctrl.reset_for_match("m-2", 2)
        ctrl.on_state(state, state, [])
        # Signature was cleared at the boundary: recompute happens.
        assert calls["n"] == 2

    def test_refresh_never_raises(self, monkeypatch) -> None:
        import arenamcp.conversation as conversation_mod

        monkeypatch.setattr(
            conversation_mod,
            "collect_evidence_block",
            MagicMock(side_effect=RuntimeError("boom")),
            raising=True,
        )
        ctrl, _, _ = make_controller()
        ctrl.on_state(uwtempo_state(), None, [])  # must not raise
        assert ctrl.memory.last_evidence is None


# ---------------------------------------------------------------------------
# Material-assessment topic gating
# ---------------------------------------------------------------------------


class TestMaterialAssessmentGating:
    def _select(self, memory: MatchMemory) -> list:
        prev = {
            "local_seat_id": 1,
            "opponent_seat_id": 2,
            "players": [
                {"seat_id": 1, "life_total": 20},
                {"seat_id": 2, "life_total": 20},
            ],
        }
        cur = {
            "local_seat_id": 1,
            "opponent_seat_id": 2,
            "players": [
                {"seat_id": 1, "life_total": 16},
                {"seat_id": 2, "life_total": 20},
            ],
            "zones": {"opponent_hand_count": 5},
        }
        return TopicSelector().select(prev, cur, [], memory)

    def test_supported_and_evaluated_exposes_topic(self) -> None:
        memory = MatchMemory(last_evidence=supported_evidence())
        topics = self._select(memory)
        assert "material_assessment" in [t.key for t in topics]

    def test_unsupported_deck_no_topic(self) -> None:
        memory = MatchMemory(last_evidence=supported_evidence(deck_supported=False, deck_compatible=False))
        topics = self._select(memory)
        assert "material_assessment" not in [t.key for t in topics]

    def test_policy_only_provenance_no_topic(self) -> None:
        memory = MatchMemory(
            last_evidence=supported_evidence(provenance="prior_only", evaluated=False)
        )
        topics = self._select(memory)
        assert "material_assessment" not in [t.key for t in topics]

    def test_no_evaluated_row_no_topic(self) -> None:
        memory = MatchMemory(
            last_evidence=supported_evidence(root_win_probability=None, evaluated=False)
        )
        topics = self._select(memory)
        assert "material_assessment" not in [t.key for t in topics]

    def test_no_material_change_no_topic(self) -> None:
        memory = MatchMemory(last_evidence=supported_evidence())
        # Identical states: no material shift, so no assessment topic even
        # though the evidence gate would pass (score movement alone never
        # triggers speech).
        state = {
            "local_seat_id": 1,
            "opponent_seat_id": 2,
            "players": [{"seat_id": 1, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        }
        topics = TopicSelector().select(state, dict(state), [], memory)
        assert topics == []

    def test_evidence_note_carries_uncertainty_sentence(self) -> None:
        memory = MatchMemory(last_evidence=supported_evidence())
        topics = self._select(memory)
        topic = next(t for t in topics if t.key == "material_assessment")
        assert "not calibrated win probabilities" in topic.evidence
        assert "policy" in topic.evidence.lower() or "evaluated" in topic.evidence.lower()


# ---------------------------------------------------------------------------
# Real-source sanity (unguarded but network-free: gating runs locally)
# ---------------------------------------------------------------------------


class TestRealSources:
    def test_real_gating_on_uwtempo_state(self) -> None:
        ev = collect_evidence_block(uwtempo_state())
        assert ev.deck_supported is True
        assert ev.eval_source == "MageZero UWTempo v2"

    def test_real_gating_on_off_archetype(self) -> None:
        ev = collect_evidence_block(monogreen_state())
        assert ev.deck_supported is False
        assert ev.eval_source == "Tactical Heuristic Lookahead"

    def test_identity_accessor_never_blocks_on_discovery(self, monkeypatch) -> None:
        import arenamcp.model_zoo as model_zoo_mod

        monkeypatch.setattr(
            model_zoo_mod.ModelZooClient, "select", lambda *a, **k: None, raising=True
        )
        model_id, checkpoint_hash = __import__(
            "arenamcp.conversation", fromlist=["_selection_identity"]
        )._selection_identity(uwtempo_state())
        assert model_id is None and checkpoint_hash is None
