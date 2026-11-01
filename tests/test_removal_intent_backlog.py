"""#274/#279: reject illegal named removal and reopen a changed Plains cap."""

from unittest.mock import Mock

from tests.test_tactical_timing_backlog import board, card, cast, menu

from arenamcp.autopilot import AutopilotEngine
from arenamcp.removal_windows import invalid_removal_intent, plains_cap_targets


def arms_board():
    state = board(3)
    state["hand"] = [
        card(
            30,
            "Lay Down Arms",
            "Exile target creature with mana value less than or equal to the number of Plains you control.",
            "Sorcery",
            "{W}",
        )
    ]
    state["battlefield"].append(
        card(50, "Drana and Linvala", "", "Legendary Creature", "{1}{W}{W}{B}", seat=2, power=3, toughness=4)
    )
    return state


def test_precast_plains_cap_claim_and_changed_cap():
    state = arms_board()
    reason = "Exiling Drana and Linvala with Lay Down Arms; three Plains meet its cap."
    assert invalid_removal_intent(menu(cast(30)), state, ["idx:30"], reason)
    state["battlefield"].append(card(4, "Plains", "", "Basic Land — Plains"))
    assert not invalid_removal_intent(menu(cast(30)), state, ["idx:30"], reason)
    assert not invalid_removal_intent(
        menu(cast(30)), arms_board(), ["idx:30"], "Cannot exile Drana and Linvala; use another target."
    )


def test_destroy_evil_cannot_remove_small_creature_or_creature_spell():
    state = board()
    state["hand"] = [
        card(
            30,
            "Destroy Evil",
            "Choose one —\n• Destroy target creature with toughness 4 or greater.\n• Destroy target enchantment.",
            "Instant",
            "{1}{W}",
        )
    ]
    ash = card(50, "Zul Ashur", "", "Creature", "{1}{B}", seat=2, power=2, toughness=2)
    state["battlefield"].append(ash)
    reason = "Removing Zul Ashur before it resolves cuts off reanimation."
    assert invalid_removal_intent(menu(cast(30)), state, ["idx:30"], reason)
    state["battlefield"].remove(ash)
    state["stack"] = [ash]
    assert "still on the stack" in invalid_removal_intent(menu(cast(30)), state, ["idx:30"], reason)


def test_cancel_rearms_only_when_increased_plains_count_opens_a_target():
    state = arms_board()
    engine = AutopilotEngine.__new__(AutopilotEngine)
    key = (8, "ActionType_Cast", 30, 0, "lay down arms")
    engine._self_cancelled_plays = {key}
    engine._self_cancelled_target_caps = {key: plains_cap_targets(state, 30)}
    assert engine._self_cancel_withheld(state, "ActionType_Cast", 30)
    state["battlefield"].append(card(4, "Mountain", "", "Basic Land — Mountain"))
    assert engine._self_cancel_withheld(state, "ActionType_Cast", 30)
    state["battlefield"].append(card(5, "Plains", "", "Basic Land — Plains"))
    assert not engine._self_cancel_withheld(state, "ActionType_Cast", 30)
    assert not engine._self_cancelled_plays
    # A new cancellation at the new cap is withheld again; no polling retry loop.
    engine._self_cancelled_plays = {key}
    engine._self_cancelled_target_caps = {key: plains_cap_targets(state, 30)}
    assert engine._self_cancel_withheld(state, "ActionType_Cast", 30)


def test_cast_stage_repair_never_announces_invalid_intent(monkeypatch):
    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner(backend=Mock())

    def answer(*args):
        planner._last_decision_reasoning = "Exiling Drana and Linvala with Lay Down Arms."
        planner._last_decision_trace = {}
        return ["idx:30"]

    monkeypatch.setattr(planner, "_plan_decision_options", answer)
    assert planner.plan_decision_options(menu(cast(30)), arms_board()) == ["pass"]
    assert "not a legal target" in planner._last_decision_reasoning
