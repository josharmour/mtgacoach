"""A drain counted by their creatures: The Scarab God (ledger #197).

Match 45de8f40 (2026-10-19, Brawl): "At the beginning of your upkeep, each
opponent loses X life and you scry X, where X is the number of Zombies you
control." drained us 3, 4, 5 and 7 (Player.log gsid 420: 18 -> 13; T20: 6 ->
-1). ``_drain_engine`` read only printed amounts, so the board facts said
"they kill us in 7 attack(s)" and "hold: survives (life 11, 11, 11, 11)" at
T18 (17:28:25), and at T19 (17:29:36-42) we held 36 attacking power home at 6
life ("Stabilize at 6 life behind the wide wall"): the all-in rule never fired.
Boards: ``tests/threat_boards_20261019.py`` (rebuilt from Player.log).
"""

from __future__ import annotations

import pytest

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model
from arenamcp.line_search_moves import counted_drain_engine, drain_engine
from arenamcp.play_safety import engine_fact
from threat_boards_20261019 import scarab_god

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


def the_god(board):
    return next(c for c in board["battlefield"] if c["instance_id"] == 531)


def test_the_x_drain_is_read_with_the_kind_it_counts():
    god = the_god(scarab_god())
    assert counted_drain_engine(god) == ((1, "at the beginning of your upkeep", "zombie"),)
    assert drain_engine(god) == ()  # the printed-amount reading is unchanged
    goblins = {
        "name": "Goblin Warcaller",
        "oracle_text": "At the beginning of your end step, this creature deals X damage to each opponent, "
        "where X is the number of Goblins you control.",
    }
    assert counted_drain_engine(goblins) == ((1, "at the beginning of your end step", "goblin"),)
    each = {
        "name": "Zombie Chant",
        "oracle_text": "At the beginning of your upkeep, each opponent loses 1 life for each Zombie you control.",
    }
    assert counted_drain_engine(each) == ((1, "at the beginning of your upkeep", "zombie"),)
    # An X it can't count on their board stays unread.
    hand = {
        "name": "Hand Drain",
        "oracle_text": "At the beginning of your upkeep, each opponent loses X life, where X is the number of "
        "cards in your hand.",
    }
    assert counted_drain_engine(hand) == ()


def test_their_engines_count_their_zombies():
    for our_attack, zombies in ((False, 5), (True, 7)):
        board = scarab_god(our_attack=our_attack)
        model = build_board_model(board)
        assert model.engines.body_drain(the_god(board)) == zombies
        god = next(b for b in model.theirs if b["instance_id"] == 531)
        assert god["_drain"] == zombies


def test_the_t18_clock_counts_the_drain():
    # Before #197: "they kill us in 7 attack(s)", "hold: survives (life 11, 11, 11, 11)".
    # With #198: on T18 Main 1 (after T18's upkeep already drained 18 -> 13), T18's upkeep drain is
    # not counted a second time before T18's attack (our_life_now_attack == 11 after 2 combat damage,
    # 4-attack clock), while at Step_Upkeep it is still to come this turn (our_life_now_attack == 6,
    # 3-attack clock).
    ba._CACHE.clear()
    facts = ba.assess(scarab_god())
    assert facts.our_life_now_attack == 11
    assert facts.their_clock == 4
    assert "The Scarab God drains 5 a turn" in facts.clock_engines
    assert facts.dead_in == 4
    upkeep_board = scarab_god()
    upkeep_board["turn"] = {**upkeep_board["turn"], "phase": "Phase_Beginning", "step": "Step_Upkeep"}
    upkeep_facts = ba.assess(upkeep_board)
    assert upkeep_facts.our_life_now_attack == 6
    assert upkeep_facts.their_clock == 3
    assert upkeep_facts.dead_in == 3


def test_t19_dead_to_the_upkeep_drain_whatever_we_block_is_all_in():
    # Seven Zombies drain 7 at their upkeep against our 6: no block or hold changes it.
    ba._CACHE.clear()
    facts = ba.assess(scarab_god(our_attack=True))
    assert facts.dead_in == 1
    assert facts.all_in
    assert facts.role == "aggressor"
    assert any(flag.startswith("ALL-IN") for flag in facts.flags)


def test_the_engine_fact_names_the_count():
    assert engine_fact(the_god(scarab_god())) == (
        "X damage to you at the beginning of their upkeep (X = the zombies they control)"
    )
