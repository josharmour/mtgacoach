"""Unit tests for the platform-independent draft guidance engine.

All data is synthetic 17lands-style; nothing here touches the network or any
cache. The engine under test (arenamcp.draft_guidance) is a pure function
layer, so tests build CardData via normalize_card from plain dicts.
"""

import itertools

import pytest

from arenamcp.draft_guidance import (
    ArchStats,
    CardData,
    FormatContext,
    _polyval,
    _wheel_probability,
    analyze_pool,
    compute_lane,
    compute_pack_signals,
    evaluate_pack,
    format_context_from_pair_stats,
    get_tier,
    normalize_card,
)

_ids = itertools.count(1000)


def mk(
    name,
    wr=0.54,
    colors="W",
    cmc=2,
    type_line="Creature — Human",
    oracle_text="",
    alsa=7.0,
    iwd=0.0,
    rarity="common",
    mana_cost=None,
    games=5000,
    arch_stats=None,
    grp_id=None,
):
    """Build a normalized synthetic card from 17lands-style values.

    wr/iwd are fractions (0.54) exactly like our draftstats.DraftStats.
    """
    color_list = list(colors) if colors else []
    if mana_cost is None:
        generic = int(cmc) - len(color_list)
        mana_cost = ("{%d}" % generic if generic > 0 else "") + "".join(
            "{%s}" % c for c in color_list
        )
    return normalize_card(
        {
            "grp_id": grp_id if grp_id is not None else next(_ids),
            "name": name,
            "colors": color_list,
            "cmc": cmc,
            "type_line": type_line,
            "oracle_text": oracle_text,
            "mana_cost": mana_cost,
            "rarity": rarity,
            "gih_wr": wr,
            "alsa": alsa,
            "iwd": iwd,
            "games_in_hand": games,
        },
        arch_stats=arch_stats,
    )


def filler_pack(n=8, wr=0.54, colors="W"):
    return [mk(f"Filler {i}", wr=wr, colors=colors) for i in range(n)]


def committed_pool(colors=("W", "U"), n=12, wr=0.57):
    """A pool clearly committed to a two-color lane."""
    pool = []
    for i in range(n):
        pool.append(mk(f"Pool {colors[i % 2]} {i}", wr=wr, colors=colors[i % 2]))
    return pool


FMT = FormatContext(mean_gih_wr_pct=54.0, std_gih_wr_pct=4.0)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_fraction_and_percent_win_rates_both_normalize_to_pct(self):
        frac = normalize_card({"grp_id": 1, "name": "A", "gih_wr": 0.57})
        pct = normalize_card({"grp_id": 2, "name": "B", "gih_wr": 57.0})
        assert frac.gih_wr_pct == pytest.approx(57.0)
        assert pct.gih_wr_pct == pytest.approx(57.0)

    def test_missing_win_rate_is_none(self):
        card = normalize_card({"grp_id": 1, "name": "A"})
        assert card.gih_wr_pct is None
        assert card.alsa is None

    def test_colors_derived_from_mana_cost_including_hybrid(self):
        card = normalize_card(
            {"grp_id": 1, "name": "A", "mana_cost": "{1}{W/U}{B}"}
        )
        assert set(card.colors) == {"W", "U", "B"}
        # WUBRG canonical ordering
        assert card.colors == ("W", "U", "B")

    def test_explicit_colors_win_over_mana_cost(self):
        card = normalize_card(
            {"grp_id": 1, "name": "A", "colors": ["R"], "mana_cost": "{W}"}
        )
        assert card.colors == ("R",)

    def test_tags_derived_from_oracle_text(self):
        removal = mk("Kill It", oracle_text="Destroy target creature.")
        evasive = mk("Birdy", oracle_text="Flying")
        dual = normalize_card(
            {
                "grp_id": 9,
                "name": "Dual",
                "colors": ["W", "U"],
                "type_line": "Land",
                "mana_cost": "",
            }
        )
        assert "removal" in removal.tags
        assert "evasion" in evasive.tags
        assert "fixing" in dual.tags

    def test_object_sources_supported(self):
        """ScryfallCard-like + DraftStats-like objects (attribute access)."""

        class FakeScryfall:
            name = "Obj Card"
            oracle_text = "Destroy target creature."
            type_line = "Instant"
            mana_cost = "{1}{B}"
            cmc = 2.0
            colors = ["B"]
            arena_id = 777
            rarity = "uncommon"

        class FakeStats:
            gih_wr = 0.585
            alsa = 3.2
            iwd = 0.051
            games_in_hand = 4321

        card = normalize_card(FakeScryfall(), FakeStats())
        assert card.grp_id == 777
        assert card.gih_wr_pct == pytest.approx(58.5)
        assert card.alsa == pytest.approx(3.2)
        assert card.iwd_pct == pytest.approx(5.1)
        assert card.games == 4321
        assert "removal" in card.tags

    def test_types_split_from_type_line(self):
        card = mk("Plains Walker", type_line="Basic Land — Plains", colors="")
        assert card.is_basic_land
        creature = mk("Bear", type_line="Creature — Bear")
        assert creature.is_creature and not creature.is_land

    def test_format_context_from_pair_stats(self):
        class PS:
            def __init__(self, wr):
                self.win_rate = wr

        fmt = format_context_from_pair_stats({"WU": PS(0.565), "BR": 0.548})
        assert fmt.pair_win_rates["WU"] == pytest.approx(0.565)
        assert fmt.pair_win_rates["BR"] == pytest.approx(0.548)


