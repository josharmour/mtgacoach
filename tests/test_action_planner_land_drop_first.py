"""Regression tests for the land-drop-first preflight in ActionPlanner.

The preflight short-circuits the LLM when the active player has 0 lands
played and a Play Land action is legal. Fixes the "drops land after combat"
pattern observed in bug_20260501_085853.
"""

from typing import Any

import pytest

from arenamcp.action_planner import ActionPlanner, ActionType


class _FakeBackend:
    """Backend stub that fails the test if the LLM is consulted."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        raise AssertionError("LLM was called when the land-drop preflight should have short-circuited")


def _planner(land_drop_first: bool = True) -> ActionPlanner:
    return ActionPlanner(_FakeBackend(), timeout=1.0, land_drop_first=land_drop_first)


def _state(
    *,
    active: int = 1,
    local_seat: int = 1,
    lands_played: int = 0,
    bridge_request: str = "ActionsAvailable",
    bridge_class: str = "ActionsAvailableRequest",
    turn_number: int = 4,
) -> dict[str, Any]:
    return {
        "turn": {"active_player": active, "turn_number": turn_number, "phase": "Phase_Main1"},
        "local_seat_id": local_seat,
        "players": [
            {"seat_id": local_seat, "is_local": True, "lands_played": lands_played},
            {"seat_id": 2 if local_seat == 1 else 1, "is_local": False, "lands_played": 0},
        ],
        "_bridge_request_type": bridge_request,
        "_bridge_request_class": bridge_class,
    }


def test_preflight_forces_land_drop_when_zero_lands_played():
    legal = [
        "Cast Spelunking [OK]",
        "Play Land: Evolving Wilds",
        "Activate Ability: Haywire Mite",
        "Pass",
    ]
    plan = _planner().plan_actions(_state(), "decision_required", legal, {"type": "actions_available"})
    assert plan.actions, "preflight should have produced a plan"
    assert plan.actions[0].action_type == ActionType.PLAY_LAND
    assert plan.actions[0].card_name == "Evolving Wilds"
    assert "land-drop-first" in plan.overall_strategy


_ACTIONS = {"type": "actions_available"}


@pytest.mark.parametrize(
    ("state_kwargs", "land_drop_first", "legal", "context"),
    [
        pytest.param({"lands_played": 1}, True, ["Cast Spelunking [OK]", "Play Land: Forest", "Pass"], _ACTIONS,
                     id="land_already_played"),
        # "Play Land" would never be legal on the opponent's turn in a real game.
        pytest.param({"active": 2, "local_seat": 1}, True, ["Play Land: Forest", "Pass"], _ACTIONS,
                     id="opponent_turn"),
        pytest.param({}, False, ["Play Land: Forest", "Pass"], _ACTIONS, id="disabled"),
        # CastingTimeOption / Search / SelectN etc.: not a priority window.
        pytest.param({"bridge_request": "CastingTimeOptions", "bridge_class": "CastingTimeOptionRequest"}, True,
                     ["Play Land: Forest", "Pass"], {"type": "modal_choice"}, id="non_priority_window"),
        pytest.param({}, True, ["Cast Foo [OK]", "Activate Ability: Bar", "Pass"], _ACTIONS,
                     id="no_play_land_legal"),
    ],
)  # fmt: skip
def test_preflight_skipped_and_the_model_decides(state_kwargs, land_drop_first, legal, context):
    calls = []
    backend = _FakeBackend()
    backend.complete = lambda *a, **k: calls.append(a) or '{"actions":[{"action_type":"pass_priority"}]}'
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=land_drop_first)
    plan = p.plan_actions(_state(**state_kwargs), "decision_required", legal, context)
    assert len(calls) == 1
    assert plan.actions[0].action_type != ActionType.PLAY_LAND
