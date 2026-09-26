"""Per-turn cap on activating the same source's abilities.

2026-09-24 (real match): with Lightning Greaves (Equip {0}) out, every
priority window offered the equip again and the planner kept taking it —
the Greaves moved 707 -> 692 -> 715 -> 692 in 40 seconds until the user
took over. Equipment gets one activation per turn, anything else three.
"""

from unittest.mock import MagicMock

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

LOCAL = 2


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _engine(monkeypatch) -> tuple[AutopilotEngine, MagicMock]:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    planner = MagicMock()
    # Always take the first non-pass option: the behaviour that looped.
    planner.plan_decision_options.side_effect = lambda decision, gs: [
        o.option_id for o in decision.options if o.option_id != "pass"
    ][:1] or ["pass"]
    engine = AutopilotEngine(planner=planner, get_game_state=lambda: {}, config=AutopilotConfig(dry_run=False))
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": 1,
        "can_pass": True,
        "actions": [
            {"actionType": "ActionType_Activate", "grpId": 19737, "instanceId": 728, "abilityGrpId": 2152},
            {"actionType": "ActionType_Pass"},
        ],
    }
    engine._gre_bridge = bridge
    engine._request_tracker = MagicMock()
    engine._request_tracker.may_submit.return_value = True
    engine._request_tracker.exhausted.return_value = False
    return engine, bridge


def _state(turn: int, type_line: str = "Legendary Artifact — Equipment") -> dict:
    return {
        "turn": {"turn_number": turn},
        "players": [{"seat_id": LOCAL, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": [
            {
                "instance_id": 728,
                "name": "Lightning Greaves",
                "type_line": type_line,
                "controller_seat_id": LOCAL,
                "owner_seat_id": LOCAL,
            }
        ],
    }


def _names(engine: AutopilotEngine) -> None:
    engine._planner._resolve_name = None


def test_equipment_is_equipped_once_per_turn(monkeypatch):
    engine, bridge = _engine(monkeypatch)
    assert engine._try_typed_decision_path(_state(18), "decision_required") is True
    assert bridge.submit_action_by_index.call_count == 1
    # The next window of the same turn no longer offers the equip.
    assert engine._try_typed_decision_path(_state(18), "decision_required") is True
    assert bridge.submit_action_by_index.call_count == 1
    bridge.submit_pass.assert_called_once()
    # A new turn resets the cap.
    assert engine._try_typed_decision_path(_state(20), "decision_required") is True
    assert bridge.submit_action_by_index.call_count == 2


def test_other_abilities_get_three_activations(monkeypatch):
    engine, bridge = _engine(monkeypatch)
    for _ in range(5):
        engine._try_typed_decision_path(_state(18, type_line="Creature — Elf"), "decision_required")
    assert bridge.submit_action_by_index.call_count == 3
    assert bridge.submit_pass.call_count == 2


def test_planner_legal_actions_hide_spent_sources(monkeypatch):
    engine, _ = _engine(monkeypatch)
    state = _state(18)
    legal = ["Activate Ability: Lightning Greaves [OK]", "Cast Llanowar Elves [OK]", "Pass"]
    assert engine._drop_exhausted_activations(legal, state) == legal
    engine._note_activation(state, 0, "Lightning Greaves")
    assert engine._drop_exhausted_activations(legal, state) == ["Cast Llanowar Elves [OK]", "Pass"]
