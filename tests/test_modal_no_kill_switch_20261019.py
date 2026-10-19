"""A modal spell whose targeted damage mode kills nothing switches to its untargeted mode (#65).

2026-10-19 11:12:55 (match 115944ce T8): Iroh's Demonstration — "Choose one —
1 damage to each creature your opponents control; or 4 damage to target
creature" — was cast to pump two Slickshot Show-Offs and a prowess Emberheart
before attacking an opponent whose only creature, a 7/7 Trostani, was tapped.
The model chose the 4-damage mode, the target step declined it (kills
nothing), the cast was cancelled and finally hidden for the turn: the pumps
were lost. Mode 1 needs no target and keeps the cast.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from tests.test_self_cancel_memory_20261019 import board_state, creature, make_iroh_engine, modes_poll

from arenamcp.cast_triggers import untargeted_mode_instead
from arenamcp.decisions import DecisionOption, PendingDecision


@pytest.fixture
def iroh_engine(monkeypatch):
    return make_iroh_engine(monkeypatch)


MODE_TEXT = {
    87912: "CARDNAME deals 1 damage to each creature your opponents control.",
    21773: "CARDNAME deals 4 damage to target creature.",
}


def modes(*texts):
    return PendingDecision(
        (191, 0),
        "CastingTimeOptions",
        tuple(
            DecisionOption(
                f"idx:{i}", f"Mode {i + 1}: {text}", meta={"choiceKind": "modal", "grpId": 1000 + i}
            )
            for i, text in enumerate(texts)
        ),
        can_cancel=True,
    )


IROH = modes(MODE_TEXT[87912], MODE_TEXT[21773])


def test_four_damage_into_a_seven_seven_takes_the_untargeted_mode():
    swap = untargeted_mode_instead(IROH, ["idx:1"], board_state())
    assert swap is not None
    assert swap[0] == "idx:0"
    assert "Trostani, Three Whispers 7/7" in swap[1]


def test_a_kill_keeps_the_targeted_mode():
    state = board_state()
    state["battlefield"].append(creature(400, "Hired Claw", 1, 1, 2))
    assert untargeted_mode_instead(IROH, ["idx:1"], state) is None


def test_damage_already_marked_or_combat_keeps_the_targeted_mode():
    state = board_state()
    state["battlefield"][0]["damaged_this_turn"] = True  # 4 more might finish it
    assert untargeted_mode_instead(IROH, ["idx:1"], state) is None


def test_only_a_one_sided_untargeted_mode_is_taken():
    sweeper = modes("CARDNAME deals 2 damage to each creature.", MODE_TEXT[21773])
    assert untargeted_mode_instead(sweeper, ["idx:1"], board_state()) is None
    other_target = modes("Target player draws a card.", MODE_TEXT[21773])
    assert untargeted_mode_instead(other_target, ["idx:1"], board_state()) is None
    hazard = modes(
        "CARDNAME deals 1 damage to each opponent and each creature they control.", MODE_TEXT[21773]
    )
    assert untargeted_mode_instead(hazard, ["idx:1"], board_state())[0] == "idx:0"


def test_the_untargeted_mode_and_other_requests_are_left_alone():
    assert untargeted_mode_instead(IROH, ["idx:0"], board_state()) is None
    targets = SimpleNamespace(request_type="SelectTargets", options=IROH.options)
    assert untargeted_mode_instead(targets, ["idx:1"], board_state()) is None


def test_the_autopilot_submits_mode_one(iroh_engine, monkeypatch, caplog):
    import arenamcp.card_db as card_db

    monkeypatch.setattr(
        card_db, "get_card_database", lambda: SimpleNamespace(get_ability_text=lambda grp: MODE_TEXT.get(grp))
    )
    engine, bridge, planner = iroh_engine  # its planner picks "idx:1" (4 damage)
    bridge.get_pending_actions.return_value = modes_poll()
    with caplog.at_level(logging.WARNING, logger="arenamcp.autopilot"):
        assert engine._try_typed_decision_path(board_state(stack=True), "decision_required")
    (index,) = bridge.submit_action_by_index.call_args.args
    assert index == 0
    assert bridge.submit_action_by_index.call_args.kwargs["expected"]["grpId"] == 87912
    assert "Modal switch: 4 damage to target creature kills nothing" in caplog.text
    planner.plan_decision_options.assert_called_once()
