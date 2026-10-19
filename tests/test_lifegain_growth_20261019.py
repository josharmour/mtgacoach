"""Their creatures that grow on their life gain (ledger #128, match 3e545401, 2026-10-19).

T17 (13:02:17): we were at 12 with Slickshot Show-Off; they had an 8/7 flying first-strike
Unflinching Hortimancer and a 2/2 Ajani's Pridemate ("Whenever you gain life, put a +1/+1
counter on this creature.") and Ajani Resolute, whose ability gains 1 life every turn. The
crackback guard counted 10 through for our Slickshot attack (13:02:20: "leaves us at 2"); on T18
Ajani's life gain made them 9/8 and 3/3: 12 into our 12. Their life-gain growth now comes before
each of their attacks (a gain ability of their planeswalker, their gain triggers, our creatures
under their Authority of the Consuls), after a lifelink hit of theirs, and not at all while
"players can't gain life" (Giant Cindermaw).
"""

from __future__ import annotations

from copy import deepcopy

from tests.engine_boards_20261019 import AJANI_RESOLUTE_TEXT, G3E5_T17
from tests.race_boards_20261019 import board, card

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model
from arenamcp.combat_solver import evaluate_attack
from arenamcp.line_search import DEAD, search_lines
from arenamcp.line_search_moves import Moves, gain_triggers, lifegain_growth, walker_gain


def _search(source: dict):
    return search_lines(build_board_model(deepcopy(source)), survival=True, lethal_now=False, hard_ms=1e9)


def _crackback(source: dict, attacking: bool, *, state: bool = True) -> int:
    snapshot = deepcopy(source)
    slickshot = next(c for c in snapshot["battlefield"] if c["name"] == "Slickshot Show-Off")
    theirs = [
        c for c in snapshot["battlefield"] if c["controller_seat_id"] == 2 and "Creature" in c["type_line"]
    ]
    untapped = [c for c in theirs if not c.get("is_tapped")]
    attack, home = ([slickshot], []) if attacking else ([], [slickshot])
    plan = evaluate_attack(attack, home, untapped, 14, 12, theirs, state=snapshot if state else None)
    return plan.worst_case_crackback


def test_lifegain_growth_reads_own_team_and_loyalty_triggers():
    assert lifegain_growth(card(1, "Unflinching Hortimancer", 2)) == (1, 0, (), False)
    assert lifegain_growth(card(2, "Ajani's Pridemate", 2)) == (1, 0, (), False)
    token = {
        "name": "Ajani's Pridemate",
        "oracle_text": "Whenever you gain life, put a +1/+1 counter on this token.",
    }
    assert lifegain_growth(token)[0] == 1
    assert lifegain_growth(card(3, "Lyra, Archangel of Dawn", 2)) == (0, 0, (("angel", 1),), False)
    ajani = {
        "name": "Ajani Resolute",
        "type_line": "Legendary Planeswalker — Ajani",
        "oracle_text": AJANI_RESOLUTE_TEXT,
    }
    assert lifegain_growth(ajani) == (0, 1, (), False)  # "put a loyalty counter on Ajani"
    once = {"name": "Once", "oracle_text": "Whenever you gain life for the first time each turn, put a +1/+1 "
            "counter on this creature."}  # fmt: skip
    assert lifegain_growth(once) == (1, 0, (), True)
    assert lifegain_growth(card(4, "Healer's Hawk", 2)) == (0, 0, (), False)
    # Their repeatable gains: Ajani's ability (Arena's text has no loyalty costs; Scryfall's "0:"),
    # Authority of the Consuls on each creature of ours, a creature of theirs entering.
    assert walker_gain(ajani) == 1
    assert walker_gain({**ajani, "oracle_text": "Whenever you gain life, put a loyalty counter on Ajani.\n0: You "
                        "gain 1 life."}) == 1  # fmt: skip
    assert walker_gain(card(5, "Unflinching Hortimancer", 2)) == 0
    assert gain_triggers(card(6, "Authority of the Consuls", 2)) == (0, 1, 1)
    feast = {
        "name": "Case of the Uneaten Feast",
        "oracle_text": "Whenever a creature you control enters, you gain 1 life.",
    }
    assert gain_triggers(feast) == (1, 0, 0)


