"""#235: a mana-illegal cast must not leave its dependents behind.

The historical trace (89f64a23) dropped Rofellos, Llanowar Emissary from T+1
("not mana-legally legal ... plan needs 2 mana budget 2 G") while the plan's
path, priorities and attack still assumed the cast happened. The bounded repair
half is pinned in tests/test_plan_mana_repair.py; this file pins the
deterministic cleanup that runs when no repair can (live decision pending,
budget spent, unhealthy backend) and the evidence the next reform receives.
"""

from tests.test_plan_mana_repair import mana_state

from arenamcp.board_assessment import assess
from arenamcp.game_plan import GamePlan, _plan_availability_block, validate_plan

ROFELLOS = "Rofellos, Llanowar Emissary"


def _plan(**kwargs) -> GamePlan:
    return GamePlan(win_conditions=["Creature combat"], **kwargs)


def test_a_colored_pip_shortfall_drops_the_cast_and_its_dependents():
    game = mana_state()
    plan = _plan(
        path="Deploy Rofellos now, then swing",
        develop_next="Rofellos beats",
        resource_priorities=["Keep mana for Rofellos", "Preserve removal"],
        turn_plan=[{"turn": "T", "cast": [ROFELLOS], "attack": ROFELLOS, "hold": "hold mana for Rofellos"}],
    )
    validate_plan(plan, assess(game), game)
    # Two sources but only one green one: the count fits, the pips do not.
    assert any(
        issue.startswith(f"T: dropped {ROFELLOS} — not mana-legal (plan needs 2 mana, budget 2 G)")
        for issue in plan.issues
    ), plan.issues
    assert plan.path == "" and plan.develop_next == ""
    assert plan.resource_priorities == ["Preserve removal"]
    step = plan.turn_plan[0]
    assert step["cast"] == []
    assert ROFELLOS not in step["attack"] and ROFELLOS not in step["hold"]
    assert any(issue.startswith("path removed after mana-illegal cast") for issue in plan.issues)
    assert any(issue.startswith("T attack removed after mana-illegal cast") for issue in plan.issues)


def test_a_conditional_wait_on_the_dropped_card_survives():
    game = mana_state()
    path = "Wait for a second green source before Rofellos"
    plan = _plan(path=path, turn_plan=[{"turn": "T", "cast": [ROFELLOS]}])
    validate_plan(plan, assess(game), game)
    assert any("not mana-legal" in issue for issue in plan.issues)
    assert plan.path == path


def test_the_degraded_plan_carries_the_drop_back_to_the_next_reform():
    game = mana_state()
    plan = _plan(turn_plan=[{"turn": "T", "cast": [ROFELLOS]}])
    validate_plan(plan, assess(game), game)
    block = _plan_availability_block(game, plan)
    assert "PREVIOUS VALIDATION" in block and "not mana-legal" in block
