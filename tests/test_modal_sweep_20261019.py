"""Ledger #87 (2026-10-19 match c8ea2b6c T14, standalone.log 11:52:45-11:52:51): modal sweep modes.

Iroh's Demonstration ("Choose one — 1 damage to each creature your opponents
control; or 4 damage to target creature") was cast with its second mode on
Healer's Hawk while the first would have killed the Hawk and both Soldier
tokens Elspeth, Storm Slayer had made. The MODES line scored the two as a tie
("survives T15 at 15, 13 after T17, 11 after T19" each): ``classify`` called
the sweep mode removal of one creature, ``compare_modes`` removed one body and
the line search's per-mode variant carried no sweep.
"""

from __future__ import annotations

from copy import deepcopy

from tests import race_boards_20261019 as rb
from tests.strategic_states import modal_decision

from arenamcp.action_planner import _modes_line
from arenamcp.board_model import build_board_model
from arenamcp.decision_memory import critical_decision
from arenamcp.line_guard import option_note
from arenamcp.line_search import compare_modes, search_lines
from arenamcp.line_search_moves import classify, mode_label

IROH = (
    "Choose one —\n"
    "• Iroh's Demonstration deals 1 damage to each creature your opponents control.\n"
    "• Iroh's Demonstration deals 4 damage to target creature."
)
SWEEP_MODE = "Iroh's Demonstration deals 1 damage to each creature your opponents control."
TARGET_MODE = "Iroh's Demonstration deals 4 damage to target creature."

CARDS = {
    "Iroh's Demonstration": {
        "grp_id": 97417, "type_line": "Sorcery — Lesson", "mana_cost": "{1}{R}",
        "card_types": ["CardType_Sorcery"], "oracle_text": IROH,
    },
    "Healer's Hawk": {
        "grp_id": 93638, "type_line": "Creature — Bird", "mana_cost": "{W}", "power": 1, "toughness": 1,
        "keywords": ["flying", "lifelink"], "card_types": ["CardType_Creature"], "oracle_text": "Flying, lifelink",
    },
    "Soldier": {
        "grp_id": 95823, "type_line": "Token Creature — Soldier", "power": 1, "toughness": 1,
        "card_types": ["CardType_Creature"], "oracle_text": "", "object_kind": "TOKEN",
    },
    "Elspeth, Storm Slayer": {
        "grp_id": 95526, "type_line": "Legendary Planeswalker — Elspeth", "mana_cost": "{3}{W}{W}",
        "card_types": ["CardType_Planeswalker"], "loyalty": 3,
        "oracle_text": "If one or more tokens would be created under your control, twice that many of those "
        "tokens are created instead.\n+1: Create a 1/1 white Soldier creature token.\n0: Put a +1/+1 counter on "
        "each creature you control. Those creatures gain flying until your next turn.\n−3: Destroy target "
        "creature an opponent controls with mana value 3 or greater.",
    },
    "Hill Giant": {
        "grp_id": 601, "type_line": "Creature — Giant", "mana_cost": "{3}{R}", "power": 3, "toughness": 4,
        "card_types": ["CardType_Creature"], "oracle_text": "",
    },
}  # fmt: skip

# The recorded menu (match packet decision 23, request [308, 394]): Arena's mode labels.
MODE_MENU = [
    (
        "idx:0",
        "Mode 1: CARDNAME deals 1 damage to each creature your opponents control.",
        {
            "actionType": "CastingTimeOption", "choiceKind": "modal", "requestClass": "CastingTimeOption_ModalRequest",
            "childIndex": 0, "label": "Mode 1", "optionIndex": 0, "grpId": 87912, "sourceId": 373,
            "abilityGrpId": 192719, "min": 1, "max": 1,
        },
    ),
    (
        "idx:1",
        "Mode 2: CARDNAME deals 4 damage to target creature.",
        {
            "actionType": "CastingTimeOption", "choiceKind": "modal", "requestClass": "CastingTimeOption_ModalRequest",
            "childIndex": 0, "label": "Mode 2", "optionIndex": 1, "grpId": 21773, "sourceId": 373,
            "abilityGrpId": 192719, "min": 1, "max": 1,
        },
    ),
]  # fmt: skip


def _card(instance_id: int, name: str, controller: int, **extra) -> dict:
    return {
        "instance_id": instance_id,
        "name": name,
        "owner_seat_id": controller,
        "controller_seat_id": controller,
        **deepcopy(CARDS[name]),
        **extra,
    }


