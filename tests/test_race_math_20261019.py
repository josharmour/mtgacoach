"""Race math from the 2026-10-19 Mono-Red Burn ladder run (autopilot-improvements ledger).

#10 lifelink: match 41753907 T7 (09:02:26) the LINES said "Fleeting Effigy +
Slickshot Show-Off; then Burst Lightning (on opponent) + Lightning Strike (o… —
lethal on T9" against a 4/4 flying lifelink Dawnwing Marshal whose 4 life a
turn no line counted; it gained the opponent 29 life over the game. Combat in
the lines, the clocks and the race term now heal each side by its lifelink
damage.

#11 combined burn: removal was one spell per target, so Lightning Strike (3) +
Burst Lightning (2) on that 4/4 at T7, and Lightning Strike + Shock on it as a
5/5 at T11 (we were at 6 and every line died on T14), were never offered. The
boards are in tests/race_boards_20261019.py.
"""

from __future__ import annotations

from copy import deepcopy

from tests import race_boards_20261019 as rb
from tests.race_boards_20261019 import BURN_HAND, MARSHAL_T7, MARSHAL_T11, board, card
from tests.strategic_states import actions_decision, cast, cpu_ms, latency_bound

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model
from arenamcp.line_guard import option_note
from arenamcp.line_search import DEAD, WIN, evaluate_plan, search_lines
from arenamcp.line_search_moves import Moves


def _body(name: str, iid: int, controller: int = rb.OPPONENT, **extra) -> dict:
    found = ba._body(card(iid, name, controller, turn_entered_battlefield=1, **extra), 10, {})
    assert found is not None
    return found


def _search(source: dict):
    survival, lethal_now = ba.search_hints(build_board_model(deepcopy(source)))
    return search_lines(
        build_board_model(deepcopy(source)), survival=survival, lethal_now=lethal_now, hard_ms=1e9
    )


def _lines(result) -> list:
    return [result.best, result.baseline, *result.lines, *result.first_action.values()]


def _first_targets(line) -> list[tuple[str, str]]:
    return sorted(line.steps[0].targets) if line.steps else []


# --- #10 lifelink -------------------------------------------------------------------------


def test_combat_heals_each_side_by_its_lifelink_damage():
    marshal = _body("Dawnwing Marshal", 1, **rb._MARSHAL_44)
    bears = _body("Grizzly Bears", 2, rb.LOCAL)
    # Unblocked: 4 through, the attacker's side gains 4.
    assert ba._combat_life([marshal], [], 20) == (4, set(), set(), 4, 0)
    # A lifelink blocker (a chump at 3 life) heals the defender by the damage it deals.
    giant = _body("Hill Giant", 3, rb.LOCAL)
    linked = {**_body("Grizzly Bears", 4), "keywords": ["lifelink"]}
    damage, _dead_att, dead_blk, gained, healed = ba._combat_life([giant], [linked], 3)
    assert (damage, dead_blk, gained, healed) == (0, {4}, 0, 2)
    # Killed by a first striker before it deals damage: no life.
    striker = {**giant, "keywords": ["first strike"]}
    assert ba._combat_life([striker], [linked], 3)[:5] == (0, set(), {4}, 0, 0)
    # The three-value form is unchanged for callers that ignore lifelink.
    assert ba._combat([marshal], [bears], 20) == (4, set(), set())


def test_a_clock_that_only_keeps_pace_with_a_lifelinker_is_no_clock():
    bears = _body("Grizzly Bears", 2, rb.LOCAL)
    # 2 a turn against 10 life is a 5-attack clock; against 4 healed between attacks, none.
    assert ba._simulate_attacks([bears], [], 10, first_attackers=None, first_blockers=None)[0] == 5
    assert (
        ba._simulate_attacks([bears], [], 10, first_attackers=None, first_blockers=None, drain=4)[0] is None
    )
    # A faster clock still gets there, later: 6 a turn against 4 healed.
    giants = [_body("Hill Giant", 10 + i, rb.LOCAL) for i in range(2)]
    assert ba._simulate_attacks(giants, [], 10, first_attackers=None, first_blockers=None)[0] == 2
    assert ba._simulate_attacks(giants, [], 10, first_attackers=None, first_blockers=None, drain=4)[0] == 3
    marshal = _body("Dawnwing Marshal", 1, **rb._MARSHAL_44)
    assert ba.lifelink_drain([marshal], [bears], 20) == 4
    assert ba.lifelink_drain([bears], [marshal], 20) == 0