# ---------------------------------------------------------------------------
# Bomb detection
# ---------------------------------------------------------------------------


class TestBombDetection:
    def test_pack_relative_bomb_tops_the_ranking_with_reasons(self):
        pack = filler_pack(9, wr=0.535) + [
            mk("Dragon Bomb", wr=0.63, iwd=0.06, colors="R", cmc=5,
               rarity="mythic", alsa=1.4)
        ]
        result = evaluate_pack(pack, pool=[], pick_number=1, pack_number=1, fmt=FMT)
        top = result[0]
        assert top.name == "Dragon Bomb"
        assert top.is_bomb
        assert top.z_score > 1.5
        joined = " ".join(top.reasons).lower()
        assert "bomb" in joined
        # IWD premium fires the "true bomb" reason
        assert any("true bomb" in r.lower() for r in top.reasons)

    def test_same_card_is_not_a_bomb_in_a_strong_pack(self):
        strong_pack = filler_pack(9, wr=0.615) + [
            mk("Dragon Bomb", wr=0.63, iwd=0.06, colors="R", cmc=5)
        ]
        result = evaluate_pack(strong_pack, pool=[], pick_number=1, fmt=FMT)
        top = {g.name: g for g in result}["Dragon Bomb"]
        assert not top.is_bomb  # z-score is pack-relative

    def test_scores_clamped_0_100_and_sorted(self):
        pack = filler_pack(6, wr=0.50) + [mk("Huge", wr=0.68, iwd=0.09)]
        result = evaluate_pack(pack, pool=[], pick_number=1, fmt=FMT)
        assert all(0.0 <= g.score <= 100.0 for g in result)
        scores = [g.score for g in result]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Lane commitment shifting across picks
# ---------------------------------------------------------------------------


