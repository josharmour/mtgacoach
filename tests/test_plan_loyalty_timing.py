"""Regression for Armour vs zambat: the strategic turn spent one walker's loyalty twice."""

import json

import pytest
from tests.deck_fakes import deck_case

from arenamcp.board_assessment import assess
from arenamcp.deck_strategy import DeckPlaybook, deck_identity
from arenamcp.game_plan import GamePlan, validate_plan
from arenamcp.loyalty_removal import loyalty_strategy_context
from arenamcp.loyalty_timing import conflicting_loyalty_sequence

WALKER = {
    "name": "Minsc & Boo, Timeless Heroes",
    "instance_id": 658,
    "controller_seat_id": 1,
    "type_line": "Legendary Planeswalker — Minsc",
    "counters": {"Loyalty": 4},
    "oracle_text": "[+1]: Put three +1/+1 counters on up to one target creature with trample or haste.\n"
    "[-2]: Sacrifice a creature. When you do, this deals X damage to any target, "
    "where X is that creature's power. If the sacrificed creature was a Hamster, draw X cards.",
}
BOO = {
    "name": "Boo",
    "instance_id": 664,
    "controller_seat_id": 1,
    "type_line": "Legendary Creature — Hamster",
    "power": 4,
    "toughness": 4,
    "oracle_text": "Trample, haste",
    "turn_entered_battlefield": 5,
}
STATE = {
    "turn": {"turn_number": 6, "active_player": 2, "phase": "Main1"},
    "local_seat_id": 1,
    "players": [{"seat_id": 1, "is_local": True, "life_total": 22}, {"seat_id": 2, "life_total": 20}],
    "battlefield": [WALKER, BOO],
}
INTENT = {"source": WALKER["name"], "attacker": "Boo", "cost": -2}


def test_recorded_plan_cannot_spend_the_same_loyalty_activation_twice():
    plan = GamePlan(
        path="T7 upkeep Boo, +1/-2 for 4 dmg+4 cards, old Boo attacks; repeat loop each upkeep to lethal",
        turn_plan=[
            {
                "turn": "T",
                "hold": "Upkeep: fresh Boo -> Minsc & Boo +1 (to 5) then -2 sacrificing buffed fresh Boo: 4 damage + 4 cards",
                "attack": "Boo (original 4/4)",
            },
            {"turn": "T+1", "hold": "Cast the best body from the 4 drawn cards."},
        ],
    )
    validate_plan(plan, assess(STATE), STATE)
    block = plan.as_planner_block()
    assert any("second loyalty activation" in issue for issue in plan.issues)
    # Q2 (fable-november-upgrades.md §Q2 Done-when): only the offending same-turn
    # double-activation clause is removed; the model's independent attack-to-win
    # strategy survives into the planner block instead of being wiped with the
    # whole path (the pre-Q2 whole-path-wipe expectation here is retired).
    assert "old Boo attacks; repeat loop each upkeep to lethal" in plan.path
    assert "+1/-2 for 4 dmg+4 cards" not in plan.path
    assert "old Boo" in block and "fresh Boo" not in block and "4 drawn" not in block
    assert "one available loyalty ability" in block


def test_same_turn_pair_strip_removes_only_the_offending_path_clause():
    """Q2 adjacent regression on the same recorded Armour vs zambat plan: the
    doubled-activation clause leaves the path while the strategy tail stays,
    and the replacement boilerplate never enters the path itself."""

    def build() -> GamePlan:
        return GamePlan(
            path="T7 upkeep Boo, +1/-2 for 4 dmg+4 cards, old Boo attacks; repeat loop each upkeep to lethal",
            turn_plan=[
                {
                    "turn": "T",
                    "hold": "Upkeep: fresh Boo -> Minsc & Boo +1 (to 5) then -2 sacrificing buffed fresh Boo: 4 damage + 4 cards",
                    "attack": "Boo (original 4/4)",
                },
                {"turn": "T+1", "hold": "Cast the best body from the 4 drawn cards."},
            ],
        )

    plan = build()
    validate_plan(plan, assess(STATE), STATE)
    assert plan.path == "T7 upkeep Boo, old Boo attacks; repeat loop each upkeep to lethal"
    assert "Reassess the other on a later turn" not in plan.path
    assert any("removed a second loyalty activation" in issue for issue in plan.issues)


