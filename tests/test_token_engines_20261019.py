"""Their token engines: value, projection and the gang-block kill (ledger #172, match 2b77f14d).

The Unbeatable Squirrel Girl ("{1}{G}{G}{G}: Create X 1/1 green Squirrel creature tokens, where X
is the number of Squirrels you control.") with Caged Sun doubling their Forests made 4, then 8
Squirrels (and one on her attack) on T9. The block solver's value table gave her a flat +3 for
"token production", and on T7 three untapped Hare Apparents, which could have killed her, stayed
home while two Rabbits chumped. A token engine is now worth its tokens a turn: X counts their
permanents of that type (and grows with the tokens it makes), times the activations their mana
pays for; the tokens join their board after their turn in the lines and their clock, join their
crackback when they have the mana up at our end step, and the declare-blockers prompt names a
gang-block kill on an engine.
"""

from __future__ import annotations

from copy import deepcopy

from tests.engine_boards_20261019 import G2B7_T7
from tests.race_boards_20261019 import card

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model, engine_facts
from arenamcp.combat_solver import _material, evaluate_attack, gang_block_kill, optimal_blocks
from arenamcp.line_search_moves import Moves, token_engine

US, THEM = 1, 2


def _creatures(state: dict, seat: int) -> list[dict]:
    return [
        c for c in state["battlefield"] if c["controller_seat_id"] == seat and "Creature" in c["type_line"]
    ]


def _our_turn_8(*, mana_up: bool) -> dict:
    """T8 as it was (our main phase, their attackers tapped), every land of theirs untapped or not."""
    state = deepcopy(G2B7_T7)
    state["turn"] = {
        "turn_number": 8,
        "active_player": US,
        "priority_player": US,
        "phase": "Phase_Main1",
        "step": "",
    }
    for entry in state["battlefield"]:
        entry["is_attacking"] = False
        if entry["controller_seat_id"] == THEM and "Land" in entry["type_line"]:
            entry["is_tapped"] = not mana_up
    return state


def test_token_engine_reads_activated_and_recurring_token_makers():
    girl = token_engine(card(1, "The Unbeatable Squirrel Girl", THEM))
    assert girl == (
        (None, False, 1, "", 1, 1, "green squirrel"),  # "Whenever ~ enters or attacks, create a 1/1 ..."
        (4, False, 0, "squirrel", 1, 1, "green squirrel"),  # {1}{G}{G}{G}: X Squirrels
    )
    assert token_engine(card(2, "Hare Apparent", US)) == ()  # an enters trigger, once
    tapper = {"name": "Tapper", "oracle_text": "{2}, {T}: Create a 1/1 white Soldier creature token."}
    assert token_engine(tapper) == ((2, True, 1, "", 1, 1, "white soldier"),)
    fodder = {
        "name": "Fodder",
        "oracle_text": "{1}, Sacrifice a creature: Create a 2/2 black Zombie creature token.",
    }
    assert token_engine(fodder) == ()


def test_t7_squirrel_girl_is_worth_her_thirteen_squirrels():
    model = build_board_model(deepcopy(G2B7_T7))
    engines = model.engines
    # Five Forests doubled by Caged Sun and Llanowar Elves: 11 mana, two activations; X = 4
    # Squirrels (her, Bakersbane Duo, two tokens), then 8: 4 + 8 + her attack's 1 (the real T9).
    assert (engines.mana, engines.mana_now, engines.kinds["squirrel"]) == (11, 0, 4)
    girl = next(b for b in model.theirs if b["name"] == "The Unbeatable Squirrel Girl")
    assert (girl["_tokens"], girl["_token_pt"]) == (13, (2, 2))
    assert ba.engine_role(girl) == 12.0  # half the material of eight 2/2s
    ba._CACHE.clear()
    threats = ba.assess(deepcopy(G2B7_T7)).threats
    assert threats[0].name == "The Unbeatable Squirrel Girl"
    assert "token engine: ~13 2/2 tokens each turn of theirs (must-kill)" in threats[0].why
    # The block solver's value table: was 15 (5/5 + 2 + a flat 3 for "token production").
    raw = next(c for c in G2B7_T7["battlefield"] if c["name"] == "The Unbeatable Squirrel Girl")
    assert _material(raw) == 15
    assert _material({**raw, **engine_facts(deepcopy(G2B7_T7))[463]}) == 27