class TestLaneCommitment:
    def test_empty_pool_stays_open_best_card_wins(self):
        pack = [
            mk("Red Beater", wr=0.575, colors="R"),
            mk("White Bear", wr=0.555, colors="W"),
        ] + filler_pack(6, wr=0.53, colors="G")
        result = evaluate_pack(pack, pool=[], pick_number=1, pack_number=1, fmt=FMT)
        assert result[0].name == "Red Beater"

    def test_committed_pool_flips_the_same_matchup(self):
        pool = committed_pool(("W", "U"), n=14)
        pack = [
            mk("Red Beater", wr=0.575, colors="R"),
            mk("White Bear", wr=0.555, colors="W"),
        ] + filler_pack(6, wr=0.53, colors="G")
        result = evaluate_pack(pack, pool=pool, pick_number=5, pack_number=2, fmt=FMT)
        by_name = {g.name: g for g in result}
        assert by_name["White Bear"].score > by_name["Red Beater"].score
        assert not by_name["Red Beater"].on_lane
        red_reasons = " ".join(by_name["Red Beater"].reasons).lower()
        assert "off-color" in red_reasons or "committed" in red_reasons
        white_reasons = " ".join(by_name["White Bear"].reasons).lower()
        assert "lane" in white_reasons or "fits" in white_reasons

    def test_lane_recency_bias_prefers_recent_colors(self):
        # 5 old green picks, then 12 recent strong W/U picks: lane should be WU.
        pool = [mk(f"Old Green {i}", wr=0.57, colors="G") for i in range(5)]
        pool += committed_pool(("W", "U"), n=12, wr=0.58)
        lane = compute_lane(pool, FMT)
        assert lane.pair_key == "WU"
        assert lane.lane_phrase() in ("white-blue", "blue-white")

    def test_pack1_off_color_penalty_is_gentle(self):
        """Early: speculation is nearly free; late pack 3: off-color is dead."""
        pool_small = committed_pool(("W", "U"), n=6)
        pool_big = committed_pool(("W", "U"), n=30)
        red = [mk("Red Spell", wr=0.57, colors="R", grp_id=42)]
        early = evaluate_pack(red + filler_pack(5), pool_small,
                              pick_number=3, pack_number=1, fmt=FMT)
        late = evaluate_pack(red + filler_pack(5), pool_big,
                             pick_number=5, pack_number=3, fmt=FMT)
        early_red = {g.grp_id: g for g in early}[42]
        late_red = {g.grp_id: g for g in late}[42]
        assert early_red.score > late_red.score

    def test_lane_reported_on_guidance_records(self):
        pool = committed_pool(("W", "U"), n=14)
        result = evaluate_pack(filler_pack(4), pool, pick_number=4,
                               pack_number=2, fmt=FMT)
        assert all(g.lane == "WU" for g in result)


# ---------------------------------------------------------------------------
# Castability pressure
# ---------------------------------------------------------------------------


class TestCastability:
    def test_double_off_pip_no_fixing_is_nearly_uncastable(self):
        pool = committed_pool(("W", "U"), n=20)
        pack = [mk("RR Spell", wr=0.60, colors="R", mana_cost="{1}{R}{R}",
                   grp_id=7)] + filler_pack(5, wr=0.53)
        result = evaluate_pack(pack, pool, pick_number=4, pack_number=3, fmt=FMT)
        rr = {g.grp_id: g for g in result}[7]
        assert rr.score < 10
        assert any("uncastable" in r.lower() for r in rr.reasons)

    def test_bomb_splash_allowed_with_fixing(self):
        base_pool = committed_pool(("W", "U"), n=12)
        duals = [
            normalize_card({
                "grp_id": next(_ids), "name": f"Dual {i}",
                "colors": ["W", "U"], "type_line": "Land", "mana_cost": "",
            })
            for i in range(3)
        ]
        pack = [mk("Off Bomb", wr=0.66, iwd=0.06, colors="R", cmc=5,
                   mana_cost="{4}{R}", grp_id=8)] + filler_pack(6, wr=0.525)

        with_fixing = evaluate_pack(pack, base_pool + duals, pick_number=4,
                                    pack_number=2, fmt=FMT)
        without_fixing = evaluate_pack(pack, base_pool, pick_number=4,
                                       pack_number=2, fmt=FMT)
        bomb_with = {g.grp_id: g for g in with_fixing}[8]
        bomb_without = {g.grp_id: g for g in without_fixing}[8]
        assert bomb_with.score > bomb_without.score
        assert any("splash" in r.lower() for r in bomb_with.reasons)


# ---------------------------------------------------------------------------
# Composition pressure
# ---------------------------------------------------------------------------


