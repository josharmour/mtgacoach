"""Factual #306 shape: last Plains reserved for Rescue, then Vendor optional {1}.

Historical raw packet is not bundled here; these are equivalent board/memory
regressions based on the ledger, not a claim of complete replay reconstruction.
"""

from copy import deepcopy
from unittest.mock import Mock

from tests.fakes import make_engine

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.optional_reservation import reserved_optional_mana

ORACLE = (
    "At the beginning of combat on your turn, you may pay {1}. When you do, create a Sorcerer Role token."
)


def case(reason="Pass and hold the last W for Rescue."):
    state = {
        "match_id": "reservation-306",
        "local_seat_id": 1,
        "turn": {"turn_number": 7, "active_player": 1, "phase": "Main1"},
        "players": [{"seat_id": 1, "life_total": 20, "mana_pool": {}}],
        "battlefield": [
            {
                "instance_id": 306,
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "oracle_text": "{T}: Add {W}.",
                "controller_seat_id": 1,
            },
            {
                "instance_id": 300,
                "name": "Spellbook Vendor",
                "type_line": "Creature — Human Peasant",
                "oracle_text": ORACLE,
                "controller_seat_id": 1,
                "power": 2,
                "toughness": 2,
            },
        ],
        "hand": [
            {
                "instance_id": 400,
                "name": "Rescue",
                "mana_cost": "{W}",
                "type_line": "Instant",
                "oracle_text": "Target creature you control gains hexproof until end of turn.",
            }
        ],
        "stack": [],
        "_bridge_can_cancel": True,
    }
    planner = ActionPlanner(Mock())
    decision = PendingDecision((1, 2), "ActionsAvailable", (DecisionOption("pass", "Pass"),), can_pass=True)
    planner._memory().record(state, decision, ["pass"], reason)
    state["turn"]["phase"] = "Combat"
    state["decision_context"] = {"type": "pay_costs", "sourceId": 500}
    state["stack"] = [
        {
            "instance_id": 500,
            "object_kind": "ABILITY",
            "name": "Spellbook Vendor",
            "oracle_text": ORACLE,
            "controller_seat_id": 1,
        }
    ]
    planner.plan_pay_or_decline = Mock(return_value=True)
    return state, planner, decision


def test_unchanged_board_reservation_prevents_even_a_pay_verdict():
    state, planner, _ = case()
    engine = make_engine(planner=planner)
    reason = engine._should_decline_optional_cost(state)
    assert "reservation for Rescue" in reason
    assert not planner.plan_pay_or_decline.called


def test_enough_spare_mana_keeps_optional_payment_review():
    state, planner, _ = case()
    state["battlefield"].append({**state["battlefield"][0], "instance_id": 307})
    assert not reserved_optional_mana(planner, state, ORACLE)
    assert make_engine(planner=planner)._should_decline_optional_cost(state) is None
    planner.plan_pay_or_decline.assert_called_once()


def test_new_threat_or_life_change_allows_fresh_plan():
    state, planner, _ = case()
    for change in ("creature", "life", "response"):
        changed = deepcopy(state)
        if change == "creature":
            changed["battlefield"].append(
                {"instance_id": 600, "type_line": "Creature", "controller_seat_id": 2}
            )
        elif change == "life":
            changed["players"][0]["life_total"] = 2
        else:
            changed["stack"].insert(0, {"instance_id": 600, "type_line": "Instant", "controller_seat_id": 2})
        assert not reserved_optional_mana(planner, changed, ORACLE)


def test_new_explicit_plan_supersedes_old_hold():
    state, planner, decision = case()
    planner._memory().record(
        state, decision, ["pass"], "Use the mana for the Role; Rescue is no longer useful."
    )
    assert not reserved_optional_mana(planner, state, ORACLE)


def test_negated_or_unaffordable_or_absent_reservation_does_not_block():
    state, planner, _ = case("Do not hold W for Rescue.")
    assert not reserved_optional_mana(planner, state, ORACLE)
    state, planner, _ = case()
    state["hand"][0]["mana_cost"] = "{1}{W}"
    assert not reserved_optional_mana(planner, state, ORACLE)
    state["hand"] = []
    assert not reserved_optional_mana(planner, state, ORACLE)


def test_stale_turn_and_unknown_payment_stay_advisory():
    state, planner, _ = case()
    assert not reserved_optional_mana(planner, state, "You may pay {X}.")
    state["turn"]["turn_number"] = 8
    assert not reserved_optional_mana(planner, state, ORACLE)


def test_mandatory_own_spell_payment_bypasses_reservation():
    state, planner, _ = case()
    state["stack"][0].update(object_kind="CARD", type_line="Instant")
    assert make_engine(planner=planner)._should_decline_optional_cost(state) is None
    state["_bridge_can_cancel"] = False
    assert make_engine(planner=planner)._should_decline_optional_cost(state) is None
