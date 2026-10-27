"""Ledger #228: the hold policy recomputes against the CURRENT plan each window.

Match f5169733 T8-T12 (2026-10-23 20:33-20:35): autopilot passed three turns
running holding Iroh's Demonstration x2 + Tectonic Hazard with the frozen
rationale "it kills none of their creatures now and pumps nothing" while
Garruk, Curse Breaker ticked and spawned beasts — zero damage for three turns
at a 15-life opponent. GamePlan T11 explicitly said "Chip with Hazard now" and
the planner held anyway: the hold never faced the plan, and the sweeper's /
burn's face-damage halves were invisible to its math.

The plan check reads the live plan text every window (plan says chip -> the
hold yields), ``two_turn_reach`` counts a sweeper's damage to each of their
creatures at that weight, and the standing hold's rationale names the chip it
is giving up.
"""

from __future__ import annotations

from tests.test_burn_timing_20261019 import _board, _card, _land, _menu, _option
from tests.test_decision_layer_20261019 import _Model, _planner, _reply

from arenamcp.burn_timing import chip_damage, plan_chip_release, sweep_chip, two_turn_reach

ME, THEM = 2, 1

HAZARD = {
    "type_line": "Sorcery",
    "mana_cost": "{R}",
    "card_types": ["CardType_Sorcery"],
    "oracle_text": "Tectonic Hazard deals 1 damage to each opponent and each creature they control.",
}
IROH = {
    "type_line": "Sorcery — Lesson",
    "mana_cost": "{1}{R}",
    "card_types": ["CardType_Sorcery"],
    "oracle_text": "Choose one —\n"
    "• Iroh's Demonstration deals 1 damage to each creature your opponents control.\n"
    "• Iroh's Demonstration deals 4 damage to target creature.",
}
GARRUK = {
    "type_line": "Legendary Planeswalker — Garruk",
    "card_types": ["CardType_Planeswalker"],
    "oracle_text": "+2: Untap up to two target lands.\n"
    "−3: Create a 4/4 green Beast creature token with trample.",
}
BEAST = {
    "type_line": "Creature — Beast",
    "card_types": ["CardType_Creature"],
    "power": 4,
    "toughness": 4,
    "oracle_text": "",
}


def _f5169733_t11(theirs=()):
    """Our main 1, turn 11: three Mountains; Garruk (loyalty 5) and whatever else they have.

    The hand is the live one: Tectonic Hazard and both Iroh's Demonstrations —
    every one of them kills nothing on this board, so the #136/#80 holds fire.
    """
    hand = [
        _card(222, "Tectonic Hazard", ME, **HAZARD),
        _card(223, "Iroh's Demonstration", ME, **IROH),
        _card(224, "Iroh's Demonstration", ME, **IROH),
    ]
    return _board(
        ME,
        11,
        ME,
        "Phase_Main1",
        "",
        ours=[_land(300 + i, ME) for i in range(3)],
        theirs=[_card(500, "Garruk, Curse Breaker", THEM, **GARRUK), *theirs],
        hand=hand,
        life=20,
        their_life=15,
        lands_played=1,
    )


# --- the plan check --------------------------------------------------------------------------------


def test_plan_chip_release_reads_a_named_directive():
    plan = "ROLE: aggressor\nT11: Chip with Hazard now. Their Garruk will make beasts."
    released, why = plan_chip_release(plan, [(None, {"name": "Tectonic Hazard"})])
    assert released == ["Tectonic Hazard"]
    assert "game plan says chip" in why


def test_plan_chip_release_generic_directive_releases_all_held():
    plan = "ROLE: aggressor\nChip their life each turn with burn."
    held = [(None, {"name": "Tectonic Hazard"}), (None, {"name": "Iroh's Demonstration"})]
    released, _why = plan_chip_release(plan, held)
    assert released == ["Tectonic Hazard", "Iroh's Demonstration"]


def test_plan_chip_release_ignores_conditional_and_absent_chips():
    held = [(None, {"name": "Tectonic Hazard"})]
    assert plan_chip_release("If the board stalls, switch to chipping.", held) == ([], "")
    assert plan_chip_release("ROLE: defender\nHold the wall; block everything.", held) == ([], "")
    assert plan_chip_release(None, held) == ([], "")


# --- the chip math ---------------------------------------------------------------------------------


