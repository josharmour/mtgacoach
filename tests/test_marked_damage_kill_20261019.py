"""Burn that finishes a damaged creature is a kill (ledger #109).

2026-10-19 12:31:48 (match e453dfa3 T16): Lightning Strike had put 3 damage on
a 4/5 Hearth Elemental; Iroh's Demonstration's 4 damage at it was judged
"kills no legal opposing target" because damage_would_kill answered None for
any damaged creature, and the target was declined.
"""

from arenamcp.play_safety import damage_would_kill


def _elemental(**extra):
    return dict({"name": "Hearth Elemental", "power": 4, "toughness": 5}, **extra)


def test_marked_damage_makes_a_smaller_burn_lethal():
    assert damage_would_kill(_elemental(damaged_this_turn=True, damage=3), 4) is True
    assert damage_would_kill(_elemental(damaged_this_turn=True, damage=3), 2) is True


def test_marked_damage_that_still_leaves_it_alive_is_a_no():
    assert damage_would_kill(_elemental(damaged_this_turn=True, damage=1), 3) is False


def test_unknown_marked_damage_stays_unknown():
    assert damage_would_kill(_elemental(damaged_this_turn=True), 3) is None


def test_combat_still_makes_it_unknown():
    assert damage_would_kill(_elemental(is_attacking=True, damaged_this_turn=True, damage=3), 1) is None


def test_undamaged_creature_unchanged():
    assert damage_would_kill(_elemental(), 4) is False
    assert damage_would_kill(_elemental(), 5) is True
