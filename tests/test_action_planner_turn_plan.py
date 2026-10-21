"""Regression tests for the multi-step turn plan in ActionPlanner.

The turn plan rides along on the first own-turn action call: the prompt asks
for a top-level "turn_plan" key in the same JSON response (R2, 2026-07-06), and
``_parse_turn_plan_response`` extracts it. Subsequent same-turn prompts see it
injected as a structured `TURN PLAN` block. It advances as actions execute and
is cleared on turn change or divergence.
"""

import json
from typing import Any

from arenamcp.action_planner import (
    ActionPlanner,
    ActionType,
    GameAction,
    TurnPlan,
)

# The prompt text that asks the merged call for a turn plan.
_TURN_PLAN_REQUEST = "ADDITIONALLY: this is the first decision of your turn"
_LEGAL = ["Cast Optimistic Scavenger [OK]", "Play Land: Forest", "Pass"]
_OWN_WINDOW = {"type": "actions_available"}


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


# ── turn-plan parsing ───────────────────────────────────────────────


def _parse(response: str, turn: int = 4) -> TurnPlan | None:
    p = ActionPlanner(_ScriptedBackend([]), timeout=1.0, land_drop_first=False)
    return p._parse_turn_plan_response(response, turn)


def _planner_with_plan() -> ActionPlanner:
    """A planner holding the three-step plan for turn 4."""
    p = ActionPlanner(_ScriptedBackend([]), timeout=1.0, land_drop_first=False)
    p._active_turn_plan = p._parse_turn_plan_response(_turn_plan_response(), 4)
    return p


def test_turn_plan_parses_multistep_response():
    plan = _parse(_turn_plan_response())

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


def test_turn_plan_handles_steps_at_top_level():
    """{"steps": [...]} shape (without the outer turn_plan wrapper) is also accepted."""
    plan = _parse(
        json.dumps(
            {
                "steps": [
                    {"action_type": "play_land", "card_name": "Forest"},
                    {"action_type": "cast_spell", "card_name": "Bolt"},
                ]
            }
        )
    )
    assert plan is not None
    assert [s.action_type for s in plan.steps] == ["play_land", "cast_spell"]


def test_turn_plan_filters_non_user_visible_steps():
    plan = _parse(
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
    )
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].action_type == "play_land"


def test_turn_plan_tolerates_fences_and_trailing_commas():
    fenced = '```json\n{"turn_plan": {"steps": [{"action_type": "play_land", "card_name": "Forest"},]}}\n```'
    plan = _parse(fenced)
    assert plan is not None
    assert [(s.action_type, s.card_name) for s in plan.steps] == [("play_land", "Forest")]


def test_turn_plan_defensive_parsing_invalid_json_returns_none():
    assert _parse("not json at all") is None
    assert _parse("") is None
    assert _parse("[1, 2]") is None


def test_turn_plan_defensive_parsing_empty_steps_returns_none():
    assert _parse(json.dumps({"turn_plan": {"steps": []}})) is None


def test_actions_without_a_turn_plan_become_the_steps():
    """P0-8 belt-and-braces: with no turn_plan block, the actions describe the turn.

    On the merged call this means a response that omits "turn_plan" still locks
    a plan made of its own user-visible actions (reasoning becomes the rationale).
    """
    plan = _parse(
        json.dumps(
            {
                "actions": [
                    {"action_type": "cast_spell", "card_name": "Bolt", "reasoning": "clear the blocker"},
                    {"action_type": "pass_priority"},
                ]
            }
        )
    )
    assert plan is not None
    assert [(s.action_type, s.card_name, s.rationale) for s in plan.steps] == [
        ("cast_spell", "Bolt", "clear the blocker")
    ]


# ── turn-plan extraction from the merged action call ────────────────


def test_merged_call_locks_the_turn_plan_and_still_returns_the_action():
    merged = json.loads(_turn_plan_response())
    merged["actions"] = [{"action_type": "play_land", "card_name": "Forest"}]
    backend = _ScriptedBackend([json.dumps(merged)])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)

    plan = p.plan_actions(_state(), "decision_required", _LEGAL, _OWN_WINDOW)

    assert len(backend.calls) == 1
    assert _TURN_PLAN_REQUEST in backend.calls[0][1]
    assert [a.card_name for a in plan.actions] == ["Forest"]
    assert [s.card_name for s in p._active_turn_plan.steps] == [
        "Forest",
        "Optimistic Scavenger",
        "Ezrim",
    ]


def test_a_failed_turn_plan_is_not_requested_again_until_the_next_turn():
    """The attempt guard: one turn-plan request per turn, even when it fails."""
    no_steps = '{"actions": [{"action_type": "pass_priority"}]}'  # nothing user-visible
    backend = _ScriptedBackend([no_steps, no_steps, no_steps])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)

    p.plan_actions(_state(), "decision_required", ["Cast Bolt [OK]", "Pass"], _OWN_WINDOW)
    assert _TURN_PLAN_REQUEST in backend.calls[0][1]
    assert p._active_turn_plan is None

    p.plan_actions(_state(), "decision_required", ["Cast Bolt [OK]", "Pass"], _OWN_WINDOW)
    assert _TURN_PLAN_REQUEST not in backend.calls[1][1]

    p.plan_actions(_state(turn_number=6), "decision_required", ["Cast Bolt [OK]", "Pass"], _OWN_WINDOW)
    assert _TURN_PLAN_REQUEST in backend.calls[2][1]