def test_t17_attacking_with_slickshot_is_dead_to_their_grown_crackback():
    result = _search(G3E5_T17)
    attack = result.first_action[("attack", frozenset({347}))]
    # Ajani's life gain on T18: Hortimancer 9/8 + Pridemate 3/3 = 12 into our 12 (was "2 left").
    assert attack.cls == DEAD and attack.dead_turn == 18, attack.summary()
    hold = result.first_action[("noattack",)]
    # Held, Slickshot chumps the Hortimancer and the 3/3 Pridemate gets through: 9 (was 10).
    assert hold.steps[0].life_after == 9, hold.summary()


def test_t17_crackback_guard_counts_their_growth():
    # The guard's evaluate_attack (action_planner._check_attack_crackback) with the snapshot.
    assert _crackback(G3E5_T17, attacking=True) == 12
    assert _crackback(G3E5_T17, attacking=False) == 3
    assert _crackback(G3E5_T17, attacking=True, state=False) == 10  # the board as it stands


def test_t17_board_facts_name_the_growth_in_their_clock():
    ba._CACHE.clear()
    facts = ba.assess(deepcopy(G3E5_T17))
    assert facts.their_next_attack == (3, 9)
    assert (
        "their clock counts: Unflinching Hortimancer grows +1/+1 a turn, Ajani's Pridemate grows +1/+1 a turn"
        in (facts.facts_line())
    )
    hortimancer = next(t for t in facts.threats if t.name == "Unflinching Hortimancer")
    assert "grows +1/+1 each turn of theirs" in hortimancer.why


def test_players_cant_gain_life_stops_the_growth():
    # Our Giant Cindermaw on the battlefield (it was exiled by their Stasis Snare on T16).
    source = deepcopy(G3E5_T17)
    source["battlefield"].append(
        card(360, "Giant Cindermaw", 1, is_tapped=False, turn_entered_battlefield=13)
    )
    model = build_board_model(deepcopy(source))
    assert model.engines.no_gain and all(not b["_growth"] for b in model.theirs)
    # Slickshot attacks: the 8/7 flier gets through, the Cindermaw blocks the 2/2 Pridemate (with
    # growth: 9 through, 3 left).
    attack = _search(source).first_action[("attack", frozenset({347}))]
    assert attack.steps[0].life_after == 4, attack.summary()
    assert _crackback(source, attacking=True) == 10


def test_a_lifelink_hit_grows_their_pridemate_for_the_next_attack():
    # 3e545401 T2-T5 style: their Healer's Hawk (1/1 flying lifelink) beside a Pridemate, no gain
    # source but the Hawk. Each Hawk hit grows the Pridemate before their next attack.
    source = board(
        turn=9, life=20, their_life=20, ours=[(401, "Mountain", False, 1)],
        theirs=[(501, "Healer's Hawk", False, 2), (502, "Ajani's Pridemate", False, 4)], hand=[],
    )  # fmt: skip
    result = _search(source)
    assert result.best.lives()[:3] == [17, 13, 8], result.best.summary()  # 3, then 1 + 3, then 1 + 4
    model = build_board_model(deepcopy(source))
    clock, lives = ba._simulate_attacks(
        list(model.theirs), [], 20, first_attackers=None, first_blockers=None,
        engines=ba.their_clock_engines(model),
    )  # fmt: skip
    assert lives[:3] == [17, 13, 8] and clock == 5


def test_their_walker_grows_on_each_life_gain_event():
    # Fleeting Effigy cast under Authority of the Consuls: Ajani's 1 life and the Consuls' 1 life
    # are two gain events, so Ajani goes 2 -> 4 by our next turn (a plus ability alone: 3).
    model = build_board_model(deepcopy(G3E5_T17))
    moves = Moves(model, survival=True, lethal_now=False, hard_ms=1e9, opponent=None)
    moves.free = True
    root = moves.root_node()
    effigy = next(i for i, hs in enumerate(moves.spells) if hs.name == "Fleeting Effigy")
    cast = next(c for c in moves.cast_sets(root, moves.sources(root, None)[1], instant_only=False)
                if [moves.spells[v.spell].name for v, _ in c] == ["Fleeting Effigy"])  # fmt: skip
    after_cast = moves.finish(moves.begin(root, None, cast), ("none", ()))
    after_hold = moves.finish(moves.begin(root, None, ()), ("none", ()))
    assert dict((iid, loyal) for iid, _n, loyal in after_cast.walkers) == {341: 4}
    assert dict((iid, loyal) for iid, _n, loyal in after_hold.walkers) == {341: 3}
    assert effigy in after_cast.hand