def test_t7_the_hares_can_kill_her_and_the_prompt_says_so():
    from arenamcp.coach import CoachEngine

    state = deepcopy(G2B7_T7)
    ours, theirs = _creatures(state, US), _creatures(state, THEM)
    girl = next(c for c in theirs if c["name"] == "The Unbeatable Squirrel Girl")
    chosen, killed = gang_block_kill(girl, ours)
    assert [c["name"] for c in chosen] == ["Hare Apparent"] * 3 and killed == 2
    attacking = [c for c in theirs if c.get("is_attacking")]
    plan = optimal_blocks(
        [{**c, **engine_facts(state).get(c["instance_id"], {})} for c in attacking], ours, 21
    )
    assert sorted(b for b, a in plan.assignments.items() if a == 463) == [473, 476, 480]
    local = next(p for p in state["players"] if p["is_local"])
    our_cards = [c for c in state["battlefield"] if c["controller_seat_id"] == US]
    their_cards = [c for c in state["battlefield"] if c["controller_seat_id"] == THEM]
    lines = CoachEngine.__new__(CoachEngine)._format_block_combat(
        our_cards, their_cards, local, 7, "Combat", set(), {}, state
    )
    assert (
        "Gang-block kill available on The Unbeatable Squirrel Girl 5/5 (token engine: ~13 2/2 tokens on their "
        "next turn): Hare Apparent + Hare Apparent + Hare Apparent deal 6 into its 5 toughness; it kills at most "
        "2 of them." in "\n".join(lines)
    ), lines
    # No engine attacking, no line.
    plain = [c for c in their_cards if c["name"] != "The Unbeatable Squirrel Girl"]
    assert not any(
        line.startswith("Gang-block kill")
        for line in CoachEngine.__new__(CoachEngine)._format_block_combat(
            our_cards, plain, local, 7, "Combat", set(), {}, {**state, "battlefield": our_cards + plain}
        )
    )


def test_their_tokens_join_their_board_after_their_turn():
    # The lines on T8: after their T9 the eight Squirrel bodies (the cap) block and attack.
    model = build_board_model(_our_turn_8(mana_up=False))
    moves = Moves(model, survival=True, lethal_now=False, hard_ms=1e9, opponent=None)
    moves.free = True
    tokens = moves.their_tokens(model.theirs, 1)
    assert len(tokens) == 8 and {(b["power"], b["toughness"]) for b in tokens} == {(2, 2)}
    node = moves.finish(moves.begin(moves.root_node(), None, ()), ("none", ()))
    assert sum(1 for b in node.theirs if b["instance_id"] in {t["instance_id"] for t in tokens}) == 8
    # Their attack under way on T7 (every land tapped): no tokens that turn.
    t7 = Moves(
        build_board_model(deepcopy(G2B7_T7)), survival=True, lethal_now=False, hard_ms=1e9, opponent=None
    )
    assert t7.their_tokens(t7.model.theirs, 0, now=True) == []
    # Their clock: the tokens attack from their second attack on.
    plain_clock = ba._simulate_attacks(list(model.theirs), list(model.ours), 21, first_attackers=None,
                                       first_blockers=None)  # fmt: skip
    clock = ba._simulate_attacks(list(model.theirs), list(model.ours), 21, first_attackers=None,
                                 first_blockers=None, engines=ba.their_clock_engines(model))  # fmt: skip
    assert plain_clock == (5, [21, 17, 13, 5, -3]) and clock == (3, [21, 3, -21]), (plain_clock, clock)


def test_their_mana_up_at_our_end_step_puts_tokens_in_the_crackback():
    def crackback(state: dict, *, engines: bool) -> int:
        ours, theirs = _creatures(state, US), _creatures(state, THEM)
        hares = [c for c in ours if c["name"] == "Hare Apparent"]
        home = [c for c in ours if c not in hares]
        untapped = [c for c in theirs if not c.get("is_tapped")]
        plan = evaluate_attack(hares, home, untapped, 25, 21, theirs, state=state if engines else None)
        return plan.worst_case_crackback

    tapped, up = _our_turn_8(mana_up=False), _our_turn_8(mana_up=True)
    assert crackback(tapped, engines=True) == crackback(tapped, engines=False) == 6
    # Eleven mana up: two activations at our end step, 4 + 8 Squirrels (8 of them counted) attack.
    assert (crackback(up, engines=True), crackback(up, engines=False)) == (18, 6)
