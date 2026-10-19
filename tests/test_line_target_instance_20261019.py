"""The searched line's target is an instance, not a name (ledger #111).

2026-10-19 12:31:40 (match e453dfa3 T16): two 4/5 Hearth Elementals, one (361)
with 3 damage from Lightning Strike. The line cast Iroh's Demonstration at the
damaged one; the low answer said "Cast Iroh's Demonstration mode 2 on the
undamaged Hearth Elemental", which counted as the board-math play because the
name matched, so the high-effort answer was not awaited. The target step then
picked the undamaged 372 and the 4 damage was declined as killing nothing.
``_searched_line_target`` also took the first body of the name from any cast
in the line, not the one this cast aims at.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arenamcp import action_planner
from arenamcp.action_planner import ActionPlanner, _reasoning_names_target, board_math_agreement
from arenamcp.line_search_moves import Step

DAMAGED = {
    "instance_id": 361,
    "name": "Hearth Elemental",
    "type_line": "Creature — Elemental",
    "power": 4,
    "toughness": 5,
    "damage": 3,
    "damaged_this_turn": True,
    "controller_seat_id": 1,
}
UNDAMAGED = dict(DAMAGED, instance_id=372, damage=0, damaged_this_turn=False)


def _state(*theirs: dict) -> dict:
    return {
        "turn": {"turn_number": 16, "active_player": 2, "phase": "Phase_Main1"},
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": list(theirs),
    }


def _cast() -> SimpleNamespace:
    return SimpleNamespace(kind="cast")


def _line(step: Step):
    return SimpleNamespace(line_search=SimpleNamespace(best=SimpleNamespace(steps=[step])))


@pytest.fixture
def line(monkeypatch):
    def use(step: Step) -> None:
        monkeypatch.setattr("arenamcp.board_assessment.assess", lambda state: _line(step))

    return use


# --- _searched_line_target -------------------------------------------------------------------------


def test_the_id_is_the_one_this_cast_aims_at(line):
    # A landcycle, then Shock at the undamaged Elemental, then Iroh's at the damaged one.
    line(
        Step(
            k=0, turn=16, label="T", casts=("Shock", "Iroh's Demonstration"), cycles=("Ash Barrens",),
            targets=(("Shock", "Hearth Elemental"), ("Iroh's Demonstration", "Hearth Elemental")),
            plays=(None, ((SimpleNamespace(kind="cycle"), None), (_cast(), UNDAMAGED), (_cast(), DAMAGED)), ()),
        )  # fmt: skip
    )
    state = _state(DAMAGED, UNDAMAGED)
    assert ActionPlanner._searched_line_target(state, "Iroh's Demonstration") == ("Hearth Elemental", 361)
    assert ActionPlanner._searched_line_target(state, "Shock") == ("Hearth Elemental", 372)


def test_without_a_per_cast_record_the_name_decides(line):
    # Plays that do not line up with the casts: the first body of the name, as before.
    line(
        Step(
            k=0, turn=16, label="T", casts=("Iroh's Demonstration",),
            targets=(("Iroh's Demonstration", "Hearth Elemental"),),
            plays=(None, ((_cast(), UNDAMAGED), (_cast(), DAMAGED)), ()),
        )  # fmt: skip
    )
    assert ActionPlanner._searched_line_target(_state(DAMAGED, UNDAMAGED), "Iroh's Demonstration") == (
        "Hearth Elemental",
        372,
    )
    # Deployment rows carry no plays at all: no id.
    line(Step(k=0, turn=16, label="T", casts=("Shock",), targets=(("Shock", "Hearth Elemental"),)))
    assert ActionPlanner._searched_line_target(_state(DAMAGED), "Shock") == ("Hearth Elemental", 0)


# --- _reasoning_names_target -----------------------------------------------------------------------

LIVE_LOW_ANSWER = (
    "Cast Iroh's Demonstration mode 2 on the undamaged Hearth Elemental, leaving it at dies-to-1 so any "
    "further burn removes it and cuts their lethal crackback."
)


def test_the_live_low_answer_does_not_agree_with_a_line_at_the_damaged_twin():
    state = _state(DAMAGED, UNDAMAGED)
    assert not _reasoning_names_target(LIVE_LOW_ANSWER, "Hearth Elemental", target_id=361, game_state=state)
    # Aimed at the undamaged one, the same words agree.
    assert _reasoning_names_target(LIVE_LOW_ANSWER, "Hearth Elemental", target_id=372, game_state=state)


@pytest.mark.parametrize(
    ("reasoning", "agrees"),
    [
        ("Iroh's finishes the damaged Hearth Elemental.", True),
        ("Iroh's 4 finishes the 3-damaged Hearth Elemental.", True),
        ("Iroh's at Hearth Elemental [id:361] kills it.", True),
        ("Iroh's at Hearth Elemental [id:372].", False),
        ("Iroh's 4 damage at a Hearth Elemental.", False),  # which one?
        ("Kill the undamaged Hearth Elemental now, the wounded one later.", False),  # both
    ],
)
def test_twins_need_the_instance_singled_out(reasoning, agrees):
    state = _state(DAMAGED, UNDAMAGED)
    assert _reasoning_names_target(reasoning, "Hearth Elemental", target_id=361, game_state=state) is agrees


def test_the_name_decides_without_twins_or_without_the_id():
    reasoning = "Iroh's 4 damage at a Hearth Elemental."
    assert _reasoning_names_target(reasoning, "Hearth Elemental", target_id=361, game_state=_state(DAMAGED))
    state = _state(DAMAGED, UNDAMAGED)
    assert _reasoning_names_target(reasoning, "Hearth Elemental")
    assert _reasoning_names_target(reasoning, "Hearth Elemental", target_id=0, game_state=state)
    # Twins that look alike are told apart only by id.
    alike = _state(DAMAGED, dict(DAMAGED, instance_id=372))
    assert not _reasoning_names_target("the damaged Hearth Elemental", "Hearth Elemental", target_id=361, game_state=alike)
    assert _reasoning_names_target("Hearth Elemental [id:361]", "Hearth Elemental", target_id=361, game_state=alike)


# --- board_math_agreement --------------------------------------------------------------------------


def test_the_low_answer_at_the_wrong_twin_is_not_the_board_math_play(monkeypatch):
    monkeypatch.setattr(action_planner, "_own_main_phase_with_empty_stack", lambda state: True)
    monkeypatch.setattr(action_planner, "board_math_option_pick", lambda decision, state: (["idx:0"], "line"))
    monkeypatch.setattr(action_planner, "line_fallback_option_pick", lambda decision, state, picked: None)
    monkeypatch.setattr(action_planner, "find_source", lambda state, meta: {"name": "Iroh's Demonstration"})
    monkeypatch.setattr(
        ActionPlanner, "_searched_line_target", staticmethod(lambda state, name: ("Hearth Elemental", 361))
    )
    decision = SimpleNamespace(request_type="ActionsAvailable", find=lambda oid: SimpleNamespace(meta={}))
    state = _state(DAMAGED, UNDAMAGED)
    assert board_math_agreement(decision, state, ["idx:0"], LIVE_LOW_ANSWER) == ""
    assert board_math_agreement(decision, state, ["idx:0"], "Iroh's finishes the damaged Hearth Elemental.") == "line"