def test_legal_attack_and_postcombat_sacrifice_survive_validation():
    plan = GamePlan(
        turn_plan=[
            {
                "turn": "T",
                "attack": "Boo",
                "hold": "Use -2 after combat if Boo survives.",
                "postcombat_sacrifice": INTENT,
            }
        ]
    )
    validate_plan(plan, assess(STATE), STATE)
    assert plan.turn_plan[0]["postcombat_sacrifice"] == INTENT
    assert plan.turn_plan[0]["attack"] == "Boo"


def test_structured_minus_and_prose_plus_cannot_share_one_turn():
    plan = GamePlan(
        turn_plan=[
            {"turn": "T", "attack": "Boo", "hold": "First +1 to grow Boo.", "postcombat_sacrifice": INTENT}
        ]
    )
    validate_plan(plan, assess(STATE), STATE)
    assert "postcombat_sacrifice" not in plan.turn_plan[0]


def test_two_uses_of_one_walker_are_rejected_even_without_costs_in_the_prose():
    other = dict(WALKER, instance_id=999, name="Another Walker")
    state = dict(STATE, battlefield=[*STATE["battlefield"], other])
    uses = [{"source": WALKER["name"], "cost": 1}, {"source": other["name"], "cost": 1}]
    plan = GamePlan(turn_plan=[{"turn": "T", "loyalty_activations": uses, "hold": "Grow both attackers."}])
    validate_plan(plan, assess(state), state)
    assert plan.turn_plan[0]["hold"] == "Grow both attackers."

    plan = GamePlan(
        turn_plan=[
            {
                "turn": "T",
                "loyalty_activations": uses,
                "hold": "Grow the attacker and sacrifice it.",
                "attack": "Boo",
                "postcombat_sacrifice": INTENT,
            }
        ]
    )
    validate_plan(plan, assess(state), state)
    assert "one available loyalty ability" in plan.turn_plan[0]["hold"]
    assert "postcombat_sacrifice" not in plan.turn_plan[0]


def test_opening_summary_rejects_impossible_loop_on_parse_and_restore():
    state, catalog, data = deck_case()
    catalog[1001] = WALKER
    data["spoken_summary"] = "Pump one with +1, then sacrifice it with -2 for damage and cards every turn."
    with pytest.raises(ValueError, match="spoken_summary spends loyalty twice"):
        DeckPlaybook.parse(json.dumps(data), catalog, [1001])
    data["spoken_summary"] = "Pump one with +1, then use -2 on a later turn if it survives."
    book = DeckPlaybook.parse(json.dumps(data), catalog, [1001], deck_identity(state))
    assert book.data["spoken_summary"] == data["spoken_summary"]
    saved = book.export()
    saved["data"]["spoken_summary"] = "Use +1 then -2 to get damage and cards every turn."
    with pytest.raises(ValueError, match="spoken_summary spends loyalty twice"):
        DeckPlaybook.restore(saved, state)


@pytest.mark.parametrize(
    "line",
    [
        "Put three +1/+1 counters on a creature with the +1 ability.",
        "Choose +1 or -2 this turn.",
        "Use +1 now and -2 next turn.",
        "T7 +1, T9 -2.",
        "Use Minsc & Boo +1 and Jace -2.",
    ],
)
def test_ordinary_alternatives_stats_and_distinct_walkers(line):
    others = [dict(WALKER, name="Jace")] if "Jace" in line else []
    assert not conflicting_loyalty_sequence(line, [WALKER, *others], turn_step=True)


def test_supported_extra_activation_is_preserved():
    state = dict(
        STATE,
        battlefield=[
            *STATE["battlefield"],
            {
                "name": "Oath",
                "controller_seat_id": 1,
                "oracle_text": "You may activate the loyalty abilities of planeswalkers you control twice each turn rather than only once.",
            },
        ],
    )
    plan = GamePlan(turn_plan=[{"turn": "T", "hold": "Use +1 then -2 this turn."}])
    validate_plan(plan, assess(state), state)
    assert "+1 then -2" in plan.turn_plan[0]["hold"]


def test_tactical_prompt_does_not_offer_a_second_activation_after_arena_removes_it():
    state = dict(
        STATE,
        turn={"turn_number": 7, "active_player": 1, "phase": "Main1"},
        _bridge_actions=[{"instanceId": 3, "actionType": "ActionType_Activate"}],
    )
    assert "one loyalty activation" in loyalty_strategy_context(state)
    assert "after combat for up to 4 damage" not in loyalty_strategy_context(state)