def test_sweep_chip_weights_their_creatures():
    hazard = {"name": "Tectonic Hazard", "oracle_text": HAZARD["oracle_text"]}
    assert sweep_chip(hazard, 0) == 0  # an empty board is #136's face-only waste
    assert sweep_chip(hazard, 2) == 2
    iroh = {"name": "Iroh's Demonstration", "oracle_text": IROH["oracle_text"]}
    assert chip_damage(iroh, {"battlefield": [], "local_seat_id": ME}) == 0


def test_two_turn_reach_counts_the_sweep_halves():
    claw = _card(10, "Hired Claw", ME)
    lands = [_land(301 + i, ME) for i in range(2)]
    beasts = [_card(400 + i, "Beast", THEM, **BEAST) for i in range(3)]
    hand = [_card(222, "Tectonic Hazard", ME, **HAZARD), _card(223, "Iroh's Demonstration", ME, **IROH)]
    state = _board(
        ME,
        11,
        ME,
        "Phase_Main1",
        "",
        ours=[claw, *lands],
        theirs=[*beasts],
        hand=hand,
        life=20,
        their_life=9,
        lands_played=1,
    )
    # Attacks 2 + 2 and the sweepers' chip (Hazard's face half plus both sweeps on three beasts).
    assert two_turn_reach(state).startswith("up to ")
    empty = _board(
        ME,
        11,
        ME,
        "Phase_Main1",
        "",
        ours=[claw, *lands],
        theirs=[],
        hand=hand,
        life=20,
        their_life=9,
        lands_played=1,
    )
    # Without their creatures the same hand is face-only waste again (#136): no reach.
    assert two_turn_reach(empty) == ""


# --- the hold yields when the plan says chip --------------------------------------------------------


def test_the_frozen_hold_passes_without_a_plan():
    state = _f5169733_t11()
    model = _Model(_reply("pass", "nothing to do"))
    planner = _planner(model)
    menu = _menu(state, (222, True), (223, True), (224, True))
    assert planner.plan_decision_options(menu, state) == ["pass"]
    assert model.efforts == []  # deterministic: the hold answered for the model
    assert "kills none of their creatures now" in planner.get_decision_reasoning(["pass"])


def test_the_plan_says_chip_so_the_hold_yields_and_hazard_goes():
    state = _f5169733_t11()
    menu = _menu(state, (222, True), (223, True), (224, True))
    model = _Model(_reply(_option(menu, 222), "The plan says chip with Hazard now."))
    planner = _planner(model)
    planner.set_game_plan("ROLE: aggressor\nT11: Chip with Hazard now.")
    chosen = planner.plan_decision_options(menu, state)
    assert chosen == [_option(menu, 222)], planner.get_decision_reasoning(chosen)
    assert model.efforts  # the model was asked: the hold no longer answers for it


def test_a_generic_chip_directive_releases_every_held_damage_spell():
    state = _f5169733_t11()
    menu = _menu(state, (222, True), (223, True), (224, True))
    model = _Model(_reply(_option(menu, 223), "Chipping with Iroh's mode 1."))
    planner = _planner(model)
    planner.set_game_plan("ROLE: aggressor\nChip their life each turn with burn.")
    chosen = planner.plan_decision_options(menu, state)
    assert chosen == [_option(menu, 223)], planner.get_decision_reasoning(chosen)


def test_a_conditional_chip_mention_does_not_release_the_hold():
    state = _f5169733_t11()
    model = _Model(_reply("pass", "nothing to do"))
    planner = _planner(model)
    planner.set_game_plan("If the board stalls, switch to chipping.")
    menu = _menu(state, (222, True), (223, True), (224, True))
    assert planner.plan_decision_options(menu, state) == ["pass"]
    assert model.efforts == []


def test_the_standing_hold_rationale_names_the_chip_it_gives_up():
    # A 6/6 survives both sweepers' printed damage halves (even combined) and
    # their burn combo; holding them still forfeits real chip damage each
    # window (#228): the rationale says so (Hazard: 1 face + 1 on the troll,
    # Iroh's mode 1: 1 on the troll).
    state = _f5169733_t11(theirs=[_card(400, "Beast", THEM, **{**BEAST, "power": 6, "toughness": 6})])
    model = _Model(_reply("pass", "nothing to do"))
    planner = _planner(model)
    menu = _menu(state, (222, True), (223, True))
    assert planner.plan_decision_options(menu, state) == ["pass"]
    assert model.efforts == []
    reasoning = planner.get_decision_reasoning(["pass"])
    assert "kills none of their creatures now" in reasoning
    assert "chips for 3" in reasoning
