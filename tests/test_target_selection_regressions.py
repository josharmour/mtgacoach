from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.autopilot_models import ClickResult
from arenamcp.autopilot_progress import DecisionProgressGuard


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


def _one_permanent_state(owner: int, name: str, type_line: str) -> dict:
    return {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 10 if owner == 1 else 20,
                "name": name,
                "type_line": type_line,
                "owner_seat_id": owner,
                "controller_id": owner,
            }
        ],
    }


@pytest.mark.parametrize(("owner", "lacks"), [(1, True), (2, False)], ids=["ours_only", "theirs"])
def test_removal_filter_needs_an_opponent_target(owner, lacks) -> None:
    card = {"name": "Seam Rip", "oracle_text": "Destroy target enchantment."}
    game_state = _one_permanent_state(owner, "An Aura", "Enchantment — Aura")
    assert _planner()._removal_lacks_opponent_target(card, game_state) is lacks


@pytest.mark.parametrize(
    ("owner", "type_line", "spell", "oracle", "picked"),
    [
        # Harmful spell, our permanent the only candidate: decline it.
        (1, "Enchantment — Aura", "Seam Rip", "Destroy target enchantment.", None),
        (1, "Creature — Human", "Shardmage's Rescue", "Target creature you control gets +1/+1 until end of turn.", 10),
        # A buff onto the opponent's only candidate is declined too.
        (2, "Creature — Soldier", "Mutagen", "Put a +1/+1 counter on target creature.", None),
        (2, "Creature — Soldier", "Seam Rip", "Destroy target enchantment.", 20),
    ],
    ids=["harm_ours", "buff_ours", "buff_theirs", "harm_theirs"],
)
def test_pick_single_target_candidate_respects_the_effect(owner, type_line, spell, oracle, picked) -> None:
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = SimpleNamespace(connected=True, connect=lambda: True, get_pending_actions=lambda: {})
    game_state = _one_permanent_state(owner, "A Permanent", type_line)
    candidate = game_state["battlefield"][0]["instance_id"]
    game_state.update(
        {
            "stack": [{"instance_id": 900, "name": spell, "oracle_text": oracle}],
            "_bridge_last_poll": {"has_pending": True, "target_candidates": [{"targetInstanceId": candidate}]},
            "decision_context": {"type": "target_selection"},
        }
    )
    assert engine._pick_single_target_candidate(game_state) == picked


def test_gre_select_target_uses_sole_bridge_candidate_when_name_lookup_fails() -> None:
    pending = {
        "has_pending": True,
        "request_class": "SelectTargetsRequest",
        "target_candidates": [{"targetInstanceId": 20}],
    }
    bridge = _SubmitTargetBridge(pending)
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._abort_event = threading.Event()
    engine._config = AutopilotConfig(dry_run=False)
    engine._progress_guard = DecisionProgressGuard()
    engine._progress_game_state = {}
    engine._progress_last_poll = None
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
