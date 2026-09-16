"""Integration tests for MageZero match gating, 1-ply afterstates, and policy priors."""

from __future__ import annotations

from unittest.mock import patch

from arenamcp.magezero_client import MageZeroClient
from arenamcp.mcts_evaluator import MCTSEvaluator


def test_mcts_evaluator_gated_in_1ply_afterstates():
    # Hero playing UWTempo cards (in-distribution)
    state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 3,
            "phase": "Phase_Main1",
            "active_player": 1,
            "priority_player": 1,
        },
        "players": [
            {
                "seat_id": 1,
                "is_local": True,
                "life_total": 20,
                "lands_played": 0,
                "mana_pool": {"U": 2},
            },
            {"seat_id": 2, "is_local": False, "life_total": 18, "cards_in_hand": 3},
        ],
        "battlefield": [
            {
                "name": "Malcolm, Alluring Scoundrel",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "power": 2,
                "toughness": 1,
                "is_tapped": False,
                "type_line": "Legendary Creature — Siren Pirate",
            },
            {
                "name": "Island",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Basic Land — Island",
                "is_tapped": False,
            },
        ],
        "hand": [
            {
                "name": "Island",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Basic Land — Island",
            },
            {
                "name": "Spell Pierce",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Instant",
                "mana_cost": "{U}",
            },
        ],
    }

    # Reset cache
    MCTSEvaluator.reset_cache()
    MageZeroClient.reset_health_cache()

    # Dynamic mock evaluate_batch
    def mock_eval_batch(items, model_id=None, checkpoint_hash=None):
        results = []
        for g_state, opp_hand in items:
            # Check if land was played (i.e. battlefield has 2 Islands)
            bf_islands = sum(
                1
                for c in (g_state.get("battlefield") or [])
                if isinstance(c, dict) and c.get("name") == "Island"
            )
            if bf_islands >= 2:
                # Land afterstate
                val = 0.35
            else:
                # Root state or other afterstate
                val = 0.20

            policy_p = [0.0] * 128
            policy_p[0] = 0.5  # Pass
            policy_p[4] = 1.0  # Add U
            policy_o = [0.0] * 128
            policy_o[17] = 2.5  # Top opponent threat (slot 17)
            results.append(
                {
                    "value": val,
                    "policy_player": policy_p,
                    "policy_opponent": policy_o,
                }
            )
        return results

    from dataclasses import replace
    from arenamcp.model_zoo import ModelZooClient, ModelSelection, ModelSpec
    from test_model_zoo import _v2_manifest
    spec = replace(ModelSpec.from_manifest(_v2_manifest()), is_resident=True, promotion_status="certified")
    selection = ModelSelection(
        model_spec=spec,
        similarity=1.0,
        label="MageZero UWTempo v2",
        is_resident=True,
    )

    with patch.object(MageZeroClient, "check_health", return_value=True), patch.object(
        MageZeroClient, "evaluate_batch", side_effect=mock_eval_batch
    ), patch.object(ModelZooClient, "select", return_value=selection):
        payload = MCTSEvaluator.evaluate(state)

        # Verified neural source label
        assert "MageZero UWTempo v2" in payload.eval_source
        # Root win probability derived from averaged root predictions (0.20 -> 60%)
        assert abs(payload.root_win_probability - 0.60) < 0.02
        # Opponent counterplay decoded from policy_opponent head
        assert len(payload.expected_opponent_actions) > 0

        # Check that candidate branches have policy priors and calibrated win probabilities
        land_branch = next((b for b in payload.branches if b.action_type == "land"), None)
        assert land_branch is not None
        assert land_branch.prior_probability > 0.0
        # Land branch afterstate had Delta V = +0.15
        assert land_branch.value_delta > 0.0
        assert abs(land_branch.win_probability - 0.675) < 0.02


def test_mcts_evaluator_gated_out_falls_back_to_heuristic():
    # Hero playing deck with 0 UWTempo similarity (e.g. Mono-Green)
    state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 2,
            "phase": "Phase_Main1",
            "active_player": 1,
            "priority_player": 1,
        },
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
            }
        ],
        "hand": [
            {
                "name": "Llanowar Elves",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Creature — Elf Druid",
            }
        ],
    }

    MCTSEvaluator.reset_cache()
    payload = MCTSEvaluator.evaluate(state)

    # Must stay pure heuristic
    assert payload.eval_source == "Tactical Heuristic Lookahead"
    assert len(payload.expected_opponent_actions) == 0
