"""A cost Arena charges to OUR spell on the stack is paid, however long ago we cast it.

2026-10-19 match 5d5d32f8 (ledger #93): their Spell Pierce ("counter target
noncreature spell unless its controller pays {2}") asked {2} for our Burst
Lightning: PayCostsReq prompt 11 "Cost o2", manaCost [{Generic, 2, objectId:
<our Burst>}], Auto Pay offered. At T11 (16.5 s after the cast, outside the
own-play window) the gate asked the model, which answered nothing -> declined;
at T21 the model read the {2} as the kicker -> declined, and the lethal Burst
(opponent at 2) was countered.
"""

from tests.fakes import make_engine


class _DeclinePlanner:
    def plan_pay_or_decline(self, name, oracle, game_state):
        return False


BURST = {
    "instance_id": 391,
    "name": "Burst Lightning",
    "oracle_text": "Kicker {4}. Burst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead.",
    "type_line": "Instant",
    "object_kind": "CARD",
    "owner_seat_id": 2,
    "controller_seat_id": 2,
}
PIERCE = {
    "instance_id": 393,
    "name": "Spell Pierce",
    "oracle_text": "Counter target noncreature spell unless its controller pays {2}.",
    "type_line": "Instant",
    "object_kind": "CARD",
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}


def _state(stack):
    return {
        "turn": {"turn_number": 21},
        "_bridge_can_cancel": True,
        "decision_context": {"type": "pay_costs", "source_card": "Burst Lightning", "source_id": 391},
        "stack": stack,
        "battlefield": [],
        "local_seat_id": 2,
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
    }


def test_spell_pierce_tax_on_our_burst_is_paid_without_asking_the_model():
    engine = make_engine(planner=_DeclinePlanner())
    assert engine._should_decline_optional_cost(_state([PIERCE, BURST])) is None


def test_a_cost_on_their_spell_still_goes_through_the_gate():
    engine = make_engine(planner=_DeclinePlanner())
    state = _state([dict(BURST, controller_seat_id=1, owner_seat_id=1)])
    assert engine._cost_on_our_spell(state) is False


def test_a_cost_on_our_triggered_ability_is_not_treated_as_our_spell():
    engine = make_engine(planner=_DeclinePlanner())
    trigger = dict(BURST, object_kind="ABILITY", name="Go-Shintai of Hidden Cruelty")
    assert engine._cost_on_our_spell(_state([trigger])) is False
