from __future__ import annotations

from types import SimpleNamespace

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_models import ClickResult


class _SubmitTargetBridge:
    def __init__(self, pending: dict, submit_ok: bool = True) -> None:
        self.connected = True
        self._pending = pending
        self.submit_ok = submit_ok
        self.submitted_target_ids: list[int] = []

    def connect(self) -> bool:
        return True

    def get_pending_actions(self) -> dict:
        return self._pending

    def submit_targets(self, target_instance_id) -> bool:
        # Mirror the real bridge contract: accept a single id or a per-slot list.
        if isinstance(target_instance_id, (list, tuple, set)):
            self.submitted_target_ids.extend(int(x) for x in target_instance_id)
        else:
            self.submitted_target_ids.append(int(target_instance_id))
        return self.submit_ok

    def submit_selection(self, ids: list[int]) -> bool:
        return False


def _planner() -> ActionPlanner:
    return ActionPlanner.__new__(ActionPlanner)


def test_removal_filter_flags_friendly_only_enchantment_target() -> None:
    card = {
        "name": "Seam Rip",
        "oracle_text": "Destroy target enchantment.",
    }
    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Helpful Aura",
                "type_line": "Enchantment — Aura",
                "owner_seat_id": 1,
                "controller_id": 1,
            }
        ],
    }

    assert _planner()._removal_lacks_opponent_target(card, game_state) is True


def test_removal_filter_allows_spell_when_opponent_has_matching_target() -> None:
    card = {
        "name": "Seam Rip",
        "oracle_text": "Destroy target enchantment.",
    }
    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 20,
                "name": "Opponent Aura",
                "type_line": "Enchantment — Aura",
                "owner_seat_id": 2,
                "controller_id": 2,
            }
        ],
    }

    assert _planner()._removal_lacks_opponent_target(card, game_state) is False


def test_pick_single_target_candidate_declines_friendly_only_harmful_spell() -> None:
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = SimpleNamespace(connected=True, connect=lambda: True, get_pending_actions=lambda: {})

    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Helpful Aura",
                "type_line": "Enchantment — Aura",
                "owner_seat_id": 1,
                "controller_id": 1,
            }
        ],
        "stack": [
            {
                "instance_id": 900,
                "name": "Seam Rip",
                "oracle_text": "Destroy target enchantment.",
            }
        ],
        "_bridge_last_poll": {
            "has_pending": True,
            "target_candidates": [{"targetInstanceId": 10}],
        },
        "decision_context": {"type": "target_selection"},
    }

    assert engine._pick_single_target_candidate(game_state) is None


def test_pick_single_target_candidate_accepts_friendly_buff_spell() -> None:
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = SimpleNamespace(connected=True, connect=lambda: True, get_pending_actions=lambda: {})

    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Veteran Survivor",
                "type_line": "Creature — Human",
                "owner_seat_id": 1,
                "controller_id": 1,
            }
        ],
        "stack": [
            {
                "instance_id": 901,
                "name": "Shardmage's Rescue",
                "oracle_text": "Target creature you control gets +1/+1 until end of turn.",
            }
        ],
        "_bridge_last_poll": {
            "has_pending": True,
            "target_candidates": [{"targetInstanceId": 10}],
        },
        "decision_context": {"type": "target_selection"},
    }

    assert engine._pick_single_target_candidate(game_state) == 10


def test_gre_select_target_uses_sole_bridge_candidate_when_name_lookup_fails() -> None:
    pending = {
        "has_pending": True,
        "request_class": "SelectTargetsRequest",
        "target_candidates": [{"targetInstanceId": 20}],
    }
    bridge = _SubmitTargetBridge(pending)
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = bridge
    engine._log_execution_path = lambda *args, **kwargs: None
    engine._gre_bridge_failed_methods = set()
    engine._get_game_state = lambda: {
        "battlefield": [],
        "players": [{"seat_id": 1, "is_local": True}],
    }

    action = GameAction(
        action_type=ActionType.SELECT_TARGET,
        target_names=["Escape Tunnel"],
    )

    result = engine._try_gre_bridge_select_target(action)

    assert isinstance(result, ClickResult)
    assert result.success is True
    assert bridge.submitted_target_ids == [20]


def test_pick_single_target_candidate_declines_opponent_buff_spell() -> None:
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = SimpleNamespace(connected=True, connect=lambda: True, get_pending_actions=lambda: {})

    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 20,
                "name": "Spectrum Sentinel",
                "type_line": "Creature — Soldier",
                "owner_seat_id": 2,
                "controller_id": 2,
            }
        ],
        "stack": [
            {
                "instance_id": 902,
                "name": "Mutagen",
                "oracle_text": "Put a +1/+1 counter on target creature.",
            }
        ],
        "_bridge_last_poll": {
            "has_pending": True,
            "target_candidates": [{"targetInstanceId": 20}],
        },
        "decision_context": {"type": "target_selection"},
    }

    assert engine._pick_single_target_candidate(game_state) is None


