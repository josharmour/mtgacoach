"""A surviving line that attacks is a race, not "survive first" (ledger #161, 2026-10-19).

Match c0af235f T11-T12 (15:21:09, 15:21:28): "CONTROL/STABILIZE — only line that
survives: attack with Hare Apparent, ... Leonardo, Big Brother, Warrior —
survives (life 17, 17, 17, 17; opponent at 12); hold: dead on T15". The game
plan copied the role ("role=control/stabilize"), its win condition became
"Stabilize behind Hare Apparent", and the T12 declaration echoed it ("Stabilize
behind the token wall") and held twenty attackers. be1a945f T12 had the same
label while nothing of ours could block their lethal fliers.

On master the T12 board (``tests.valkyrie_boards_20261019``, gsid 306) reads
"the greedy line dies; best surviving line: attack with Warrior, Rabbit, ...";
the label is now RACE ("attacking survives"). A surviving line that does not
attack (a life-gain cast) stays CONTROL.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from tests import valkyrie_boards_20261019 as vb

from arenamcp import board_assessment as ba
from arenamcp.game_plan import GAME_PLAN_PROMPT, GamePlanManager, validate_plan

# The lines these tests pin must not depend on a loaded machine (the hard cap is a clock).
pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


def _line(attack: tuple[str, ...]):
    step = SimpleNamespace(attack=attack, opp_life_after=20, life_after=10)
    return SimpleNamespace(steps=[step], outcome="alive", cls=1, win_at=None, win_turn=None)


def _role(**overrides):
    facts = dict(
        lethal_now=False, opp_lethal=False, dead_in=None, race="behind", our_clock=3, their_clock=2,
        our_power=20, their_power=14, ours=20, theirs=7, our_life=25, opp_life=32, our_lives=[20],
        their_lives=[26], card_advantage=0, deck_curve=1.3, lookahead=[], best_line=None,
        line_text="", race_term=None,
    )  # fmt: skip
    facts.update(overrides)
    return ba._role(**facts)


ATTACK = "attack with Warrior, Rabbit, Rabbit — survives (life 20, 15, 13; opponent at 26)"
GAIN = "Island + Archive Arbiter (gain 4 life) — survives (life 4, 2, 1)"


def test_the_t12_board_is_a_race():
    facts = ba.assess(vb.t12_declare_attackers())
    assert facts.role == ba.ROLE_RACE
    assert facts.role_reason.startswith(
        "the greedy line dies; attacking survives: attack with Warrior, Rabbit, Rabbit, Warrior"
    )
    assert not facts.role_reason.startswith("the greedy line dies; best surviving line")


def test_only_attacking_survives_is_a_race_and_a_surviving_cast_stays_control():
    role, reason = _role(only_survivor=True, best_line=_line(("Warrior", "Rabbit")), line_text=ATTACK)
    assert (role, reason) == (ba.ROLE_RACE, f"only attacking survives: {ATTACK}")
    role, reason = _role(only_survivor=True, best_line=_line(()), line_text=GAIN)
    assert (role, reason) == (ba.ROLE_CONTROL, f"only line that survives: {GAIN}")
    role, reason = _role(greedy_dies=True, best_line=_line(("Warrior",)), line_text=ATTACK)
    assert (role, reason) == (ba.ROLE_RACE, f"the greedy line dies; attacking survives: {ATTACK}")
    role, _reason = _role(greedy_dies=True, best_line=_line(()), line_text=GAIN)
    assert role == ba.ROLE_CONTROL


def test_their_lethal_board_with_only_an_attacking_survivor_is_a_race():
    lethal = dict(opp_lethal=True, our_life=17, their_lives=[-7])
    role, reason = _role(
        **lethal, only_first_attack_survivor=True, best_line=_line(("Hare Apparent",)), line_text=ATTACK
    )
    assert role == ba.ROLE_RACE
    assert reason == (
        f"only attacking survives: opponent has lethal on board (24 through our best blocks vs 17 life); "
        f"only line: {ATTACK}"
    )
    # A caveat (a cast the search can't value) keeps the old "best modelled line" control text.
    role, reason = _role(
        **lethal, only_first_attack_survivor=True, best_line=_line(("Hare Apparent",)), line_text=ATTACK,
        caveat="Pacifism",
    )  # fmt: skip
    assert role == ba.ROLE_CONTROL and "best modelled line" in reason


def test_their_lethal_fliers_nothing_of_ours_blocks_with_an_attacking_line_is_a_race():
    lethal = dict(opp_lethal=True, our_life=17, their_lives=[-7], evasive_clock=True)
    role, reason = _role(**lethal, best_line=_line(("Hare Apparent",)), line_text=ATTACK)
    assert role == ba.ROLE_RACE
    assert reason == (
        "opponent has lethal on board (24 through our best blocks vs 17 life) and none of ours can block it "
        f"— race it; best line: {ATTACK}"
    )
    # Survive first still holds when the best line gains life instead of attacking.
    role, reason = _role(**lethal, best_line=_line(()), line_text=GAIN)
    assert role == ba.ROLE_CONTROL and reason.endswith("survive first")


def test_the_game_plan_takes_the_race_role():
    assert "the assessed role is race because only attacking survives" in GAME_PLAN_PROMPT
    state = vb.t12_declare_attackers()
    facts = ba.assess(state)
    answer = {
        "role": "control/stabilize",
        "role_reason": "stabilize behind the token wall and close with later swarm swings",
        "turns": [{"turn": "T", "land": "", "cast": [], "attack": "none", "hold": "everything"}],
        "win_conditions": ["Repeated all-in swarm swings force fatal blocks"],
        "path": "hold, then swing",
    }
    plan = GamePlanManager._parse(json.dumps(answer), 12)
    validate_plan(plan, facts, state)
    assert plan.role == ba.ROLE_RACE
    assert any(
        issue == "role control/stabilize rejected: the greedy line dies; attacking survives"
        for issue in plan.issues
    ), plan.issues