_MOUNTAINS = [(401 + i, "Mountain", False, 2 + 2 * i) for i in range(5)]
_PLAINS = [(501 + i, "Plains", False, 1 + 2 * i) for i in range(6)]


def _t14(*, on_stack: bool, theirs: list[dict] | None = None) -> dict:
    """c8ea2b6c T14 (11:52:36): we are at 17 with five Mountains, they are at 12 with Elspeth
    (loyalty 3), a fresh Healer's Hawk and two Soldier tokens. Iroh's Demonstration is in hand,
    or (``on_stack``) cast and waiting for its mode."""
    source = rb.board(
        turn=14, life=17, their_life=12, lands_played=0, opponent_hand=3,
        ours=_MOUNTAINS, theirs=_PLAINS, hand=[(372, "Mountain")],
    )  # fmt: skip
    source["battlefield"] += (
        theirs
        if theirs is not None
        else [
            _card(357, "Elspeth, Storm Slayer", rb.OPPONENT, turn_entered_battlefield=11),
            _card(367, "Healer's Hawk", rb.OPPONENT, turn_entered_battlefield=13),
            _card(370, "Soldier", rb.OPPONENT, turn_entered_battlefield=13),
            _card(371, "Soldier", rb.OPPONENT, turn_entered_battlefield=13),
        ]
    )
    iroh = _card(373 if on_stack else 365, "Iroh's Demonstration", rb.LOCAL)
    if on_stack:
        source["stack"] = [iroh]
    else:
        source["hand"].append(iroh)
    return source


def _search(source: dict):
    return search_lines(build_board_model(deepcopy(source)), survival=False, lethal_now=False, hard_ms=1e9)


# --- the mode's kind ------------------------------------------------------------------------


def test_the_one_sided_damage_mode_is_a_sweep_and_the_targeted_one_stays_removal():
    kind, gain, reach, bounce = classify("Iroh's Demonstration", SWEEP_MODE)
    assert (kind, gain, reach, bounce) == ("sweep", 0, None, False)
    kind, _gain, reach, _bounce = classify("Iroh's Demonstration", TARGET_MODE)
    assert kind == "removal" and reach is not None
    # Arena's own label text (CARDNAME) reads the same.
    assert classify(
        "Iroh's Demonstration", "CARDNAME deals 1 damage to each creature your opponents control."
    )[0] == ("sweep")
    assert mode_label("sweep", 0, SWEEP_MODE, "Iroh's Demonstration") == "deals 1 damage to each creature…"


# --- the recorded mode menu -----------------------------------------------------------------


def test_compare_modes_sweeps_the_hawk_and_both_soldiers():
    source = _t14(on_stack=True)
    decision = modal_decision(MODE_MENU, (308, 394))
    comparison = compare_modes(deepcopy(source), decision, hard_ms=1e9)
    assert comparison is not None and comparison.complete
    assert comparison.kills["idx:0"] == ("Healer's Hawk", "Soldier", "Soldier")
    assert len(comparison.kills["idx:1"]) == 1
    sweep, single = comparison.lines["idx:0"], comparison.lines["idx:1"]
    # Recorded: both "survives T15 at 15, 13 after T17, 11 after T19". Their board is empty after
    # the sweep; since #229 Elspeth's +1 Soldier engine is modelled, so her replacement attacks
    # from their next turn on (16), and two of them the turn after (14). The sweep still dominates.
    assert sweep.lives()[:3] == [17, 16, 14], sweep.summary()
    assert single.lives()[0] < 17, single.summary()
    assert sweep.score > single.score
    assert option_note(comparison, decision.options[0], source).startswith("[LINE best: ")
    assert not option_note(comparison, decision.options[1], source).startswith("[LINE best")


def test_the_modes_line_names_what_each_mode_kills():
    source = _t14(on_stack=True)
    decision = modal_decision(MODE_MENU, (308, 394))
    comparison = compare_modes(deepcopy(source), decision, hard_ms=1e9)
    line = _modes_line(comparison, decision)
    assert (
        "deals 1 damage to each creature… (kills Healer's Hawk, Soldier ×2) — survives T15 at 17" in line
    ), line
    assert "deals 4 damage to target creature (kills " in line, line
    assert not line.endswith("…"), line


