"""Unrated events still have local rules and an actual pool to rank by."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.draft_fakes import PICK_STATE, FakeBridge

from arenamcp.draft_event import DraftEventDriver


def make_driver(pack, *, seconds=10, advisor=None, pool=()):
    cards = {
        1: SimpleNamespace(name="Vanilla", mana_cost="{3}{R}", type_line="Creature", rarity="common"),
        2: SimpleNamespace(
            name="Removal",
            mana_cost="{1}{R}",
            type_line="Instant",
            rarity="common",
            oracle_text="This spell deals 3 damage to any target.",
        ),
        3: SimpleNamespace(name="Blue Body", mana_cost="{1}{U}", type_line="Creature", rarity="common"),
        4: SimpleNamespace(name="Red Body", mana_cost="{1}{R}", type_line="Creature", rarity="common"),
        5: SimpleNamespace(name="Forest"),
        6: SimpleNamespace(name="Green Body", mana_cost="{1}{G}", type_line="Creature", rarity="common"),
    }
    bridge = FakeBridge(
        {
            "get_draft_state": {
                **PICK_STATE,
                "pack_cards": pack,
                "pick_seconds_remaining": seconds,
            },
            "submit_draft_pick": {"ok": True},
        }
    )
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: None,
        primer_service=Mock(),
        card_db=SimpleNamespace(get_card=cards.get),
        picked_fn=lambda: list(pool),
        pick_advisor_fn=(lambda: advisor) if advisor else None,
        pack_fn=lambda: {
            "cards": [{"grp_id": g} for g in pack],
            "pack_number": 1,
            "pick_number": 1,
        },
    )
    driver.run.event_name = "CubeDraft_Powered_20261020"
    driver.set_enabled(True)
    return driver, bridge, cards


@pytest.mark.parametrize("pack", [[1, 2], [2, 1]])
def test_unrated_short_clock_uses_rules_instead_of_first_pack_position(pack):
    advisor = Mock()
    driver, bridge, _ = make_driver(pack, advisor=advisor)
    driver._step_pick()
    submits = [fields for action, fields in bridge.sent if action == "submit_draft_pick"]
    assert submits[0]["cards"][0]["grp_id"] == 2
    advisor.recommend.assert_not_called()
    driver._primers.get.assert_not_called()


def test_unrated_advisor_failure_keeps_card_rules_fallback():
    advisor = Mock()
    advisor.recommend.return_value = {"reasoning_source": "heuristic"}
    driver, bridge, _ = make_driver([1, 2], seconds=60, advisor=advisor)
    driver._step_pick()
    submits = [fields for action, fields in bridge.sent if action == "submit_draft_pick"]
    assert submits[0]["cards"][0]["grp_id"] == 2
    assert advisor.recommend.call_args.args[1]["evaluations"][0]["grp_id"] == 2


def test_unrated_narration_uses_the_same_card_rules_ranking():
    driver, _, _ = make_driver([1, 2])
    driver._narrator = Mock()
    driver._pick_commentary(
        chosen=[2],
        names=["Removal"],
        pack=[1, 2],
        pool=[],
        primer=None,
        pack_number=1,
        pick_number=1,
        model_reasons={},
    )
    narrated = driver._narrator.pick_line.call_args.kwargs
    assert narrated["ranking"][0].grp_id == 2
    assert narrated["chosen"][0] == narrated["ranking"][0]


def test_unrated_estimates_do_not_veto_valid_model_picks():
    advisor = Mock()
    advisor.recommend.return_value = {
        "reasoning_source": "card_rules",
        "recommendations": [{"grp_id": 1, "reason": "A role the generic estimate misses."}],
    }
    driver, _, _ = make_driver([2, 1], advisor=advisor)
    refined = driver._refine_pick([2, 1], None, [], [], 1)
    assert refined[0] == [1]
    # Basic lands remain disallowed while a spell is available.
    driver._pack_fn = lambda: {"cards": [{"grp_id": 2}, {"grp_id": 5}]}
    advisor.recommend.return_value["recommendations"][0]["grp_id"] = 5
    assert driver._refine_pick([2, 5], None, [], [], 1) is None


def test_unrated_fallback_uses_actual_pool_and_no_printing_ratings():
    driver, _, cards = make_driver([3, 4])
    cards[3].gih_wr = 0.99  # A printing's retail data must never affect this path.
    ranking = driver._unrated_ranking([3, 4], [4, 6] * 10, 3, 1)
    assert ranking[0].grp_id == 4
    assert all(pick.band == "unknown" for pick in ranking)
    assert any("lane" in reason.lower() or "color" in reason.lower() for reason in ranking[0].reasons)
    assert driver._unrated_ranking([5, 1], [], 1, 1)[0].grp_id == 1