def test_their_lifelink_attack_heals_them_in_the_line():
    # T7: we cast nothing and hold; the Marshal attacks on T8 for 4 and gains 4.
    model = build_board_model(deepcopy(MARSHAL_T7))
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None)
    moves.free = True
    node = moves.finish(moves.begin(moves.root_node(), None, ()), ("none", ()))
    assert (node.our_life, node.opp_life) == (12, 14)


def test_our_lifelink_attack_heals_us_in_the_line():
    source = board(
        turn=7, life=5, their_life=20, lands_played=1,
        ours=[(401, "Mountain", False, 1), (294, "Dawnwing Marshal", False, 3, rb._MARSHAL_44)],
        theirs=[(601, "Hill Giant", False, 2)], hand=[],
    )  # fmt: skip
    model = build_board_model(source)
    moves = Moves(model, survival=True, lethal_now=False, hard_ms=1e9, opponent=None)
    moves.free = True
    mid = moves.begin(moves.root_node(), None, ())
    attack = next(option for option in moves.attack_choices(mid)[0] if option[0] == "all")
    node = moves.finish(mid, attack)
    # 5 + 4 (lifelink) - 3 (their Giant) = 6; before, 5 - 3 = 2.
    assert node.history[0].opp_life_after == 16
    assert node.our_life == 6


# --- #11 combined burn --------------------------------------------------------------------


def test_t7_strike_and_burst_on_the_marshal_is_a_line():
    result = _search(MARSHAL_T7)
    # Since ledger #94 the casts pump Emberheart Challenger (prowess) and the Slickshot cast
    # with them, and burn at the face is lethal on T9 (no line said that before): the race
    # lines are now the best for every action key. The combined kill is still one of the T
    # step's own cast sets, and its line still clears the Marshal.
    search = result._search
    root = search.root_node()
    _base, sources = search.sources(root, None)
    combined = [
        choice
        for choice in search.cast_sets(root, sources, instant_only=False)
        if sorted((search.spells[var.spell].name, target["name"]) for var, target in choice if target)
        == [("Burst Lightning", "Dawnwing Marshal"), ("Lightning Strike", "Dawnwing Marshal")]
    ]
    assert combined, "Strike + Burst on the Marshal is not a T-step cast set"
    plan = [
        {
            "label": "T",
            "cast": ["Burst Lightning (on Dawnwing Marshal)", "Lightning Strike (on Dawnwing Marshal)"],
        }
    ]
    line = evaluate_plan(result, plan).line
    assert line is not None and _first_targets(line) == [
        ("Burst Lightning", "Dawnwing Marshal"),
        ("Lightning Strike", "Dawnwing Marshal"),
    ]
    # The Marshal is gone after the T step: nothing of theirs attacks on T8.
    assert line.steps[0].their_creatures_after == 0 and line.steps[0].life_after == 16, line.summary()
    assert result.best.cls == WIN and result.best.win_turn == 9, result.best.summary()


def test_t11_the_only_way_to_live_is_two_burn_spells_on_the_marshal():
    result = _search(MARSHAL_T11)
    best = result.best
    assert best.cls != DEAD, best.summary()
    killed = {target for name, target in best.steps[0].targets}
    assert killed == {"Dawnwing Marshal"} and len(best.steps[0].targets) == 2, best.summary()
    # Before: every line died on T14 (no single spell kills a 5/5).
    assert result.baseline.cls == DEAD and result.greedy_dies


def test_t11_both_burn_spells_carry_the_best_line_tag():
    result = _search(MARSHAL_T11)
    state = deepcopy(MARSHAL_T11)
    first = {name for name, target in result.best.steps[0].targets if target == "Dawnwing Marshal"}
    assert len(first) == 2, result.best.summary()
    ids = {"Burst Lightning": 318, "Lightning Strike": 350, "Shock": 336}
    decision = actions_decision(
        [(f"idx:{i}", f"Cast {name}", cast(ids[name]), True) for i, name in enumerate(ids)]
    )
    tags = {
        option.label.removeprefix("Cast "): option_note(result, option, state) for option in decision.options
    }
    assert all(tags[name].startswith("[LINE best: ") for name in first), tags


