"""Regression tests for the multi-step turn plan in ActionPlanner.

The turn plan is built once on the first non-trivial own-turn LLM call
via an additional `plan_turn` LLM call. Subsequent same-turn prompts see
it injected as a structured `TURN PLAN` block. It advances as actions
execute and is cleared on turn change or divergence.
"""

import json
from typing import Any

from arenamcp.action_planner import (
    ActionPlanner,
    ActionType,
    GameAction,
    TurnPlan,
)


class _ScriptedBackend:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *args: Any, **kwargs: Any) -> str:
        self.calls.append((system, user))
        return self.responses.pop(0) if self.responses else "{}"


def _state(
    *,
    active: int = 1,
    local_seat: int = 1,
    lands_played: int = 1,
    turn_number: int = 4,
) -> dict[str, Any]:
    return {
        "turn": {
            "active_player": active,
            "turn_number": turn_number,
            "phase": "Phase_Main1",
        },
        "local_seat_id": local_seat,
        "players": [
            {"seat_id": local_seat, "is_local": True, "lands_played": lands_played},
            {"seat_id": 2 if local_seat == 1 else 1, "is_local": False, "lands_played": 0},
        ],
        "_bridge_request_type": "ActionsAvailable",
        "_bridge_request_class": "ActionsAvailableRequest",
    }


def _turn_plan_response() -> str:
    return json.dumps(
        {
            "turn_plan": {
                "steps": [
                    {
                        "action_type": "play_land",
                        "card_name": "Forest",
                        "rationale": "fix mana",
                    },
                    {
                        "action_type": "cast_spell",
                        "card_name": "Optimistic Scavenger",
                        "rationale": "early pressure",
                    },
                    {
                        "action_type": "activate_ability",
                        "card_name": "Ezrim",
                        "rationale": "value",
                    },
                ]
            }
        }
    )


def _cast_response(card: str, strategy: str = "stub") -> str:
    return (
        '{"actions":[{"action_type":"cast_spell","card_name":"' + card + '"}],'
        '"overall_strategy":"' + strategy + '"}'
    )


# ── plan_turn parsing ───────────────────────────────────────────────


def test_plan_turn_parses_multistep_response():
    backend = _ScriptedBackend([_turn_plan_response()])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)

    plan = p.plan_turn(_state(), ["Cast Optimistic Scavenger [OK]", "Pass"], None)

    assert isinstance(plan, TurnPlan)
    assert plan.turn_number == 4
    assert len(plan.steps) == 3
    assert plan.steps[0].action_type == "play_land"
    assert plan.steps[0].card_name == "Forest"
    assert plan.steps[1].action_type == "cast_spell"
    assert plan.steps[1].card_name == "Optimistic Scavenger"
    # First step is auto-marked "current" by TurnPlan.__post_init__.
    assert plan.steps[0].status == "current"
    assert plan.steps[1].status == "pending"
    # Plan is cached on the planner.
    assert p._active_turn_plan is plan


def test_plan_turn_handles_steps_at_top_level():
    """{"steps": [...]} shape (without the outer turn_plan wrapper) is also accepted."""
    backend = _ScriptedBackend(
        [
            json.dumps(
                {
                    "steps": [
                        {"action_type": "play_land", "card_name": "Forest"},
                        {"action_type": "cast_spell", "card_name": "Bolt"},
                    ]
                }
            )
        ]
    )
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    plan = p.plan_turn(_state(), [], None)
    assert plan is not None
    assert [s.action_type for s in plan.steps] == ["play_land", "cast_spell"]


def test_plan_turn_filters_non_user_visible_steps():
    backend = _ScriptedBackend(
        [
            json.dumps(
                {
                    "turn_plan": {
                        "steps": [
                            {"action_type": "pay_costs", "card_name": ""},
                            {"action_type": "search_library", "card_name": ""},
                            {"action_type": "play_land", "card_name": "Forest"},
                        ]
                    }
                }
            )
        ]
    )
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    plan = p.plan_turn(_state(), [], None)
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].action_type == "play_land"


def test_plan_turn_defensive_parsing_invalid_json_returns_none():
    backend = _ScriptedBackend(["not json at all"])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    plan = p.plan_turn(_state(), [], None)
    assert plan is None
    assert p._active_turn_plan is None


def test_plan_turn_defensive_parsing_empty_steps_returns_none():
    backend = _ScriptedBackend([json.dumps({"turn_plan": {"steps": []}})])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    plan = p.plan_turn(_state(), [], None)
    assert plan is None


def test_plan_turn_defensive_parsing_garbage_does_not_clobber_existing():
    """A failed parse must NOT erase an already-cached turn plan."""
    from arenamcp.action_planner import TurnPlanStep

    p = ActionPlanner(_ScriptedBackend([]), timeout=1.0, land_drop_first=False)
    p._active_turn_plan = TurnPlan(
        turn_number=4,
        steps=[TurnPlanStep(action_type="play_land", card_name="Forest")],
    )
    p._backend = _ScriptedBackend(["completely invalid"])
    plan = p.plan_turn(_state(), [], None)
    # plan_turn returned None, but the cache was preserved.
    assert plan is None
    assert p._active_turn_plan is not None
    assert p._active_turn_plan.steps[0].card_name == "Forest"


# ── advance_turn_plan ────────────────────────────────────────────────


def test_advance_turn_plan_matches_executed_action():
    backend = _ScriptedBackend([_turn_plan_response()])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    p.plan_turn(_state(), [], None)

    executed = GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")
    advanced = p.advance_turn_plan(executed)

    assert advanced is True
    assert p._active_turn_plan.current_idx == 1
    assert p._active_turn_plan.steps[0].status == "done"
    assert p._active_turn_plan.steps[1].status == "current"


