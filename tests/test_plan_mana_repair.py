"""#235: one bounded repair for a valid JSON plan with an illegal mana schedule."""

import json
import threading

import pytest
from tests.test_plan_availability_feedback import Backend, state

from arenamcp.board_assessment import assess
from arenamcp.game_plan import GamePlan, GamePlanManager, validate_plan


def mana_state():
    game = state()
    game["turn"]["priority_player"] = 1
    game["hand"] = [
        {
            "instance_id": 7,
            "name": "Rofellos, Llanowar Emissary",
            "type_line": "Legendary Creature — Elf Druid",
            "mana_cost": "{G}{G}",
            "power": 2,
            "toughness": 1,
            "controller_seat_id": 1,
            "owner_seat_id": 1,
        }
    ]
    game["battlefield"] = [
        {
            "instance_id": 1,
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "{T}: Add {G}.",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
        },
        {
            "instance_id": 2,
            "name": "Wastes",
            "type_line": "Basic Land",
            "oracle_text": "{T}: Add {C}.",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
        },
    ]
    return game


BAD = json.dumps(
    {
        "win_conditions": ["Creature combat"],
        "path": "Deploy Rofellos now",
        "turns": [{"turn": "T", "cast": ["Rofellos, Llanowar Emissary"]}],
    }
)
GOOD = json.dumps(
    {
        "win_conditions": ["Creature combat"],
        "path": "Wait for a second green source",
        "turns": [{"turn": "T", "cast": [], "hold": "Wait for green mana"}],
    }
)


def test_mana_illegal_plan_repaired_once_with_original_evidence(caplog):
    backend = Backend(BAD, GOOD)
    manager = GamePlanManager(backend)
    with caplog.at_level("INFO", logger="arenamcp.game_plan"):
        plan = manager._reform(mana_state(), 9)
    assert len(backend.requests) == 2
    prompt, kwargs = backend.requests[1]
    assert "not mana-legal" in prompt and "Rofellos" in prompt
    assert kwargs["reasoning_effort"] == "low"
    assert 0 < kwargs["request_timeout_s"] <= manager._timeout
    assert plan.path == "Wait for a second green source"
    assert BAD in caplog.text


def test_second_illegal_plan_does_not_trigger_a_third_call():
    backend = Backend(BAD, BAD)
    plan = GamePlanManager(backend)._reform(mana_state(), 9)
    assert len(backend.requests) == 2
    assert plan.turn_plan[0]["cast"] == []
    assert any("not mana-legal" in issue for issue in plan.issues)
    assert not plan.path


@pytest.mark.parametrize("answer", ["{}", "broken"])
def test_failed_repair_keeps_validated_fallback(answer):
    backend = Backend(BAD, answer)
    plan = GamePlanManager(backend)._reform(mana_state(), 9)
    assert len(backend.requests) == 2
    assert plan.turn_plan[0]["cast"] == []


def test_json_retry_uses_up_the_single_repair_opportunity():
    backend = Backend("{}", BAD)
    plan = GamePlanManager(backend)._reform(mana_state(), 9)
    assert len(backend.requests) == 2
    assert plan.turn_plan[0]["cast"] == []


@pytest.mark.parametrize("blocked", ["decision", "budget", "backend"])
def test_no_extra_request_while_decision_pending_budget_spent_or_backend_blocked(blocked):
    game = mana_state()
    backend = Backend(BAD)
    manager = GamePlanManager(backend, timeout=0 if blocked == "budget" else 75)
    if blocked == "decision":
        game["_bridge_request_type"] = "ActionsAvailable"
    if blocked == "backend":

        class Blocked(Backend):
            def background_blocked(self):
                return True

        backend = Blocked(BAD)
        manager = GamePlanManager(backend)
    plan = manager._reform(game, 9)
    assert len(backend.requests) == 1
    assert plan.turn_plan[0]["cast"] == []
    assert not plan.path


def test_cancellation_during_repair_discards_the_stale_plan():
    cancel = threading.Event()

    class Cancelling(Backend):
        def complete(self, *args, **kwargs):
            answer = super().complete(*args, **kwargs)
            if len(self.requests) == 2:
                cancel.set()
            return answer

    backend = Cancelling(BAD, GOOD)
    plan = GamePlanManager(backend)._reform(mana_state(), 9, cancel=cancel)
    assert len(backend.requests) == 2
    assert plan is None


@pytest.mark.parametrize("existing", [False, True])
def test_dropped_creature_cannot_attack_but_existing_same_name_body_can(existing):
    game = mana_state()
    if existing:
        game["battlefield"].append(dict(game["hand"][0], instance_id=8, turn_entered_battlefield=1))
    plan = GamePlan(
        path="Rofellos attacks",
        turn_plan=[
            {
                "turn": "T",
                "cast": ["Rofellos, Llanowar Emissary"],
                "attack": "Rofellos",
            }
        ],
    )
    validate_plan(plan, assess(game), game)
    assert bool(plan.turn_plan[0]["attack"]) is existing
    assert bool(plan.path) is existing


def test_conditional_mana_wait_survives_fallback_cleanup():
    game = mana_state()
    plan = GamePlan(
        path="Wait for green mana to cast Rofellos",
        turn_plan=[
            {
                "turn": "T",
                "cast": ["Rofellos, Llanowar Emissary"],
                "hold": "Rofellos until another Forest",
            }
        ],
    )
    validate_plan(plan, assess(game), game)
    assert plan.path == "Wait for green mana to cast Rofellos"
    assert plan.turn_plan[0]["hold"] == "Rofellos until another Forest"
