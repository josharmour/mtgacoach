from arenamcp.action_planner import ActionPlanner, ActionType
from arenamcp.autopilot import AutopilotEngine
from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.rules_engine import RulesEngine


class _DummyBackend:
    def __init__(self, response: str):
        self._response = response

    def complete(self, system_prompt: str, user_message: str) -> str:
        return self._response


def test_pay_costs_req_clears_stale_actions_and_sets_local_decision():
    state = GameState()
    state.set_local_seat_id(1, source=2)
    state.legal_actions = ["Cast Hei Bai, Forest Guardian", "Play Land: Forest"]
    state.legal_actions_raw = [{"actionType": "ActionType_Cast", "grpId": 98284}]

    handler = create_game_state_handler(state)
    handler(
        {
            "greToClientEvent": {
                "greToClientMessages": {
                    "type": "GREMessageType_PayCostsReq",
                    "systemSeatIds": 1,
                    "payCostsReq": {
                        "manaCost": [
                            {"color": "ManaColor_Generic", "count": 3},
                            {"color": "ManaColor_Green", "count": 1},
                        ]
                    },
                }
            }
        }
    )

    assert state.pending_decision == "Pay Costs"
    assert state.decision_seat_id == 1
    assert state.legal_actions == []
    assert state.legal_actions_raw == []
    assert state.decision_context["type"] == "pay_costs"
    assert state.decision_context["mana_requirements"] == {"generic": 3, "G": 1}


def test_rules_engine_prefers_pay_costs_decision_over_stale_actions():
    game_state = {
        "pending_decision": "Pay Costs",
        "decision_context": {"type": "pay_costs", "source_card": "Hei Bai, Forest Guardian"},
        "legal_actions": ["Cast Hei Bai, Forest Guardian", "Play Land: Forest"],
    }

    assert RulesEngine.get_legal_actions(game_state) == [
        "Pay costs for Hei Bai, Forest Guardian",
        "Auto-pay",
    ]