class TestComposition:
    def test_two_drop_boosted_when_pool_has_none(self):
        # 20 mid-cost creatures, zero two-drops: identical-WR 2-drop must
        # outrank the 4-drop, with a curve reason attached.
        pool = [mk(f"Mid {i}", wr=0.55, colors="W", cmc=4) for i in range(20)]
        pack = [
            mk("Cheap Bear", wr=0.55, colors="W", cmc=2, grp_id=1),
            mk("Big Bear", wr=0.55, colors="W", cmc=4, grp_id=2),
        ]
        result = evaluate_pack(pack, pool, pick_number=7, pack_number=2, fmt=FMT)
        by_id = {g.grp_id: g for g in result}
        assert by_id[1].score > by_id[2].score
        assert any("two-drop" in r.lower() for r in by_id[1].reasons)

    def test_removal_boosted_when_pool_lacks_removal(self):
        pool = [mk(f"Bear {i}", wr=0.55, colors="W", cmc=3) for i in range(15)]
        pack = [
            mk("Zap", wr=0.55, colors="W", cmc=3, type_line="Instant",
               oracle_text="Destroy target creature.", grp_id=1),
            mk("Vanilla", wr=0.55, colors="W", cmc=3, grp_id=2),
        ]
        result = evaluate_pack(pack, pool, pick_number=3, pack_number=2, fmt=FMT)
        by_id = {g.grp_id: g for g in result}
        assert by_id[1].score > by_id[2].score
        assert any("removal" in r.lower() for r in by_id[1].reasons)

    def test_removal_saturation_penalized(self):
        pool = [
            mk(f"Kill {i}", wr=0.55, colors="W", cmc=3, type_line="Instant",
               oracle_text="Destroy target creature.")
            for i in range(7)
        ] + [mk(f"Bear {i}", wr=0.55, colors="W") for i in range(8)]
        pack = [
            mk("Another Kill", wr=0.55, colors="W", cmc=3, type_line="Instant",
               oracle_text="Destroy target creature.", grp_id=1),
            mk("Vanilla", wr=0.55, colors="W", cmc=3, grp_id=2),
        ]
        result = evaluate_pack(pack, pool, pick_number=3, pack_number=2, fmt=FMT)
        by_id = {g.grp_id: g for g in result}
        assert by_id[2].score > by_id[1].score
        assert any("saturated" in r.lower() for r in by_id[1].reasons)

    def test_top_heavy_curve_penalizes_more_big_spells(self):
        pool = [mk(f"Fatty {i}", wr=0.56, colors="W", cmc=6) for i in range(4)]
        pool += [mk(f"Bear {i}", wr=0.55, colors="W") for i in range(6)]
        pack = [
            mk("Another Fatty", wr=0.56, colors="W", cmc=6, grp_id=1),
            mk("Curve Filler", wr=0.56, colors="W", cmc=3, grp_id=2),
        ]
        result = evaluate_pack(pack, pool, pick_number=6, pack_number=2, fmt=FMT)
        by_id = {g.grp_id: g for g in result}
        assert by_id[2].score > by_id[1].score
        assert any("top-heavy" in r.lower() for r in by_id[1].reasons)

    def test_analyze_pool_census(self):
        pool = [
            mk("Bear", cmc=2),                                        # creature + early
            mk("Kill", cmc=1, type_line="Instant",
               oracle_text="Destroy target creature."),               # removal + early
            mk("Fatty", cmc=6),                                       # heavy
            normalize_card({"grp_id": 1, "name": "Dual",
                            "colors": ["W", "U"], "type_line": "Land"}),  # fixing
        ]
        lane = compute_lane(pool, FMT)
        needs = analyze_pool(pool, FMT, lane)
        assert needs.creatures == 2
        assert needs.early_plays == 2
        assert needs.removal == 1
        assert needs.heavy_drops == 1
        assert needs.fixing == 1


# ---------------------------------------------------------------------------
# Wheel logic
# ---------------------------------------------------------------------------


