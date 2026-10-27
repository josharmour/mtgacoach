"""A lower-band choice must expose the comparison, without inventing confidence."""

from unittest.mock import Mock

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
