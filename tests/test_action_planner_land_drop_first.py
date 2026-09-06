"""Regression tests for the land-drop-first preflight in ActionPlanner.

The preflight short-circuits the LLM when the active player has 0 lands
played and a Play Land action is legal. Fixes the "drops land after combat"
pattern observed in bug_20260501_085853.
"""

from typing import Any

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


def test_preflight_skipped_when_lands_played_already():
    backend = _FakeBackend()
    backend.complete = lambda *a, **k: '{"actions":[{"action_type":"pass_priority"}]}'
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=True)
    legal = ["Cast Spelunking [OK]", "Play Land: Forest", "Pass"]
    plan = p.plan_actions(_state(lands_played=1), "decision_required", legal, {"type": "actions_available"})
    assert plan.actions[0].action_type != ActionType.PLAY_LAND


def test_preflight_skipped_on_opponent_turn():
    legal = ["Play Land: Forest", "Pass"]  # would never be legal on opponent's turn IRL
    backend = _FakeBackend()
    backend.complete = lambda *a, **k: '{"actions":[{"action_type":"pass_priority"}]}'
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=True)
    plan = p.plan_actions(
        _state(active=2, local_seat=1),
        "decision_required",
        legal,
        {"type": "actions_available"},
    )
    assert plan.actions[0].action_type != ActionType.PLAY_LAND


def test_preflight_skipped_when_disabled():
    backend = _FakeBackend()
    backend.complete = lambda *a, **k: '{"actions":[{"action_type":"pass_priority"}]}'
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    legal = ["Play Land: Forest", "Pass"]
    plan = p.plan_actions(_state(), "decision_required", legal, {"type": "actions_available"})
    assert plan.actions[0].action_type != ActionType.PLAY_LAND


def test_preflight_skipped_for_non_priority_window():
    # CastingTimeOption / Search / SelectN etc — preflight must not fire
    backend = _FakeBackend()
    backend.complete = lambda *a, **k: '{"actions":[{"action_type":"pass_priority"}]}'
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=True)
    legal = ["Play Land: Forest", "Pass"]
    plan = p.plan_actions(
        _state(bridge_request="CastingTimeOptions", bridge_class="CastingTimeOptionRequest"),
        "decision_required",
        legal,
        {"type": "modal_choice"},
    )
    assert plan.actions[0].action_type != ActionType.PLAY_LAND


def test_preflight_skipped_when_no_play_land_in_legal():
    backend = _FakeBackend()
    backend.complete = lambda *a, **k: '{"actions":[{"action_type":"pass_priority"}]}'
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=True)
    legal = ["Cast Foo [OK]", "Activate Ability: Bar", "Pass"]
    plan = p.plan_actions(_state(), "decision_required", legal, {"type": "actions_available"})
    assert plan.actions[0].action_type != ActionType.PLAY_LAND
