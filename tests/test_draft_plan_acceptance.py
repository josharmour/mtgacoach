"""A ranking veto cannot make an unpicked card part of the next plan (#312)."""

import json
from unittest.mock import Mock

import pytest
from tests.draft_fakes import FakeBridge, driver_for, make_primer

from arenamcp.draft_advisor import DraftAdvisor


@pytest.mark.parametrize("mode", ["veto", "accepted", "partial", "invalid"])
def test_driver_and_advisor_keep_only_wholly_accepted_plan(mode):
    primer = make_primer()
    backend = Mock()
    advisor = DraftAdvisor(backend)
    ids = {"veto": [6], "accepted": [1], "partial": [1, 6], "invalid": [999]}[mode]
    proposed_plan = "Keep building a blue-red deck around " + ("Shiny Trap" if 6 in ids else "Blue Ace")
    backend.complete.return_value = json.dumps(
        dict(
            picks=[dict(grp_id=g, reason="A useful card.") for g in ids],
            plan=proposed_plan,
            lane="UR",
            needs=[],
        )
    )
    cards = [
        dict(grp_id=g, name=primer.card(g).name, mana_cost="{2}{U}", type_line="Creature") for g in [1, 3, 6]
    ]
    details = dict(
        event_name="QuickDraft_TST",
        pack_number=2,
        pick_number=6,
        cards=cards,
        picked_cards=[dict(grp_id=2, name="Blue Common", mana_cost="{U}")] * 4,
    )
    driver = driver_for(FakeBridge({}), primer, pick_advisor_fn=lambda: advisor, pack_fn=lambda: details)
    driver.run.plan, driver.run.plan_lane = "Build blue-red with the cards actually picked.", "UR"
    old = driver.run.plan
    advisor._event_name = details["event_name"]
    try:
        result = driver._refine_pick([1, 3, 6], primer, [2, 4] * 4, [], 2 if mode == "partial" else 1)
        expected = proposed_plan if mode == "accepted" else old
        assert driver.run.plan == advisor._plan == expected
        assert driver.run.plan_lane == advisor._lane == "UR"
        if mode == "veto" or mode == "invalid":
            assert result is None
        elif mode == "partial":
            assert 6 not in result[0]
            assert result[2] == "model+ranking"
        # The next model request must see the accepted context as well.
        driver._refine_pick([1, 3, 6], primer, [2, 4] * 4, [], 2 if mode == "partial" else 1)
        request = json.loads(backend.complete.call_args.args[1])
        assert request["previous_plan"] == expected
    finally:
        advisor._executor.shutdown(wait=True)
