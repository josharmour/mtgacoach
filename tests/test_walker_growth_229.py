"""Ledger #229: planeswalker escalation in the 2-turn line search and the board clocks.

Match f5169733 T8-T19 (2026-10-23): every line scored "survives (life 20, 20,
20)" while Garruk, Curse Breaker ticked 5→7 and spawned beasts — passing tied
with developing and the walker snowballed unchecked; the T16 race projection
said "survives (11, 5, 1)" and we died T19 at -1. Presence was modelled
(#42/#91); the loyalty ability's token generation was not.

``walker_growth`` (beside ``cast_growth``) reads a planeswalker's token-making
loyalty abilities; ``Moves.walker_turn`` replays the loyalty cost each of their
turns in the search, and the board clocks pace a minus ability at one casting
per recharge by its plus ability (``_token_every``).
"""

from __future__ import annotations

from copy import deepcopy

from arenamcp import board_assessment as ba
from arenamcp.board_assessment import ClockEngines, _simulate_attacks, _walker_token_sources
from arenamcp.board_model import build_board_model
from arenamcp.line_search_moves import Moves, their_engines, walker_growth

ME, THEM = 2, 1

GARRUK_TEXT = (
    "Whenever a creature you control with power 4 or greater enters, draw a card.\n"
    "+2: Untap up to two target lands.\n"
    "−3: Create a 4/4 green Beast creature token with trample.\n"
    "−4: Until your next turn, whenever one or more creatures attack one of your opponents, "
    "those creatures get +2/+2 and gain trample until end of turn."
)


def _card(iid: int, name: str, seat: int, **extra) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        **extra,
    }


def _garruk(iid: int = 500, loyalty: int = 5, seat: int = THEM, text: str = GARRUK_TEXT) -> dict:
    return _card(
        iid,
        "Garruk, Curse Breaker",
        seat,
        type_line="Legendary Planeswalker — Garruk",
        card_types=["CardType_Planeswalker"],
        oracle_text=text,
        counters={"Loyalty": loyalty},
    )


def _land(iid: int, seat: int = ME) -> dict:
    return _card(iid, "Mountain", seat, type_line="Land — Mountain", card_types=["CardType_Land"])


def _board(*, ours=(), theirs=(), hand=(), turn: int = 10, life: int = 20, their_life: int = 15) -> dict:
    return {
        "match_id": "walker-growth-229",
        "local_seat_id": ME,
        "opponent_seat_id": THEM,
        "turn": {"turn_number": turn, "active_player": ME, "priority_player": ME,
                 "phase": "Phase_Main1", "step": ""},
        "players": [
            {"seat_id": ME, "life_total": life, "is_local": True, "lands_played": 1},
            {"seat_id": THEM, "life_total": their_life, "is_local": False, "lands_played": 0},
        ],
        "battlefield": [*ours, *theirs],
        "hand": list(hand),
        "graveyard": [],
        "zones": {"library_count": 30, "library_count_source": "log_zone_membership",
                  "opponent_hand_count": 5},
        "deck_cards": [],
    }  # fmt: skip


# --- the reader -----------------------------------------------------------------------------------


def test_walker_growth_reads_garruks_loyalty_abilities():
    plus, abilities = walker_growth(_garruk())
    assert plus == 2
    assert abilities == ((-3, 1, 4, 4),)


def test_walker_growth_reads_arena_style_text_without_brackets():
    text = "+2: Untap up to two target lands.\n-3: Create two 2/2 Knight creature tokens."
    plus, abilities = walker_growth(_garruk(text=text))
    assert plus == 2
    assert abilities == ((-3, 2, 2, 2),)


def test_walker_growth_skips_an_x_it_cannot_read_and_nonwalkers():
    _plus, abilities = walker_growth(_garruk(text="+1: Create X 1/1 Soldier creature tokens."))
    assert abilities == ()
    bear = _card(
        9,
        "Bear",
        THEM,
        type_line="Creature — Bear",
        card_types=["CardType_Creature"],
        oracle_text="Create a 2/2 token.",
    )
    assert walker_growth(bear) == (0, ())
    jace = _garruk(iid=9, text="+1: Draw a card.")
    assert walker_growth(jace) == (0, ())


