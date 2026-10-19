"""A priority window with only our own spell on the stack (2026-10-19, match 41753907 T8).

standalone.log 09:03:36-51: with our own Fleeting Effigy on the stack, the
window went to the model at high effort ("removal available: Burst
Lightning"), the high leg ran its 15 s, and the fallback then passed saying
"outside our main phase only a land drop or a pass is safe" — in our main
phase.
"""

from __future__ import annotations

import logging

from tests.test_burn_targeting_20261019 import (
    BURST,
    OPP,
    SHOCK,
    _cast_menu,
    _creature,
    _mountain,
    _planner,
    _spell,
    _state,
)

from arenamcp import action_planner as planner_module
from arenamcp.action_planner import board_math_option_pick, own_stack_only


def _own_stack_window():
    effigy = _spell(327, "Fleeting Effigy", "Creature — Elemental", "{R}", "Haste")
    burst = _spell(321, "Burst Lightning", "Instant", "{R}", BURST)
    shock = _spell(322, "Shock", "Instant", "{R}", SHOCK)
    marshal = _creature(300, "Dawnwing Marshal", OPP, 2, 2, "Flying")
    state = _state(
        [marshal, _mountain(279, True), _mountain(1), _mountain(2)], hand=[burst, shock], stack=[effigy]
    )
    return state, _cast_menu((321, "Burst Lightning"), (322, "Shock"))


def test_the_fallback_says_our_spell_resolves_first_not_outside_our_main_phase():
    state, menu = _own_stack_window()
    picked, why = board_math_option_pick(menu, state)
    assert picked == ["pass"]
    assert why == "our own Fleeting Effigy is on the stack, so pass and let it resolve"


def test_a_menu_that_proves_the_stack_empty_is_no_own_stack():
    state, menu = _own_stack_window()
    assert own_stack_only(menu, state) == "Fleeting Effigy"
    slickshot = _spell(323, "Slickshot Show-Off", "Creature — Bird Wizard", "{1}{R}", "Flying, haste")
    state["hand"].append(slickshot)
    with_creature = _cast_menu((321, "Burst Lightning"), (323, "Slickshot Show-Off"))
    assert own_stack_only(with_creature, state) == ""  # sorcery speed is offered: the stack is empty
    state["stack"][0]["controller_seat_id"] = OPP
    assert own_stack_only(menu, state) == ""


def test_our_own_spell_alone_on_the_stack_is_decided_at_low_effort(monkeypatch, caplog):
    monkeypatch.setattr(
        planner_module, "critical_decision", lambda *a, **k: "removal available: Burst Lightning"
    )
    state, menu = _own_stack_window()
    planner = _planner('{"option_ids": ["pass"], "reasoning": "let Effigy resolve"}')
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(menu, state) == ["pass"]
    assert planner._backend.efforts == [None]
    assert "only our own Fleeting Effigy is on the stack — low effort" in caplog.text