def test_advance_turn_plan_returns_false_on_mismatch():
    backend = _ScriptedBackend([_turn_plan_response()])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    p.plan_turn(_state(), [], None)

    # Player cast something different than the next planned step (Forest land).
    executed = GameAction(action_type=ActionType.CAST_SPELL, card_name="Random Bolt")
    advanced = p.advance_turn_plan(executed)

    assert advanced is False
    # Plan unchanged on miss.
    assert p._active_turn_plan.current_idx == 0
    assert p._active_turn_plan.steps[0].status == "current"


def test_advance_turn_plan_ignores_non_user_visible_actions():
    """Pay-costs, sub-decisions, etc. don't advance the plan, but also don't fail."""
    backend = _ScriptedBackend([_turn_plan_response()])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    p.plan_turn(_state(), [], None)

    executed = GameAction(action_type=ActionType.PAY_COSTS)
    advanced = p.advance_turn_plan(executed)

    assert advanced is False
    assert p._active_turn_plan.current_idx == 0


def test_advance_turn_plan_with_no_active_plan_returns_false():
    p = ActionPlanner(_ScriptedBackend([]), timeout=1.0, land_drop_first=False)
    executed = GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")
    assert p.advance_turn_plan(executed) is False


def test_advance_turn_plan_card_name_decoration_is_stripped():
    """Trailing tags like `[OK]` shouldn't block the match."""
    backend = _ScriptedBackend([_turn_plan_response()])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    p.plan_turn(_state(), [], None)

    # First step: Forest. Advance past it.
    p.advance_turn_plan(GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest"))

    executed = GameAction(
        action_type=ActionType.CAST_SPELL,
        card_name="Optimistic Scavenger [OK]",
    )
    assert p.advance_turn_plan(executed) is True


# ── invalidate_turn_plan ─────────────────────────────────────────────


def test_invalidate_turn_plan_clears_and_records_reason():
    backend = _ScriptedBackend([_turn_plan_response()])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    p.plan_turn(_state(), [], None)
    assert p._active_turn_plan is not None

    p.invalidate_turn_plan("opponent counter")

    assert p._active_turn_plan is None


def test_invalidate_turn_plan_with_no_active_plan_is_noop():
    p = ActionPlanner(_ScriptedBackend([]), timeout=1.0, land_drop_first=False)
    p.invalidate_turn_plan("nothing to invalidate")
    assert p._active_turn_plan is None


# ── prompt injection ─────────────────────────────────────────────────


def test_turn_plan_block_injected_into_subsequent_prompts():
    """A cached turn plan must appear in subsequent same-turn prompts.

    R2 (2026-07-06): the turn plan arrives as a "turn_plan" key on the FIRST
    own-turn action response (one merged LLM call, not a separate plan_turn
    call); the block is injected into the next window's prompt.
    """
    merged = json.loads(_turn_plan_response())
    merged["actions"] = [
        {"action_type": "play_land", "card_name": "Forest"}
    ]
    merged["overall_strategy"] = "do the thing"
    backend = _ScriptedBackend(
        [
            json.dumps(merged),
            _cast_response("Optimistic Scavenger", "continue the plan"),
        ]
    )
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)

    legal = ["Cast Optimistic Scavenger [OK]", "Play Land: Forest", "Pass"]

    # First window: ONE merged call requests actions + turn_plan together.
    p.plan_actions(_state(), "decision_required", legal, {"type": "actions_available"})
    assert len(backend.calls) == 1
    assert "turn_plan" in backend.calls[0][1]  # the ride-along request
    assert p._active_turn_plan is not None

    # Second window of the same turn sees the locked plan in its prompt.
    p.plan_actions(_state(), "decision_required", legal, {"type": "actions_available"})
    assert len(backend.calls) == 2
    second_user_msg = backend.calls[1][1]
    assert "TURN PLAN (turn 4)" in second_user_msg
    # The currently expected next step should be marked.
    assert "currently expected next" in second_user_msg


# ── turn-change clears the plan ──────────────────────────────────────


def test_active_turn_plan_clears_on_turn_change():
    backend = _ScriptedBackend([_turn_plan_response()])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    p.plan_turn(_state(turn_number=4), [], None)
    assert p._active_turn_plan is not None

    # Bump the turn — the next plan_actions call should clear the cache.
    p.plan_actions(
        _state(turn_number=5),
        "decision_required",
        ["Pass"],
        {"type": "actions_available"},
    )
    # Note: plan_actions on turn 5 will also run plan_turn (no active plan
    # yet on turn 5, our turn, ActionsAvailable). Backend has no responses
    # left so plan_turn returns None and the cache stays None.
    assert p._active_turn_plan is None


# ── get_turn_plan_payload ────────────────────────────────────────────


def test_get_turn_plan_payload_serializes_active_plan():
    backend = _ScriptedBackend([_turn_plan_response()])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    p.plan_turn(_state(), [], None)

    payload = p.get_turn_plan_payload()

    assert payload is not None
    assert payload["turn_number"] == 4
    assert payload["current_idx"] == 0
    assert payload["replanned_reason"] == ""
    assert isinstance(payload["steps"], list)
    assert len(payload["steps"]) == 3
    first = payload["steps"][0]
    assert first["action_type"] == "play_land"
    assert first["card_name"] == "Forest"
    assert first["status"] == "current"
    assert first["target_names"] == []
    assert first["rationale"] == "fix mana"


def test_get_turn_plan_payload_returns_none_when_no_plan():
    p = ActionPlanner(_ScriptedBackend([]), timeout=1.0, land_drop_first=False)
    assert p.get_turn_plan_payload() is None
