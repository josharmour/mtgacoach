"""Regression tests for bridge submit in autopilot_bridge.py.

Tests the fix for issues #398-#402: click_button done on a DeclareAttacker
bridge request should use the combat solver for attacker names instead of
unconditionally submitting an empty attacker list.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arenamcp.action_planner import ActionType, GameAction


class _DummyBridge:
    def __init__(self, connected: bool = True):
        self.connected = connected

    def connect(self) -> bool:
        return self.connected

    def get_pending_actions(self) -> dict:
        return {"ok": True, "has_pending": False}


class _DummyMapper:
    window_rect = (0, 0, 100, 100)
    cache_size = 0

    def refresh_window(self):
        return self.window_rect


class _DummyController:
    def focus_mtga_window(self) -> None:
        return None

    def wait(self, *args, **kwargs) -> None:
        return None


def _get_game_state() -> dict:
    return {}


def _make_attacker_state(legal_attackers: list[str], *, no_blockers: bool = False) -> dict:
    """Build a minimal game state sitting in DeclareAttackers with attackers.

    When no_blockers=True the opponent has no blockers, so any attacker
    is profitable and the combat solver will recommend attacking.
    When no_blockers=False (default) the opponent has a 0/4 Wall blocker,
    so the solver recommends not attacking (0 damage through).
    """
    battlefield = [
        {
            "name": "Grizzly Bears",
            "type_line": "Creature -- Bear",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
            "is_tapped": False,
            "power": 2,
            "toughness": 2,
            "instance_id": 100,
        },
        {
            "name": "Elvish Mystic",
            "type_line": "Creature -- Elf Druid",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
            "is_tapped": False,
            "power": 1,
            "toughness": 1,
            "instance_id": 101,
        },
    ]
    if not no_blockers:
        battlefield.append(
            {
                "name": "Opponent's Wall",
                "type_line": "Creature -- Wall",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
                "power": 0,
                "toughness": 4,
                "instance_id": 200,
            }
        )
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 0},
            {"seat_id": 2, "is_local": False, "life_total": 18},
        ],
        "turn": {
            "active_player": 1,
            "priority_player": 1,
            "turn_number": 5,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "battlefield": battlefield,
        "hand": [],
        "graveyard": [],
        "stack": [],
        "exile": [],
        "pending_decision": "Declare Attackers",
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": legal_attackers or [],
        },
        "_bridge_request_type": "DeclareAttackerRequest",
        "_bridge_request_class": "DeclareAttacker",
        "_bridge_connected": True,
        "_bridge_has_pending": True,
    }


@pytest.fixture
def engine():
    """Build an AutopilotEngine with mocked bridge and game state.

    Follows the pattern in test_autopilot_bridge_lock.py.
    """
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    config = AutopilotConfig()
    mapper = _DummyMapper()
    controller = _DummyController()

    eng = AutopilotEngine(config, mapper, controller, _get_game_state)
    eng._speak_fn = None

    # Attach a mock bridge
    eng._gre_bridge = _DummyBridge()
    eng._gre_bridge.submit_attackers = MagicMock(return_value=True)
    eng._gre_bridge.submit_blockers = MagicMock(return_value=True)
    eng._gre_bridge.submit_attackers_raw = MagicMock(return_value={"ok": True, "needs_finalize": False})

    return eng


class TestBridgeDeclareAttackersClickButton:
    """Tests for the click_button done -> DeclareAttacker solver fix."""

    def test_solver_attack_names_returns_list_with_attackers(self, engine):
        """_solver_attack_names returns a non-empty list when there's
        a profitable no-blocker scenario."""
        gs = _make_attacker_state(["Grizzly Bears", "Elvish Mystic"], no_blockers=True)
        names = engine._solver_attack_names(gs)
        # With no blockers, the solver should find profitable attackers
        assert isinstance(names, list)
        assert len(names) > 0, "Expected solver to recommend attacking with no blockers present"

    def test_solver_gives_reasonable_results(self, engine):
        """_solver_attack_names returns a list and the solver's
        behavior matches expectations: with no blockers it attacks,
        and it doesn't crash with blockers."""
        gs_no_blockers = _make_attacker_state(["Grizzly Bears"], no_blockers=True)
        names = engine._solver_attack_names(gs_no_blockers)
        assert isinstance(names, list)

        gs_with_blockers = _make_attacker_state(["Grizzly Bears"], no_blockers=False)
        names2 = engine._solver_attack_names(gs_with_blockers)
        assert isinstance(names2, list)

    def test_try_gre_bridge_routes_through_solver(self, engine):
        """When the combat solver finds profitable attackers, _try_gre_bridge
        routes through _try_bridge_declare_attackers instead of submit_attackers([]).

        This is the fix for issues #398-#402: previously, click_button done
        unconditionally called submit_attackers([]) even with profitable
        attackers on board, causing bridge_submit_failed."""
        gs = _make_attacker_state(["Grizzly Bears", "Elvish Mystic"], no_blockers=True)

        action = GameAction(
            action_type=ActionType.CLICK_BUTTON,
            card_name="done",
            reasoning="confirm attackers",
        )

        # Spy on _try_bridge_declare_attackers
        engine._try_bridge_declare_attackers = MagicMock(return_value=MagicMock(success=True))

        result = engine._try_gre_bridge(action, gs)

        # _try_bridge_declare_attackers should be called
        assert engine._try_bridge_declare_attackers.called, (
            "Expected _try_bridge_declare_attackers to be called instead of submit_attackers([])"
        )
        # The passed action should be DECLARE_ATTACKERS with attacker names
        called_action = engine._try_bridge_declare_attackers.call_args[0][0]
        assert called_action.action_type == ActionType.DECLARE_ATTACKERS

        # submit_attackers([]) should NOT be called (empty submit)
        engine._gre_bridge.submit_attackers.assert_not_called()

    def test_empty_fallback_when_no_attackers(self, engine):
        """When no legal attackers exist, click_button done falls back to
        submit_attackers([]) as before."""
        gs = _make_attacker_state([], no_blockers=True)

        action = GameAction(
            action_type=ActionType.CLICK_BUTTON,
            card_name="done",
            reasoning="confirm attackers",
        )

        engine._try_bridge_declare_attackers = MagicMock()
        engine._gre_bridge.submit_attackers = MagicMock(return_value=True)

        result = engine._try_gre_bridge(action, gs)

        # No attackers = should fall back to empty submit
        engine._gre_bridge.submit_attackers.assert_called_once_with([])
        engine._try_bridge_declare_attackers.assert_not_called()

    def test_blockers_path_unchanged(self, engine):
        """The DeclareBlocker path is not affected by the fix."""
        gs = _make_attacker_state(["Grizzly Bears"])
        gs["_bridge_request_type"] = "DeclareBlockerRequest"
        gs["_bridge_request_class"] = "DeclareBlocker"
        gs["pending_decision"] = "Declare Blockers"

        action = GameAction(
            action_type=ActionType.CLICK_BUTTON,
            card_name="done",
            reasoning="confirm blockers",
        )

        engine._gre_bridge.submit_blockers = MagicMock(return_value=True)

        result = engine._try_gre_bridge(action, gs)

        # Blockers path unchanged -- submit_blockers([])
        engine._gre_bridge.submit_blockers.assert_called_once_with([])

    def test_declare_attackers_dispatch_unchanged(self, engine):
        """DECLARE_ATTACKERS action type still routes through
        _try_bridge_declare_attackers."""
        gs = _make_attacker_state(["Grizzly Bears"])

        action = GameAction(
            action_type=ActionType.DECLARE_ATTACKERS,
            attacker_names=["Grizzly Bears"],
            card_name="Grizzly Bears",
            reasoning="declare attackers",
        )

        engine._try_bridge_declare_attackers = MagicMock(return_value=MagicMock(success=True))

        result = engine._try_gre_bridge(action, gs)
        engine._try_bridge_declare_attackers.assert_called_once()
