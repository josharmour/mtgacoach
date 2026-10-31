"""Regressions for the Boros session's public land/attack facts (#303/#339)."""

import pytest
from tests.test_losing_attack_guard import creature, incident, land

from arenamcp.board_model import build_board_model
from arenamcp.combat_solver import attacking_trick_exposure
from arenamcp.combat_strategy import _attack_payoff, losing_attackers
from arenamcp.land_mana import enters_tapped, land_colors, mana_source, refresh_sources
from arenamcp.line_search import action_key

VERGE = {
    "name": "Sunbillow Verge",
    "instance_id": 101,
    "type_line": "Land",
    "oracle_text": "{T}: Add {W}. {T}: Add {R}. Activate only if you control a Mountain or a Plains.",
}
PASS = {
    "name": "Sundown Pass",
    "instance_id": 102,
    "type_line": "Land",
    "oracle_text": "This land enters tapped unless you control two or more other lands. {T}: Add {R} or {W}.",
}
VOICE = "Mobilize 2 (Whenever this creature attacks, create two tapped and attacking 1/1 red Warrior creature tokens. Sacrifice them at the beginning of the next end step.)\nYour opponents can’t cast spells during your turn."


def test_mobilize_retains_attack_value_after_reminder_stripped():
    voice = creature(317, "Voice of Victory", 2, 1, 3, VOICE)
    assert _attack_payoff(voice) is None
    s = incident([voice], [creature(400, "Tifa", 1, 1, 2)])
    s["hand"] = []
    s["graveyard"] = []
    s["opponent_hand_count"] = 4
    for c in s["battlefield"]:
        if c["controller_seat_id"] == 1:
            c["is_tapped"] = False
    assert 317 not in losing_attackers(s, [317])
    assert not attacking_trick_exposure(s, 1).live


@pytest.mark.parametrize("change", ["their_turn", "no_voice", "conditional", "phased"])
def test_voice_does_not_suppress_tricks_without_current_restriction(change):
    voice = creature(317, "Voice", 2, 1, 3, VOICE)
    s = incident([voice], [creature(400, "Tifa", 1, 1, 2)])
    s["opponent_hand_count"] = 4
    for c in s["battlefield"]:
        if c["controller_seat_id"] == 1:
            c["is_tapped"] = False
    if change == "their_turn":
        s["turn"]["active_player"] = 1
    if change == "no_voice":
        s["battlefield"].remove(voice)
    if change == "conditional":
        voice["oracle_text"] = (
            "Whenever this creature attacks, your opponents can't cast spells during your turn."
        )
    if change == "phased":
        voice["is_phased_out"] = True
    assert attacking_trick_exposure(s, 1).live


@pytest.mark.parametrize(
    "board,expected",
    [
        ([], {"W"}),
        ([PASS], {"W"}),
        ([{"type_line": "Basic Land — Mountain"}], {"W", "R"}),
        ([{"type_line": "Land — Plains Island"}], {"W", "R"}),
    ],
)
def test_verge_requires_real_basic_type(board, expected):
    card = {**VERGE, "color_production": ["White", "Red"]}
    assert land_colors(card, board) == expected
    assert land_colors({**card, "oracle_text": card["oracle_text"].replace("{", "{o")}, board) == expected


def test_slow_land_counts_other_lands_and_dynamic_verge_enablement():
    mountain = land(103, "Mountain", 2, tapped=False)
    assert enters_tapped(PASS, [VERGE])
    assert not enters_tapped(PASS, [VERGE, mountain])
    assert enters_tapped(PASS, [VERGE, PASS])
    source = mana_source(VERGE, [VERGE])
    assert source.produces == {"W"}
    assert refresh_sources([source], [VERGE, mountain])[0].produces == {"W", "R"}


def test_model_and_action_key_do_not_forecast_verge_red_without_basic_type():
    from types import SimpleNamespace

    s = incident([], [])
    s["battlefield"] = [{**VERGE, "controller_seat_id": 2}]
    s["hand"] = [{**PASS, "controller_seat_id": 2}]
    s["graveyard"] = []
    model = build_board_model(s)
    assert model.sources_all[0].produces == {"W"}
    opt = SimpleNamespace(option_id="idx:0", meta={"actionType": "Play", "instanceId": 102})
    assert action_key(opt, s) == ("land", (frozenset({"W", "R"}), True))


def test_actual_transition_verge_cannot_cast_red_but_mountain_unlocks_both():
    from arenamcp.line_search_moves import Moves

    s = incident([], [])
    s["turn"].update(phase="Main1", step="None")
    s["battlefield"] = [{**VERGE, "controller_seat_id": 2}]
    mountain = {**land(103, "Mountain", 2, tapped=False), "oracle_text": "{T}: Add {R}."}
    s["hand"] = [{**PASS, "controller_seat_id": 2}, mountain]
    s["graveyard"] = []
    for p in s["players"]:
        p["lands_played"] = 0
    moves = Moves(build_board_model(s), survival=False, lethal_now=False, hard_ms=100000, opponent=None)
    node = moves.root_node()
    choices = {x.name: x for x in moves.land_options(node) if x is not None}
    assert choices["Sundown Pass"].tapped
    assert all("R" not in x.produces for x in moves.sources(node, choices["Sundown Pass"])[1])
    assert sum("R" in x.produces for x in moves.sources(node, choices["Mountain"])[1]) == 2
