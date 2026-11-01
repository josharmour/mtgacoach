"""A lower-band choice must expose the comparison, without inventing confidence."""

from unittest.mock import Mock

import pytest
from tests.test_draft_unrated_fallback import make_driver

from arenamcp.draft_advisor import DraftAdvisor


def result(alternative=None, chosen_band="average", other_band="strong"):
    cards = [
        dict(grp_id=1, name="Removal", quality_band=chosen_band),
        dict(grp_id=2, name="Flying threat", quality_band=other_band),
    ]
    payload = {
        "picks": [{"grp_id": 1, "reason": "Interaction fits the pool.", "synergy_with": []}],
        "plan": "Develop a supported deck.",
        "lane": "",
        "needs": [],
    }
    if alternative:
        payload["alternative"] = {"grp_id": 2, "reason": alternative}
    return DraftAdvisor._validate(payload, cards, [], 1)


def test_lower_band_pick_preserves_named_comparison_and_uncertainty():
    rec = result("We have no removal; this answers a threat but sacrifices evasive pressure.")[
        "recommendations"
    ][0]
    assert "Removal is average; Flying threat is strong" in rec["reason"]
    assert "Coach's rationale: We have no removal" in rec["reason"]
    assert "not proven advantage" in rec["reason"]
    assert "17Lands" in rec["rating_tradeoff"]


def test_missing_alternative_is_disclosed_instead_of_fabricated():
    rec = result()["recommendations"][0]
    assert "No comparison" in rec["reason"]
    assert "unverified" in rec["reason"]


def test_unknown_or_equal_bands_do_not_invent_a_rating_gap():
    for chosen, other in [("unknown", "strong"), ("good", "good"), ("strong", "weak")]:
        assert "rating_tradeoff" not in result(chosen_band=chosen, other_band=other)["recommendations"][0]


def test_comparison_survives_short_narration():
    driver, _, _ = make_driver([1, 2])
    driver._narrator = Mock()
    driver._narrator.pick_line.return_value = "Picking Removal."
    comparison = result()["recommendations"][0]["rating_tradeoff"]
    line = driver._pick_commentary(
        chosen=[2],
        names=["Removal"],
        pack=[1, 2],
        pool=[],
        primer=None,
        pack_number=1,
        pick_number=6,
        model_reasons={2: "Short reason. " + comparison},
    )
    assert comparison in line


@pytest.mark.parametrize("band", ["unknown", "good"])
def test_supplied_comparison_survives_without_a_measured_rating_gap(band):
    rec = result("We need interaction; we give up evasive pressure.", band, band)["recommendations"][0]
    assert "rating_tradeoff" not in rec
    comparison = rec["alternative_comparison"]
    assert "Alternative: Flying threat." in comparison
    assert "not proven advantage" in comparison
    assert "17Lands" not in comparison
    assert comparison in rec["reason"]

    driver, _, _ = make_driver([1, 2])
    driver._narrator = Mock()
    driver._narrator.pick_line.return_value = "Picking Removal."
    line = driver._pick_commentary(
        chosen=[2],
        names=["Removal"],
        pack=[1, 2],
        pool=[],
        primer=None,
        pack_number=1,
        pick_number=6,
        model_reasons={2: rec["reason"]},
    )
    assert comparison in line


def test_missing_unranked_comparison_is_disclosed_without_inventing_rationale():
    rec = result(chosen_band="unknown", other_band="unknown")["recommendations"][0]
    assert "No comparison was supplied" in rec["alternative_comparison"]
    assert "strongest alternative unknown" in rec["alternative_comparison"]
    assert rec["reason"].startswith("Interaction fits the pool.")


def test_missing_unknown_band_comparison_uses_remaining_heuristic_leader():
    cards = [dict(grp_id=g, name=f"Card {g}") for g in (1, 2, 3)]
    payload = dict(picks=[dict(grp_id=1, reason="Flexible card.")], plan="Stay open.", needs=[])
    result = DraftAdvisor._validate(
        payload,
        cards,
        [],
        1,
        rankings=[
            dict(grp_id=999, rank=1),
            dict(grp_id=1, rank=2),
            dict(grp_id=3, rank=3),
            dict(grp_id=2, rank=4),
        ],
    )
    assert result["alternative"]["grp_id"] == 3
    assert result["alternative"]["comparison_missing"] is True
    reason = result["recommendations"][0]["reason"]
    assert "No comparison was supplied" in reason
    assert "heuristic ranking, not measured quality" in reason
    assert "rating_tradeoff" not in result["recommendations"][0]


def test_no_distinct_remaining_choice_needs_no_comparison():
    card = dict(grp_id=1, name="Only card")
    payload = dict(picks=[dict(grp_id=1, reason="Only choice.")], plan="Stay open.", needs=[])
    for cards in ([card], [card, card]):
        assert "alternative" not in DraftAdvisor._validate(payload, cards, [], 1)


def test_missing_comparison_prefers_higher_supplied_band_to_heuristic_order():
    cards = [
        dict(grp_id=1, name="Pick", quality_band="average"),
        dict(grp_id=2, name="Stronger", quality_band="strong"),
        dict(grp_id=3, name="Other", quality_band="good"),
    ]
    payload = dict(picks=[dict(grp_id=1, reason="Flexible card.")], plan="Stay open.", needs=[])
    result = DraftAdvisor._validate(payload, cards, [], 1, rankings=[dict(grp_id=3, rank=1)])
    assert result["alternative"]["grp_id"] == 2
    assert "No comparison" in result["recommendations"][0]["rating_tradeoff"]
