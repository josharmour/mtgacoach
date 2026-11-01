"""Replay exposed #300 reports against supplied rules, not against pick scores."""

import json
from pathlib import Path

import pytest

from arenamcp.draft_advisor import DraftAdvisor
from arenamcp.draft_facts import reason_corrections

CASES = json.loads((Path(__file__).parent / "fixtures/draft_factual_claims_300.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["case"])
def test_captured_false_reason_is_corrected_without_changing_pick(case):
    card = case["selected"]
    payload = dict(picks=[dict(grp_id=card["grp_id"], reason=case["reason"])], plan="Stay open.", needs=[])
    pick = DraftAdvisor._validate(payload, [card], case["context"], 1)["recommendations"][0]
    assert pick["grp_id"] == card["grp_id"]
    assert pick["original_reason"] == case["reason"]
    assert pick["rules_corrections"]
    assert pick["reason"].startswith("Rules check:")
    if case["case"] == "equipment_interaction":
        assert "does not equip" in pick["reason"]
        assert "attaches that Equipment to that token" in pick["reason"]
    elif case["case"] == "generic_discount":
        assert "leaves {G}" in pick["reason"]
        assert "Mount or Vehicle you control" in pick["reason"]


def test_real_equipment_link_and_living_weapon_are_not_rejected():
    case = next(c for c in CASES if c["case"] == "equipment_interaction")
    reason = "Stoneforge Mystic deploys Kaldra Compleat; Living weapon attaches it to its token."
    assert not reason_corrections(reason, case["selected"], [case["selected"], *case["context"]])
    source = {
        **case["context"][0],
        "oracle_text": case["context"][0]["oracle_text"] + "\nEquipment you control have equip {0}.",
    }
    assert not reason_corrections(case["reason"], case["selected"], [case["selected"], source])
    reason = "Do not cheat it out via Stoneforge Mystic's free-equip ability; that ability does not exist."
    assert not reason_corrections(reason, case["selected"], [case["selected"], *case["context"]])


def test_generic_discount_keeps_colors_and_respects_alternative_cost_unknowns():
    case = next(c for c in CASES if c["case"] == "generic_discount")
    card = case["selected"]
    for reason in [
        "Run Over costs {G} targeting Vehicles.",
        "Run Over costs {0} targeting Dragons.",
        "Do not assume this second copy discounts to {0} targeting Vehicles.",
        "Run Over costs {0} targeting Vehicles with a free-casting effect.",
    ]:
        assert not reason_corrections(reason, card, [card])
    for changed in [
        {**card, "mana_cost": "{1}"},
        {**card, "mana_cost": "{1}{G/U}"},
        {
            **card,
            "oracle_text": card["oracle_text"] + " You may cast this spell without paying its mana cost.",
        },
    ]:
        assert not reason_corrections(case["reason"], changed, [changed])


def test_single_black_commitment_does_not_need_to_state_generic_mana():
    case = next(c for c in CASES if c["case"] == "late_castability")
    card = case["selected"]
    assert not reason_corrections("Demonic Tutor has a mana commitment of B.", card, [card])
    assert not reason_corrections("Demonic Tutor does not have a mana commitment of BB.", card, [card])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["case"])
def test_corrections_follow_rules_under_different_card_names(case):
    selected = {**case["selected"], "name": "Test Selection"}
    context = [{**card, "name": f"Test Source {i}"} for i, card in enumerate(case["context"])]
    reason = case["reason"].replace(case["selected"]["name"], selected["name"])
    for old, new in zip(case["context"], context, strict=True):
        reason = reason.replace(old["name"], new["name"])
    assert reason_corrections(reason, selected, [selected, *context])