def test_their_engines_collect_the_walker_token_abilities():
    engines = their_engines([_garruk(), _land(401)], ME, THEM)
    assert engines.walker_engines == ((500, 2, ((-3, 1, 4, 4),)),)


# --- the search replays the loyalty cost ----------------------------------------------------------


def _moves(source: dict) -> Moves:
    return Moves(
        build_board_model(deepcopy(source)), survival=False, lethal_now=False, hard_ms=1e9, opponent=None
    )


def _pass_turn(moves: Moves, node):
    mid = moves.begin(node, None, ())
    return moves.finish(mid, ("none", ()))


def test_the_search_projects_garruks_beasts_and_loyalty():
    # Paranoid cadence live at f5169733: −3 beast (5→2), +2 (2→4), −3 beast again (4→1).
    source = _board(ours=[_land(401), _land(402)], theirs=[_garruk()])
    moves = _moves(source)
    node = moves.root_node()
    node = _pass_turn(moves, node)
    assert node.walkers == ((500, "Garruk, Curse Breaker", 2),)
    assert [(b["name"], b["power"], b["toughness"]) for b in node.theirs] == [
        ("Garruk, Curse Breaker token", 4, 4)
    ]
    node = _pass_turn(moves, node)
    assert node.walkers == ((500, "Garruk, Curse Breaker", 4),)
    assert len(node.theirs) == 1  # the plus turn makes no beast
    node = _pass_turn(moves, node)
    assert node.walkers == ((500, "Garruk, Curse Breaker", 1),)
    assert len(node.theirs) == 2  # the second beast


def test_a_low_walker_ticks_instead_of_creating():
    # At loyalty 2 the −3 is unaffordable this turn: the plus gain (+2) instead.
    source = _board(ours=[_land(401)], theirs=[_garruk(loyalty=2)])
    moves = _moves(source)
    node = _pass_turn(moves, moves.root_node())
    assert node.walkers == ((500, "Garruk, Curse Breaker", 4),)
    assert list(node.theirs) == []


# --- the board clocks pace the minus ability ------------------------------------------------------


def test_the_clock_sim_paces_a_minus_ability_by_its_plus():
    # One 4/4 a turn from turn one kills in 5 attacks; paced at one casting per
    # plus-recharge (Garruk's −3 every second turn) it kills in 7.
    clocks = []
    for every in (1, 2):
        body = {"instance_id": 500, "name": "Garruk", "_tokens": 1, "_token_pt": (4, 4)}
        if every == 2:
            body["_token_every"] = 2
        clock, _lives = _simulate_attacks(
            [body], [], 40, first_attackers=None, first_blockers=[], engines=ClockEngines(), horizon=8
        )
        clocks.append(clock)
    assert clocks == [5, 7]


def test_walker_token_sources_skip_the_other_seat_and_unaffordable_minuses():
    sources = _walker_token_sources([_garruk(), _garruk(iid=501, seat=ME), _land(402)], THEM)
    assert [s["instance_id"] for s in sources] == [500]
    assert sources[0]["_tokens"] == 1 and sources[0]["_token_every"] == 2
    # At loyalty 1 the −3 is unaffordable even after the plus: no source this turn.
    assert _walker_token_sources([_garruk(loyalty=1)], THEM) == []


# --- the board facts see the escalation -----------------------------------------------------------


def test_the_board_facts_clock_sees_the_walker_escalation():
    source = _board(ours=[_land(401), _land(402)], theirs=[_garruk()])
    ba._CACHE.clear()
    assessment = ba.assess(deepcopy(source))
    # Before #229 this was their_clock None ("no clock") while Garruk ticked.
    assert assessment.their_clock is not None
    assert any("Garruk" in note for note in assessment.clock_engines)
    projections = assessment.lookahead
    assert projections[1].life_after < projections[0].life_after  # the beast's attack is projected
