from __future__ import annotations

from types import SimpleNamespace

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.input_controller import ClickResult


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
