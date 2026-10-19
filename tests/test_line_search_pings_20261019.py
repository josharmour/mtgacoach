"""Burn-deck cards the line search couldn't value (2026-10-19 Mono-Red Burn ladder run).

#19 Hired Claw: "Whenever you attack with one or more Lizards, this creature
deals 1 damage to target opponent." was "its damage to players" (34 skipped
game-plan line checks that morning; matches 7efad315 T2 and f9e0ec3b T8): the
line guard and the plan check never judged a Claw choice. The ping now hits
on each attack whose attackers meet its condition.

#20 "Burst Lightning never tagged LINE best" (f9e0ec3b packet decisions 3, 4,
9 and 12): not a key mismatch. ``action_key`` and ``play_keys`` agree; the
tag was demoted by ``ActionPlanner._line_tag`` because another option on the
menu was "not modelled": Boltwave ("3 damage to each opponent", which the
search does cast as face damage, flagged "its effect") or Hired Claw.

#43 Tectonic Hazard ("1 damage to each opponent and each creature they
control") was "its effect" too; its sweep now hits every creature of theirs
and adds to burn aimed at one (Hazard + Shock kills a 1/3).
"""

from __future__ import annotations

from copy import deepcopy

from tests import race_boards_20261019 as rb
from tests.race_boards_20261019 import board, card
from tests.strategic_states import actions_decision, cast, play

from arenamcp.action_planner import ActionPlanner, _unmodelled_options
from arenamcp.board_model import build_board_model
from arenamcp.line_guard import option_note
from arenamcp.line_search import WIN, action_key, search_lines
from arenamcp.line_search_moves import attack_pings, fully_applied, unmodelled_effect


def _search(source: dict, *, survival: bool = False):
    return search_lines(build_board_model(deepcopy(source)), survival=survival, lethal_now=False, hard_ms=1e9)


# --- #19 attack-trigger pings -------------------------------------------------------------


def test_attack_pings_read_who_must_attack():
    assert attack_pings(card(1, "Hired Claw", rb.LOCAL)) == ((1, "lizard"),)
    raider = {
        "name": "Goblin Raider",
        "type_line": "Creature — Goblin",
        "oracle_text": "Whenever Goblin Raider attacks, it deals 1 damage to each opponent.",
    }
    assert attack_pings(raider) == ((1, "self"),)
    drainer = {
        "name": "X",
        "type_line": "Creature",
        "oracle_text": "Whenever you attack, each opponent loses 2 life.",
    }
    assert attack_pings(drainer) == ((2, "any"),)
    # A condition or a variable amount is not a fixed ping.
    for text in (
        "Whenever you attack, if you control an artifact, X deals 1 damage to each opponent.",
        "Whenever X attacks, it deals damage equal to its power to each opponent.",
    ):
        assert attack_pings({"name": "X", "type_line": "Creature", "oracle_text": text}) == ()


def test_hired_claws_ping_is_lethal_with_its_own_attack():
    # They are at 2 with no creatures: the Claw's 1 combat damage plus its ping.
    source = board(
        turn=9, life=20, their_life=2, ours=[(401, "Mountain", False, 1), (559, "Hired Claw", False, 5)],
        theirs=[], hand=[],
    )  # fmt: skip
    result = _search(source)
    assert result.best.cls == WIN and result.best.win_at == 1, result.best.summary()
    assert result.posture == "lethal"
    assert unmodelled_effect(card(559, "Hired Claw", rb.LOCAL), result) == ""


def test_the_lizard_ping_needs_a_lizard_among_the_attackers():
    # The Claw is summoning-sick; Emberheart Challenger (a Mouse) attacks alone: no ping.
    source = board(
        turn=9, life=20, their_life=3, ours=[(401, "Mountain", False, 1), (559, "Hired Claw", False, 9),
                                             (289, "Emberheart Challenger", False, 3)],
        theirs=[], hand=[],
    )  # fmt: skip
    result = _search(source)
    assert not (result.best.cls == WIN and result.best.win_at == 1), result.best.summary()
    attacking = result.first_action[("attack", frozenset({289}))]
    assert attacking.steps[0].opp_life_after == 1, attacking.summary()