def test_hexproof_from_instants_is_never_targeted_by_instant_burn():
    elenda = _body("Elenda, Saint of Dusk", 9)
    strike, shock = card(1, "Lightning Strike", rb.LOCAL), card(2, "Shock", rb.LOCAL)
    joke = {"name": "Impractical Joke", "type_line": "Sorcery", "mana_cost": "{R}",
            "oracle_text": "Damage can't be prevented this turn. Impractical Joke deals 3 damage to up to one target "
                           "creature or planeswalker."}  # fmt: skip
    assert ba.burn_damage(strike, elenda) == 0 and ba.burn_damage(shock, elenda) == 0
    assert ba.burn_damage(joke, elenda) == 3
    small = {**elenda, "toughness": 3}
    assert not ba._kills(strike, small) and ba._kills(joke, small)
    source = board(
        turn=9, life=10, their_life=10, ours=[*rb._THREE_MOUNTAINS], theirs=[(337, "Elenda, Saint of Dusk", False, 7)],
        hand=[(221, "Lightning Strike"), (318, "Burst Lightning")],
    )  # fmt: skip
    result = _search(source)
    assert not any(
        target == "Elenda, Saint of Dusk"
        for line in _lines(result)
        for s in line.steps
        for _n, target in s.targets
    )


def test_indestructible_and_protected_creatures_take_no_pieces():
    giant = _body("Hill Giant", 5, keywords=["indestructible"])
    strike = card(1, "Lightning Strike", rb.LOCAL)
    assert ba.burn_damage(strike, giant) == 0
    warded = _body("Hill Giant", 6, oracle_text="Protection from red")
    assert ba.burn_damage(strike, warded) == 0 and not ba._kills(strike, {**warded, "toughness": 2})
    # A grant to other creatures is not this body's protection.
    granting = _body("Hill Giant", 7, oracle_text="Other creatures you control have protection from red.")
    assert ba.burn_damage(strike, granting) == 3


def test_damage_already_marked_this_turn_completes_a_kill():
    # A 4/4 with 2 damage marked dies to Shock alone (2 + 2), our turn only.
    source = board(
        turn=9, life=10, their_life=20, ours=[(401, "Mountain", False, 1)],
        theirs=[(601, "Hill Giant", False, 2, {"power": 4, "toughness": 4, "damage": 2})], hand=[(336, "Shock")],
    )  # fmt: skip
    result = _search(source)
    assert ("Shock", "Hill Giant") in result.best.steps[0].targets, result.best.summary()
    assert result.best.steps[0].their_creatures_after == 0


def test_a_combined_kill_uses_no_spare_piece():
    # Shock + Shock + Lightning Strike on a 4/5 (7 damage): one Shock is spare, never a set.
    source = board(
        turn=9, life=20, their_life=20, ours=[*rb._THREE_MOUNTAINS, (404, "Mountain", False, 7)],
        theirs=[(601, "Hill Giant", False, 2, {"power": 4, "toughness": 5})],
        hand=[(701, "Shock"), (702, "Shock"), (703, "Lightning Strike")],
    )  # fmt: skip
    model = build_board_model(source)
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None)
    moves.free = True
    node = moves.root_node()
    sets = moves.cast_sets(node, moves.sources(node, None)[1], instant_only=False)
    aimed = [sorted(moves.spells[v.spell].name for v, t in choice if v.chip) for choice in sets]
    assert ["Lightning Strike", "Shock"] in aimed and ["Shock", "Shock"] not in aimed
    assert ["Lightning Strike", "Shock", "Shock"] not in aimed


def test_a_plan_aiming_two_burn_spells_at_one_creature_replays_the_kill():
    result = _search(MARSHAL_T11)
    plan = [{"label": "T", "cast": ["Lightning Strike (on Dawnwing Marshal)", "Shock (on Dawnwing Marshal)"]}]
    evaluation = evaluate_plan(result, plan)
    assert evaluation.line is not None and evaluation.line.cls != DEAD, evaluation.issues
    assert sorted(evaluation.line.steps[0].targets) == [
        ("Lightning Strike", "Dawnwing Marshal"),
        ("Shock", "Dawnwing Marshal"),
    ]


def test_a_burn_heavy_hand_stays_within_the_latency_target():
    # Seven burn spells against four creatures no single spell kills: the combined-kill
    # pieces are pruned (two targets per spell, completable sets only) so the work budget,
    # not the clock, ends the search (~45 ms on an idle M-series core, 2026-10-19).
    model = build_board_model(deepcopy(BURN_HAND))
    results = []
    elapsed = cpu_ms(lambda: results.append(search_lines(model, survival=True, lethal_now=False)))
    assert all(not result.truncated for result in results)
    assert elapsed < latency_bound(80), elapsed
