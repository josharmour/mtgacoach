"""Supplied infect reproduction and permanent combat counter transitions."""

from copy import deepcopy

import pytest

from arenamcp.combat_infect import apply_deltas, counter_change
from arenamcp.combat_loss import LossState
from arenamcp.combat_solver import _resolve_attacker


def body(i, p, t, text="", **more):
    return dict(
        instance_id=i,
        name=f"Creature {i}",
        type_line="Creature",
        power=p,
        toughness=t,
        oracle_text=text,
        _can_block=True,
        _can_attack=True,
        _unblockable=False,
        **more,
    )


def test_supplied_first_strike_infect_shrinks_blocker_before_return_damage():
    attacker, blocker = body(1, 2, 2, "Infect\nFirst strike"), body(2, 3, 3)
    before = deepcopy([attacker, blocker])
    outcome = _resolve_attacker(attacker, [blocker])
    assert not outcome.attacker_died and not outcome.blockers_died
    assert outcome.counter_deltas == {2: 2}
    changed = apply_deltas([blocker], outcome.counter_deltas)[0]
    assert (changed["power"], changed["toughness"]) == (1, 1)
    assert [attacker, blocker] == before


def test_infect_blocker_shrinks_first_strike_attacker_before_regular_strike():
    outcome = _resolve_attacker(body(1, 3, 3), [body(2, 2, 2, "Infect\nFirst strike")])
    assert outcome.counter_deltas == {1: 2}
    assert not outcome.attacker_died and not outcome.blockers_died


def test_simultaneous_infect_does_not_reduce_same_step_return_damage():
    outcome = _resolve_attacker(body(1, 2, 2, "Infect"), [body(2, 3, 3)])
    assert outcome.attacker_died and outcome.counter_deltas == {2: 2}


def test_double_strike_uses_changed_toughness_and_real_lifelink():
    outcome = _resolve_attacker(body(1, 2, 4, "Infect\nDouble strike\nLifelink"), [body(2, 5, 5)])
    assert outcome.counter_deltas == {2: 4} and outcome.life_gained == 4
    assert not outcome.attacker_died and not outcome.blockers_died


@pytest.mark.parametrize(
    "trample,expected,poison",
    [
        (True, {2: 2, 3: 3}, 2),
        (False, {2: 2, 3: 5}, 0),
    ],
)
def test_infect_allocation_multiple_blockers_overassignment_and_trample(trample, expected, poison):
    attacker = body(1, 7, 8, "Infect\nLifelink" + ("\nTrample" if trample else ""))
    outcome = _resolve_attacker(attacker, [body(2, 2, 2), body(3, 3, 3)])
    assert outcome.counter_deltas == expected
    assert outcome.poison_given == poison and outcome.damage_through == 0
    assert outcome.life_gained == 7 and len(outcome.blockers_died) == 2


def test_zero_toughness_kills_indestructible_but_prevention_stops_counters():
    attacker = body(1, 3, 3, "Infect\nFirst strike", colors=["R"])
    blocker = body(2, 2, 2, "Indestructible")
    assert _resolve_attacker(attacker, [blocker]).blockers_died == [blocker]
    blocker["oracle_text"] += "\nProtection from red"
    fight = _resolve_attacker(attacker, [blocker])
    assert not fight.counter_deltas and not fight.blockers_died


def test_protection_prevents_blocker_infect_and_its_lifelink():
    attacker = body(1, 3, 3, "Protection from green")
    blocker = body(2, 2, 4, "Infect\nLifelink", colors=["G"])
    fight = _resolve_attacker(attacker, [blocker])
    assert fight.counter_deltas == {} and fight.blocker_life_gained == 0


def test_deathtouch_infect_is_lethal_damage_without_zero_toughness():
    attacker = body(1, 1, 5, "Infect\nDeathtouch")
    blocker = body(2, 2, 8)
    assert _resolve_attacker(attacker, [blocker]).blockers_died == [blocker]
    blocker["oracle_text"] = "Indestructible"
    assert not _resolve_attacker(attacker, [blocker]).blockers_died


def test_killed_infect_source_does_not_deal_regular_damage():
    outcome = _resolve_attacker(body(1, 3, 3, "First strike"), [body(2, 2, 2, "Infect")])
    assert not outcome.counter_deltas


def test_inactive_infect_grant_is_not_treated_as_infect():
    outcome = _resolve_attacker(
        body(1, 2, 2, "{B}: This creature gains infect until end of turn."), [body(2, 3, 3)]
    )
    assert not outcome.counter_deltas and outcome.attacker_died


def test_observed_stats_are_not_reduced_twice_and_opposite_counters_cancel():
    existing = body(2, 4, 4, counters={"CounterType_M1M1": 1})
    result = counter_change(existing, minus=2)
    assert result["power"] == 2 and result["counters"] == {"CounterType_M1M1": 3}
    plus = body(3, 4, 4, counters={"CounterType_P1P1": 2})
    result = counter_change(plus, minus=1)
    assert result["power"] == 3 and result["counters"] == {"CounterType_P1P1": 1}
    result = counter_change(result, minus=2)
    assert result["power"] == 1 and result["counters"] == {"CounterType_M1M1": 1}
    result = counter_change(result, plus=2)
    assert result["power"] == 3 and result["counters"] == {"CounterType_P1P1": 1}


