"""Marked ordinary damage persists between same-turn extra combats."""

from copy import deepcopy

from arenamcp.attack_potential import ExtraCombat, attack_round
from arenamcp.board_assessment import _combat_life
from arenamcp.combat_solver import _resolve_attacker
from test_infect_counters_281 import body, moves_for


def extra(body):
    return {**body, "_atk_extra": ExtraCombat(body["instance_id"], "Fixture", True)}


def test_two_nonlethal_combats_accumulate_to_lethal_on_both_creatures():
    attackers, blockers = [extra(body(1, 2, 3))], [body(2, 2, 3)]
    before = deepcopy((attackers, blockers))
    result = attack_round(attackers, blockers, 2, _combat_life)
    assert result[1] == {1} and result[2] == {2}
    assert result.damage_deltas == {1: 4, 2: 4}
    assert (attackers, blockers) == before


def test_first_strike_kills_wounded_blocker_before_second_return_strike():
    result = attack_round([extra(body(1, 3, 5, "First strike"))], [body(2, 2, 4)], 3, _combat_life)
    assert result[1] == set() and result[2] == {2}
    assert result.damage_deltas[1] == 2


def test_trample_assigns_only_remaining_lethal_before_extra_combat_spill():
    result = attack_round([extra(body(1, 3, 8, "Trample\nLifelink"))], [body(2, 1, 5)], 3, _combat_life)
    assert result[0] == 1 and result[2] == {2}
    assert result[5] == 6 and result.damage_deltas[2] == 5


def test_prevented_damage_does_not_leave_marks_or_gain_life():
    result = attack_round([extra(body(1, 3, 8, "Lifelink", colors=["R"]))],
                          [body(2, 1, 4, "Protection from red")], 3, _combat_life)
    assert result[2] == set() and result[5] == 0
    assert result.damage_deltas == {1: 2}


def test_indestructible_survives_cumulative_lethal_damage():
    result = attack_round([extra(body(1, 2, 3, "Indestructible"))],
                          [body(2, 2, 3, "Indestructible")], 2, _combat_life)
    assert result[1] == result[2] == set()
    assert result.damage_deltas == {1: 4, 2: 4}


def test_combat_memo_distinguishes_wounded_bodies_and_tracking_requests():
    moves, _ = moves_for([body(1, 2, 3)], [body(2, 2, 3)])
    a, b = moves.root_node().ours[0], moves.root_node().theirs[0]
    ordinary = moves.combat_detail([a], [b], 2)
    tracking = moves.combat_detail([{**a, "_track_combat_damage": True}], [b], 2)
    wounded = moves.combat_detail([{**a, "_combat_damage": 2}], [{**b, "_combat_damage": 2}], 2)
    assert not ordinary.damage_deltas and tracking.damage_deltas == {1: 2, 2: 2}
    assert not ordinary[1] and wounded[1] == frozenset({1}) and wounded[2] == frozenset({2})


def test_extra_combat_marks_do_not_persist_into_next_turn():
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search_moves import Moves
    from test_combat_loss_281 import board

    state = board([body(1, 1, 8)], [body(2, 2, 8)], active=2)
    state["players"][0]["life_total"] = 2
    model = build_board_model(state)
    model.theirs[0]["_atk_extra"] = ExtraCombat(2, "Fixture", True)
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    roots = [node for _, node in moves.roots() if not node.terminal]
    assert roots
    for node in roots:
        assert node.ours and node.theirs
        for card in (*node.ours, *node.theirs):
            assert not card.get("_combat_damage") and not card.get("_track_combat_damage")


def test_infect_and_ordinary_damage_remain_separate():
    fight = _resolve_attacker(body(1, 2, 5, "Infect"), [body(2, 2, 5)])
    assert fight.counter_deltas == {2: 2}
    assert fight.damage_deltas == {1: 2}