def test_pick_single_target_candidate_accepts_opponent_harmful_spell() -> None:
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = SimpleNamespace(connected=True, connect=lambda: True, get_pending_actions=lambda: {})

    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 20,
                "name": "Spectrum Sentinel",
                "type_line": "Creature — Soldier",
                "owner_seat_id": 2,
                "controller_id": 2,
            }
        ],
        "stack": [
            {
                "instance_id": 903,
                "name": "Seam Rip",
                "oracle_text": "Destroy target enchantment.",
            }
        ],
        "_bridge_last_poll": {
            "has_pending": True,
            "target_candidates": [{"targetInstanceId": 20}],
        },
        "decision_context": {"type": "target_selection"},
    }

    assert engine._pick_single_target_candidate(game_state) == 20


def test_rules_engine_compound_flying_target_selection_pure_log_state() -> None:
    """Pure log-based state (no BepInEx bridge) for Mutant Chain Reaction.

    Verifies that:
    1. Opponent's non-flying Food token (artifact) is matched and ranked first.
    2. Opponent's Wind Drake (creature with flying) is matched.
    3. Opponent's Grizzly Bears (creature WITHOUT flying) is excluded.
    4. Hero's Mutagen token (artifact) is matched but ranked behind opponent targets.
    5. Hero's Michelangelo (creature WITHOUT flying) is excluded.
    """
    from arenamcp.rules_engine import RulesEngine

    game_state = {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 101,
                "name": "Michelangelo, Weirdness to 11",
                "type_line": "Legendary Creature — Mutant Turtle",
                "oracle_text": "Whenever Michelangelo attacks, create a Mutagen token.",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            },
            {
                "instance_id": 102,
                "name": "Mutagen",
                "type_line": "Artifact — Mutagen",
                "oracle_text": "{1}, {T}, Sacrifice this token: Put a +1/+1 counter on target creature.",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            },
            {
                "instance_id": 201,
                "name": "Food",
                "type_line": "Artifact — Food",
                "oracle_text": "{2}, {T}, Sacrifice this token: You gain 3 life.",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
            {
                "instance_id": 202,
                "name": "Grizzly Bears",
                "type_line": "Creature — Bear",
                "oracle_text": "",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
            {
                "instance_id": 203,
                "name": "Wind Drake",
                "type_line": "Creature — Drake",
                "oracle_text": "Flying",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
        ],
        "stack": [
            {
                "instance_id": 500,
                "name": "Mutant Chain Reaction",
                "oracle_text": "Destroy up to one target artifact, enchantment, or creature with flying. Create a Mutagen token.",
            }
        ],
        "decision_context": {
            "type": "target_selection",
            "source_id": 500,
            "source_card": "Mutant Chain Reaction",
            "source_oracle_text": "Destroy up to one target artifact, enchantment, or creature with flying. Create a Mutagen token.",
        },
    }

    actions = RulesEngine._get_target_selection_actions(game_state)
    assert len(actions) > 0
    # Food (OPP) should be ranked first as opponent target
    assert "Select target: Food (OPP)" in actions
    assert "Select target: Wind Drake (OPP)" in actions
    # Non-flying creatures must not be in legal target actions
    assert not any("Grizzly Bears" in a for a in actions)
    assert not any("Michelangelo" in a for a in actions)
    # Opponent targets must come before friendly targets
    food_idx = actions.index("Select target: Food (OPP)")
    if "Select target: Mutagen (YOURS)" in actions:
        mutagen_idx = actions.index("Select target: Mutagen (YOURS)")
        assert food_idx < mutagen_idx


def test_rules_engine_select_targets_req_with_gre_candidates() -> None:
    """GRE-enriched SelectTargetsReq resolves target candidates from targets slot."""
    from arenamcp.rules_engine import RulesEngine

    game_state = {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 646,
                "name": "Mutagen",
                "type_line": "Artifact — Mutagen",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            },
            {
                "instance_id": 638,
                "name": "Food",
                "type_line": "Artifact — Food",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
        ],
        "decision_context": {
            "type": "target_selection",
            "source_id": 242,
            "source_card": "Mutant Chain Reaction",
            "source_oracle_text": "Destroy up to one target artifact, enchantment, or creature with flying. Create a Mutagen token.",
            "targets": [
                {
                    "targetIdx": 1,
                    "targets": [
                        {"targetInstanceId": 638, "highlight": "HighlightType_Hot"},
                        {"targetInstanceId": 646, "highlight": "HighlightType_Cold"},
                    ],
                }
            ],
        },
    }

    actions = RulesEngine._get_target_selection_actions(game_state)
    assert actions[0] == "Select target: Food (OPP)"
    assert "Select target: Mutagen (YOURS)" in actions
