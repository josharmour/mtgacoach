"""Regressions for the conservative out rule on castable ETB commanders.

Review round 2 of K1 (Dotty, 2026-11-11) showed an earlier draft dropping
castable commanders as outs when their rules text missed a narrow keyword
list - Kogla's ETB fight, Barrin's ETB bounce and Fblthp's ETB draw all
collapsed the estimate to 0.97 and sent a mock concede where pristine base
keeps the 0.60 cap and no concede. These tests pin the conservative behaviour:
absence of known keywords is not proof of a plain body, so a castable
commander stays a possible out unless its irrelevance is proven.

States: tests/concede_etb_out_states.py (reviewer probes, verbatim).
"""

from tests.concede_etb_out_states import barrin_state, fblthp_state, kogla_state
from tests.concede_fakes import Harness

from arenamcp.concede import estimate_loss


def _assert_conservative_out(state, commander_name):
    estimate = estimate_loss(state)
    assert estimate.confidence <= 0.60, estimate.blockers
    assert any(commander_name in out for out in estimate.facts["outs"]), estimate.facts["outs"]
    harness = Harness(state)
    harness.observe()
    # At the 0.60 cap the countdown never arms and no concede is sent -
    # exactly the pristine-base behaviour the reviewer's harness showed
    # (MOCK_CONCEDES [] THREADS_ARMED 0).
    assert harness.threads == []
    assert harness.concedes == []
    return estimate


def test_kogla_etb_fight_keeps_the_commander_an_out():
    # Castable Kogla fights their sole 5/5 flier on ETB - removal that could
    # stop the lethal attack - so she stays an out and no concede is sent.
    _assert_conservative_out(kogla_state(), "Kogla")


def test_barrin_etb_bounce_keeps_the_commander_an_out():
    # Castable Barrin returns their flier to hand on ETB and draws on the end
    # step - alternate wording ("return up to one other target") must not hide
    # the effect - so he stays an out and no concede is sent.
    _assert_conservative_out(barrin_state(), "Barrin")


def test_fblthp_etb_draw_keeps_the_commander_an_out():
    # Castable Fblthp draws on ETB with mana remaining - a draw effect alone
    # keeps the conservative cap - so he stays an out and no concede is sent.
    _assert_conservative_out(fblthp_state(), "Fblthp")
