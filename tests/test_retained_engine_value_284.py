"""#270/#307: count the body loss separately from the retained enchantment."""

from copy import deepcopy

import pytest

from arenamcp.combat_solver import _death_material, _material, _rebuild_block_plan, optimal_blocks
from arenamcp.death_triggers import annotate_death_swings
from arenamcp.persistent_returns import return_credit
from test_combat_persistence_284 import innocence
from test_infect_counters_281 import body, moves_for


def engine(**changes):
    return innocence(controller_seat_id=1, owner_seat_id=1, **changes)


@pytest.mark.parametrize("budget", [1, 50000])
def test_retained_draw_engine_makes_safe_equal_body_trade_affordable(budget):
    attacker = body(3, 2, 2)
    blocker = engine(power=2, toughness=2)
    blocker["oracle_text"] = blocker["oracle_text"].replace("Lifelink\n", "")
    plan = optimal_blocks([attacker], [blocker], 21, max_options=budget)
    assert plan.assignments == {275: 3}
    assert plan.blockers_lost_material == plan.attackers_killed_material == 6
    # The draw engine without its return still deserves protection.
    gone = {**blocker, "oracle_text": blocker["oracle_text"].split("When Enduring Innocence dies")[0]}
    assert optimal_blocks([attacker], [gone], 21, max_options=budget).assignments == {}


def test_rebuild_and_attacker_deaths_use_the_same_residual_credit():
    attacker, blocker = engine(power=2, toughness=2), body(3, 2, 2)
    plan = _rebuild_block_plan([attacker], [blocker], (0,), 21)
    assert plan.attackers_killed_material == _material(attacker) - 3
    assert plan.blockers_lost_material == 6


@pytest.mark.parametrize(
    "change",
    [
        {"is_token": True},
        {"object_kind": "TOKEN"},
        {"is_copy": True},
        {"card_types": ["Enchantment"]},
        {"owner_seat_id": None},
    ],
)
def test_no_speculative_return_credit_for_ineligible_or_unknown_identity(change):
    card = {**engine(), **change}
    assert return_credit(card) == 0
    assert _death_material(card) == _material(card)


def test_stolen_engine_returns_value_to_opponent_instead_of_retaining_it():
    card = {**engine(), "owner_seat_id": 2}
    assert return_credit(card) == -3
    assert _death_material(card) == _material(card) + 3


@pytest.mark.parametrize(
    "oracle",
    [
        "Creatures entering the battlefield or dying don't cause abilities to trigger.",
        "If a creature would die, exile it instead.",
        "If a card or token would be put into a graveyard from anywhere, exile it instead.",
    ],
)
def test_visible_interference_blocks_credit_and_refresh_invalidates_memo(oracle):
    attacker, blocker = body(3, 2, 2), engine(power=2, toughness=2)
    blocker["oracle_text"] = blocker["oracle_text"].replace("Lifelink\n", "")
    interference = dict(instance_id=90, oracle_text=oracle)
    annotate_death_swings([attacker, blocker])
    assert optimal_blocks([attacker], [blocker], 21).assignments
    annotate_death_swings([attacker, blocker, interference])
    assert not optimal_blocks([attacker], [blocker], 21).assignments
    assert _death_material(blocker) == _material(blocker)
    interference["is_phased_out"] = True
    annotate_death_swings([attacker, blocker, interference])
    assert optimal_blocks([attacker], [blocker], 21).assignments


def test_our_dead_attackers_retain_owned_engine_in_search_value():
    moves, state = moves_for([engine()], [body(2, 3, 3)])
    before = deepcopy(state)
    node = moves.finish(moves.begin(moves.root_node(), None, ()), ("all", (275,)))
    assert not node.ours and len(node.persistent) == 1
    assert node.persistent[0]["controller_seat_id"] == 1
    assert node.persistent[0]["card_types"] == ["CardType_Enchantment"]
    assert moves.value(20, 20, (), (), (), persistent=node.persistent) == moves.value(20, 20, (), (), ()) + 3
    assert state == before


def test_our_dead_blockers_retain_owned_engine_in_next_node():
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search_moves import Moves

    attacker = body(2, 4, 5, is_attacking=True)
    _, state = moves_for([engine()], [attacker], active=2)
    # At low life a block is mandatory; independent of historical hidden responses.
    state["players"][0]["life_total"] = 4
    moves = Moves(build_board_model(state), survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    nodes = [n for _, n in moves.roots() if not n.terminal]
    assert nodes and any(not n.ours and len(n.persistent) == 1 for n in nodes)


def test_stolen_enemy_return_is_valued_for_its_owner():
    moves, _ = moves_for([], [body(2, 2, 2)])
    ours = {**engine(), "power": None, "toughness": None, "card_types": ["Enchantment"]}
    theirs = {**ours, "controller_seat_id": 2, "owner_seat_id": 2}
    baseline = moves.value(20, 20, (), (), ())
    assert moves.value(20, 20, (), (), (), persistent=(ours,)) == baseline + 3
    assert moves.value(20, 20, (), (), (), persistent=(theirs,)) == baseline - 3


def test_return_credit_never_makes_engine_body_free():
    card = engine(power=8, toughness=8)
    assert _death_material(card) == 18
    assert _material(card) == 21
