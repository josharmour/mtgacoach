"""The clock math on the 2026-10-08 FRA QuickDraft boards (game 1, match 07c043d8).

Field report: "I think that the clock is not right." The boards standalone.log
printed "Board facts" for (17:05-17:13) are rebuilt from Player.log in
tests/strategic_states_1008.py; each clock below is computed by hand under the
documented model (board-only; the defender's best blocks; attackers that only
die to a kill-block stay home; six attacks simulated, then the settled attack
repeats) and pinned against the code. The review found four defects, each
pinned by its own test below:

* the attacker's damage was assigned to blockers "in the order given", so a
  3/2 double-blocked by a 1/5 and a 1/2 killed nothing — and the answer
  depended on the battlefield's order (the live coach said "ours 8" where the
  replayed snapshot said "none");
* the "hold back what just dies" line re-blocked the survivors and killed
  them for nothing ("losing" creatures that never had to attack);
* the clock extrapolated only the last simulated attack, so a chump block at
  the horizon turned a finite clock into "no clock";
* "we get nothing through" was printed when damage did get through before the
  board stalled.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tests import strategic_states_1008 as fx

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model
from arenamcp.combat_solver import _resolve_attacker


def _clocks(state):
    return ba._clock_facts(build_board_model(deepcopy(state)))


def _assessment(state):
    ba._CACHE.clear()
    return ba.assess(deepcopy(state))


# --- the logged boards -------------------------------------------------------------

# (fixture, our clock, our lives, their clock, their lives, creatures, power)
HAND_COMPUTED = {
    # Geist (1/2 flyer) alone: 1 a turn into 20 life, capped at the 20-attack maximum.
    "T4_GEIST": (None, [20, 20], 20, [20, 19, 18, 17, 16, 15], (0, 1), (0, 1)),
    # Arni 3/2 vs Geist: 3 a turn, one chump block = ceil(20/3) + 1 = 8; Geist: 20.
    "T6_ARNI_VS_GEIST": (8, [17, 14, 14, 11, 8, 5], 20, [19, 18, 17, 16, 15, 14], (1, 1), (3, 1)),
    # Keeper + Arni vs Tetsuko 1/3 + Geist 1/2: a double block kills Arni (Arni kills
    # Tetsuko), Keeper then connects for 3 with one chump left = 1 + 7 = 8 attacks;
    # two unblockable 1-power attackers into 19 life = ceil(19/2) = 10.
    "T8_TWO_UNBLOCKABLE": (8, [17, 14, 14, 11, 8, 5], 10, [17, 15, 13, 11, 9, 7], (2, 2), (6, 2)),
    # Blazing Crescendo on Geist (4/3) with the damage not in: 5 unblockable a turn
    # into 19 = 4 attacks (the snapshot cannot see the pump is temporary). Ours: the
    # double block on Arni now costs them Geist, Tetsuko chumps once: 3,3,3,0,3,... = 8.
    "T8_BLOCKS_PUMPED_GEIST": (8, [17, 14, 11, 11, 8, 5], 4, [14, 9, 4, -1], (2, 2), (6, 5)),
    # Three unblockable 1-power attackers into 14 = 5. Ours: 4 through once (Oculus
    # and Arni die to blocks, their Geist dies), then our last flyer is walled by
    # Traxos 1/5 and Keeper by Traxos + Tetsuko: no clock.
    "T10_THREE_UNBLOCKABLE": (None, [16, 15, 15], 5, [11, 8, 5, 2, -1], (4, 3), (9, 3)),
    # Jiang (4/4 menace) is double-blocked every attack; the unblockable three still
    # connect for 3 into 14 = 5. Ours: 4 through once, then walled.
    "T12_JIANG": (None, [16, 16], 5, [11, 8, 5, 2, -1], (5, 4), (12, 7)),
    # Our attack: Traxos is the only untapped blocker (kills Oculus), 9 through; then
    # their three blockers absorb nine and die, then 12 into 8 = 3 attacks. Theirs:
    # 3 unblockable into 11 = 4.
    "T13_OUR_ATTACK": (3, [11, 8, -4], 4, [8, 5, 2, -1], (5, 3), (14, 3)),
    # Theirs: Puller, Traxos, Tetsuko, Geist are 6 unblockable into 7 life = 2 attacks
    # even with Koth and the Cadet blocked. Ours: 3 through into four kill-blocks,
    # Foreseer 3 more past Traxos/Tetsuko/Geist, then Traxos walls it: no clock.
    "T15_WIDE_BOARDS": (None, [14, 11, 11], 2, [1, -5], (6, 6), (17, 11)),
}


@pytest.mark.parametrize("name", list(HAND_COMPUTED))
def test_the_logged_clocks_match_the_hand_computed_model(name):
    ours, our_lives, theirs, their_lives, creatures, power = HAND_COMPUTED[name]
    clocks = _clocks(getattr(fx, name))
    assert (clocks.our_clock, clocks.our_lives) == (ours, our_lives)
    assert (clocks.their_clock, clocks.their_lives) == (theirs, their_lives)
    a = _assessment(getattr(fx, name))
    assert (a.our_creatures, a.their_creatures) == creatures
    assert (a.our_power, a.their_power) == power
    assert (a.our_clock, a.their_clock) == (ours, theirs)


def test_tetsukos_grant_marks_their_creatures_and_both_sides_are_annotated():
    model = build_board_model(deepcopy(fx.T15_WIDE_BOARDS))
    unblockable = {b["name"] for b in model.theirs if b["_unblockable"]}
    assert unblockable == {
        "Heartstring Puller", "Traxos, Academy Guardian", "Tetsuko Umezawa, Fugitive", "Geist of Saint Thalia"
    }  # fmt: skip
    assert not any(b["_unblockable"] for b in model.ours)
    # The mark alone (no Tetsuko text to parse) keeps their clock: 6 unblockable into 7.
    state = deepcopy(fx.T15_WIDE_BOARDS)
    for permanent in state["battlefield"]:
        if permanent["name"].startswith("Tetsuko"):
            permanent["oracle_text"] = ""
    assert _clocks(state).their_clock == 2


def test_their_sick_and_tapped_creatures_attack_next_turn_but_not_this_one():
    # Their T12 main phase: Jiang (entered T10) attacks now; on our turn their whole
    # board, tapped or not, is counted for their next attack.
    pending = build_board_model(deepcopy(fx.T12_JIANG))
    assert pending.their_attack_pending
    assert sorted(b["name"] for b in pending.first_their_attackers) == sorted(
        b["name"] for b in pending.theirs
    )
    ours = build_board_model(deepcopy(fx.T15_WIDE_BOARDS))
    assert ours.first_their_attackers is None and not ours.their_attack_pending
    assert {b["name"] for b in ours.theirs if b["_tapped"]} == {
        "Tetsuko Umezawa, Fugitive", "Geist of Saint Thalia"
    }  # fmt: skip
    clocks = _clocks(fx.T15_WIDE_BOARDS)
    assert clocks.their_clock == 2 and clocks.their_lives[0] == 1  # Tetsuko and Geist attack


def test_the_pump_is_what_changed_between_the_two_t8_facts():
    before, during = _clocks(fx.T8_TWO_UNBLOCKABLE), _clocks(fx.T8_BLOCKS_PUMPED_GEIST)
    assert (before.their_clock, during.their_clock) == (10, 4)
    assert before.our_clock == during.our_clock == 8


# --- the defects --------------------------------------------------------------------


def _body(name, power, toughness, instance_id, **extra):
    return {
        "instance_id": instance_id,
        "name": name,
        "power": power,
        "toughness": toughness,
        "oracle_text": "",
        "keywords": list(extra.pop("keywords", [])),
        "type_line": "Creature",
        "is_token": False,
        "_tapped": False,
        "_attacking": False,
        "_sick": False,
        "_can_attack": True,
        "_can_block": True,
        "_unblockable": False,
        **extra,
    }


def test_the_attacker_orders_its_damage_to_kill_blockers_whatever_order_they_come_in():
    arni = _body("Arni", 3, 2, 1)
    traxos, geist = _body("Traxos", 1, 5, 2), _body("Geist", 1, 2, 3)
    for blockers in ([traxos, geist], [geist, traxos]):
        outcome = _resolve_attacker(arni, blockers)
        assert [b["name"] for b in outcome.blockers_died] == ["Geist"] and outcome.attacker_died
    # The most kills, then the biggest bodies: 4 power into 2/1, 2/3 and 1/1 kills the
    # 2/1 and the 2/3 (1 + 3), not the 2/1 and the 1/1 with two damage wasted.
    emberling = _body("Emberling", 4, 4, 4)
    adept, phantasm, yuriko = _body("Adept", 2, 1, 5), _body("Phantasm", 2, 3, 6), _body("Yuriko", 1, 1, 7)
    died = _resolve_attacker(emberling, [adept, phantasm, yuriko]).blockers_died
    assert sorted(b["name"] for b in died) == ["Adept", "Phantasm"]
    # Deathtouch: a point each, so everything dies; indestructible is never worth the damage.
    stinger = _body("Stinger", 2, 2, 8, keywords=["deathtouch"])
    assert len(_resolve_attacker(stinger, [adept, phantasm]).blockers_died) == 2
    wall = _body("Wall", 0, 4, 9, keywords=["indestructible"])
    assert [
        b["name"] for b in _resolve_attacker(_body("Bear", 4, 2, 10), [wall, phantasm]).blockers_died
    ] == ["Phantasm"]


def test_held_back_attackers_are_not_sent_to_die_for_nothing():
    # Everyone: X kills A, Y stops B. Hold A back: X and Y double-block B and kill it.
    # Neither line gets damage through, so nobody attacks — B does not "die".
    a, b = _body("A", 3, 2, 1), _body("B", 2, 2, 2)
    x, y = _body("X", 4, 4, 3), _body("Y", 1, 3, 4)
    assert ba._attack_round([a, b], [x, y], 20) == (0, set(), set())
    assert ba._simulate_attacks([a, b], [x, y], 20, first_attackers=None, first_blockers=None) == (
        None,
        [20, 20],
    )


def test_a_chump_block_at_the_horizon_is_one_attack_of_nothing_not_no_clock():
    # A 3/2 into 20 life and six 0/1s that chump one by one: six attacks of nothing,
    # then seven attacks of three = 13. Before the fix the sixth attack (the horizon)
    # was extrapolated as the steady state: zero damage, "no clock".
    attacker = _body("Bear", 3, 2, 1)
    chumps = [_body(f"Wall {i}", 0, 1, 10 + i) for i in range(6)]
    clock, lives = ba._simulate_attacks([attacker], chumps, 20, first_attackers=None, first_blockers=None)
    assert clock == 13 and lives == [20] * ba._HORIZON


def test_a_lone_flyer_is_still_a_twenty_turn_clock_and_lethal_next_turn_probes_still_work():
    flyer = _body("Geist", 1, 2, 1, keywords=["flying"])
    bear = _body("Bear", 2, 2, 2)
    assert ba._simulate_attacks([flyer], [bear], 20, first_attackers=None, first_blockers=None)[0] == 20
    assert ba._simulate_attacks([flyer], [bear], 20, first_attackers=None, first_blockers=None, horizon=1) == (
        20, [19]
    )  # fmt: skip
    assert ba._simulate_attacks([flyer], [bear], 1, first_attackers=None, first_blockers=None, horizon=1) == (
        1, [0]
    )  # fmt: skip


def test_the_race_line_says_what_got_through_before_the_board_stalled():
    assert ba._stalled_text("we", [20, 20], 20) == "we get nothing through"
    assert (
        ba._stalled_text("we", [14, 11, 11], 17)
        == "we get 6 through before the blocks stall it (11 life left)"
    )
    a = _assessment(fx.T15_WIDE_BOARDS)
    assert a.our_clock is None and a.their_clock == 2
    assert a.race == "behind"
    assert a.race_detail == "they kill in 2, we get 6 through before the blocks stall it (11 life left)"
    assert "we get nothing through" not in a.facts_line()
    assert _assessment(fx.T4_GEIST).race_detail == "they kill in 20, we get nothing through"
