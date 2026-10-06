from arenamcp.draft_autopick import rank_pack
from arenamcp.set_primer import SetCard, SetPrimer


def curve_primer():
    return SetPrimer(
        set_code="TST",
        cards={
            1: SetCard(1, "Early creature", "U", "C", "Creature", cmc=2, gih_wr=0.55, baseline=0),
            2: SetCard(2, "Big creature", "U", "C", "Creature", cmc=6, gih_wr=0.56, baseline=0.3),
            3: SetCard(3, "Expensive spell", "U", "C", "Sorcery", cmc=6, gih_wr=0.55, baseline=0),
        },
    )


def test_fallback_fills_early_curve_instead_of_adding_more_top_end():
    primer = curve_primer()
    assert rank_pack([1, 2], [], primer)[0].grp_id == 2
    ranked = rank_pack([1, 2], [3] * 6, primer)
    assert ranked[0].grp_id == 1
    assert any("fills the early creature curve" in reason for reason in ranked[0].reasons)
    assert "pool already has enough expensive spells" in ranked[1].reasons


def test_fallback_can_take_a_finisher_when_early_curve_is_filled():
    ranked = rank_pack([1, 2], [1] * 8, curve_primer())
    assert ranked[0].grp_id == 2
    assert not any("fills the early" in reason for pick in ranked for reason in pick.reasons)


def test_token_spells_count_as_early_bodies():
    primer = curve_primer()
    primer.cards[1].types = "Sorcery"
    primer.cards[1].oracle = "Create a 2/2 green Bear creature token."
    assert rank_pack([1, 2], [3] * 6, primer)[0].grp_id == 1
    assert rank_pack([1, 2], [1] * 8, primer)[0].grp_id == 2


def test_three_drops_do_not_hide_a_shortage_of_two_drops():
    primer = curve_primer()
    primer.cards[3].types = "Creature"
    primer.cards[3].cmc = 3
    ranked = rank_pack([1, 2], [3] * 8, primer)
    assert ranked[0].grp_id == 1
    assert any("one- and two-mana" in reason for reason in ranked[0].reasons)


def test_curve_need_does_not_make_filler_beat_an_exceptional_bomb():
    primer = curve_primer()
    primer.cards[2].baseline = 2.5
    primer.cards[2].gih_wr = 0.65
    assert rank_pack([1, 2], [3] * 6, primer)[0].grp_id == 2


def test_hybrid_two_drop_gets_curve_credit_without_requiring_a_splash():
    primer = curve_primer()
    primer.cards[1].colors = "UB"
    ranked = rank_pack([1, 2], [3] * 8, primer, mana_costs={1: "{1}{U/B}"})
    assert ranked[0].grp_id == 1
    assert "in lane U" in ranked[0].reasons
    assert any("early creature" in reason for reason in ranked[0].reasons)
    # A genuine gold card still needs both colors.
    ranked = rank_pack([1, 2], [3] * 8, primer, mana_costs={1: "{U}{B}"})
    assert ranked[0].grp_id == 2
