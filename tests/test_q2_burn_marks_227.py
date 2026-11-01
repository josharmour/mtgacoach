"""Verification-only negative controls; expected to fail on 7c979d8."""

import pytest
from tests.test_burn_timing_20261019 import _card, _targets
from tests.test_decision_layer_20261019 import _Model, _planner, _reply
from tests.test_hold_recompute_227 import _strike_target_state, _t14_main

from arenamcp.decision_memory import burn_kills_nothing


@pytest.fixture
def planned():
    p = _planner(_Model(_reply("tgt:338", "Strike the Drake; Hazard finishes it.")))
    s = _strike_target_state()
    assert p.plan_decision_options(_targets("Lightning Strike", [338]), s) == ["tgt:338"]
    return p, s


def damage(s):
    return next(c for c in s["battlefield"] if c["instance_id"] == 338).get("damage", 0)


def test_unsubmitted_plan_does_not_become_marked_damage(planned):
    p, s = planned
    assert damage(p._state_with_our_burn_marks(s)) == 0


def test_countered_spell_does_not_release_false_lethal_followup(planned):
    p, _ = planned
    s = _t14_main()
    s["stack"] = []
    s["graveyard"] = [_card(400, "Lightning Strike", 2, is_countered=True)]
    h = s["hand"][0]
    assert burn_kills_nothing(h["oracle_text"], s, castable=h)
    assert burn_kills_nothing(h["oracle_text"], p._state_with_our_burn_marks(s), castable=h)


def test_authoritative_damage_is_not_counted_twice(planned):
    p, _ = planned
    s = _t14_main()
    next(c for c in s["battlefield"] if c["instance_id"] == 338)["damage"] = 3
    assert damage(p._state_with_our_burn_marks(s)) == 3
