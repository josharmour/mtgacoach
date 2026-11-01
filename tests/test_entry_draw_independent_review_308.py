import pytest
from tests.test_entry_draw_transition_308 import army_factory, bowmasters, card, innocence, recruit, run

from arenamcp.sacrifice_continuation import UnsupportedContinuation


def test_opposing_trigger_doubler_is_not_silently_ignored():
    doubler = card(
        820,
        "Test Opposing Doubler",
        "Triggered abilities of creatures you control trigger an additional time.",
        "Enchantment",
        "{2}",
        seat=2,
    )
    with pytest.raises(UnsupportedContinuation):
        run([innocence(), recruit()], [bowmasters()], new_army=army_factory(), their_permanents=[doubler])


def test_opposing_army_entry_payoff_is_not_silently_ignored():
    payoff = card(
        821,
        "Test Army Entry Payoff",
        "Whenever a creature enters under your control, you gain 1 life.",
        "Enchantment",
        "{2}",
        seat=2,
    )
    with pytest.raises(UnsupportedContinuation):
        run([innocence(), recruit()], [bowmasters()], new_army=army_factory(), their_permanents=[payoff])
