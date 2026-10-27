"""Opening picks choose standalone cards before a deck lane is established."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.draft_fakes import FakeBridge, driver_for, make_primer
from tests.test_draft_curve import curve_primer
from tests.test_draft_unrated_fallback import make_driver

from arenamcp.draft_autopick import deck_plan_for, rank_pack


@pytest.mark.parametrize("pick_number", [1, 2, 3, 4])
def test_explicit_opening_position_does_not_inherit_a_pool_lane_or_speculative_plan(pick_number):
    primer = make_primer()
    pack = [1, 3, 5, 12]
    expected = rank_pack(pack, [], primer, pack_number=1, pick_number=pick_number)
    # Even a stale large pool cannot override an explicit new-draft position.
    for pool, plan in [([3, 4] * 3, "UR"), ([6] * 20, "BG")]:
        actual = rank_pack(
            pack,
            pool,
            primer,
            pack_number=1,
            pick_number=pick_number,
            plan_lane=plan,
            signals={"B": 1.0, "G": 1.0},
            picks_per_pass=2,
        )
        assert [(p.grp_id, p.score) for p in actual] == [(p.grp_id, p.score) for p in expected]
        assert actual[0].grp_id == 1  # strongest standalone card; no hardcoded Cube ranking
        assert deck_plan_for(pool, primer, pack_number=1, pick_number=pick_number, plan_lane=plan) is None


def test_pick_two_opening_curve_quota_starts_after_the_fourth_window():
    primer = curve_primer()
    pool = [3] * 6
    opening = rank_pack([1, 2], pool, primer, pack_number=1, pick_number=4, picks_per_pass=2)
    established = rank_pack([1, 2], pool, primer, pack_number=1, pick_number=5, picks_per_pass=2)
    assert opening[0].grp_id == 2  # higher standalone quality despite six early picks
    assert established[0].grp_id == 1  # now the real early-curve shortage matters
    assert not any("needs" in reason or "curve" in reason for pick in opening for reason in pick.reasons)


def test_opening_keeps_intrinsic_castability_flexibility():
    primer = curve_primer()
    primer.cards[1].baseline = primer.cards[2].baseline = 0.0
    primer.cards[1].colors = "UB"
    ranked = rank_pack(
        [1, 2],
        [],
        primer,
        pack_number=1,
        pick_number=1,
        mana_costs={1: "{U}{B}", 2: "{5}{U}"},
    )
    assert ranked[0].grp_id == 2  # equal-quality mono-color card over a gold commitment


def test_driver_opening_request_has_no_committed_deck_and_does_not_save_model_lane():
    primer = make_primer()
    primer.prompt_context = Mock(return_value={"archetypes": []})
    advisor = Mock()
    advisor.recommend.return_value = {
        "reasoning_source": "card_rules",
        "recommendations": [{"grp_id": 1, "reason": "The strongest standalone card."}],
        "lane": "UR",
        "plan": "Commit to Izzet now.",
        "splash": "B",
    }
    driver = driver_for(
        FakeBridge({}),
        primer,
        pick_advisor_fn=lambda: advisor,
        pack_fn=lambda: {
            "pack_number": 1,
            "pick_number": 4,
            "cards": [{"grp_id": 1}, {"grp_id": 3}],
            "deck_plan": {"committed": True},
        },
    )
    driver.run.plan, driver.run.plan_lane = "Old Izzet plan", "UR"
    assert driver._refine_pick([1, 3], primer, [3, 4] * 3, [], 1)[0] == [1]
    details = advisor.recommend.call_args.args[0]
    assert "deck_plan" not in details
    primer.prompt_context.assert_called_once_with("", card_stats=False)
    assert (driver.run.plan, driver.run.plan_lane, driver.run.plan_splash) == ("", "", "")


def test_unrated_opening_fallback_does_not_inherit_color_or_curve_needs():
    driver, _, cards = make_driver([3, 4])
    cards[6] = SimpleNamespace(
        name="Green Top End", mana_cost="{5}{G}", type_line="Creature", rarity="common"
    )
    empty = driver._unrated_ranking([3, 4], [], 1, 4)
    held = driver._unrated_ranking([3, 4], [4, 6] * 3, 1, 4)
    assert [(p.grp_id, p.score) for p in held] == [(p.grp_id, p.score) for p in empty]
    driver.run.plan = "Draft red-green"
    driver.run.plan_lane = "RG"
    driver._step_pick()  # short-clock path also discards speculative saved advice
    assert (driver.run.plan, driver.run.plan_lane) == ("", "")


def test_opening_narration_does_not_announce_the_speculative_lane():
    driver = driver_for(FakeBridge({}), make_primer())
    driver._narrator = Mock()
    driver.run.plan, driver.run.plan_lane = "Old Izzet plan", "UR"
    driver._pick_commentary(
        chosen=[1],
        names=["Blue Ace"],
        pack=[1, 3],
        pool=[3, 4] * 3,
        primer=make_primer(),
        pack_number=1,
        pick_number=4,
        model_reasons={},
        required=2,
    )
    narration = driver._narrator.pick_line.call_args.kwargs
    assert narration["lane_after"].colors == ""
    assert narration["model_lane"] == ""
    assert narration["model_plan"] == ""
