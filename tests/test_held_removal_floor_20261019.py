"""Held damage removal and kicker spells keep a value (ledger #147, #154, 2026-10-19).

#147 (match 1b4af0f9 T6): held removal with no target was worth ``max(1.0, value) x 0.3`` in the
lines against 0.6 a point of face damage, so face burn into an empty board always won: Tectonic
Hazard went face at 14 ("Mountain + Boltwave + Tectonic Hazard"); held, it kills an X/1.
#154 (match f5bc17dc T6): Burst Lightning was always 2; at their beginning of combat both Burst
Lightnings and Shock went face (14 -> 8, the lines' "then Burst Lightning (on opponent) + Burst
Lightning (on opponent)"), and Baron Helmut Zemo came down on T8 and grew into the 5/5 that killed
us. While we are not under a clock, held damage removal is now worth killing a typical creature it
can kill (an X/D for its damage D: 3 + D, ``_spell_value``'s scale), D being the kicked damage when
the kicker is at most two land drops away. Under a clock, and for lethal, burn goes face as before.
"""

from __future__ import annotations

from copy import deepcopy

from tests.engine_boards_20261019 import F5BC_T6, G1B4_T6
from tests.race_boards_20261019 import BURN_HAND, MARSHAL_T7, card

from arenamcp.board_model import build_board_model
from arenamcp.line_search import WIN, search_lines
from arenamcp.line_search_moves import Moves, held_removal_floor


def _hand_values(source: dict, *, survival: bool) -> dict[str, float]:
    moves = Moves(
        build_board_model(deepcopy(source)), survival=survival, lethal_now=False, hard_ms=1e9, opponent=None
    )
    return {hs.name: hs.hand_value for hs in moves.spells}


def _face_casts(line) -> list[str]:
    return [name for step in line.steps for name, target in step.targets if target == "opponent"]


def test_the_floor_is_killing_a_creature_the_spell_can_kill():
    assert held_removal_floor(card(1, "Shock", 1), "removal", mana=1) == 5.0  # an X/2
    assert held_removal_floor(card(2, "Lightning Strike", 1), "removal", mana=2) == 6.0
    hazard = card(3, "Tectonic Hazard", 1)
    assert held_removal_floor(hazard, "other", mana=1) == 4.0  # 1 to each creature: an X/1
    assert held_removal_floor(card(4, "Boltwave", 1), "other", mana=1) == 0.0  # face only
    burst = card(5, "Burst Lightning", 1)
    assert held_removal_floor(burst, "removal", mana=4) == 5.0  # kicker {4}: five mana is 3 drops away
    assert held_removal_floor(burst, "removal", mana=5) == 7.0  # within two land drops: an X/4


def test_1b4af0f9_t6_tectonic_hazard_is_held_for_a_creature():
    values = _hand_values(G1B4_T6, survival=False)
    assert values == {"Tectonic Hazard": 4.0, "Iroh's Demonstration": 7.0, "Boltwave": 1.0}
    result = search_lines(build_board_model(deepcopy(G1B4_T6)), survival=False, lethal_now=False, hard_ms=1e9)
    # Boltwave still goes face; Hazard waits (was "Mountain + Boltwave + Tectonic Hazard").
    assert result.best.steps[0].casts == ("Boltwave",), result.best.summary()
    assert "Tectonic Hazard" not in [name for step in result.best.steps for name in step.casts]


def test_under_a_clock_burn_still_goes_face():
    values = _hand_values(G1B4_T6, survival=True)
    assert values == {"Tectonic Hazard": 1.0, "Iroh's Demonstration": 1.0, "Boltwave": 1.0}
    result = search_lines(build_board_model(deepcopy(G1B4_T6)), survival=True, lethal_now=False, hard_ms=1e9)
    assert result.best.steps[0].casts == ("Boltwave", "Tectonic Hazard"), result.best.summary()
    held = search_lines(build_board_model(deepcopy(F5BC_T6)), survival=True, lethal_now=False, hard_ms=1e9)
    assert _face_casts(held.best), held.best.summary()


def test_f5bc17dc_t6_kicker_two_drops_away_keeps_the_burst_lightnings():
    # Three Mountains: Burst Lightning kicked ({R} + {4}) is two land drops away.
    values = _hand_values(F5BC_T6, survival=False)
    assert values == {"Burst Lightning": 7.0, "Shock": 5.0}
    result = search_lines(build_board_model(deepcopy(F5BC_T6)), survival=False, lethal_now=False, hard_ms=1e9)
    assert not _face_casts(result.best), result.best.summary()  # was Burst + Burst at their face on T9


def test_lethal_burn_still_goes_face():
    # The Marshal board's best line burns them out on T9 whatever the floor (the race tests' own).
    result = search_lines(
        build_board_model(deepcopy(MARSHAL_T7)), survival=False, lethal_now=False, hard_ms=1e9
    )
    assert result.best.cls == WIN and result.best.win_turn == 9, result.best.summary()
    # A burn hand under a clock values its removal as before: nothing is held for its floor.
    moves = Moves(
        build_board_model(deepcopy(BURN_HAND)), survival=True, lethal_now=False, hard_ms=1e9, opponent=None
    )
    assert not moves.floored and not any(hs.floored for hs in moves.spells)