def test_no_turn_plan_is_requested_on_the_opponents_turn_or_over_a_locked_plan():
    backend = _ScriptedBackend([_cast_response("Bolt"), _cast_response("Bolt")])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    p.plan_actions(_state(active=2), "decision_required", ["Cast Bolt [OK]", "Pass"], _OWN_WINDOW)
    assert _TURN_PLAN_REQUEST not in backend.calls[0][1]

    p._active_turn_plan = p._parse_turn_plan_response(_turn_plan_response(), 4)
    p.plan_actions(_state(), "decision_required", _LEGAL, _OWN_WINDOW)
    assert _TURN_PLAN_REQUEST not in backend.calls[1][1]
    assert p._active_turn_plan.steps[0].card_name == "Forest"


# ── advance_turn_plan ────────────────────────────────────────────────


def test_advance_turn_plan_matches_executed_action():
    p = _planner_with_plan()

    executed = GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")
    advanced = p.advance_turn_plan(executed)

    assert advanced == "advanced"
    assert p._active_turn_plan.current_idx == 1
    assert p._active_turn_plan.steps[0].status == "done"
    assert p._active_turn_plan.steps[1].status == "current"


def test_advance_turn_plan_reports_divergence_on_mismatch():
    p = _planner_with_plan()

    # Player cast something different than any remaining planned step.
    executed = GameAction(action_type=ActionType.CAST_SPELL, card_name="Random Bolt")
    advanced = p.advance_turn_plan(executed)

    assert advanced == "diverged"
    # Plan unchanged on miss.
    assert p._active_turn_plan.current_idx == 0
    assert p._active_turn_plan.steps[0].status == "current"


def test_advance_turn_plan_ignores_non_user_visible_actions():
    """Pay-costs, sub-decisions, etc. don't advance the plan, but also don't fail."""
    p = _planner_with_plan()

    executed = GameAction(action_type=ActionType.PAY_COSTS)
    advanced = p.advance_turn_plan(executed)

    # P2-8: tri-state — benign non-user-visible actions are "neutral",
    # never divergence.
    assert advanced == "neutral"
    assert p._active_turn_plan.current_idx == 0


def test_advance_turn_plan_with_no_active_plan_is_neutral():
    p = ActionPlanner(_ScriptedBackend([]), timeout=1.0, land_drop_first=False)
    executed = GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")
    assert p.advance_turn_plan(executed) == "neutral"


def test_advance_turn_plan_card_name_decoration_is_stripped():
    """Trailing tags like `[OK]` shouldn't block the match."""
    p = _planner_with_plan()

    # First step: Forest. Advance past it.
    p.advance_turn_plan(GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest"))

    executed = GameAction(
        action_type=ActionType.CAST_SPELL,
        card_name="Optimistic Scavenger [OK]",
    )
    assert p.advance_turn_plan(executed) == "advanced"


# ── invalidate_turn_plan ─────────────────────────────────────────────


def test_invalidate_turn_plan_clears_and_records_reason():
    p = _planner_with_plan()
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
    own-turn action response (one merged LLM call); the block is injected
    into the next window's prompt.
    """
    merged = json.loads(_turn_plan_response())
    merged["actions"] = [{"action_type": "play_land", "card_name": "Forest"}]
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
    p = _planner_with_plan()
    assert p._active_turn_plan is not None

    # Bump the turn — the next plan_actions call should clear the cache.
    p.plan_actions(
        _state(turn_number=5),
        "decision_required",
        ["Pass"],
        {"type": "actions_available"},
    )
    # Turn 5's merged call also asks for a fresh turn plan, but the scripted
    # backend answers "{}", so nothing is parsed and the cache stays None.
    assert p._active_turn_plan is None


# ── get_turn_plan_payload ────────────────────────────────────────────


def test_get_turn_plan_payload_serializes_active_plan():
    p = _planner_with_plan()

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


def test_advance_turn_plan_lookahead_skips_stepped_over(monkeypatch):
    # P2-8: a later step executing early (preflight already made the land
    # drop) marks intermediate steps "skipped" instead of diverging.
    p = _planner_with_plan()
    # Plan: [play_land Forest, cast Optimistic Scavenger, ...]. Execute the
    # CAST first — Forest step should be skipped, not divergence.
    outcome = p.advance_turn_plan(
        GameAction(action_type=ActionType.CAST_SPELL, card_name="Optimistic Scavenger")
    )
    assert outcome == "advanced"
    assert p._active_turn_plan.steps[0].status == "skipped"
    assert p._active_turn_plan.steps[1].status == "done"