class TestWheel:
    def test_polyval_horner(self):
        assert _polyval((1, 2, 3), 2) == pytest.approx(11.0)

    def test_high_alsa_low_rank_card_likely_wheels(self):
        card = mk("Late Card", wr=0.52, alsa=9.0)
        mult, prob, reason = _wheel_probability(card, pick_number=2, rank_in_pack=5)
        assert prob >= 75.0
        assert mult == pytest.approx(0.8)
        assert reason is not None and "wheel" in reason.lower()

    def test_best_card_in_pack_wont_wheel(self):
        card = mk("Top Card", wr=0.62, alsa=9.0)
        _, prob, reason = _wheel_probability(card, pick_number=2, rank_in_pack=0)
        assert prob < 15.0
        assert reason is None

    def test_no_wheel_estimate_after_pick_8(self):
        card = mk("Whatever", wr=0.52, alsa=12.0)
        mult, prob, reason = _wheel_probability(card, pick_number=9, rank_in_pack=6)
        assert (mult, prob, reason) == (1.0, 0.0, None)

    def test_wheel_probability_surfaces_on_guidance(self):
        # A weak high-ALSA card in a pack with 5 better cards.
        pack = filler_pack(5, wr=0.58) + [
            mk("Wheeler", wr=0.52, alsa=9.0, grp_id=3)
        ]
        result = evaluate_pack(pack, pool=[], pick_number=2, fmt=FMT)
        wheeler = {g.grp_id: g for g in result}[3]
        assert wheeler.wheel_probability >= 75.0
        assert any("wheel" in r.lower() for r in wheeler.reasons)


# ---------------------------------------------------------------------------
# Bayesian archetype blending
# ---------------------------------------------------------------------------


class TestArchetypeBlend:
    def test_pair_overperformer_scores_higher_late_when_committed(self):
        pool = committed_pool(("W", "U"), n=25)
        plain = mk("Glue Card", wr=0.54, colors="U", grp_id=1)
        gluey = mk("Glue Card", wr=0.54, colors="U", grp_id=2,
                   arch_stats={"WU": ArchStats(gih_wr_pct=60.0, games=5000)})
        pack = [plain, gluey] + filler_pack(4, wr=0.53)
        result = evaluate_pack(pack, pool, pick_number=5, pack_number=3, fmt=FMT)
        by_id = {g.grp_id: g for g in result}
        assert by_id[2].score > by_id[1].score
        assert any("overperforms in wu" in r.lower() for r in by_id[2].reasons)

    def test_tiny_sample_pair_stats_are_ignored(self):
        pool = committed_pool(("W", "U"), n=25)
        plain = mk("Glue Card", wr=0.54, colors="U", grp_id=1)
        noisy = mk("Glue Card", wr=0.54, colors="U", grp_id=2,
                   arch_stats={"WU": ArchStats(gih_wr_pct=65.0, games=5)})
        pack = [plain, noisy] + filler_pack(4, wr=0.53)
        result = evaluate_pack(pack, pool, pick_number=5, pack_number=3, fmt=FMT)
        by_id = {g.grp_id: g for g in result}
        assert by_id[1].score == pytest.approx(by_id[2].score)

    def test_blend_weight_slides_with_pick_number(self):
        """The same overperformer gets a bigger arch boost late than early."""
        pool = committed_pool(("W", "U"), n=16)
        arch = {"WU": ArchStats(gih_wr_pct=60.0, games=5000)}

        def score_at(pick, pack_num):
            plain = mk("Plain", wr=0.54, colors="U", grp_id=1)
            gluey = mk("Gluey", wr=0.54, colors="U", grp_id=2, arch_stats=arch)
            res = evaluate_pack([plain, gluey] + filler_pack(4, wr=0.53),
                                pool, pick_number=pick, pack_number=pack_num,
                                fmt=FMT)
            by_id = {g.grp_id: g for g in res}
            return by_id[2].score - by_id[1].score

        early_gap = score_at(2, 1)
        late_gap = score_at(10, 3)
        assert late_gap > early_gap


# ---------------------------------------------------------------------------
# Open-lane signals
# ---------------------------------------------------------------------------