def test_action_planner_does_not_attach_stale_gre_refs_during_pay_costs():
    planner = ActionPlanner(
        _DummyBackend(
            '{"actions":[{"action_type":"pay_costs","reasoning":"Tap lands"}],'
            '"overall_strategy":"Pay for the spell already on the stack."}'
        ),
        timeout=0.1,
    )
    game_state = {
        "turn": {"turn_number": 6, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "is_local": True}],
        "battlefield": [],
        "hand": [],
        "stack": [],
        "decision_context": {"type": "pay_costs", "source_card": "Hei Bai, Forest Guardian"},
        "_bridge_request_type": "PayCostsReq",
        "_bridge_actions": None,
        "legal_actions_raw": [{"actionType": "ActionType_Cast", "grpId": 98284, "instanceId": 240}],
    }

    plan = planner.plan_actions(
        game_state,
        "decision_required",
        legal_actions=["Pay costs for Hei Bai, Forest Guardian", "Auto-pay"],
        decision_context=game_state["decision_context"],
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.PAY_COSTS
    assert plan.actions[0].gre_action_ref is None


def test_action_planner_rejects_uncastable_spell_and_falls_back_to_pass():
    planner = ActionPlanner(
        _DummyBackend(
            '{"actions":[{"action_type":"cast_spell","card_name":"Hei Bai, Forest Guardian",'
            '"reasoning":"Cast the commander now."}],'
            '"overall_strategy":"Develop the board with Hei Bai."}'
        ),
        timeout=0.1,
    )
    game_state = {
        "turn": {"turn_number": 8, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "is_local": True}],
        "battlefield": [],
        "hand": [{"name": "Hei Bai, Forest Guardian", "mana_cost": "{3}{G}"}],
        "stack": [],
    }

    plan = planner.plan_actions(
        game_state,
        "decision_required",
        legal_actions=[
            "Cast Hei Bai, Forest Guardian",
            "Action: Activate_Mana",
            "Pass",
            "Action: FloatMana",
        ],
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.PASS_PRIORITY
    assert plan.actions[0].card_name == ""


def test_pay_cost_source_selection_prefers_specific_then_least_flexible_sources():
    game_state = {
        "turn": {"turn_number": 6},
        "battlefield": [
            {
                "instance_id": 11,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "oracle_text": "{T}: Add {G}.",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 1,
                "color_production": ["5"],
            },
            {
                "instance_id": 12,
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "oracle_text": "{T}: Add {W}.",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 1,
                "color_production": ["1"],
            },
            {
                "instance_id": 13,
                "name": "Blossoming Sands",
                "type_line": "Land",
                "oracle_text": "{T}: Add {G} or {W}.",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 1,
            },
            {
                "instance_id": 14,
                "name": "Island",
                "type_line": "Basic Land — Island",
                "oracle_text": "{T}: Add {U}.",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 1,
                "color_production": ["2"],
            },
        ]
    }
    decision_context = {
        "type": "pay_costs",
        "mana_requirements": {"generic": 1, "G": 1},
    }

    selected = AutopilotEngine._select_pay_cost_sources(game_state, decision_context, local_seat=1)

    selected_ids = [card["instance_id"] for card in selected]
    assert selected_ids[0] == 11
    assert 13 not in selected_ids
    assert len(selected_ids) == 2


def _untapped_land(name: str, type_line: str, oracle_text: str) -> dict:
    return {
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle_text,
    }


def _pool_for(battlefield: list[dict]) -> dict:
    game_state = {"turn": {"turn_number": 4}, "battlefield": battlefield}
    return RulesEngine._get_mana_pool(game_state, 1)


def test_can_afford_hybrid_pips_cannot_reuse_one_surplus_source():
    # Boros Reckoner {R/W}{R/W}{R/W} with 1 Mountain + 2 Wastes: only the
    # Mountain can pay a hybrid pip, and it can pay exactly one — the two
    # colorless sources can't cover the other hybrids.
    pool = _pool_for([
        _untapped_land("Mountain", "Basic Land — Mountain", "{T}: Add {R}."),
        _untapped_land("Wastes", "Basic Land", "{T}: Add {C}."),
        _untapped_land("Wastes", "Basic Land", "{T}: Add {C}."),
    ])
    assert RulesEngine._can_afford("{R/W}{R/W}{R/W}", pool) is False


def test_can_afford_hybrid_pips_with_three_duals():
    # Three R/W duals cover all three hybrid pips.
    dual = _untapped_land("Battlefield Forge", "Land", "{T}: Add {R} or {W}.")
    pool = _pool_for([dual, dict(dual), dict(dual)])
    assert RulesEngine._can_afford("{R/W}{R/W}{R/W}", pool) is True


def test_can_afford_multicolor_source_is_one_source_not_two():
    # {W}{U} with one W/U dual + one Wastes: the dual bumps both the W and U
    # counts but can only produce one mana — must NOT be affordable.
    pool = _pool_for([
        _untapped_land("Hallowed Fountain", "Land — Plains Island", "({T}: Add {W} or {U}.)"),
        _untapped_land("Wastes", "Basic Land", "{T}: Add {C}."),
    ])
    assert pool["W"] == 1 and pool["U"] == 1
    assert RulesEngine._can_afford("{W}{U}", pool) is False
    # Either single pip alone is fine — the dual pays one or the other.
    assert RulesEngine._can_afford("{W}", pool) is True
    assert RulesEngine._can_afford("{U}", pool) is True


def test_get_mana_pool_counts_dual_land_once():
    # A plains+island dual is a single source: total 1, one two-color entry
    # in the per-source capability list.
    pool = _pool_for([
        _untapped_land("Hallowed Fountain", "Land — Plains Island", "({T}: Add {W} or {U}.)"),
    ])
    assert pool["total"] == 1
    assert pool["_sources"] == [frozenset({"W", "U"})]


def test_can_afford_plain_pool_hybrid_commits_surplus():
    # Legacy plain-dict pools (no _sources): the per-color counts are used,
    # but a single surplus source still can't pay multiple hybrid pips.
    plain = {"W": 0, "U": 0, "B": 0, "R": 1, "G": 0, "C": 2, "Any": 0, "total": 3}
    assert RulesEngine._can_afford("{R/W}{R/W}{R/W}", plain) is False
    enough = {"W": 3, "U": 0, "B": 0, "R": 3, "G": 0, "C": 0, "Any": 0, "total": 3}
    assert RulesEngine._can_afford("{R/W}{R/W}{R/W}", enough) is True
    # Any-color budget is spent exclusively: one Any source, two hybrid pips.
    one_any = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "Any": 1, "total": 2}
    assert RulesEngine._can_afford("{R/W}{G/U}", one_any) is False
