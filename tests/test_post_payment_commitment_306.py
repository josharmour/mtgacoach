"""#306 current-source reproduction: confirmed payment invalidates an old W-open assertion.

This validates a reproducible render path, not a replay of the absent historical
fdbc5148 packet. Payment remains permitted after an explicit legitimate replan;
only the old assertion of currently available protection mana is repaired.
"""

from copy import deepcopy

from tests.fakes import make_engine
from tests.test_optional_reservation_306 import case
from tests.test_tactical_timing_backlog import card

from arenamcp.board_assessment import assess
from arenamcp.game_plan import GamePlan, compose_strategy_block, validate_plan
from arenamcp.play_safety import exact_mana_total
from arenamcp.resource_hold import current_mana_hold

HOLD = "Keep W open for Rescue."


def planned():
    state, planner, decision = case()
    before = deepcopy(state)
    before["stack"] = []
    before["decision_context"] = {}
    before["turn"]["phase"] = "Main1"
    before["players"][0].update(is_local=True, lands_played=1)
    before["players"].append({"seat_id": 2, "life_total": 20})
    plan = validate_plan(
        GamePlan(
            turn_formed=7,
            win_conditions=["Creature pressure"],
            turn_plan=[{"turn": "T", "cast": [], "attack": "Spellbook Vendor", "hold": HOLD}],
        ),
        assess(before),
        before,
    )
    assert plan.turn_plan[0]["hold"] == HOLD
    return before, state, planner, decision, plan


def paid_state(before):
    after = deepcopy(before)
    after["battlefield"][0]["is_tapped"] = True
    after["turn"].update(phase="Combat", step="DeclareAttack")
    return after


def rendered(state, plan):
    return compose_strategy_block(assess(state), plan, state=state)


def test_confirmed_zero_mana_repairs_current_plan_hold_in_actual_attack_prompt():
    before, _, planner, _, plan = planned()
    after = paid_state(before)
    assert exact_mana_total(before) == 1 and exact_mana_total(after) == 0
    assert "hold: " + HOLD in rendered(before, plan)
    planner.set_game_plan_source(lambda state, **kwargs: rendered(state, plan))
    prompt = planner._build_action_prompt(
        after, "declare_attackers", ["Attack with: Spellbook Vendor"], {"type": "declare_attackers"}
    )
    assert "Mana: 0" in prompt
    assert "hold: " + HOLD not in prompt
    assert "Rescue is not currently payable ({W}; 0 mana available)" in prompt
    # Old decision records are history, not silently deleted or rewritten.
    assert "Pass and hold the last W for Rescue" in planner._memory().prompt_block(after)
    assert plan.turn_plan[0]["hold"] == HOLD


def test_intentional_payment_still_allowed_but_old_hold_is_not_current_fact():
    before, payment, planner, decision, plan = planned()
    planner._memory().record(
        payment, decision, ["pass"], "Use the mana for the Role; Rescue is no longer useful."
    )
    assert make_engine(planner=planner)._should_decline_optional_cost(payment) is None
    after = paid_state(before)
    assert "hold: " + HOLD not in rendered(after, plan)
    assert "Use the mana for the Role" in planner._memory().prompt_block(after)


def test_no_payment_and_tap_then_untap_do_not_erase_valid_saved_hold():
    before, payment, planner, _, plan = planned()
    assert make_engine(planner=planner)._should_decline_optional_cost(payment)
    assert "hold: " + HOLD in rendered(before, plan)
    after = paid_state(before)
    assert "hold: " + HOLD not in rendered(after, plan)
    after["battlefield"][0]["is_tapped"] = False
    assert "hold: " + HOLD in rendered(after, plan)


def test_unknown_or_multi_mana_sources_do_not_prove_shortfall():
    before, _, _, _, _ = planned()
    after = paid_state(before)
    after["battlefield"].append(
        card(900, "Variable mana", "{T}: Add an amount of {W} equal to your devotion to white.", "Artifact")
    )
    assert exact_mana_total(after) is None
    assert current_mana_hold(HOLD, after) == HOLD
    after["battlefield"][-1]["oracle_text"] = "{T}: Add {W}{W}."
    after["hand"][0]["mana_cost"] = "{3}{W}"
    assert current_mana_hold(HOLD, after) == HOLD


def test_unknown_card_or_payment_permission_remains_advisory():
    before, _, _, _, _ = planned()
    after = paid_state(before)
    after["battlefield"].append({"instance_id": 901, "controller_seat_id": 1, "name": "Unknown"})
    assert current_mana_hold(HOLD, after) == HOLD
    after = paid_state(before)
    after["hand"][0].pop("mana_cost")
    assert current_mana_hold(HOLD, after) == HOLD
    after = paid_state(before)
    after["_bridge_actions"] = [
        {
            "actionType": "ActionType_Cast",
            "instanceId": 400,
            "alternativeCostText": "Cast without paying mana cost",
            "hasAutoTap": True,
        }
    ]
    assert current_mana_hold(HOLD, after) == HOLD
    after = paid_state(before)
    after["hand"][0]["oracle_text"] += " Convoke"
    assert current_mana_hold(HOLD, after) == HOLD


def test_hand_mana_and_floating_mana_are_not_lost():
    before, _, _, _, _ = planned()
    after = paid_state(before)
    after["floating_mana"] = {"W": 1}
    assert current_mana_hold(HOLD, after) == HOLD
    after.pop("floating_mana")
    after["hand"].append(card(902, "Spirit guide", "Exile this card from your hand: Add {W}.", "Creature"))
    assert current_mana_hold(HOLD, after) == HOLD


def test_holds_without_current_mana_assertions_are_preserved():
    before, _, _, _, _ = planned()
    after = paid_state(before)
    for hold in (
        "Keep Rescue in hand.",
        "Keep W open for Rescue next turn.",
        "After untap, hold W for Rescue.",
        "If they attack, hold W for Rescue.",
        "Use the mana for the Role; Rescue is no longer useful.",
    ):
        assert current_mana_hold(hold, after) == hold


def test_pending_land_or_cast_keeps_conditional_future_budget():
    before, _, _, _, plan = planned()
    after = paid_state(before)
    plan.turn_plan[0]["land"] = "Plains"
    assert "hold: " + HOLD in rendered(after, plan)
    plan.turn_plan[0]["land"] = ""
    plan.turn_plan[0]["cast"] = ["Untap engine"]
    assert "hold: " + HOLD in rendered(after, plan)


def test_only_untapped_red_does_not_infer_white_shortfall_from_total():
    before, _, _, _, _ = planned()
    before["battlefield"][0].update(
        name="Mountain", type_line="Basic Land — Mountain", oracle_text="{T}: Add {R}."
    )
    assert exact_mana_total(before) == 1
    assert current_mana_hold(HOLD, before) == HOLD  # colored payment is outside this numeric check


def test_cancelled_or_submitted_payment_without_confirmed_tap_keeps_hold():
    before, payment, planner, _, plan = planned()
    engine = make_engine(planner=planner)
    # The payment path records submission/decline for telemetry. Those events
    # alone cannot establish a tap or change current affordability.
    for summary in ("submitted AutoTap solution via bridge", "declined optional cost: preserve Rescue"):
        engine._record_autopilot_decision(payment, "pay_costs", "pay_costs", summary)
        assert exact_mana_total(before) == 1
        assert "hold: " + HOLD in rendered(before, plan)