class TestSignals:
    def test_pack_signals_accumulate_for_late_quality(self):
        pack = [
            mk("Late Blue", wr=0.58, colors="U", alsa=3.0),
            mk("On-time White", wr=0.58, colors="W", alsa=7.0),
        ]
        signals = compute_pack_signals(pack, pick_number=6, fmt=FMT)
        assert signals["U"] > 0
        assert signals["W"] == 0.0  # not late (alsa >= pick)

    def test_signal_tie_breaker_reason_appears(self):
        pack = [mk("Blue Pick", wr=0.56, colors="U", grp_id=1)] + \
            filler_pack(4, wr=0.53, colors="G")
        result = evaluate_pack(pack, pool=[], pick_number=6, pack_number=1,
                               fmt=FMT, color_signals={"U": 15.0})
        blue = {g.grp_id: g for g in result}[1]
        assert any("open" in r.lower() for r in blue.reasons)

    def test_late_signal_bonus_in_pack_one(self):
        # Strong card at pick 7 with ALSA 3: the lane is being passed to us.
        pack = [mk("Passed Bomb", wr=0.61, colors="U", alsa=3.0, grp_id=1)] + \
            filler_pack(5, wr=0.53)
        result = evaluate_pack(pack, pool=[], pick_number=7, pack_number=1, fmt=FMT)
        top = {g.grp_id: g for g in result}[1]
        assert any("late signal" in r.lower() for r in top.reasons)


# ---------------------------------------------------------------------------
# Reasons & record shape
# ---------------------------------------------------------------------------


class TestReasonsAndShape:
    def test_every_card_gets_at_least_one_reason(self):
        pool = committed_pool(("W", "U"), n=10)
        pack = (
            filler_pack(3, wr=0.55, colors="W")
            + filler_pack(3, wr=0.52, colors="R")
            + [mk("No Data Card", wr=None)]
        )
        result = evaluate_pack(pack, pool, pick_number=5, pack_number=2, fmt=FMT)
        for g in result:
            assert g.reasons, f"{g.name} has no reasons"
            assert all(isinstance(r, str) and r for r in g.reasons)

    def test_no_data_card_says_so(self):
        result = evaluate_pack([mk("Mystery", wr=None)], [], pick_number=1, fmt=FMT)
        assert any("no 17lands data" in r.lower() for r in result[0].reasons)

    def test_basic_land_is_skipped(self):
        pack = [
            mk("Plains", wr=None, colors="", cmc=0,
               type_line="Basic Land — Plains", grp_id=1),
            mk("Bear", wr=0.55, grp_id=2),
        ]
        result = evaluate_pack(pack, [], pick_number=1, fmt=FMT)
        by_id = {g.grp_id: g for g in result}
        assert by_id[1].score == 0.0
        assert by_id[1].tier == "WEAK"
        assert any("basic land" in r.lower() for r in by_id[1].reasons)
        assert result[0].grp_id == 2

    def test_lone_basic_land_is_acknowledged(self):
        pack = [mk("Plains", wr=None, colors="", cmc=0,
                   type_line="Basic Land — Plains")]
        result = evaluate_pack(pack, [], pick_number=14, fmt=FMT)
        assert any("only card" in r.lower() for r in result[0].reasons)

    def test_tier_mapping(self):
        assert get_tier(90) == "FIRE"
        assert get_tier(75) == "GOLD"
        assert get_tier(60) == "SILVER"
        assert get_tier(50) == "BRONZE"
        assert get_tier(10) == "WEAK"

    def test_empty_pack_returns_empty(self):
        assert evaluate_pack([], [], pick_number=1) == []

    def test_guidance_record_fields(self):
        result = evaluate_pack(filler_pack(3), [], pick_number=1, fmt=FMT)
        g = result[0]
        assert isinstance(g.grp_id, int)
        assert isinstance(g.score, float)
        assert g.tier in ("WEAK", "BRONZE", "SILVER", "GOLD", "FIRE")
        assert isinstance(g.reasons, list)
        assert isinstance(g.wheel_probability, float)
