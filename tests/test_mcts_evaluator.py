from __future__ import annotations

from typing import Any

from arenamcp.magezero_client import MageZeroClient
from arenamcp.mcts_evaluator import MCTSEvaluator, MCTSTreePayload
from arenamcp.opponent_model import OpponentModel, OpponentProfile


def test_mcts_evaluator_basic():
    state: dict[str, Any] = {
        "local_seat_id": 2,
        "turn": {
            "turn_number": 3,
            "phase": "Phase_Main1",
        },
        "players": [
            {"seat_id": 1, "life_total": 18, "is_local": False},
            {"seat_id": 2, "life_total": 20, "is_local": True, "mana_pool": {"U": 2}},
        ],
        "battlefield": [
            {
                "instance_id": 101,
                "name": "Kitsa, Otter Ball",
                "controller_seat_id": 2,
                "power": 1,
                "toughness": 3,
                "is_tapped": False,
                "type_line": "Creature — Otter Wizard",
            },
            {
                "instance_id": 102,
                "name": "Island",
                "controller_seat_id": 2,
                "type_line": "Basic Land — Island",
                "is_tapped": False,
            },
        ],
        "hand": [
            {
                "name": "Spell Pierce",
                "mana_cost": "{U}",
                "type_line": "Instant",
                "oracle_text": "Counter target noncreature spell unless its controller pays {2}.",
            },
        ],
    }

    tree: MCTSTreePayload = MCTSEvaluator.evaluate(state)
    assert isinstance(tree, MCTSTreePayload)
    assert tree.turn_number == 3
    assert tree.phase == "Main1"
    assert tree.hero_life == 20
    assert tree.opp_life == 18
    assert len(tree.branches) > 0

    # Top line should be one of our valid actions
    assert any(b.action_type in ("cast", "attack", "pass", "sequence") for b in tree.branches)
    best = tree.branches[0]
    assert best.win_probability >= 0.0
    assert best.simulated_visits > 0
    assert best.tag in ("⭐ BEST LINE", "⚡ TEMPO", "🛡️ SAFE", "⚠️ BLUNDER TRAP", "NORMAL")


def test_mcts_evaluator_combat_attack():
    state: dict[str, Any] = {
        "local_seat_id": 2,
        "turn": {"turn_number": 4, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 4, "is_local": False},
            {"seat_id": 2, "life_total": 20, "is_local": True},
        ],
        "battlefield": [
            {
                "instance_id": 201,
                "name": "Carnage Tyrant",
                "controller_seat_id": 2,
                "power": 7,
                "toughness": 6,
                "is_tapped": False,
                "type_line": "Creature — Dinosaur",
                "oracle_text": "Trample",
            }
        ],
        "hand": [],
    }

    tree = MCTSEvaluator.evaluate(state)
    assert tree.hero_life == 20
    assert tree.opp_life == 4

    # Attack should be lethal and marked Best Line
    attack_branch = next((b for b in tree.branches if b.action_type == "attack"), None)
    assert attack_branch is not None
    assert "Carnage Tyrant" in attack_branch.action
    assert attack_branch.tag == "⭐ BEST LINE"
    assert attack_branch.details.get("damage_through") == 7


def test_mcts_evaluator_multi_step_sequence():
    state: dict[str, Any] = {
        "local_seat_id": 1,
        "turn": {"turn_number": 2, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True, "mana_pool": {}},
            {"seat_id": 2, "life_total": 20, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Island",
                "controller_seat_id": 1,
                "type_line": "Basic Land — Island",
                "is_tapped": False,
            }
        ],
        "hand": [
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
            },
            {
                "name": "Malcolm, Alluring Scoundrel",
                "mana_cost": "{1}{U}",
                "type_line": "Legendary Creature — Siren Pirate",
                "power": 2,
                "toughness": 1,
                "oracle_text": "Flying, haste",
            },
        ],
    }

    tree = MCTSEvaluator.evaluate(state)
    seq_branch = next((b for b in tree.branches if b.action_type == "sequence"), None)
    assert seq_branch is not None
    assert "Play Plains" in seq_branch.action
    assert "Malcolm" in seq_branch.action
    assert len(seq_branch.sequence_steps) >= 2
    assert "Play Land: Plains" in seq_branch.sequence_steps[0]
    assert seq_branch.projected_state.get("hero_power", 0) >= 2


