"""Emrakul, the Promised End / Mindslaver: answering the opponent's requests.

2026-09-24 (real match): Emrakul's cast trigger gave us the opponent's
turn 10. Player.log flipped the opponent's PlayerInfo.controllerSeatId to
our seat, and each of their requests reached us with
turnInfo.decisionPlayer = their seat. The autopilot took them for its own:
it played the opponent's Plains and cast their Weapons Vendor.
"""

from unittest.mock import MagicMock

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.gamestate import GameState
from arenamcp.server import controlled_turn_state

LOCAL, OPP = 2, 1


def _players(opp_controller: int = OPP, local_controller: int = LOCAL) -> list[dict]:
    return [
        {"seat_id": OPP, "is_local": False, "controller_seat_id": opp_controller},
        {"seat_id": LOCAL, "is_local": True, "controller_seat_id": local_controller},
    ]


def test_log_tracks_controller_seat_and_decision_player():
    state = GameState()
    state.update_from_message(
        {
            "players": [
                {"systemSeatNumber": OPP, "lifeTotal": 2, "controllerSeatId": LOCAL},
                {"systemSeatNumber": LOCAL, "lifeTotal": 11, "controllerSeatId": LOCAL},
            ],
            "turnInfo": {
                "turnNumber": 10,
                "activePlayer": OPP,
                "priorityPlayer": OPP,
                "decisionPlayer": OPP,
                "phase": "Phase_Main1",
            },
        }
    )
    assert state.players[OPP].controller_seat_id == LOCAL
    assert state.turn_info.decision_player == OPP

    # Diffs that omit the fields keep them; control comes back explicitly.
    state.update_from_message({"players": [{"systemSeatNumber": OPP, "lifeTotal": 2}]})
    assert state.players[OPP].controller_seat_id == LOCAL
    state.update_from_message(
        {
            "players": [{"systemSeatNumber": OPP, "controllerSeatId": OPP}],
            "turnInfo": {"turnNumber": 11, "activePlayer": OPP, "decisionPlayer": LOCAL},
        }
    )
    assert state.players[OPP].controller_seat_id == OPP
    assert state.turn_info.decision_player == LOCAL


def test_controlled_turn_state():
    normal = controlled_turn_state(_players(), {"decision_player": LOCAL}, LOCAL)
    assert normal == {
        "opponent_controlled_by_you": False,
        "you_controlled_by_opponent": False,
        "deciding_for_opponent": False,
    }

    theirs = controlled_turn_state(_players(opp_controller=LOCAL), {"decision_player": OPP}, LOCAL)
    assert theirs["opponent_controlled_by_you"] and theirs["deciding_for_opponent"]

    # Still their controlled turn, but this request (e.g. our blocks) is ours.
    ours = controlled_turn_state(_players(opp_controller=LOCAL), {"decision_player": LOCAL}, LOCAL)
    assert ours["opponent_controlled_by_you"] and not ours["deciding_for_opponent"]

    stolen = controlled_turn_state(_players(local_controller=OPP), {"decision_player": LOCAL}, LOCAL)
    assert stolen["you_controlled_by_opponent"] and not stolen["deciding_for_opponent"]


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _engine(monkeypatch, poll: dict) -> tuple[AutopilotEngine, MagicMock]:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    planner = MagicMock()
    engine = AutopilotEngine(
        planner=planner,
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=False),
    )
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = poll
    bridge.submit_attackers_raw.return_value = {"ok": True}
    engine._gre_bridge = bridge
    return engine, bridge


def _controlled_state() -> dict:
    return {
        "turn": {"turn_number": 10, "active_player": OPP, "priority_player": OPP, "decision_player": OPP},
        "players": _players(opp_controller=LOCAL),
        "local_seat_id": LOCAL,
        "stack": [],
        "controlled_turn": {
            "opponent_controlled_by_you": True,
            "you_controlled_by_opponent": False,
            "deciding_for_opponent": True,
        },
    }


def test_opponents_priority_is_passed_not_played(monkeypatch):
    poll = {
        "has_pending": True,
        "request_class": "ActionsAvailableRequest",
        "can_pass": True,
        "actions": [
            {"actionType": "Play", "grpId": 105174, "instanceId": 493},
            {"actionType": "Cast", "grpId": 95896, "instanceId": 249, "hasAutoTap": True},
            {"actionType": "Pass"},
        ],
    }
    engine, bridge = _engine(monkeypatch, poll)
    assert engine.process_trigger(_controlled_state(), "decision_required") is True
    bridge.submit_pass.assert_called_once()
    bridge.submit_action_by_index.assert_not_called()
    engine._planner.plan_actions.assert_not_called()


def test_opponents_attack_is_declared_empty(monkeypatch):
    engine, bridge = _engine(monkeypatch, {"has_pending": True, "request_class": "DeclareAttackersRequest"})
    assert engine.process_trigger(_controlled_state(), "decision_required") is True
    bridge.submit_attackers_raw.assert_called_once_with([])


def test_forced_choice_for_opponent_goes_to_the_player(monkeypatch):
    poll = {"has_pending": True, "request_class": "SelectTargetsRequest", "can_cancel": False}
    engine, bridge = _engine(monkeypatch, poll)
    assert engine.process_trigger(_controlled_state(), "decision_required") is True
    bridge.submit_targets.assert_not_called()
    assert engine.is_window_given_up(_controlled_state())