def test_their_attack_pings_hit_us():
    source = board(
        turn=9, life=2, their_life=20, ours=[(401, "Mountain", False, 1), (601, "Hill Giant", False, 2)],
        theirs=[(559, "Hired Claw", False, 5)], hand=[],
    )  # fmt: skip
    # Their Claw attacks into our untapped Giant: blocked, but the ping still lands; then
    # another ping next turn: at 2 life we are dead to two attacks however we block.
    result = _search(source)
    lives = result.best.lives()
    assert lives and lives[0] == 1, result.best.summary()


# --- #20 / #43 casts the search applies in full -------------------------------------------


def test_boltwave_and_tectonic_hazard_are_modelled():
    for name in ("Boltwave", "Tectonic Hazard"):
        spell = card(1, name, rb.LOCAL)
        assert fully_applied(spell) and unmodelled_effect(spell) == "", name
    # A sweep of creatures on both sides is not one the search applies.
    both = {
        "name": "Y",
        "type_line": "Sorcery",
        "oracle_text": "Y deals 2 damage to each creature and each player.",
    }
    assert not fully_applied(both)


def test_hazard_and_shock_kill_a_one_three_and_the_one_one():
    source = board(
        turn=9, life=20, their_life=20, ours=[(401, "Mountain", False, 1), (402, "Mountain", False, 1)],
        theirs=[(601, "Grizzly Bears", False, 2, {"power": 1, "toughness": 3}),
                (602, "Grizzly Bears", False, 2, {"power": 1, "toughness": 1})],
        hand=[(1, "Tectonic Hazard"), (2, "Shock")],
    )  # fmt: skip
    result = _search(source)
    first = result.best.steps[0]
    assert sorted(first.casts) == ["Shock", "Tectonic Hazard"], result.best.summary()
    assert first.their_creatures_after == 0 and first.life_after == 20


def _f9e0ec3b_t4() -> tuple[dict, object]:
    """f9e0ec3b decision 3 (09:15, T4): Burst Lightning x2, Boltwave and a Mountain on the menu
    against a 2/2 flier; the best line opened Mountain + Burst Lightning on it."""
    source = board(
        turn=4, life=20, their_life=12, lands_played=0, opponent_hand=5,
        ours=[(401, "Mountain", False, 1), (402, "Mountain", False, 3)],
        theirs=[(501, "Plains", False, 2), (305, "Dawnwing Marshal", False, 3)],
        hand=[(318, "Burst Lightning"), (319, "Burst Lightning"), (223, "Boltwave"), (340, "Mountain")],
    )  # fmt: skip
    decision = actions_decision(
        [
            ("idx:1", "Cast Burst Lightning", cast(318), True),
            ("idx:2", "Cast Burst Lightning", cast(319), True),
            ("idx:5", "Cast Boltwave", cast(223), True),
            ("idx:6", "Play land: Mountain", play(340), True),
            ("pass", "Pass", None, None),
        ]
    )
    return source, decision


def test_burst_lightning_carries_the_best_tag_next_to_boltwave():
    source, decision = _f9e0ec3b_t4()
    state = deepcopy(source)
    result = _search(source)
    unmodelled = _unmodelled_options(result, decision, state)
    assert unmodelled == {}, unmodelled
    tags = {o.option_id: ActionPlanner._line_tag(result, o, state, unmodelled) for o in decision.options}
    best_keys = result.best.first_actions
    for option in decision.options:
        key = action_key(option, state)
        if key is not None and key in best_keys:
            assert tags[option.option_id].startswith("[LINE best: "), (
                option.label,
                tags,
                result.best.summary(),
            )
    # Both copies of Burst share one key (the lowest instance id in hand).
    assert (
        action_key(decision.options[0], state)
        == action_key(decision.options[1], state)
        == ("cast", 318, None)
    )
    assert option_note(result, decision.options[0], state) == option_note(result, decision.options[1], state)
