"""The opponent's attack in the lines, match 18b1601a (2026-10-19, ledger #95, #96, #97).

T9 (12:05:06): the autopilot went all in with Fleeting Effigy; the line
projected us at 8 after their T10 attack. T10 (12:05:38-12:05:56, their main
1): Ordeal of Nylea on the 4/5 Sazh's Chocobo; its attack put a fifth counter
on the Chocobo, sacrificed the Ordeal and fetched two Forests; the Chocobo's
two landfall counters (7/8) and Tifa Lockhart's two doublings (1 -> 4) put 11
into our 9 life with all three of our creatures tapped.

- #95: a policy of theirs was "suicidal" from our board's crackback alone; our
  burn in hand (and the cast triggers it fires) never counted, and when every
  policy left them dead to our next turn the least damaging was assumed.
- #96: Tifa's "double its power", Traveling Chocobo's landfall doubling, and
  lands their permanents fetch (Escape Tunnel; an Ordeal its creature's next
  attack sacrifices) were not read.
- #97: their attack still to come this turn never got their land drop.
"""

from __future__ import annotations

from copy import deepcopy

from tests.race_boards_20261019 import OPPONENT, ORDEAL_T10, board, card

from arenamcp.board_model import build_board_model
from arenamcp.line_search import DEAD, search_lines
from arenamcp.line_search_moves import land_fetch, landfall_doubler, landfall_doubles


def _search(source: dict):
    return search_lines(build_board_model(deepcopy(source)), survival=True, lethal_now=False, hard_ms=1e9)


def _their_lands_played(source: dict, count: int) -> dict:
    source = deepcopy(source)
    for player in source["players"]:
        if not player["is_local"]:
            player["lands_played"] = count
    return source


# --- #96 ------------------------------------------------------------------------------------


def test_landfall_payoffs_are_read():
    assert landfall_doubles(card(1, "Tifa Lockhart", OPPONENT)) == 1
    assert landfall_doubles(card(2, "Sazh's Chocobo", OPPONENT)) == 0
    assert landfall_doubler(card(3, "Traveling Chocobo", OPPONENT))
    assert not landfall_doubler(card(4, "Sazh's Chocobo", OPPONENT))


def test_land_fetches_are_read():
    ordeal = card(5, "Ordeal of Nylea", OPPONENT)
    # Three counters sacrifice it: the attack's own counter must reach three.
    assert land_fetch(ordeal, {"counters": {"P1P1": 2}}) == 2
    assert land_fetch(ordeal, {"counters": {"+1/+1": 4}}) == 2
    assert land_fetch(ordeal, {"counters": {"P1P1": 1}}) == 0
    assert land_fetch(ordeal, None) == 0
    tunnel = {
        "name": "Escape Tunnel",
        "type_line": "Land",
        "oracle_text": "{T}, Sacrifice this land: Search your library for a basic land card, put it onto the "
        "battlefield tapped, then shuffle.\n{T}, Sacrifice this land: Target creature with power 2 or less "
        "can't be blocked this turn.",
    }
    assert land_fetch(tunnel) == 1
    # Ramp a spell or an enters trigger does is not a permanent's fetch.
    assert land_fetch({"name": "Ramp", "oracle_text": "Search your library for a basic land card, put it onto "
                       "the battlefield tapped, then shuffle."}) == 0  # fmt: skip


def test_t10_the_ordeal_attack_kills_us():
    # Their main 1, no land played: paranoid, they play one (#97) before the attack. Chocobo
    # 4/5 + the Ordeal's counter + three landfall counters = 8/9; Tifa 1 doubled three times = 8.
    result = _search(ORDEAL_T10)
    assert result.best.cls == DEAD and result.best.dead_turn == 10, result.best.summary()
    assert result.now_life == 9 - 16


def test_t10_as_it_happened_eleven_into_nine():
    # With their land drop already made, only the Ordeal's two Forests: 7/8 + 4 = 11 into 9.
    result = _search(_their_lands_played(ORDEAL_T10, 1))
    assert result.best.cls == DEAD and result.best.dead_turn == 10, result.best.summary()
    assert result.now_life == -2


