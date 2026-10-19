"""Board-facts role text from the 2026-10-19 ladder reviews (ledger #46, #48).

#46 (a51dea64, 10:24:18): an evasive clock of theirs read "race it or remove
it, holding creatures back changes nothing" while our clock was "none".

#48 (10a78106, 10:31:19): "DEFENDER — their clock 5 vs ours none (0 vs 1
creatures, 0 vs 4 power)" at 20 vs 7 life, while the same search's best line
recast Fleeting Effigy each turn and left the opponent at 1: the board clock
counts creatures on the battlefield only, and the defender branch ran before
the clause that makes a safe line attacking for a third of their life the
beatdown.
"""

from __future__ import annotations

from types import SimpleNamespace

from arenamcp import board_assessment as ba


def _line(opp_after: list[int], lives: list[int], outcome: str = "alive"):
    steps = [
        SimpleNamespace(opp_life_after=o, life_after=life) for o, life in zip(opp_after, lives, strict=True)
    ]
    return SimpleNamespace(steps=steps, outcome=outcome, cls=1, win_at=None, win_turn=None)


def _role(**overrides):
    facts = dict(
        lethal_now=False, opp_lethal=False, dead_in=None, race="behind", our_clock=None, their_clock=5,
        our_power=0, their_power=4, ours=0, theirs=1, our_life=20, opp_life=7, our_lives=[7], their_lives=[16],
        card_advantage=0, deck_curve=1.3, lookahead=[], best_line=None, line_text="", race_term=None,
    )  # fmt: skip
    facts.update(overrides)
    return ba._role(**facts)


def test_a_safe_line_that_beats_them_down_makes_us_the_aggressor_when_ahead_on_life():
    # 10:31:19: Fleeting Effigy each turn puts them at 3, then 1; we drop to 16, 12.
    line = _line([5, 3, 1], [16, 12, 8])
    role, reason = _role(best_line=line, line_text="Mountain + Fleeting Effigy, attack…", race_term=3.75)
    assert role == ba.ROLE_AGGRESSOR, reason
    assert "20 vs 7 life" in reason and "attacks for 6 by T+2" in reason
    # Without a safe line (race term negative), or behind on life, the defender branch stands.
    assert _role(best_line=line, race_term=-3.0)[0] == ba.ROLE_DEFENDER
    assert _role(best_line=line, race_term=3.75, our_life=6, opp_life=7)[0] == ba.ROLE_DEFENDER


def test_no_race_advice_without_a_clock_of_ours():
    role, reason = _role(evasive_clock=True)
    assert role == ba.ROLE_RACE
    assert "none of it: remove it," in reason and "race it" not in reason
    role, reason = _role(evasive_clock=True, our_clock=4, race="behind", their_clock=3)
    assert "race it or remove it" in reason
    # The slow-clock branch says the same.
    role, reason = _role(evasive_clock=True, their_clock=9, our_clock=None)
    assert reason.endswith("blockers stop none of it: remove it"), reason
