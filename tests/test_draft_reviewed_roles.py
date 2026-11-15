import pytest

from arenamcp.card_facts.limited_roles import partner_reason, with_role
from arenamcp.draft_advisor import _card_details
from arenamcp.draft_guidance import evaluate_pack, normalize_card
from arenamcp.limited_deck import _fill_role_slots, _interaction_kind, fallback_deck

# Public native BLB and TRK print texts. Every pack and pool below is synthetic.
FOOD = dict(
    grp_id=91698,
    name="Bakersbane Duo",
    mana_cost="{1}{G}",
    type_line="Creature — Squirrel Raccoon",
    oracle_text="When this creature enters, create a Food token.\nWhenever you expend 4, this creature gets +1/+1 until end of turn.",
)
PAYOFF = dict(
    grp_id=91737,
    name="Treetop Sentries",
    mana_cost="{3}{G}",
    type_line="Creature — Squirrel Archer",
    oracle_text="Reach\nWhen this creature enters, you may forage. If you do, draw a card.",
)
BOUNCE = dict(
    grp_id=106643,
    name="Interstellar Effigy",
    mana_cost="{4}{U}",
    type_line="Artifact Creature — Construct",
    oracle_text="When this creature enters, return target creature an opponent controls to its owner's hand.",
)


def filler(i, mana="{2}{B}"):
    return dict(grp_id=i, name=f"Test creature {i}", mana_cost=mana, type_line="Creature", oracle_text="")


@pytest.mark.parametrize(
    "change",
    [
        {"grp_id": 123},
        {"oracle_text": "different"},
        {"mana_cost": "{U}"},
        {"type_line": ""},
        {"name": "alias"},
    ],
)
def test_only_exact_native_print_and_event_are_bound(change):
    assert with_role({**FOOD, **change}, "BLB").get("limited_role") is None
    assert with_role(FOOD, "FRA") is FOOD


def test_real_roles_reach_normal_pick_and_build_prompts():
    food = with_role(FOOD, "BLB")
    payoff = with_role(PAYOFF, "BLB")
    assert food["limited_role"]["roles"] == ["enabler"]
    assert _card_details(payoff)["limited_role"]["archetypes"][0]["colors"] == "BG"
    assert "limited_role" not in _card_details(FOOD)
    pool = [food] * 4 + [filler(700)] * 4
    without = evaluate_pack([normalize_card(PAYOFF)], [normalize_card(c) for c in pool], 9, 2)[0]
    with_roles = evaluate_pack([normalize_card(payoff)], [normalize_card(c) for c in pool], 9, 2)[0]
    assert with_roles.score > without.score
    assert with_roles.reasons[0] == "Forage payoff with Bakersbane Duo"
    assert with_roles.gih_wr_pct is without.gih_wr_pct is None
    assert not partner_reason(payoff["limited_role"], {123: "Unknown"}, "BG")
    assert not partner_reason(payoff["limited_role"], {FOOD["grp_id"]: FOOD["name"]}, "UG")


def test_legal_deck_checks_count_chosen_prints_and_retain_missing_data_fallback():
    p = (
        [FOOD] * 2
        + [PAYOFF] * 2
        + [filler(1000 + i) for i in range(12)]
        + [filler(2000 + i, "{2}{G}") for i in range(9)]
    )
    prior = fallback_deck(p)
    assert prior == fallback_deck([with_role(c, "FRA") for c in p])
    after = fallback_deck([with_role(c, "BLB") for c in p])
    assert after["total_cards"] == 40
    checks = after["limited_checks"]
    assert checks["early_bodies"] >= checks["early_target"] == 6
    assert checks["removal"] == checks["removal_target"] == 0
    assert any("Bakersbane Duo" in line for line in checks["payoffs"])
    assert "Forage payoff" in after["detailed_text"]
    assert not prior.get("limited_checks")


def test_real_native_conditional_bounce_does_not_count_as_durable_removal():
    card = with_role(BOUNCE, "TRK")
    assert card["limited_role"]["removal"] == "conditional"
    assert _interaction_kind(card) == "tempo"


def test_pool_relative_repairs_preserve_preset_and_attain_early_and_removal_floors():
    chosen = [filler(i, "{4}{G}") for i in range(20)]
    removal = dict(
        grp_id=200,
        name="Test interaction",
        type_line="Instant",
        mana_cost="{B}",
        oracle_text="Destroy target creature.",
    )
    remaining = [filler(100 + i, "{G}") for i in range(7)] + [removal] * 3
    protected = chosen[0]
    _fill_role_slots(chosen, remaining, ("B", "G"), (protected,))
    assert protected in chosen
    assert sum(c["mana_cost"] == "{G}" for c in chosen) == 6
    assert chosen.count(removal) == 3
    assert len(chosen) == 20