def test_extra_combat_sees_shrunk_survivor_and_combines_deltas():
    from arenamcp.attack_potential import ExtraCombat, attack_round
    from arenamcp.board_assessment import _combat_life

    attacker = body(1, 2, 3, "Infect\nFirst strike", _atk_extra=ExtraCombat(1, "Fixture", True))
    blocker = body(2, 4, 4)
    seen = []

    def once(a, b, life):
        seen.append([(c["power"], c["toughness"]) for c in b])
        return _combat_life(a, b, life, defender=LossState(poison=9))

    result = attack_round([attacker], [blocker], 30, once)
    assert seen == [[(4, 4)], [(2, 2)]]
    assert result[2] == {2} and result.counter_deltas == {2: 4}


def moves_for(ours, theirs, **state_fields):
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search_moves import Moves
    from test_combat_loss_281 import board

    state = board(ours, theirs, **state_fields)
    return Moves(
        build_board_model(state), survival=False, lethal_now=False, hard_ms=1e8, opponent=None
    ), state


def test_counter_changes_persist_across_turns_and_memo_contexts():
    moves, state = moves_for([body(1, 2, 2, "Infect\nFirst strike")], [body(2, 3, 3)], opp_poison=9)
    original = deepcopy(state)
    root = moves.root_node()
    first = moves.finish(moves.begin(root, None, ()), ("all", (1,)))
    assert len(first.theirs) == 1 and first.theirs[0]["power"] == 1
    assert first.theirs[0]["_card"]["counters"] == {"CounterType_M1M1": 2}
    second = moves.finish(moves.begin(first, None, ()), ("all", (1,)))
    assert not second.theirs
    assert state == original


def test_pending_opponent_infect_attack_carries_our_shrink_into_next_turn():
    attacker = body(2, 2, 2, "Infect\nFirst strike", is_attacking=True)
    moves, _ = moves_for([body(1, 3, 3)], [attacker], our_poison=9, active=2)
    roots = [node for _, node in moves.roots() if not node.terminal]
    assert roots and any(node.ours[0]["power"] == 1 for node in roots if node.ours)


def test_cast_pump_expiration_keeps_counters_and_zero_toughness_dies():
    moves, _ = moves_for([body(1, 2, 2)], [body(2, 3, 3, "Infect\nFirst strike")])
    root = moves.root_node()
    mid = moves.begin(root, None, ())
    mid.pumps = {1: (2, 2)}
    after = moves.finish(mid, ("all", (1,)))
    assert not after.ours


def test_planeswalker_combat_also_carries_surviving_infect_counters():
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search_moves import Moves
    from test_combat_loss_281 import board

    state = board([body(1, 2, 2, "Infect\nFirst strike")], [body(2, 3, 3)])
    state["battlefield"].append(
        dict(
            instance_id=88,
            name="Walker",
            type_line="Planeswalker",
            loyalty=1,
            controller_seat_id=2,
            owner_seat_id=2,
        )
    )
    moves = Moves(build_board_model(state), survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    after = moves.finish(moves.begin(moves.root_node(), None, ()), ("at:88:1", (1,)))
    assert after.theirs[0]["power"] == 1 and after.opponent_loss.poison == 0


def test_attack_score_crackback_uses_permanent_shrink():
    from arenamcp.combat_solver import evaluate_attack
    from test_combat_loss_281 import board

    attacker, blocker = body(1, 2, 2, "Infect\nFirst strike"), body(2, 3, 3)
    state = board([attacker], [blocker], opp_poison=9)
    plan = evaluate_attack([attacker], [], [blocker], 20, 20, [blocker], state=state)
    assert plan.worst_case_crackback == 1


def test_anthem_cache_does_not_restore_infect_stats():
    moves, _ = moves_for([], [body(2, 4, 4)])
    original = moves.root_node().theirs[0]
    moves.their_anthems = [(88, False, (7, 2, 2, "all"))]
    full = moves.anthem_bodies([original], 30)[0]
    shrunk = moves.anthem_bodies([counter_change(original, minus=1)], 30)[0]
    assert shrunk["power"] == full["power"] - 1
    assert shrunk["counters"] == {"CounterType_M1M1": 1}


def test_rescale_cache_keeps_counter_history_at_equal_effective_stats():
    moves, _ = moves_for([body(1, 3, 3)], [])
    original = moves.root_node().ours[0]
    moves._scale_base = {1: (1, 1, 1, 1, False)}
    first = moves.rescale([original])[0]
    shrunk = counter_change(original, minus=1)
    second = moves.rescale([shrunk, body(3, 1, 1)])[0]
    assert second["power"] == first["power"] == 2
    assert second["_counter_shift"] == -1
    assert moves.rescale([second])[0]["power"] == 1


def test_extra_combat_retains_alternate_loss_metadata():
    from arenamcp.attack_potential import ExtraCombat, attack_round
    from arenamcp.combat_loss import CombatDetail

    attacker = body(1, 2, 3, "Infect", _atk_extra=ExtraCombat(1, "Fixture", True))
    results = iter(
        [
            CombatDetail((0, set(), set(), 0, 0), LossState(poison=8)),
            CombatDetail((0, set(), set(), 0, 0), LossState(poison=10), True),
        ]
    )
    result = attack_round([attacker], [], 20, lambda *_: next(results))
    assert result.terminal and result.loss.poison == 10


def test_counter_copy_keeps_cast_spell_identity_for_end_step_return():
    moves, _ = moves_for(
        [body(1, 3, 3, "At the beginning of your end step, return this creature to its owner's hand.")], []
    )
    original = moves.root_node().ours[0]
    moves._body_spell[id(original)] = 77
    changed = moves._combat_counters([original], {1: 1})[0]
    assert changed["power"] == 2 and original["power"] == 3
    mid = moves.begin(moves.root_node(), None, ())
    mid.ours = [changed]
    moves._end_step(mid)
    assert mid.hand == [77] and not mid.ours
