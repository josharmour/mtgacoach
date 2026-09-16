"""Unit tests for Task 09: score provenance, calibrated delta units, and honest lookahead attribution."""
from typing import Any
import pytest

from arenamcp.mcts_evaluator import MCTSBranch, MCTSTreePayload, MCTSEvaluator


def test_raw_to_normalized_calibrated_delta():
    """Acceptance criterion: raw values 0.20 -> 0.35 yield a normalized-score change
    of 0.075, not 0.15 probability points.
    """
    root_raw_val = 0.20
    cand_raw_val = 0.35

    root_normalized = (root_raw_val + 1.0) / 2.0  # 0.600
    cand_normalized = (cand_raw_val + 1.0) / 2.0  # 0.675

    norm_delta = cand_normalized - root_normalized  # 0.075
    raw_delta = cand_raw_val - root_raw_val  # 0.150

    branch = MCTSBranch(
        action="Cast Malcolm, Alluring Scoundrel",
        action_type="cast",
        win_probability=round(cand_normalized, 3),
        value_delta=round(norm_delta, 3),
        raw_value=round(cand_raw_val, 3),
        normalized_score=round(cand_normalized, 3),
        raw_value_delta=round(raw_delta, 3),
        score_provenance="neural_afterstate",
        tag="⭐ BEST LINE",
    )

    # 1. Check data fields
    assert branch.value_delta == 0.075
    assert branch.raw_value_delta == 0.15
    assert branch.normalized_score == 0.675
    assert branch.score_provenance == "neural_afterstate"
    assert branch.simulated_visits == 0

    # 2. Check LLM prompt formatting
    tree = MCTSTreePayload(
        root_win_probability=root_normalized,
        total_simulations=1,
        turn_number=3,
        phase="Main1",
        best_action=branch.action,
        branches=[branch],
        eval_source="UW Tempo RL (92%)",
    )
    prompt = tree.format_for_llm_prompt()

    # The delta in prompt should be +7.5%, NOT +15.0%
    assert "+7.5%" in prompt
    assert "+15.0%" not in prompt
    assert "[Neural 1-Ply]" in prompt
    assert "=== ONE-PLY TACTICAL LOOKAHEAD (=== MCTS MULTI-PLY TACTICAL SEARCH ===) ===" in prompt


def test_score_provenance_differentiation():
    """Verify distinct provenance tags for neural afterstate, policy prior, and heuristic."""
    b_neural = MCTSBranch(
        action="Play Land: Island",
        action_type="land",
        win_probability=0.60,
        value_delta=0.05,
        score_provenance="neural_afterstate",
    )
    b_prior = MCTSBranch(
        action="Cast Complex Spell",
        action_type="cast",
        win_probability=0.55,
        value_delta=0.02,
        score_provenance="prior_only",
    )
    b_heuristic = MCTSBranch(
        action="Attack with Siren Pirate",
        action_type="attack",
        win_probability=0.52,
        value_delta=0.01,
        score_provenance="heuristic_lookahead",
    )

    tree = MCTSTreePayload(
        root_win_probability=0.50,
        total_simulations=3,
        branches=[b_neural, b_prior, b_heuristic],
        eval_source="MageZero RL",
    )
    prompt = tree.format_for_llm_prompt()

    assert "[Neural 1-Ply]" in prompt
    assert "[Policy Prior Only]" in prompt
    assert "[Heuristic]" in prompt


def test_hypothesized_opponent_threat_attribution():
    """Verify opponent threat candidates are described as hypothesized policy suggestions,
    not decoded facts about their hand.
    """
    tree = MCTSTreePayload(
        root_win_probability=0.50,
        total_simulations=1,
        expected_opponent_actions=["Cast Lightning Bolt", "Cast Spell Pierce"],
        branches=[
            MCTSBranch(
                action="Pass Priority",
                action_type="pass",
                win_probability=0.50,
            )
        ],
    )
    prompt = tree.format_for_llm_prompt()

    assert "• Opponent Threat Candidates (Hypothesized Policy Suggestions): Cast Lightning Bolt, Cast Spell Pierce" in prompt
    assert "Expected Opponent Counterplay:" not in prompt


