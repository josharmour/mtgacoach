"""R3: indexed-menu picks resolve to legal actions by construction."""

from arenamcp.action_planner import ActionPlanner, ActionType


class _NoBackend:
    def complete(self, *a, **k):
        raise AssertionError("parser tests must not call the LLM")


def _planner_with_menu(menu):
    p = ActionPlanner(_NoBackend())
    p._last_menu = list(menu)
    return p


MENU = ["Cast The Spirit Oasis [OK]", "Play Land: Forest", "Pass"]


def test_pick_resolves_to_menu_entry():
    p = _planner_with_menu(MENU)
    plan = p._parse_response(
        '{"actions": [{"pick": 1, "reasoning": "best play"}],'
        ' "overall_strategy": "develop"}',
        MENU,
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.CAST_SPELL
    assert plan.actions[0].card_name == "The Spirit Oasis"
    assert plan.actions[0].reasoning == "best play"


def test_pick_land_and_pass():
    p = _planner_with_menu(MENU)
    plan = p._parse_response('{"actions": [{"pick": 2}]}', MENU)
    assert plan.actions[0].action_type == ActionType.PLAY_LAND
    assert plan.actions[0].card_name == "Forest"

    plan = p._parse_response('{"actions": [{"pick": 3}]}', MENU)
    assert plan.actions[0].action_type == ActionType.PASS_PRIORITY


def test_out_of_range_pick_falls_back_to_structured_fields():
    p = _planner_with_menu(MENU)
    plan = p._parse_response(
        '{"actions": [{"pick": 99, "action_type": "pass_priority"}]}', MENU
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.PASS_PRIORITY


def test_structured_actions_still_work_without_pick():
    p = _planner_with_menu(MENU)
    plan = p._parse_response(
        '{"actions": [{"action_type": "cast_spell",'
        ' "card_name": "The Spirit Oasis"}]}',
        MENU,
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].card_name == "The Spirit Oasis"


class _CountingBackend:
    def __init__(self):
        self.calls = 0

    def complete(self, *a, **k):
        self.calls += 1
        return '{"actions": [{"action_type": "pass_priority"}]}'


def test_trivial_window_skips_llm_entirely():
    # P2-6: Wait-only and pass-only menus burned 7+ full LLM calls on
    # 2026-07-05 with predetermined outcomes.
    from arenamcp.action_planner import ActionPlanner, ActionType

    be = _CountingBackend()
    p = ActionPlanner(be, timeout=1.0, land_drop_first=False)
    state = {"turn": {"turn_number": 9, "phase": "Combat"}}

    plan = p.plan_actions(state, "combat_blockers", ["Wait (Opponent has priority)"])
    assert be.calls == 0
    assert plan.actions and plan.actions[0].action_type == ActionType.PASS_PRIORITY

    plan = p.plan_actions(
        state, "decision_required",
        ["Action: Activate_Mana", "Pass", "Action: FloatMana"],
    )
    assert be.calls == 0
    assert plan.actions and plan.actions[0].action_type == ActionType.PASS_PRIORITY
