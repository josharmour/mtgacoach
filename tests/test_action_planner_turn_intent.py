"""Regression tests for the turn-intent locking in ActionPlanner.

The first non-trivial strategy produced on the active player's own turn
is captured and held until turn change. Subsequent same-turn prompts
include it as 'TURN PLAN' so the LLM stops flip-flopping. Cleared on turn
change.
"""

from typing import Any

from arenamcp.action_planner import ActionPlanner, ActionType


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


def _cast_response(card: str, strategy: str) -> str:
    return (
        '{"actions":[{"action_type":"cast_spell","card_name":"' + card + '"}],'
        '"overall_strategy":"' + strategy + '"}'
    )


def _pass_response(strategy: str) -> str:
    return (
        '{"actions":[{"action_type":"pass_priority"}],'
        '"overall_strategy":"' + strategy + '"}'
    )


# R2 (2026-07-06): the turn plan rides along on the FIRST own-turn action
# call as a "turn_plan" key in the same JSON response — there is no separate
# plan_turn LLM call anymore. One scripted response per planning window.


def test_turn_intent_locked_on_first_non_trivial_plan():
    backend = _ScriptedBackend([
        _cast_response("Bolt", "Cast Bolt to deal 3 to opp"),
    ])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    legal = ["Cast Bolt [OK]", "Pass"]
    p.plan_actions(_state(), "decision_required", legal, {"type": "actions_available"})
    assert p._turn_intent == "Cast Bolt to deal 3 to opp"


def test_turn_intent_not_locked_on_pass_only_plan():
    backend = _ScriptedBackend([
        _pass_response("Hold up Counterspell"),
    ])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    legal = ["Cast Counterspell [OK]", "Pass"]
    p.plan_actions(_state(), "decision_required", legal, {"type": "actions_available"})
    assert p._turn_intent is None


def test_turn_intent_not_locked_on_opponent_turn():
    # Opponent turn: plan_turn shouldn't even be invoked, so just one response.
    backend = _ScriptedBackend([_cast_response("Bolt", "Bolt their face")])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    legal = ["Cast Bolt [OK]", "Pass"]
    p.plan_actions(
        _state(active=2, local_seat=1),
        "decision_required",
        legal,
        {"type": "actions_available"},
    )
    assert p._turn_intent is None


def test_turn_intent_not_locked_by_preflight_landdrop():
    backend = _ScriptedBackend([])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=True)
    legal = ["Cast Spelunking [OK]", "Play Land: Forest", "Pass"]
    p.plan_actions(
        _state(lands_played=0),
        "decision_required",
        legal,
        {"type": "actions_available"},
    )
    # Preflight ran (no LLM call needed)
    assert backend.calls == []
    # But intent should NOT be the preflight tag
    assert p._turn_intent is None


def test_turn_intent_persists_across_same_turn_calls():
    # Turn 4: one merged call per window; the second window of the same
    # turn does not re-request a turn plan (attempt guard).
    backend = _ScriptedBackend([
        _cast_response("Bolt", "Burn out their threat then deploy creatures"),
        _cast_response("Goblin", "Continue burn-and-creature plan"),
    ])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    legal = ["Cast Bolt [OK]", "Cast Goblin [OK]", "Pass"]
    state = _state()

    # First call locks the intent
    p.plan_actions(state, "decision_required", legal, {"type": "actions_available"})
    locked = p._turn_intent
    assert locked == "Burn out their threat then deploy creatures"

    # Second call same turn — intent must NOT be re-captured, and
    # plan_turn must NOT be re-invoked (attempt guard).
    p.plan_actions(state, "decision_required", legal, {"type": "actions_available"})
    assert p._turn_intent == locked

    # The second per-window action call should contain the locked intent
    # in its prompt. backend.calls indexes:
    #   [0] = first per-window action call (turn plan rides along)
    #   [1] = second per-window action call
    third_user_msg = backend.calls[1][1]
    assert "TURN PLAN" in third_user_msg
    assert locked in third_user_msg


def test_turn_intent_clears_on_turn_change():
    backend = _ScriptedBackend([
        _cast_response("Bolt", "Turn 4 plan"),
        _cast_response("Goblin", "Turn 5 plan"),
    ])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    legal = ["Cast Bolt [OK]", "Cast Goblin [OK]", "Pass"]

    p.plan_actions(_state(turn_number=4), "decision_required", legal, {"type": "actions_available"})
    assert p._turn_intent == "Turn 4 plan"

    p.plan_actions(_state(turn_number=5), "decision_required", legal, {"type": "actions_available"})
    assert p._turn_intent == "Turn 5 plan"


def test_system_prompt_includes_ok_trust_rule():
    from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT
    assert "TRUST [OK]" in AUTOPILOT_SYSTEM_PROMPT
    assert "hybrid" in AUTOPILOT_SYSTEM_PROMPT.lower()


def test_turn_executed_only_records_verified_actions():
    # P1-7: guardrail-rejected proposals must not appear as "already
    # executed"; only note_executed (verified callback) records.
    from arenamcp.action_planner import ActionType as AT, GameAction

    backend = _ScriptedBackend([
        _cast_response("Bolt", "Burn plan"),
        _cast_response("Goblin", "Continue plan"),
    ])
    p = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    legal = ["Cast Bolt [OK]", "Cast Goblin [OK]", "Pass"]
    state = _state()

    p.plan_actions(state, "decision_required", legal, {"type": "actions_available"})
    # Second window WITHOUT a verified execution — nothing assumed executed.
    p.plan_actions(state, "decision_required", legal, {"type": "actions_available"})
    assert p._turn_executed == []

    # Verified execution records.
    p.note_executed(GameAction(action_type=AT.CAST_SPELL, card_name="Bolt"))
    assert p._turn_executed == ["cast_spell(Bolt)"]
    # Idempotent.
    p.note_executed(GameAction(action_type=AT.CAST_SPELL, card_name="Bolt"))
    assert p._turn_executed == ["cast_spell(Bolt)"]
