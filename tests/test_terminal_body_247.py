"""The retained Odin #247 grant must survive normalization and cache boundaries."""

from copy import deepcopy

import pytest

from arenamcp.board_assessment import _body, _signature, assess
from arenamcp.board_model import build_board_model
from arenamcp.combat_solver import _resolve_attacker
from arenamcp.line_search_moves import Moves
from test_combat_loss_281 import board as snapshot
from test_terminal_combat_hit_247 import ODIN, creature


@pytest.mark.parametrize(
    "evidence",
    [
        {"oracle_text": ODIN, "counters": {"Lore": 2}},
        {"ability_grp_ids": [19443]},
        {"granted_abilities": [19443]},
    ],
)
def test_odin_terminal_evidence_survives_body_normalization(evidence):
    odin = creature(372, 5, 3, 2, **evidence)
    assert _resolve_attacker(odin, []).player_loses
    body = _body(odin, 11, {})
    assert _resolve_attacker(body, []).player_loses


@pytest.mark.parametrize("kind", ["lore", "ability_grp_ids", "granted_abilities"])
def test_odin_grant_changes_assessment_cache_key_and_can_be_removed(kind):
    odin = creature(372, 5, 3, 2, oracle_text=ODIN if kind == "lore" else "")
    before = snapshot(theirs=[odin])
    after = deepcopy(before)
    active = after["battlefield"][0]
    if kind == "lore":
        before["battlefield"][0]["counters"] = {"Lore": 1}
        active["counters"] = {"Lore": 2}
    else:
        active[kind] = [19443]
    assert _signature(before) != _signature(after)
    assert assess(before) is not assess(after)
    assert not _resolve_attacker(_body(before["battlefield"][0], 11, {}), []).player_loses
    assert _resolve_attacker(_body(active, 11, {}), []).player_loses
    assert _signature(deepcopy(before)) == _signature(before)


def test_pending_odin_attack_kills_in_search_even_at_twenty_life():
    odin = creature(372, 5, 3, 2, oracle_text=ODIN, counters={"Lore": 2}, is_attacking=True)
    state = snapshot(theirs=[odin], active=2)
    state["players"][0]["life_total"] = 20
    state["turn"].update(turn_number=11, phase="Phase_Combat", step="Step_DeclareBlockers")
    model = build_board_model(state)
    work = Moves(model, survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    roots = [node for _, node in work.roots()]
    assert roots and all(node.dead_at is not None for node in roots)
    assert all(node.our_life == 15 for node in roots)


def test_search_keeps_an_unprofitable_chump_against_active_odin():
    odin = creature(372, 5, 3, 2, ability_grp_ids=[19443], is_attacking=True)
    chump = creature(102, 1, 1, 1)
    state = snapshot([chump], [odin], active=2)
    state["turn"].update(turn_number=11, phase="Phase_Combat", step="Step_DeclareBlockers")
    work = Moves(build_board_model(state), survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    roots = [node for _, node in work.roots() if not node.terminal]
    assert roots and all(node.our_life == 30 and not node.ours for node in roots)
