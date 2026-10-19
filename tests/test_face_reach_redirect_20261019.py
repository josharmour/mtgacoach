"""The #56 redirect keeps a face pick when this turn's damage can finish them (ledger #145).

2026-10-19 13:54:11: opponent at 7; the model's line was Burst Lightning face
(7->5), Boltwave (->2), then the attack and Hired Claw's ping. Burst alone is
not lethal, so the redirect sent it at a creature the line search preferred.
"""

from __future__ import annotations

import logging

from tests.test_death_trigger_redirect_20261019 import (  # noqa: F401 (autouse fixture)
    BOLTWAVE_TEXT,
    INSPECTOR_ID,
    _card,
    _not_lethal,
    burst_window,
    planner_after_the_cast,
)

ME, THEM = 2, 1
REASON = "Un-kicked Burst Lightning goes to the face: 7 to 5, then Boltwave takes them to 2 and the attack plus Hired Claw ping finishes."


def _near_lethal(life: int):
    state, decision = burst_window("Menace")  # no death trigger: #126 does not apply
    next(p for p in state["players"] if p["seat_id"] == THEM)["life_total"] = life
    state["stack"] = [s for s in state["stack"] if s["name"] != "Boltwave"]
    state["hand"] = [_card(388, "Boltwave", ME, "Sorcery", BOLTWAVE_TEXT, mana_cost="{R}")]
    return state, decision


def test_a_face_pick_that_can_finish_them_this_turn_is_not_redirected(caplog):
    state, decision = _near_lethal(7)
    planner = planner_after_the_cast(REASON)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(decision, state) == ["tgt:1"]
    assert "Keeping the face pick" in caplog.text
    assert "Retargeting:" not in caplog.text


def test_far_from_lethal_the_line_target_still_wins(caplog):
    state, decision = _near_lethal(20)
    planner = planner_after_the_cast(REASON)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(decision, state) == [f"tgt:{INSPECTOR_ID}"]
    assert "Retargeting:" in caplog.text
