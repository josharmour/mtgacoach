"""Ledger #235/#240/#272/#287: rejected plans must not keep their prerequisites."""

import json
from copy import deepcopy

import pytest

from arenamcp.board_assessment import assess
from arenamcp.game_plan import GamePlan, GamePlanManager, validate_plan


def state():
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 9, "active_player": 1, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        "hand": [],
        "battlefield": [],
        "deck_catalog": {1: {"name": "Akroma's Will", "type_line": "Instant", "mana_cost": "{3}{W}"}},
    }


def test_library_win_does_not_leak_into_path_priorities_or_turn_holds():
    game = state()
    plan = GamePlan(
        win_conditions=["Akroma's Will alpha strike"],
        path="Preserve Leonardo for Akroma's Will lethal",
        develop_next="Cast Akroma's Will",
        resource_priorities=["Keep Leonardo back as Akroma's Will enabler", "Preserve removal"],
        turn_plan=[{"turn": "T", "hold": "Keep Leonardo back for Akroma's Will"}],
    )
    validate_plan(plan, assess(game), game)
    assert "Akroma's Will" not in plan.as_planner_block()
    assert plan.resource_priorities == ["Preserve removal"]
    assert any("path rejected" in issue for issue in plan.issues)


@pytest.mark.parametrize("text", ["If drawn: Akroma's Will alpha strike", "Tutor for Akroma's Will"])
def test_conditional_draw_and_tutor_lines_survive(text):
    game = state()
    plan = GamePlan(win_conditions=[text], path=text, develop_next=text)
    validate_plan(plan, assess(game), game)
    assert plan.path == text
    assert plan.develop_next == text


@pytest.mark.parametrize("zone", ["hand", "battlefield", "graveyard", "exile", "stack", "command"])
def test_visible_card_is_not_mislabelled_as_still_in_library(zone):
    game = state()
    game[zone] = [{"name": "Akroma's Will", "owner_seat_id": 1, "type_line": "Instant"}]
    plan = GamePlan(win_conditions=["Akroma's Will alpha strike"], path="Akroma's Will lethal")
    validate_plan(plan, assess(game), game)
    assert plan.path == "Akroma's Will lethal"
    assert not any("still in the library" in issue for issue in plan.issues)


class Backend:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def complete(self, system, user, *args, **kwargs):
        self.requests.append((user, kwargs))
        return self.answers.pop(0)


def test_reform_supplies_availability_and_previous_rejections(monkeypatch):
    game = state()
    backend = Backend(json.dumps({"win_conditions": ["Creature combat"], "path": "Build the board"}))
    manager = GamePlanManager(backend)
    manager._plan = GamePlan(
        win_conditions=["Creature combat"],
        issues=[
            "win condition 'Akroma's Will alpha strike' rejected: needs akroma s will (still in the library)"
        ],
    )
    manager._reform(game, 9)
    prompt = backend.requests[0][0]
    assert "PLAN AVAILABILITY" in prompt
    assert "akroma s will" in prompt.lower()
    assert "PREVIOUS VALIDATION" in prompt
    assert "Recheck against the current snapshot" in prompt


def test_library_constraints_refresh_when_the_card_is_drawn():
    game = state()
    game["hand"] = [dict(game["deck_catalog"][1], owner_seat_id=1)]
    backend = Backend(json.dumps({"win_conditions": ["Akroma's Will alpha strike"]}))
    manager = GamePlanManager(backend)
    manager._reform(deepcopy(game), 9)
    prompt = backend.requests[0][0]
    availability = prompt.split("PLAN AVAILABILITY", 1)[1].split("\n\n", 1)[0]
    assert "akroma s will" not in availability.lower()


def test_explicit_unavailability_notes_are_not_removed():
    game = state()
    plan = GamePlan(
        win_conditions=["Creature combat"],
        path="Do not wait for Akroma's Will",
        active_mechanisms=["Akroma's Will unavailable"],
        resource_priorities=["Do not hold Leonardo back for Akroma's Will"],
    )
    validate_plan(plan, assess(game), game)
    assert plan.path == "Do not wait for Akroma's Will"
    assert plan.active_mechanisms == ["Akroma's Will unavailable"]
    assert plan.resource_priorities == ["Do not hold Leonardo back for Akroma's Will"]


def test_rejected_library_win_falls_back_to_the_visible_mill_deadline():
    # #272 mechanism replay, not a reconstruction of the entire bed0e3fd match.
    game = state()
    game["zones"] = {"library_count": 20, "library_count_source": "log_zone_membership"}
    game["battlefield"] = [
        {
            "name": "Restless Reef",
            "instance_id": 90,
            "controller_seat_id": 2,
            "owner_seat_id": 2,
            "type_line": "Land",
            "oracle_text": "Whenever Restless Reef attacks, target player mills four cards.",
        }
    ]
    plan = GamePlan(win_conditions=["Akroma's Will alpha strike"], path="Hold Leonardo for Akroma's Will")
    validate_plan(plan, assess(game), game)
    assert "Remove Restless Reef" in plan.win_conditions[0]
    assert "Akroma's Will" not in plan.as_planner_block()
