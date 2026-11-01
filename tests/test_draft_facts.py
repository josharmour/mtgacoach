"""Bounded #300 fact checks; they do not certify arbitrary model prose."""

from unittest.mock import Mock

import pytest
from tests.test_draft_unrated_fallback import make_driver
from tests.test_limited_rules_cube import KALDRA, STONEFORGE

from arenamcp.draft_advisor import DraftAdvisor
from arenamcp.draft_facts import reason_corrections

TUTOR = dict(grp_id=1, name="Demonic Tutor", mana_cost="{1}{B}", type_line="Sorcery")
EIGANJO = dict(
    grp_id=2,
    name="Eiganjo, Seat of the Empire",
    type_line="Legendary Land",
    oracle_text="{T}: Add {W}.\nChannel — {2}{W}, Discard Eiganjo: Deal 4 damage.",
)


@pytest.mark.parametrize("claim", ["BB", "{B}{B}", "{oBoB}", "{C}{B}"])
def test_generic_is_neither_a_second_black_pip_nor_colorless(claim):
    assert reason_corrections(f"Demonic Tutor costs {claim}.", TUTOR, [TUTOR]) == [
        "Demonic Tutor's printed mana cost is {1}{B}."
    ]


@pytest.mark.parametrize("cost", ["{1}{B}", "{o1oB}"])
def test_arena_and_oracle_costs_have_the_same_fact(cost):
    card = {**TUTOR, "mana_cost": cost}
    assert not reason_corrections("Demonic Tutor costs 1B.", card, [card])
    assert reason_corrections("It costs BB.", card, [card])


@pytest.mark.parametrize(
    "claim",
    [
        "Eiganjo produces colorless mana.",
        "Eiganjo, Seat of the Empire taps for colorless.",
        "It adds one colorless mana.",
    ],
)
def test_direct_mana_production_claim_uses_ability_not_card_color(claim):
    assert reason_corrections(claim, EIGANJO, [EIGANJO]) == [
        "Eiganjo, Seat of the Empire's supplied tap ability produces white mana."
    ]


@pytest.mark.parametrize(
    "reason",
    [
        "Demonic Tutor costs B with our cost reducer.",
        "Demonic Tutor costs B instead of its printed cost.",
        "Demonic Tutor costs B when we control a reducer.",
        "Unlike a spell that costs BB, Demonic Tutor is easy to splash.",
        "Do not say Demonic Tutor costs BB.",
        "Demonic Tutor costs 1B.",
        "Demonic Tutor costs 2.",
        "Demonic Tutor has a colorless-effect role in our deck.",
    ],
)
def test_qualified_payments_and_nonliteral_claims_are_not_rewritten(reason):
    assert not reason_corrections(reason, TUTOR, [TUTOR])


@pytest.mark.parametrize(
    "text",
    [
        "{T}: Add one mana of any color.",
        '{T}: Add {W}.\nOther lands have "{T}: Add {C}."',
        "{T}: Add {W}.\n{1}, {T}: Add {C}{C}.",
        "{T}: Add {W}. Activate only if you control a creature.",
        "",
    ],
)
def test_complex_or_missing_mana_evidence_is_not_treated_as_exhaustive(text):
    card = {**EIGANJO, "oracle_text": text}
    assert not reason_corrections("It produces colorless mana.", card, [card])


def test_missing_cost_and_related_faces_do_not_invent_contradictions():
    for card in ({**TUTOR, "mana_cost": ""}, {**TUTOR, "related_faces": [{"mana_cost": "{B}{B}"}]}):
        assert not reason_corrections("It costs BB.", card, [card])


def validate(reason, alternative=None):
    payload = dict(picks=[dict(grp_id=1, reason=reason)], plan="Stay open.", lane="", needs=[])
    if alternative:
        payload["alternative"] = dict(grp_id=2, reason=alternative)
    return DraftAdvisor._validate(payload, [TUTOR, EIGANJO], [], 1)


def test_false_reason_is_retained_for_audit_but_not_published_as_advice(caplog):
    result = validate("Demonic Tutor costs BB.")
    pick = result["recommendations"][0]
    assert pick["grp_id"] == 1  # A prose correction is not a ranking override.
    assert pick["original_reason"] == "Demonic Tutor costs BB."
    assert "BB" not in pick["reason"]
    assert "{1}{B}" in pick["reason"]
    assert "rationale was omitted" in pick["reason"]
    assert "original: Demonic Tutor costs BB." in caplog.text


def test_false_alternative_does_not_leak_into_preserved_comparison():
    result = validate("A flexible tutor.", "Eiganjo produces colorless mana.")
    assert result["alternative"]["original_reason"] == "Eiganjo produces colorless mana."
    assert "produces colorless" not in result["recommendations"][0]["reason"]
    assert "produces white" in result["recommendations"][0]["reason"]


def test_legitimate_equipment_link_is_preserved_control_296():
    payload = dict(
        picks=[
            dict(
                grp_id=KALDRA["grp_id"],
                reason="Living weapon supplies its own body.",
                synergy_with=[STONEFORGE["grp_id"]],
            )
        ],
        plan="Build around our Equipment.",
        needs=[],
        lane="",
    )
    pick = DraftAdvisor._validate(payload, [KALDRA], [STONEFORGE], 1)["recommendations"][0]
    assert pick["synergy_with"] == [STONEFORGE["grp_id"]]
    assert "rules_corrections" not in pick


def test_correct_loyalty_and_colorless_empower_claims_are_not_rejected():
    for reason in [
        "Inspired Tethermage adds loyalty counters.",
        "Keeper supplies colorless Empower support.",
    ]:
        assert validate(reason)["recommendations"][0]["reason"] == reason


@pytest.mark.parametrize(
    "reason",
    [
        "Eiganjo produces colorless mana with a replacement effect.",
        "Eiganjo produces colorless or white mana.",
        "Eiganjo produces colorless mana?",
        "Demonic Tutor costs BB?",
    ],
)
def test_questions_and_conditional_mana_production_are_not_rewritten(reason):
    assert not reason_corrections(reason, EIGANJO, [TUTOR, EIGANJO])


def test_pronoun_after_another_card_is_not_assumed_to_refer_to_pick():
    other = dict(grp_id=3, name="Another spell", mana_cost="{B}{B}")
    reason = "Another spell is an option. It costs BB."
    assert not reason_corrections(reason, TUTOR, [TUTOR, other])
    assert not reason_corrections("It costs B plus one generic mana.", TUTOR, [TUTOR])


def test_non_mana_nouns_and_implicit_basic_land_abilities_are_out_of_scope():
    assert not reason_corrections("It produces colorless tokens.", EIGANJO, [EIGANJO])
    card = {**EIGANJO, "type_line": "Land — Mountain"}
    assert not reason_corrections("It produces red mana.", card, [card])


def test_correction_and_comparison_survive_short_narration_once():
    reason = validate("Demonic Tutor costs BB.", "We need spells more than lands.")["recommendations"][0][
        "reason"
    ]
    driver, _, _ = make_driver([1, 2])
    driver._narrator = Mock()
    driver._narrator.pick_line.return_value = "Picking Demonic Tutor."
    line = driver._pick_commentary(
        chosen=[2],
        names=["Demonic Tutor"],
        pack=[1, 2],
        pool=[],
        primer=None,
        pack_number=1,
        pick_number=6,
        model_reasons={2: reason},
    )
    assert reason in line
    assert line.count("Rules check:") == line.count("Alternative:") == 1