def test_the_line_search_casts_the_sweep_mode_from_hand():
    result = _search(_t14(on_stack=False))
    sweep = result.first_action[("cast", 365, 0)]
    single = result.first_action[("cast", 365, 1)]
    assert sweep.steps[0].their_creatures_after == 0, sweep.summary()
    assert single.steps[0].their_creatures_after == 2, single.summary()
    assert sweep.score > single.score
    # The best line still casts the sweep mode from hand; since #229 Elspeth's +1 Soldier engine
    # is modelled it waits a turn and clears her new Soldier with the rest (their attack lands
    # first, then the sweep takes the hawk, both Soldiers and the replacement).
    best_modes = [label for step in result.best.steps for _name, label in step.modes]
    assert any("deals 1 damage to each creature…" in label for label in best_modes), result.best.summary()


# --- the targeted mode and plain sweeps still work ------------------------------------------


def test_the_targeted_mode_still_kills_what_the_sweep_cannot():
    giant = _card(601, "Hill Giant", rb.OPPONENT, turn_entered_battlefield=13)
    source = _t14(on_stack=True, theirs=[giant])
    decision = modal_decision(MODE_MENU, (308, 394))
    comparison = compare_modes(deepcopy(source), decision, hard_ms=1e9)
    assert comparison is not None and comparison.complete
    assert comparison.kills == {"idx:0": (), "idx:1": ("Hill Giant",)}
    assert comparison.lines["idx:1"].score > comparison.lines["idx:0"].score
    assert "(kills nothing)" in _modes_line(comparison, decision)
    # From hand, alone: the targeted mode on the Giant beats the sweep that misses it.
    result = _search(_t14(on_stack=False, theirs=[giant]))
    single, sweep = result.first_action[("cast", 365, 1)], result.first_action[("cast", 365, 0)]
    assert single.score > sweep.score, (single.summary(), sweep.summary())
    assert single.steps[0].their_creatures_after == 0 and sweep.steps[0].their_creatures_after == 1
    assert result.best.steps[0].their_creatures_after == 0, result.best.summary()


def test_a_sweep_mode_adds_to_a_shock_on_a_three_toughness_creature():
    # 1 to each + Shock's 2 kill the 1/3 and the 1/1 together (as Tectonic Hazard + Shock do).
    source = rb.board(
        turn=9, life=20, their_life=20, ours=[(401, "Mountain", False, 1), (402, "Mountain", False, 1),
                                              (403, "Mountain", False, 3)],
        theirs=[(601, "Grizzly Bears", False, 2, {"power": 1, "toughness": 3}),
                (602, "Grizzly Bears", False, 2, {"power": 1, "toughness": 1})],
        hand=[(2, "Shock")],
    )  # fmt: skip
    source["hand"].append(_card(365, "Iroh's Demonstration", rb.LOCAL))
    result = _search(source)
    first = result.best.steps[0]
    assert sorted(first.casts) == ["Iroh's Demonstration", "Shock"], result.best.summary()
    assert first.their_creatures_after == 0


def test_tectonic_hazard_still_sweeps():
    source = rb.board(
        turn=9, life=20, their_life=20, ours=[(401, "Mountain", False, 1)],
        theirs=[(601, "Grizzly Bears", False, 2, {"power": 1, "toughness": 1}),
                (602, "Grizzly Bears", False, 2, {"power": 1, "toughness": 1})],
        hand=[(1, "Tectonic Hazard")],
    )  # fmt: skip
    result = _search(source)
    first = result.best.steps[0]
    assert first.casts == ("Tectonic Hazard",) and first.their_creatures_after == 0, result.best.summary()


# --- effort: the mode of a removal spell is a critical decision -----------------------------


def test_choosing_a_removal_spells_mode_runs_at_high_effort():
    # 11:52:45 the mode menu ran at low effort ("removal available" had escalated the cast only).
    source = _t14(on_stack=True)
    decision = modal_decision(MODE_MENU, (308, 394))
    assert critical_decision(decision, source) == "removal mode: Iroh's Demonstration"
    # Nothing of theirs it can kill: no escalation (the 11:52:22 board, Elspeth alone).
    elspeth = _card(357, "Elspeth, Storm Slayer", rb.OPPONENT, turn_entered_battlefield=11)
    assert critical_decision(decision, _t14(on_stack=True, theirs=[elspeth])) == ""