def test_traveling_chocobo_doubles_every_landfall_trigger():
    # Our turn, nothing to block with: their next attack after one land drop. With the
    # Traveling Chocobo each land triggers twice: Chocobo 4/5 -> 6/7, Tifa 1 -> 4, plus its 3.
    ours = [(401, "Mountain", False, 1)]
    theirs = [(346, "Sazh's Chocobo", False, 2, {"power": 4, "toughness": 5, "counters": {"P1P1": 4}}),
              (426, "Tifa Lockhart", False, 6), (442, "Traveling Chocobo", False, 8)]  # fmt: skip
    source = board(turn=11, life=20, their_life=20, ours=ours, theirs=theirs, hand=[])
    assert _search(source).best.lives()[0] == 20 - 13
    # Without it: 5/6, Tifa 2, Chocobo gone: 7.
    single = board(turn=11, life=20, their_life=20, ours=ours, theirs=theirs[:2], hand=[])
    assert _search(single).best.lives()[0] == 20 - 7


# --- #97 ------------------------------------------------------------------------------------


def _chocobo_main1(**extra) -> dict:
    return board(
        turn=10, life=20, their_life=20, active=OPPONENT, ours=[(401, "Mountain", False, 1)],
        theirs=[(346, "Sazh's Chocobo", False, 2, {"power": 4, "toughness": 5, "counters": {"P1P1": 4}})],
        hand=[], **extra,
    )  # fmt: skip


def test_their_attack_still_to_come_gets_their_land_drop():
    assert _search(_chocobo_main1()).now_life == 15  # 5/6 after their land
    assert _search(_their_lands_played(_chocobo_main1(), 1)).now_life == 16  # land already played
    # Combat has begun: no land drop before this attack.
    assert _search(_chocobo_main1(phase="Phase_Combat", step="Step_BeginCombat")).now_life == 16


# --- #95 ------------------------------------------------------------------------------------


def _bears(hand: list[tuple], lands: int = 3) -> dict:
    # Their main 1. We are at 5 with a tapped 1/2 flier (it attacked) and an untapped 3/3; they
    # are at 3 with two 2/2s. All in, the 3/3 blocks one 2/2 and we go to 3, but our crackback
    # (1 in the air, 3 on the ground, nothing of theirs untapped) kills them. Keeping one 2/2
    # home, the attacker is blocked: we stay at 5 and the 2/2 at home stops our 3/3.
    return board(
        turn=10, life=5, their_life=3, active=OPPONENT,
        ours=[*[(401 + i, "Mountain", False, 1) for i in range(lands)],
              (320, "Slickshot Show-Off", True, 3), (321, "Hill Giant", False, 3)],
        theirs=[(501, "Forest", False, 2), (601, "Grizzly Bears", False, 4), (602, "Grizzly Bears", False, 4)],
        hand=hand,
    )  # fmt: skip


def test_a_crackback_they_can_survive_still_softens_their_attack():
    assert _search(_bears([])).now_life == 5


def test_burn_in_hand_makes_them_dead_anyway_so_they_attack_with_everything():
    # Boltwave (3 to each opponent) kills them on our turn whatever they keep home: they race.
    assert _search(_bears([(701, "Boltwave")])).now_life == 3
    # Without the mana to cast it, the Boltwave is no threat.
    assert _search(_bears([(701, "Boltwave")], lands=0)).now_life == 5


def test_t9_all_in_projects_their_whole_attack():
    # 18b1601a T9, the Effigy line the autopilot played (Hazard held): their T10 attack. Before,
    # "keep one home" left us at 8; with Hazard's 1 and its +2/+0 on each Slickshot they are dead
    # on our T11 whatever they keep home, so everything comes: Chocobo 5/6 and Tifa 2 (no
    # Traveling Chocobo after it blocked the Effigy) take us from 9 to 2.
    from tests.race_boards_20261019 import SLICKSHOTS_T9

    result = _search(SLICKSHOTS_T9)
    effigy = next(line for line in result.lines if line.steps and line.steps[0].casts == ("Fleeting Effigy",))
    assert effigy.steps[0].policy == "all" and effigy.steps[0].life_after == 2, effigy.summary()