def test_verified_pool_link_breaks_a_tie_without_modifying_17lands():
    payoff = {**PAYOFF, "gih_wr": 0.55}
    peer = {**payoff, "grp_id": 42, "name": "Unaffiliated print"}
    pool = [with_role(FOOD, "BLB")] * 4 + [filler(700)] * 4

    def ranks(menu):
        return evaluate_pack([normalize_card(c) for c in menu], [normalize_card(c) for c in pool], 9, 2)

    assert ranks([peer, payoff])[0].grp_id == peer["grp_id"]
    rated = ranks([peer, with_role(payoff, "BLB")])
    assert rated[0].grp_id == payoff["grp_id"]
    assert {card.grp_id: card.gih_wr_pct for card in rated} == {
        card.grp_id: card.gih_wr_pct for card in ranks([peer, payoff])
    }


def test_partial_payoff_coverage_does_not_prove_missing_support():
    payoff = with_role(PAYOFF, "BLB")
    varied = (
        [payoff] + [filler(1000 + i) for i in range(12)] + [filler(2000 + i, "{2}{G}") for i in range(10)]
    )
    from arenamcp.limited_deck import _limited_checks

    checks = _limited_checks(varied, {c["grp_id"]: 1 for c in varied})
    assert checks["payoffs"] == ["Treetop Sentries rewards forage; its named example enabler is absent."]


def test_payoff_checks_do_not_treat_own_card_blink_or_graveyard_recycling_as_removal():
    from arenamcp.limited_deck import _role_removal

    base = {"grp_id": 1, "name": "Test instant", "type_line": "Instant", "mana_cost": "{U}"}
    for rules in (
        "Put target card from your graveyard on the bottom of your library.",
        "Exile target creature you control. Return it to the battlefield.",
        "Exile target creature card from a graveyard.",
    ):
        assert not _role_removal({**base, "oracle_text": rules})
    assert _role_removal({**base, "oracle_text": "Destroy target creature."})


@pytest.mark.parametrize(
    "code, changes", [("FRA", {}), ("BLB", {"grp_id": 1}), ("BLB", {"oracle_text": "Reach"})]
)
def test_previous_annotation_removed_when_event_or_print_changes(code, changes):
    bound = with_role(PAYOFF, "BLB")
    changed = {**bound, **changes}
    rebound = with_role(changed, code)
    assert "limited_role" not in rebound and "limited_role" in bound
    assert normalize_card(rebound).limited_role is None
    assert "limited_role" not in _card_details(rebound)
    if changes:
        assert normalize_card(changed).limited_role is None
        assert "limited_role" not in _card_details(changed)
    pool = [rebound] + [filler(1000 + i) for i in range(23)]
    assert "limited_checks" not in fallback_deck(pool)


def test_serialized_claim_cannot_mutate_or_override_its_reviewed_source():
    first = with_role(PAYOFF, "BLB")
    first["limited_role"]["archetypes"][0]["partners"].clear()
    first["limited_role"]["note"] = "Invented win-rate claim"
    again = with_role(PAYOFF, "BLB")
    assert again["limited_role"]["archetypes"][0]["partners"]
    assert normalize_card(first).limited_role == again["limited_role"]
    assert _card_details(first)["limited_role"] == again["limited_role"]


def test_missing_or_changed_installed_corpus_cannot_credit_a_cached_role(monkeypatch):
    from arenamcp.card_facts import limited_roles as roles

    saved = with_role(PAYOFF, "BLB")
    for data in (b"{}", b"unparseable", b'{"prints":{"91737":{}}}'):
        with monkeypatch.context() as patch:
            patch.setattr(roles.Path, "read_bytes", lambda *_, data=data: data)
            roles._records.cache_clear()
            assert "limited_role" not in with_role(saved, "BLB")
        roles._records.cache_clear()


@pytest.mark.parametrize(
    "oracle",
    [
        "Destroy target planeswalker.",
        "Exile target noncreature permanent.",
        "Target opponent sacrifices a planeswalker.",
    ],
)
def test_noncreature_only_answers_do_not_fill_creature_removal_slots(oracle):
    from arenamcp.limited_deck import _role_removal

    card = {"grp_id": 2, "name": "Public test", "type_line": "Sorcery", "mana_cost": "{B}"}
    assert not _role_removal(dict(card, oracle_text=oracle))
    assert _role_removal(dict(card, oracle_text="Destroy target creature or planeswalker."))