def test_mcts_evaluator_lookahead_score_provenance_integration(monkeypatch):
    """Mock MageZeroClient and ModelZooClient to test end-to-end 1-ply lookahead scoring."""
    from arenamcp.magezero_client import MageZeroClient
    from arenamcp.model_zoo import ModelZooClient, ModelSpec, ModelSelection
    from arenamcp.format_profile import FormatProfile

    # Invalidate cached payload
    MCTSEvaluator._last_sig = None
    MCTSEvaluator._last_payload = None

    # Mock health and selection
    monkeypatch.setattr(MageZeroClient, "check_health", lambda: True)
    spec = ModelSpec(
        model_id="test_model",
        deck="uw_tempo",
        version=1,
        format_family="constructed",
        deck_size=60,
        singleton=False,
        deck_counts={"Island": 10, "Plains": 10, "Malcolm, Alluring Scoundrel": 4},
    )
    selection = ModelSelection(
        model_spec=spec,
        similarity=0.95,
        label="Test UW Tempo RL",
        is_resident=True,
    )
    monkeypatch.setattr(ModelZooClient, "select", lambda fmt, cards: selection)

    # Mock evaluate_batch:
    # 8 root rows with value 0.20
    # 8 afterstate rows for Island with value 0.35
    def mock_evaluate_batch(items, model_id, checkpoint_hash=None):
        results = []
        for i, (st, hand) in enumerate(items):
            # Root items have 1 Island on battlefield
            # Afterstate of Play Island has 2 Islands on battlefield
            is_afterstate = len(st.get("battlefield", [])) > 1
            val = 0.35 if is_afterstate else 0.20
            pol_player = [0.0] * 128
            pol_player[0] = 2.0  # prior for action
            pol_opp = [0.0] * 128
            pol_opp[1] = 1.5  # threat
            results.append({
                "request_index": i,
                "value": val,
                "policy_player": pol_player,
                "policy_opponent": pol_opp,
            })
        return results

    monkeypatch.setattr(MageZeroClient, "evaluate_batch", mock_evaluate_batch)

    state: dict[str, Any] = {
        "local_seat_id": 1,
        "turn": {"turn_number": 3, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True, "lands_played": 0, "cards_in_hand": 2},
            {"seat_id": 2, "life_total": 20, "is_local": False, "cards_in_hand": 3},
        ],
        "battlefield": [
            {
                "instance_id": 101,
                "name": "Island",
                "controller_seat_id": 1,
                "type_line": "Basic Land — Island",
                "is_tapped": False,
            },
        ],
        "hand": [
            {
                "instance_id": 102,
                "name": "Island",
                "type_line": "Basic Land — Island",
                "controller_seat_id": 1,
            },
            {
                "instance_id": 103,
                "name": "Malcolm, Alluring Scoundrel",
                "mana_cost": "{1}{U}",
                "type_line": "Legendary Creature — Siren Pirate",
                "controller_seat_id": 1,
            },
        ],
    }

    tree = MCTSEvaluator.evaluate(state)
    assert tree is not None
    for branch in tree.branches:
        if branch.score_provenance == 'prior_only':
            assert branch.value_delta == 0.0
            assert branch.raw_value_delta == 0.0
    # Root win probability: (0.20 + 1.0) / 2.0 = 0.60
    assert abs(tree.root_win_probability - 0.60) < 1e-3

    # Find the evaluated land branch
    land_branch = next((b for b in tree.branches if b.action == "Play Land: Island"), None)
    assert land_branch is not None
    assert land_branch.score_provenance == "neural_afterstate"
    assert land_branch.raw_value == 0.35
    assert land_branch.raw_value_delta == 0.15
    # Normalized score: (0.35 + 1.0) / 2.0 = 0.675
    assert abs(land_branch.normalized_score - 0.675) < 1e-3
    assert abs(land_branch.value_delta - 0.075) < 1e-3
    assert land_branch.details.get("afterstate_supported") is True

    # Find the unevaluated sequence branch (prior-only)
    seq_branch = next((b for b in tree.branches if b.action.startswith("Sequence:")), None)
    if seq_branch:
        assert seq_branch.score_provenance == "prior_only"
        assert seq_branch.details.get("afterstate_supported") is False

    # Prompt check
    prompt = tree.format_for_llm_prompt()
    assert "+7.5%" in prompt
    assert "+15.0%" not in prompt
    assert "[Neural 1-Ply]" in prompt
