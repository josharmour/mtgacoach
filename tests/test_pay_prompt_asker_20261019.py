"""The pay prompt names who asks for the payment and that the kicker was already chosen (ledger #104).

2026-10-19 12:23:49 (match 5d5d32f8 T21): their Spell Pierce ("Counter target
noncreature spell unless its controller pays {2}") asked {2} for our Burst
Lightning, lethal with the opponent at 2. The kicker had been declined at the
cast (12:23:43, "paying 5 total for the kicker wastes mana"). The pay prompt
read "Optional cost from: Burst Lightning ... Should you pay this optional
cost?"; the model answered "paying 4 for the kicker wastes mana", declined,
and the Burst was countered. A cost on our own spell no longer reaches the
model (#93), but the request can name their Spell Pierce instead, and taxes
on our permanents and abilities still go to the model.
"""

from __future__ import annotations

from types import SimpleNamespace

from arenamcp.action_planner import ActionPlanner
from arenamcp.ward import pay_decision_facts, tax_asker_fact

LOCAL, OPP = 1, 2
PIERCE_TEXT = "Counter target noncreature spell unless its controller pays {2}."
BURST_TEXT = "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead."
LINE = (
    "Spell Pierce (their spell) is resolving: it counters Burst Lightning unless you pay {2}; "
    "declining = Burst Lightning is countered. This is not an optional extra of Burst Lightning."
)


def _mountain(iid: int) -> dict:
    return {
        "instance_id": iid,
        "name": "Mountain",
        "type_line": "Basic Land — Mountain",
        "card_types": ["CardType_Land"],
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
    }


BURST = {
    "instance_id": 390,
    "name": "Burst Lightning",
    "type_line": "Instant",
    "oracle_text": BURST_TEXT,
    "mana_cost": "{R}",
    "object_kind": "CARD",
    "controller_seat_id": LOCAL,
    "targeting": [2],
}
PIERCE = {
    "instance_id": 395,
    "name": "Spell Pierce",
    "type_line": "Instant",
    "oracle_text": PIERCE_TEXT,
    "mana_cost": "{U}",
    "object_kind": "CARD",
    "controller_seat_id": OPP,
    "targeting": [390],
}


def _state(*, source_id: int = 390, stack=(BURST, PIERCE), battlefield=()) -> dict:
    return {
        "match_id": "5d5d32f8",
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 21, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": LOCAL, "life_total": 9, "is_local": True},
            {"seat_id": OPP, "life_total": 2, "is_local": False},
        ],
        "battlefield": [_mountain(279 + n) for n in range(4)] + list(battlefield),
        "hand": [],
        "stack": [dict(item) for item in stack],
        "pending_decision": "Pay Costs",
        "decision_context": {
            "type": "pay_costs",
            "source_id": source_id,
            "source_card": "Burst Lightning",
            "mana_requirements": {"generic": 2},
        },
    }


def test_the_request_on_our_burst_names_spell_pierce():
    assert tax_asker_fact(_state()) == LINE
    facts = pay_decision_facts(_state())
    assert facts[0].startswith("Cost to pay now: {2}") and facts[1] == LINE


def test_the_request_on_their_spell_pierce_names_what_it_counters():
    assert tax_asker_fact(_state(source_id=395)) == LINE


def test_without_targets_seen_their_top_counter_unless_pays_is_the_asker():
    pierce = dict(PIERCE, targeting=[])
    assert tax_asker_fact(_state(stack=(BURST, pierce))) == LINE


def test_a_tax_on_our_permanent():
    bears = {
        "instance_id": 500,
        "name": "Grizzly Bears",
        "type_line": "Creature — Bear",
        "power": 2,
        "toughness": 2,
        "controller_seat_id": LOCAL,
    }
    tithe = {
        "instance_id": 396,
        "name": "Ruthless Tithe",
        "type_line": "Sorcery",
        "oracle_text": "Destroy target creature unless its controller pays {3}.",
        "controller_seat_id": OPP,
        "targeting": [500],
    }
    state = _state(source_id=500, stack=(tithe,), battlefield=(bears,))
    state["decision_context"]["mana_requirements"] = {"generic": 3}
    assert tax_asker_fact(state) == (
        "Ruthless Tithe (their spell) is resolving: it destroys Grizzly Bears unless you pay {3}; "
        "declining = Grizzly Bears is destroyed. This is not an optional extra of Grizzly Bears."
    )


def test_no_line_without_an_opposing_demand():
    assert tax_asker_fact(_state(stack=(BURST,))) == ""
    # Their spell on the stack that is not an "unless you pay" effect, aimed elsewhere.
    shock = dict(PIERCE, name="Shock", oracle_text="Shock deals 2 damage to any target.", targeting=[2])
    assert tax_asker_fact(_state(stack=(BURST, shock))) == ""
    # Their spell targets another of our spells, not the one charged.
    assert tax_asker_fact(_state(stack=(BURST, dict(PIERCE, targeting=[391])))) == ""


def _planner(prompts: list[str]) -> ActionPlanner:
    class _Backend:
        def complete(self, system, user, *args, **kwargs):
            prompts.append(user)
            return '{"pay": true, "reasoning": "keep the lethal Burst"}'

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._timeout = 1.0
    planner._backend = _Backend()
    return planner


def test_the_pay_prompt_names_the_asker_and_the_answered_kicker():
    prompts: list[str] = []
    planner = _planner(prompts)
    state = _state(source_id=395)
    kicker_step = SimpleNamespace(
        request_type="CastingTimeOptions",
        source_label="Burst Lightning",
        options=[SimpleNamespace(option_id="idx:0", label="Kicker"), SimpleNamespace(option_id="idx:1", label="Done")],
    )
    planner._memory().record(state, kicker_step, ["idx:1"], "the base 2 is already lethal")
    assert planner.plan_pay_or_decline("Spell Pierce", PIERCE_TEXT, state) is True
    assert LINE in prompts[0]
    assert (
        "The kicker of Burst Lightning was already answered when it was cast (not kicked): "
        "this cost is not its kicker." in prompts[0]
    )


def test_no_kicker_line_without_a_kicker_step_this_turn():
    prompts: list[str] = []
    planner = _planner(prompts)
    state = _state()
    modal = SimpleNamespace(
        request_type="CastingTimeOptions",
        source_label="Burst Lightning",
        options=[SimpleNamespace(option_id="idx:0", label="Mode 0"), SimpleNamespace(option_id="idx:1", label="Done")],
    )
    planner._memory().record(state, modal, ["idx:0"], "")
    assert planner.plan_pay_or_decline("Burst Lightning", BURST_TEXT, state) is True
    assert "kicker of" not in prompts[0]
    assert "\n\n\n" not in prompts[0]
