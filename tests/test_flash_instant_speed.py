"""Flash creatures are instant-speed plays (2026-10-18 pro games, PremierDraft_YFRA).

The pro flashed Teyo, Lightshield Expert in three of five matches: during the
opponent's attack to shield Garruk (match 1 turn 10), after blocks as a +1/+1
trick (match 2 turn 7) and in response to Silence the Echo (match 3 turn 12).
Each test uses the real cards' Oracle text.
"""

from __future__ import annotations

from tests.strategic_states import state

from arenamcp.board_model import build_board_model
from arenamcp.coach_triggers import GameStateTrigger
from arenamcp.line_search_moves import Moves, _bodies, enters_counters
from arenamcp.mcts_evaluator import MCTSEvaluator

TEYO = {
    "name": "Teyo, Lightshield Expert",
    "type_line": "Legendary Creature — Human Cleric",
    "mana_cost": "{1}{W}",
    "power": 1,
    "toughness": 1,
    "oracle_text": "Flash\nWhen Teyo enters, target permanent you control gains hexproof until end of turn. "
    "Put a +1/+1 counter on it if it's a creature. Put a loyalty counter on it if it's a planeswalker. "
    "(It can't be the target of spells or abilities your opponents control.)",
    "card_types": ["CardType_Creature"],
}
# A sorcery with flashback: "flashback" is not the Flash keyword.
FLASHBACK_SORCERY = {
    "name": "Think Twice Sorcery",
    "type_line": "Sorcery",
    "mana_cost": "{1}{W}",
    "oracle_text": "Draw a card.\nFlashback {2}{W}",
    "card_types": ["CardType_Sorcery"],
}
PLAINS = {"name": "Plains", "type_line": "Basic Land — Plains", "card_types": ["CardType_Land"]}
FOREST = {"name": "Forest", "type_line": "Basic Land — Forest", "card_types": ["CardType_Land"]}


def _trigger_state(hand: list[dict], lands: list[dict]) -> dict:
    return {
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "battlefield": [
            {**land, "instance_id": 10 + i, "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": False}
            for i, land in enumerate(lands)
        ],
        "hand": hand,
    }


def test_flash_creature_with_its_colours_open_is_castable_at_instant_speed():
    detector = GameStateTrigger()
    assert detector._has_castable_instants(_trigger_state([TEYO], [PLAINS, FOREST]))


def test_flashback_is_not_flash():
    detector = GameStateTrigger()
    assert not detector._has_castable_instants(_trigger_state([FLASHBACK_SORCERY], [PLAINS, PLAINS, PLAINS]))


def test_instant_speed_needs_the_right_colours_not_just_enough_lands():
    detector = GameStateTrigger()
    assert not detector._has_castable_instants(_trigger_state([TEYO], [FOREST, FOREST]))
    assert not detector._has_castable_instants(_trigger_state([TEYO], [PLAINS]))


def _mcts_state(hand: list[dict]) -> dict:
    return {
        "local_seat_id": 2,
        "turn": {"turn_number": 9, "phase": "Phase_Main1", "active_player": 2, "priority_player": 2},
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": False},
            {"seat_id": 2, "life_total": 20, "is_local": True},
        ],
        "battlefield": [
            {**PLAINS, "instance_id": 101, "controller_seat_id": 2, "is_tapped": False},
            {**FOREST, "instance_id": 102, "controller_seat_id": 2, "is_tapped": False},
        ],
        "hand": hand,
    }


def _hold_branch(tree) -> bool:
    return any(b.action.startswith("Pass Priority (Hold") for b in tree.branches)


def test_holding_mana_for_a_flash_creature_is_an_option():
    # Match 1 turn 9: Garruk +2 untapped two lands, then the pro passed with 1W open for Teyo.
    assert _hold_branch(MCTSEvaluator.evaluate(_mcts_state([TEYO]), force=True))
    assert not _hold_branch(MCTSEvaluator.evaluate(_mcts_state([FLASHBACK_SORCERY]), force=True))


def test_teyo_can_put_its_counter_on_itself():
    assert enters_counters(TEYO) == 1
    other = {**TEYO, "oracle_text": "Flash\nWhen this creature enters, put a +1/+1 counter on another target "
             "creature you control."}  # fmt: skip
    assert enters_counters(other) == 0
    assert enters_counters({**TEYO, "oracle_text": "Flash\nFlying"}) == 0


def test_held_flash_creature_blocks_with_its_enters_counter():
    # Their turn, declare attackers: we hold 1W and Teyo; Teyo enters as a 2/2 blocker.
    source = state(
        turn=10, active=2, phase="Phase_Combat", step="Step_DeclareAttack", life={1: 20, 2: 20},
        lands_played={1: 1, 2: 1}, library=20, opponent_hand=2,
        battlefield=[(10, "Plains", 1, False, 1), (11, "Plains", 1, False, 1), (30, "Cadet", 2, False, 5)],
        hand=[], graveyard=[],
    )  # fmt: skip
    source["hand"] = [{**TEYO, "instance_id": 40, "owner_seat_id": 1, "controller_seat_id": 1}]
    model = build_board_model(source)
    assert model is not None
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None)
    teyo = next(i for i, hs in enumerate(moves.spells) if hs.name == "Teyo, Lightshield Expert")
    _removal, flash, _left = moves._held((teyo,), (list(model.sources_now), 0, ()), 20)
    bodies = [b for v in flash for b in _bodies(v)]
    assert [(b["power"], b["toughness"]) for b in bodies] == [(2, 2)]
