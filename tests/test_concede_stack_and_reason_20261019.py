"""Concede: what on the stack can still save us, and a reason whose numbers add up.

2026-10-19 08:11:51 (match a74bbc90, standalone.log): a running countdown
aborted with "loss confidence dropped to 0.70 (Dead next attack (23 through
vs 11 life), but something is on the stack)" — the opponent's own Pest
attack trigger. Only stack objects that could change the outcome for us
(ours, unknown ones, or theirs with an effect that could take their own
attackers out of the fight) hold the verdict off now.

2026-10-19 09:21:34 (match f9e0ec3b): "their 2/4 flying Youthful Valkyrie,
6/6 flying Exemplar of Light get through this attack vs your 11 life" named 8
damage while the 13 through counted an unnamed 5/5 flying Lyra Dawnbringer.
"""

from __future__ import annotations

from tests.bug_180436_states import bug_state
from tests.concede_fakes import Harness
from tests.test_concede import FLYING, board, creature

from arenamcp.concede import _concede_reason, estimate_loss

THOPTER = 287  # their 3/3 flier on the bug board
OUR_FORESEER = 321


def _with_stack(*items: dict) -> dict:
    state = bug_state()
    state["stack"] = list(items)
    return state


def _their(name: str, text: str, **extra) -> dict:
    return {"instance_id": 990, "name": name, "oracle_text": text, "controller_seat_id": 2, **extra}


def test_their_attack_trigger_on_the_stack_leaves_the_verdict():
    gain = _their("Pest ability", "Whenever this creature attacks, you gain 1 life.", object_kind="ABILITY")
    estimate = estimate_loss(_with_stack(gain))
    assert estimate.confidence >= 0.95, estimate.blockers
    pump = _their("Inspiring Call", "Put a +1/+1 counter on target creature you control.", targeting=[THOPTER])
    assert estimate_loss(_with_stack(pump)).confidence >= 0.95


def test_their_burn_at_us_or_ours_leaves_the_verdict():
    at_us = _their("Shock", "Shock deals 2 damage to any target.", targeting=[1])
    assert estimate_loss(_with_stack(at_us)).confidence >= 0.95
    at_ours = _their("Shock", "Shock deals 2 damage to any target.", targeting=[OUR_FORESEER])
    assert estimate_loss(_with_stack(at_ours)).confidence >= 0.95


def test_stack_objects_that_could_still_save_us_hold_it_off():
    # Their removal aimed at their own attacker, or with no known target.
    own_attacker = _their("Shock", "Shock deals 2 damage to any target.", targeting=[THOPTER])
    blind = _their("Shock", "Shock deals 2 damage to any target.")
    # A board-wide effect, an unknown text, and anything of ours.
    sweeper = _their("Pyroclasm", "Pyroclasm deals 2 damage to each creature.")
    unknown = _their("Mystery", "")
    ours = {"instance_id": 991, "name": "Fog", "oracle_text": "Prevent all combat damage.", "controller_seat_id": 1}
    for item in (own_attacker, blind, sweeper, unknown, ours):
        estimate = estimate_loss(_with_stack(item))
        assert estimate.confidence < 0.95, item["name"]
        assert any("stack" in why for why in estimate.blockers)


def test_their_trigger_no_longer_aborts_a_running_countdown():
    harness = Harness(bug_state())
    harness.observe()
    assert harness.states == ["offering"]
    harness.state = _with_stack(_their("Pest ability", "Whenever this creature attacks, you gain 1 life."))
    harness.observe(times=1)
    assert harness.states == ["offering"]
    harness.run_countdown()
    assert len(harness.concedes) == 1 and "aborted" not in harness.states


# --- the spoken reason --------------------------------------------------------------


def _flier(name: str, power: int, toughness: int) -> dict:
    return {"name": name, "power": power, "toughness": toughness, "keywords": ["flying"], "_unblockable": False}


def test_the_named_fliers_and_the_rest_add_up_to_the_damage_through():
    fliers = [
        _flier("Youthful Valkyrie", 2, 4),
        _flier("Exemplar of Light", 6, 6),
        _flier("Lyra Dawnbringer", 5, 5),
    ]
    reason = _concede_reason(fliers, 13, 11, "this attack")
    assert reason.startswith(
        "Concede: their 6/6 flying Exemplar of Light, 5/5 flying Lyra Dawnbringer and 2 more damage get through"
    )
    # Two fliers carrying all the damage need no remainder.
    assert "more damage" not in _concede_reason(fliers[1:], 11, 11, "this attack")


def test_the_f9e0ec3b_board_names_what_gets_through():
    their = [
        creature(10, "Youthful Valkyrie", 2, 2, 4, keywords=FLYING),
        creature(11, "Exemplar of Light", 2, 6, 6, keywords=FLYING),
        creature(12, "Lyra Dawnbringer", 2, 5, 5, keywords=FLYING),
    ]
    estimate = estimate_loss(board(their, life=11))
    assert estimate.confidence >= 0.95, estimate.blockers
    named = estimate.reason
    assert "Exemplar of Light" in named and "Lyra Dawnbringer" in named
    assert "Youthful Valkyrie" not in named and "2 more damage" in named