def test_mcts_evaluator_blunder_trap_detection():
    # Hero is at 5 life, attacking with their only blocker leaves 10 crackback damage from opponent
    state: dict[str, Any] = {
        "local_seat_id": 1,
        "turn": {"turn_number": 5, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 5, "is_local": True},
            {"seat_id": 2, "life_total": 18, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 1,
                "name": "Grizzly Bears",
                "controller_seat_id": 1,
                "power": 2,
                "toughness": 2,
                "is_tapped": False,
                "type_line": "Creature — Bear",
            },
            {
                "instance_id": 2,
                "name": "Colossal Dreadmaw",
                "controller_seat_id": 2,
                "power": 6,
                "toughness": 6,
                "is_tapped": False,
                "type_line": "Creature — Dinosaur",
            },
            {
                "instance_id": 3,
                "name": "Questing Beast",
                "controller_seat_id": 2,
                "power": 4,
                "toughness": 4,
                "is_tapped": False,
                "type_line": "Legendary Creature — Beast",
            },
        ],
        "hand": [],
    }

    tree = MCTSEvaluator.evaluate(state)
    assert len(tree.blunder_traps) >= 1
    trap = tree.blunder_traps[0]
    assert "BLUNDER TRAP" in trap.tag
    assert "crackback" in trap.outcome_summary.lower()


def test_mcts_decision_packet_format_for_llm_prompt():
    state: dict[str, Any] = {
        "local_seat_id": 2,
        "turn": {"turn_number": 3, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 14, "is_local": False},
            {"seat_id": 2, "life_total": 20, "is_local": True},
        ],
        "battlefield": [
            {
                "instance_id": 101,
                "name": "Island",
                "controller_seat_id": 2,
                "type_line": "Basic Land — Island",
                "is_tapped": False,
            },
            {
                "instance_id": 102,
                "name": "Plains",
                "controller_seat_id": 2,
                "type_line": "Basic Land — Plains",
                "is_tapped": False,
            },
        ],
        "hand": [
            {
                "name": "Malcolm, Alluring Scoundrel",
                "mana_cost": "{1}{U}",
                "type_line": "Legendary Creature — Siren Pirate",
                "power": 2,
                "toughness": 1,
            }
        ],
    }

    tree = MCTSEvaluator.evaluate(state)
    prompt_text = tree.format_for_llm_prompt()
    assert "=== MCTS MULTI-PLY TACTICAL SEARCH ===" in prompt_text
    assert "Root Win Expectancy:" in prompt_text
    assert "BEST LINE" in prompt_text
    assert "======================================" in prompt_text


def test_opponent_model_classification():
    # Opponent played Mountain, Monastery Swiftspear, Monstrous Rage
    state: dict[str, Any] = {
        "local_seat_id": 1,
        "turn": {"turn_number": 2, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True},
            {"seat_id": 2, "life_total": 20, "is_local": False},
        ],
        "battlefield": [
            {
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "controller_seat_id": 2,
                "is_tapped": False,
            },
            {
                "name": "Monastery Swiftspear",
                "type_line": "Creature — Human Monk",
                "controller_seat_id": 2,
                "power": 1,
                "toughness": 2,
                "is_tapped": False,
            },
        ],
        "graveyard": [
            {
                "name": "Monstrous Rage",
                "type_line": "Instant",
                "owner_seat_id": 2,
            }
        ],
    }

    profile: OpponentProfile = OpponentModel.classify(state)
    assert profile.archetype == "Standard Mono-Red Aggro"
    assert profile.confidence >= 0.80
    assert "R" in profile.colors
    assert any("Monstrous Rage" in t for t in profile.open_mana_threats)
    summary = profile.format_summary()
    assert "Standard Mono-Red Aggro" in summary


def test_opponent_model_uw_control_sweeper():
    state: dict[str, Any] = {
        "local_seat_id": 1,
        "turn": {"turn_number": 5, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True},
            {"seat_id": 2, "life_total": 20, "is_local": False},
        ],
        "battlefield": [
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "controller_seat_id": 2,
                "is_tapped": False,
            },
            {
                "name": "Island",
                "type_line": "Basic Land — Island",
                "controller_seat_id": 2,
                "is_tapped": False,
            },
            {
                "name": "Hallowed Fountain",
                "type_line": "Land — Plains Island",
                "controller_seat_id": 2,
                "is_tapped": False,
            },
            {
                "name": "Restless Anchorage",
                "type_line": "Land",
                "controller_seat_id": 2,
                "is_tapped": False,
            },
        ],
        "graveyard": [
            {
                "name": "No More Lies",
                "type_line": "Instant",
                "owner_seat_id": 2,
            }
        ],
    }

    profile = OpponentModel.classify(state)
    assert profile.archetype == "Standard Azorius / UW Control"
    assert profile.sweeper_risk > 0.0
    assert "Sunfall" in profile.sweeper_warning or "sweeper" in profile.sweeper_warning.lower()


def test_magezero_client_graceful():
    # Should safely return a boolean or handle offline state without raising
    is_avail = MageZeroClient.is_available()
    assert isinstance(is_avail, bool)


def test_mcts_evaluator_empty_state_graceful():
    tree = MCTSEvaluator.evaluate({})
    assert isinstance(tree, MCTSTreePayload)
    assert tree.total_simulations == 1000
    assert len(tree.branches) >= 1
    assert tree.branches[0].action_type == "pass"
