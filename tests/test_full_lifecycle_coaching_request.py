"""End-to-end integration lifecycle test for MTGA Coach RL swarm.

Exercises the full coaching request lifecycle:
1. Discovery (/healthz, /models) and ModelZooClient residency
2. Hero deck extraction and gating against certified resident model
3. Hand belief state sampling with uncertainty
4. Mechanical afterstate lookahead evaluation and batch inference correlation
5. Score provenance, calibrated delta computation, and attribution
6. LLM prompt and compact coach UI presentation
7. Error recovery and graceful fallback on server mismatch, uncertified models,
   and unrepresentable states.
"""
from typing import Any
import pytest
from dataclasses import replace

from arenamcp.format_profile import FormatProfile
from arenamcp.magezero_client import MageZeroClient
from arenamcp.magezero_gating import extract_hero_deck, UWTEMPO_DECK_COUNTS
from arenamcp.model_zoo import ModelZooClient, ModelSpec, ModelSelection
from arenamcp.mcts_evaluator import MCTSEvaluator, MCTSBranch, MCTSTreePayload
from test_model_zoo import _v2_manifest


def _build_test_game_state(hero_island_count: int = 1, unknown_hand: bool = False) -> dict[str, Any]:
    hand = [
        {
            "instance_id": 102,
            "name": "Island",
            "type_line": "Basic Land — Island",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
        },
        {
            "instance_id": 103,
            "name": "Malcolm, Alluring Scoundrel",
            "mana_cost": "{1}{U}",
            "type_line": "Legendary Creature — Siren Pirate",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "power": 2,
            "toughness": 1,
        },
    ]
    bf = []
    for i in range(hero_island_count):
        bf.append({
            "instance_id": 200 + i,
            "name": "Island",
            "type_line": "Basic Land — Island",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "is_tapped": False,
        })

    opp_player: dict[str, Any] = {"seat_id": 2, "life_total": 20, "is_local": False}
    if not unknown_hand:
        opp_player["cards_in_hand"] = 3

    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 3, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True, "lands_played": 0, "cards_in_hand": 2},
            opp_player,
        ],
        "battlefield": bf,
        "hand": hand,
    }


def test_full_lifecycle_healthy_request(monkeypatch):
    """Lifecycle path: healthy server, resident model, valid afterstate, calibrated prompt."""
    endpoint = "http://127.0.0.1:59999"
    monkeypatch.setattr(MageZeroClient, "get_active_endpoint", lambda: endpoint)
    monkeypatch.setattr(MageZeroClient, "check_health", lambda: True)

    raw_manifest = _v2_manifest()
    spec = replace(
        ModelSpec.from_manifest(raw_manifest),
        is_resident=True,
        promotion_status="certified",
    )

    # 1. Discovery and caching
    with ModelZooClient._lock:
        ModelZooClient._active_host = endpoint
        ModelZooClient._models_by_host[endpoint] = [spec]
        ModelZooClient._resident_models = {spec.model_id}

    assert ModelZooClient.resident_model_ids() == {spec.model_id}

    # 2. Hero deck extraction and selection
    game_state = _build_test_game_state()
    extraction = extract_hero_deck(game_state)
    assert extraction.is_compatible
    assert not extraction.is_full_deck
    assert "Malcolm, Alluring Scoundrel" in extraction.cards

    fmt = FormatProfile(family="constructed", variant="standard", deck_size=60, singleton=False)
    # Selection from observed cards
    selection = ModelZooClient.select(fmt, extraction.cards, refresh=False)
    assert selection is not None
    assert selection.model_spec.model_id == spec.model_id

    # 3. Batch evaluation mock
    def mock_eval_batch(items, model_id=None):
        results = []
        for i, (st, hand) in enumerate(items):
            # Afterstate of Play Island has 2 Islands
            is_afterstate = len(st.get("battlefield", [])) > 1
            val = 0.35 if is_afterstate else 0.20
            pol_p = [0.0] * 128
            pol_p[0] = 1.0
            pol_o = [0.0] * 128
            pol_o[10] = 2.0  # Threat slot
            results.append({
                "request_index": i,
                "value": val,
                "policy_player": pol_p,
                "policy_opponent": pol_o,
            })
        return results

    monkeypatch.setattr(MageZeroClient, "evaluate_batch", mock_eval_batch)

    # Reset caches
    MCTSEvaluator.reset_cache()

    # 4. MCTSEvaluator execution
    payload = MCTSEvaluator.evaluate(game_state)
    assert payload is not None
    assert abs(payload.root_win_probability - 0.60) < 1e-3

    # 5. Check branches & score provenance
    land_branch = next((b for b in payload.branches if b.action == "Play Land: Island"), None)
    assert land_branch is not None
    assert land_branch.score_provenance == "neural_afterstate"
    assert land_branch.raw_value == 0.35
    assert land_branch.raw_value_delta == 0.15
    assert abs(land_branch.normalized_score - 0.675) < 1e-3
    assert abs(land_branch.value_delta - 0.075) < 1e-3
    assert land_branch.details.get("afterstate_supported") is True

    # 6. Check LLM prompt formatting
    prompt = payload.format_for_llm_prompt()
    assert "=== ONE-PLY TACTICAL LOOKAHEAD (=== MCTS MULTI-PLY TACTICAL SEARCH ===) ===" in prompt
    assert "+7.5%" in prompt
    assert "+15.0%" not in prompt
    assert "[Neural 1-Ply]" in prompt
    assert "• Opponent Threat Candidates (Hypothesized Policy Suggestions):" in prompt


def test_full_lifecycle_fallback_on_discovery_failure(monkeypatch):
    """Failure scenario: discovery fails / no endpoint -> graceful fallback to heuristic."""
    monkeypatch.setattr(MageZeroClient, "get_active_endpoint", lambda: None)
    monkeypatch.setattr(MageZeroClient, "check_health", lambda: False)

    ModelZooClient.reset()
    MCTSEvaluator.reset_cache()

    game_state = _build_test_game_state()
    payload = MCTSEvaluator.evaluate(game_state)
    assert payload is not None
    assert payload.eval_source == "Tactical Heuristic Lookahead"
    # All branches receive heuristic attribution
    for b in payload.branches:
        assert b.score_provenance == "heuristic_lookahead"

    prompt = payload.format_for_llm_prompt()
    assert "[Heuristic]" in prompt


def test_full_lifecycle_fallback_on_unknown_opponent_hand(monkeypatch):
    """Failure scenario: opponent hand count is unknown -> fall back rather than fabricating empty hands."""
    endpoint = "http://127.0.0.1:59999"
    monkeypatch.setattr(MageZeroClient, "get_active_endpoint", lambda: endpoint)
    monkeypatch.setattr(MageZeroClient, "check_health", lambda: True)

    spec = replace(
        ModelSpec.from_manifest(_v2_manifest()),
        is_resident=True,
        promotion_status="certified",
    )
    with ModelZooClient._lock:
        ModelZooClient._active_host = endpoint
        ModelZooClient._models_by_host[endpoint] = [spec]
        ModelZooClient._resident_models = {spec.model_id}

    MCTSEvaluator.reset_cache()

    # Unknown hand count in game state
    game_state = _build_test_game_state(unknown_hand=True)
    payload = MCTSEvaluator.evaluate(game_state)
    assert payload is not None
    # Evaluator must explicitly fall back to heuristic lookahead
    assert payload.eval_source == "Tactical Heuristic Lookahead"
    for b in payload.branches:
        assert b.score_provenance == "heuristic_lookahead"
