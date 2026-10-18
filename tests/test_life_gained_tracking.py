"""Life gained this turn / this game (pro YFRA games, 2026-10-18).

Match 1 turn 11: the pro cast Something Worth Saving (gain 1 life) before
Germinate Recruits ("Create X 2/2 ... Cadet, where X is the amount of life
you gained this turn") and got one Cadet. The planner had no life-gained
counter and read Germinate's tokens as unknown (worth nothing).
"""

from __future__ import annotations

from copy import deepcopy

from tests.pro_yfra_states import M1_T11, board

from arenamcp.board_model import build_board_model
from arenamcp.gamestate import GameState
from arenamcp.line_search import search_lines
from arenamcp.line_search_moves import token_specs, unmodelled_effect

GERMINATE = (
    "create x 2/2 colorless wizard soldier creature tokens named cadet, where x is the amount of life "
    "you gained this turn."
)
FLOURISHING = (
    "create a 2/2 colorless wizard soldier creature token named cadet. for every 3 life you gained this "
    "game, create an additional one of those tokens."
)


# --- the log-derived counter ------------------------------------------------


def _life_annotation(ann_id: int, seat: int, delta: int) -> dict:
    return {
        "id": ann_id,
        "affectorId": 279,
        "affectedIds": [seat],
        "type": ["AnnotationType_ModifiedLife"],
        "details": [{"key": "life", "type": "KeyValuePairValueType_int32", "valueInt32": [delta]}],
    }


def _message(turn: int, annotations=(), life=None) -> dict:
    players = [{"seatId": 2, "systemSeatNumber": 2, **({"lifeTotal": life} if life else {})}]
    return {
        "type": "GameStateType_Diff",
        "turnInfo": {"turnNumber": turn, "activePlayer": 2, "phase": "Phase_Main1"},
        "players": players,
        "annotations": list(annotations),
    }


def test_modified_life_gains_count_per_turn_and_game():
    gs = GameState()
    gs.update_from_message(_message(11, life=19))
    # Match 1 T11: "life me [1] (src Something Worth Saving)" (annotation 738)
    gs.update_from_message(_message(11, [_life_annotation(738, 2, 1)], life=20))
    # The same annotation id seen again is not counted twice; a loss is not a gain.
    gs.update_from_message(_message(11, [_life_annotation(738, 2, 1), _life_annotation(740, 2, -2)]))
    player = gs.players[2]
    assert (player.life_gained_this_turn, player.life_gained_this_game) == (1, 1)
    assert player.to_dict()["life_gained_this_turn"] == 1

    gs.update_from_message(_message(12))
    assert (gs.players[2].life_gained_this_turn, gs.players[2].life_gained_this_game) == (0, 1)
    gs.update_from_message(_message(13, [_life_annotation(801, 2, 3)]))
    assert (gs.players[2].life_gained_this_turn, gs.players[2].life_gained_this_game) == (3, 4)


# --- token counts -----------------------------------------------------------


def test_life_scaled_token_count_reads_life_gained():
    assert token_specs(GERMINATE) is None  # unknown without the counter, as before
    assert token_specs(GERMINATE, life_gained=0) == []
    assert token_specs(GERMINATE, life_gained=2) == [(2, 2, 2, (), "Cadet")]


def test_flourishing_forces_counts_life_gained_this_game():
    assert token_specs(FLOURISHING) == [(1, 2, 2, (), "Cadet")]
    assert token_specs(FLOURISHING, life_gained_game=7) == [(3, 2, 2, (), "Cadet")]


def test_germinate_recruits_is_modelled():
    card = {"name": "Germinate Recruits", "type_line": "Instant", "mana_cost": "{2}{W}",
            "oracle_text": GERMINATE.capitalize()}  # fmt: skip
    assert unmodelled_effect(card) == ""


# --- the planner: gain life first -------------------------------------------


def _first_step(source):
    model = build_board_model(deepcopy(source))
    assert model is not None
    result = search_lines(model, survival=False, lethal_now=False, hard_ms=1e9)
    return model, result.best.steps[0]


def test_pro_m1_t11_gains_life_before_germinate_recruits():
    _model, step = _first_step(M1_T11)
    assert step.casts == ("Something Worth Saving", "Germinate Recruits"), step


def test_life_gained_earlier_this_turn_sets_the_cadet_count():
    source = deepcopy(M1_T11)
    source["hand"] = [h for h in source["hand"] if h["name"] == "Germinate Recruits"]
    source["players"][1]["life_gained_this_turn"] = 2
    model = build_board_model(source)
    assert model.life_gained_turn == 2
    result = search_lines(model, survival=False, lethal_now=False, hard_ms=1e9)
    step = result.best.steps[0]
    assert step.casts == ("Germinate Recruits",), step


def test_board_helper_carries_counters():
    model = build_board_model(
        board(turn=3, phase="Phase_Main1", life={1: 20, 2: 20}, battlefield=[], hand=[],
              gained_turn=1, gained_game=5)
    )  # fmt: skip
    assert (model.life_gained_turn, model.life_gained_game) == (1, 5)
