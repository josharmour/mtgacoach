"""The planner must accept every JSON shape models actually emit.

Live logs had 9/18 "Planner JSON parse returned 0 actions" failures where the
model answered with a bare {"pick": N, ...} instead of {"actions": [...]}; the
fallback heuristic then chose Pass with castable spells in hand
(bug_20260920_231337 replayed through the gateway on 2026-09-22).
"""

from __future__ import annotations

from arenamcp.action_planner import ActionPlanner, ActionType

MENU = ["Cast Optimistic Scavenger [OK]", "Action: Activate_Mana", "Pass", "Action: FloatMana"]


def _planner() -> ActionPlanner:
    p = ActionPlanner.__new__(ActionPlanner)
    p._last_menu = list(MENU)
    return p


def _parse(text: str):
    return _planner()._parse_response(text, MENU)


def test_wrapped_actions_still_parse():
    plan = _parse('{"actions": [{"pick": 1, "reasoning": "develop"}], "overall_strategy": "curve out"}')
    assert [a.action_type for a in plan.actions] == [ActionType.CAST_SPELL]
    assert plan.overall_strategy == "curve out"


def test_bare_pick_object_parses():
    plan = _parse('{"pick": 1, "reasoning": "Seam Rip has no legal targets; develop the body.", '
                  '"turn_plan": {"steps": []}}')
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.CAST_SPELL
    assert plan.actions[0].card_name == "Optimistic Scavenger"
    assert "develop the body" in plan.overall_strategy


def test_bare_pick_with_action_type_parses():
    plan = _parse('{"pick": 3, "action_type": "pass_priority", "reasoning": "nothing to do"}')
    assert [a.action_type for a in plan.actions] == [ActionType.PASS_PRIORITY]


def test_bare_list_parses():
    plan = _parse('[{"pick": 1, "reasoning": "develop"}]')
    assert [a.action_type for a in plan.actions] == [ActionType.CAST_SPELL]


def test_unrelated_object_is_still_empty():
    assert _parse('{"thoughts": "hmm"}').actions == []


# ── attack menu: GRE's live attacker list is authoritative ─────────────────


def _declare_state(step: str) -> dict:
    return {
        "players": [{"seat_id": 1, "is_local": True, "life_total": 18},
                    {"seat_id": 2, "is_local": False, "life_total": 3}],
        "turn": {"turn_number": 9, "active_player": 1, "priority_player": 1,
                 "phase": "Phase_Combat", "step": step},
        "battlefield": [
            # Being declared: GRE reports it tapped mid-declaration.
            {"name": "Veteran Survivor", "type_line": "Creature — Human Survivor", "power": 12,
             "toughness": 10, "controller_seat_id": 1, "owner_seat_id": 1, "is_tapped": True,
             "turn_entered_battlefield": 1},
            {"name": "Optimistic Scavenger", "type_line": "Creature — Human Scout", "power": 1,
             "toughness": 1, "controller_seat_id": 1, "owner_seat_id": 1, "is_tapped": False,
             "turn_entered_battlefield": 7},
        ],
        "decision_context": {"type": "declare_attackers",
                             "legal_attackers": ["Veteran Survivor", "Optimistic Scavenger"]},
    }


def test_live_declare_step_keeps_gre_attacker_reported_tapped():
    from arenamcp.rules_engine import RulesEngine

    menu = RulesEngine.get_legal_actions(_declare_state("Step_DeclareAttack"))
    assert "Attack with: Veteran Survivor (12/10)" in menu
    assert "Attack with: Optimistic Scavenger (1/1)" in menu


def test_stale_context_outside_the_step_still_filters_tapped():
    from arenamcp.rules_engine import RulesEngine

    menu = RulesEngine.get_legal_actions(_declare_state("Step_EndCombat"))
    assert not any("Veteran Survivor" in a for a in menu)


# ── ChooseX casting-time request: X options come from the log ──────────────


def _choose_x_state(forests: int, mana_cost: str = "{X}{G}{G}") -> dict:
    # Shape copied from bug_20260901_193836 (Nature's Rhythm, 3 Forests).
    return {
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "turn": {"turn_number": 5, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "pending_decision": "Choose Casting Option",
        "stack": [{"name": "Nature's Rhythm", "mana_cost": mana_cost, "instance_id": 557, "owner_seat_id": 1}],
        "battlefield": [
            {"name": "Forest", "type_line": "Basic Land — Forest", "owner_seat_id": 1,
             "controller_seat_id": 1, "is_tapped": False}
            for _ in range(forests)
        ],
        "decision_context": {"type": "casting_time_options", "raw": {"castingTimeOptionReq": [{
            "ctoId": 2, "castingTimeOptionType": "CastingTimeOptionType_ChooseX", "affectedId": 557,
            "numericInputReq": {"maxValue": 2147483647, "stepSize": 1, "sourceId": 557,
                                "numericInputType": "NumericInputType_ChooseX"}}]}},
    }


def test_choose_x_menu_is_bounded_by_untapped_mana():
    from arenamcp.rules_engine import RulesEngine

    assert RulesEngine.get_legal_actions(_choose_x_state(3)) == ["X = 0", "X = 1"]
    assert RulesEngine.get_legal_actions(_choose_x_state(6)) == ["X = 0", "X = 1", "X = 2", "X = 3", "X = 4"]
    assert RulesEngine.get_legal_actions(_choose_x_state(6, "{X}{X}{G}")) == ["X = 0", "X = 1", "X = 2"]


def test_choose_x_pick_becomes_numeric_input():
    p = ActionPlanner.__new__(ActionPlanner)
    p._last_menu = ["X = 0", "X = 1"]
    plan = p._parse_response('{"actions": [{"pick": 2, "reasoning": "use all mana"}]}', ["X = 0", "X = 1"])
    assert [(a.action_type, a.numeric_value) for a in plan.actions] == [(ActionType.NUMERIC_INPUT, 1)]


def test_other_casting_time_options_keep_generic_menu():
    from arenamcp.rules_engine import RulesEngine

    state = _choose_x_state(3)
    state["decision_context"]["raw"]["castingTimeOptionReq"][0]["castingTimeOptionType"] = "CastingTimeOptionType_Kicker"
    state["decision_context"]["raw"]["castingTimeOptionReq"][0].pop("numericInputReq")
    assert RulesEngine.get_legal_actions(state)[0] == "Cast normally"
