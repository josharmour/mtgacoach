"""Strategic draft picks: the deck decides, 17Lands is about a quarter (2026-10-13).

The user's direction after the 2026-10-13 FRA Contender Draft: lane, rules-text
synergy with the pool and the pre-researched archetypes should decide picks;
the 17Lands score is a small piece. These tests replay that draft
(tests/fra_contender_draft_2026_10_13.py) with the table's signals computed the
way the live driver computes them.
"""

from __future__ import annotations

import statistics

import pytest
from tests import fra_contender_draft_2026_10_13 as fx

from arenamcp.draft_autopick import color_openness, color_signals, note_openness, rank_pack
from arenamcp.draft_deck_plan import build_deck_plan
from arenamcp.draft_playbook import playbook_for

PRIMER = fx.primer()
GROUPS = {
    "quality": ("quality", "bomb"),
    "signal": ("signal",),
    "deck": ("lane", "needs", "archetype", "engine", "edges", "notes", "penalty"),
}


def signals_at(key: str) -> dict[str, float]:
    store: dict = {}
    for seen in fx.ORDER[: fx.ORDER.index(key) + 1]:
        pack_no, pick_no = (int(part) for part in seen[1:].split("p"))
        note_openness(store, pack_no, color_openness(fx.PACKS[seen], pick_no, PRIMER))
    return color_signals(store, int(key[1:].split("p")[0]))


def ranked(key: str, pool: list[int] | None = None):
    pack_no, pick_no = (int(part) for part in key[1:].split("p"))
    return rank_pack(
        fx.PACKS[key],
        fx.pool_before(key) if pool is None else pool,
        PRIMER,
        pack_number=pack_no,
        pick_number=pick_no,
        names=fx.NAMES,
        mana_costs=fx.COSTS,
        signals=signals_at(key),
    )


def by_name(picks, name: str):
    return next(pick for pick in picks if pick.name == name)


def position(picks, name: str) -> int:
    return [pick.name for pick in picks].index(name)


def test_17lands_is_about_a_quarter_of_what_separates_the_contenders():
    # Share of the spread (mean absolute deviation) among each pack's top five.
    shares: dict[str, list[float]] = {group: [] for group in GROUPS}
    signal_window: list[float] = []
    for key in fx.ORDER:
        contenders = [pick for pick in ranked(key) if pick.score > -900][:5]
        if len(contenders) < 3:
            continue
        spread = {}
        for group, keys in GROUPS.items():
            values = [sum(pick.components.get(k, 0.0) for k in keys) for pick in contenders]
            mean = statistics.fmean(values)
            spread[group] = statistics.fmean(abs(value - mean) for value in values)
        total = sum(spread.values()) or 1.0
        for group in GROUPS:
            shares[group].append(spread[group] / total)
        pack_no, pick_no = (int(part) for part in key[1:].split("p"))
        if pack_no <= 2 and 4 <= pick_no <= 10:
            signal_window.append(spread["signal"] / total)
    quality = statistics.fmean(shares["quality"])
    assert 0.2 <= quality <= 0.3, quality
    assert statistics.fmean(shares["deck"]) > 0.6
    # Signals matter while they can still be acted on (packs 1-2, picks 4-10).
    assert 0.1 <= statistics.fmean(signal_window) <= 0.2


def test_components_add_up_to_the_score():
    for pick in ranked("P2p5"):
        if pick.score > -900:
            assert abs(sum(pick.components.values()) - pick.score) < 1e-3


def test_p1p1_takes_the_best_card_while_the_pool_is_empty():
    # Picks 1-4 take the best card: deck credit ramps in as the pool grows.
    assert ranked("P1p1")[0].name == "Kiora of Fire and Ashes"


def test_p3p3_stays_in_lane_over_an_off_color_rare():
    # Live: autoplay took Proctor of Potential (WU rare, 61.9% GIH) for an Izzet pool.
    picks = ranked("P3p3")
    assert picks[0].name == "Twinned Vision"
    assert position(picks, "Proctor of Potential") > position(picks, "Twinned Vision")


def test_p3p1_takes_the_card_that_works_with_the_pool_over_the_higher_rated_one():
    # Live: Fblthp, Impossibly Lost (59.7% GIH, no rules link to the pool).
    picks = ranked("P3p1")
    foreseer, fblthp = picks[0], by_name(picks, "Fblthp, Impossibly Lost")
    assert foreseer.name == "Semester Foreseer"
    # 17Lands prefers Fblthp; Foreseer's surveil feeds four payoffs we drafted.
    assert fblthp.components["quality"] > foreseer.components["quality"]
    assert any("works with Proft, Consulting Detective" in reason for reason in foreseer.reasons)
    assert any("Izzet Spells & Jace Empower" in reason for reason in foreseer.reasons)


def test_noncreature_spells_are_linked_to_saheeli():
    awaken = by_name(ranked("P3p4"), "Awaken the Inferno")
    assert any("Saheeli, Jewel of Avishkar" in reason for reason in awaken.reasons)
    assert awaken.components["edges"] > 0


def test_fra_engines_come_from_the_whole_set():
    book = playbook_for(PRIMER)
    assert "noncreature_spell" in book.archetype("UR").engine
    assert "graveyard" in book.archetype("UB").engine
    assert "surveil" in book.archetype("WU").engine


def test_prowess_and_temporary_pumps_are_weaker_payoffs():
    book = playbook_for(PRIMER)
    assert book.tag(fx.ID["Cryotheory Adept"]).payoff_strength < 1.0
    assert book.tag(fx.ID["Saheeli, Jewel of Avishkar"]).payoff_strength == 1.0


def test_a_committed_plan_is_the_only_hypothesis():
    plan = build_deck_plan(fx.pool_before("P2p3"), PRIMER, plan_lane="UR", mana_costs=fx.COSTS)
    assert plan.committed and [h.plan.colors for h in plan.hypotheses] == ["UR"]


@pytest.mark.parametrize("pack_number,kept", [(2, 4), (3, 2)])
def test_an_open_second_color_keeps_the_main_colors_pairs_alive(pack_number, kept):
    plan = build_deck_plan(
        fx.pool_before("P1p8"),
        PRIMER,
        lane_colors="UR",
        lane_main="U",
        commitment=0.9,
        second_hold=0.1,
        lane_weights={"U": 10.0, "R": 1.0, "B": 1.0},
        pack_number=pack_number,
        mana_costs=fx.COSTS,
    )
    assert not plan.committed
    assert len(plan.hypotheses) == kept
    assert all("U" in h.plan.colors for h in plan.hypotheses)


def test_rarely_played_cards_get_no_deck_credit():
    crescendo = by_name(ranked("P2p12"), "Blazing Crescendo")
    assert any("rarely played" in reason for reason in crescendo.reasons)
    for key in ("archetype", "engine", "edges", "notes"):
        assert crescendo.components[key] == 0
    assert crescendo.components["needs"] <= 0


# ---------------------------------------------------------------------------
# What the rest of the draft can still offer (2026-10-13)
# ---------------------------------------------------------------------------


def test_future_looks_cover_the_rest_of_the_draft():
    from arenamcp.draft_deck_plan import future_supply

    book = playbook_for(PRIMER)
    assert future_supply(book, 1, 1).looks == 41  # this pack's 13 more + two fresh packs
    assert future_supply(book, 3, 14).looks == 0
    # Pick-Two pods: seven looks per pack, each one two picks deeper.
    two = future_supply(book, 1, 1, picks_per_pass=2)
    assert two.looks == 20 and max(two.schedule) == 13


def test_commons_keep_coming_and_early_picks_disappear():
    from arenamcp.draft_deck_plan import future_supply

    book = playbook_for(PRIMER)
    early, late = future_supply(book, 1, 2), future_supply(book, 3, 6)
    common = fx.ID["Twinned Vision"]
    assert early.expected([common]) > late.expected([common]) > 0
    # A whole engine's worth of mono-colored spells shows up at a good share of looks.
    spells = [
        g
        for g, t in book.tags.items()
        if "noncreature_spell" in t.engine_enables() and t.colors in ("U", "R")
    ]
    assert early.expected(spells) > 0.3 * early.looks > late.expected(spells)


def test_a_payoff_is_worth_more_while_enablers_can_still_come():
    from arenamcp.draft_deck_plan import Hypothesis, Ledger, engine_gain, future_supply

    book = playbook_for(PRIMER)
    plan = book.archetype("UR")
    hypothesis = Hypothesis(plan, 1.0, 0.0, Ledger("UR"))  # no enablers drafted yet
    saheeli = book.tag(fx.ID["Saheeli, Jewel of Avishkar"])
    early = engine_gain(saheeli, hypothesis, future_supply(book, 1, 3))[0]
    late = engine_gain(saheeli, hypothesis, future_supply(book, 3, 12))[0]
    assert early > late


def test_scarce_removal_says_so_and_scores_for_it():
    awaken = by_name(ranked("P3p4"), "Awaken the Inferno")
    assert awaken.components["scarcity"] > 0
    assert any(reason.startswith("scarce: ") for reason in awaken.reasons)
    # A plentiful role late in a pack (another two-drop body) gets none.
    assert by_name(ranked("P3p4"), "Murmuring Volume").components["scarcity"] == 0


def test_a_listed_payoff_that_loses_games_does_not_beat_a_playable():
    # Live 2026-10-13 P1p4: the rebuilt research listed Inspired Tethermage (50.0% GIH,
    # z -1.52) as a Simic payoff and the ranking took it over playable cards.
    from arenamcp.set_primer import SetCard, SetPrimer

    cards = {
        i: SetCard(
            i, f"Filler {i}", "U", "C", "Creature", cmc=3, gih_wr=0.55, baseline=0.0, games=3000, alsa=6.0
        )
        for i in range(1, 40)
    }
    cards[50] = SetCard(
        50,
        "Weak Payoff",
        "G",
        "C",
        "Creature",
        cmc=2,
        oracle="Whenever you put one or more loyalty counters on a planeswalker, put a +1/+1 counter on this creature.",
        gih_wr=0.50,
        baseline=-1.52,
        games=3000,
        alsa=7.0,
    )
    cards[51] = SetCard(
        51, "Playable Bear", "G", "C", "Creature", cmc=2, gih_wr=0.53, baseline=-0.6, games=3000, alsa=6.0
    )
    primer = SetPrimer(
        set_code="TST",
        cards=cards,
        archetypes=[{"colors": "UG", "name": "Simic Jace", "tier": 1, "payoffs": ["Weak Payoff"]}],
        pair_stats={"UG": {"win_rate": 0.55, "games": 5000}},
    )
    picks = rank_pack([50, 51], [1, 2, 3, 4, 5, 51], primer, pack_number=1, pick_number=7)
    assert picks[0].name == "Playable Bear"


# ---------------------------------------------------------------------------
# The deck build follows the draft (2026-10-13)
# ---------------------------------------------------------------------------


def _pool_cards(grp_ids):
    cards = []
    for grp_id in grp_ids:
        card = PRIMER.card(grp_id)
        if card is None:
            continue
        cards.append(
            {
                "grp_id": grp_id,
                "name": card.name,
                "type_line": card.types,
                "oracle_text": card.oracle,
                "mana_cost": fx.COSTS.get(grp_id, ""),
                "gih_wr": card.gih_wr,
                "rarity": {"C": "common", "U": "uncommon", "R": "rare", "M": "mythic"}.get(card.rarity, ""),
                "cmc": card.cmc,
            }
        )
    return cards


def test_the_build_follows_the_drafted_lane_without_trading_away_card_quality():
    from arenamcp.limited_deck import DeckStrategy, fallback_deck

    cards = _pool_cards(fx.PICKS.values())
    rates = {key: row["win_rate"] for key, row in PRIMER.pair_stats.items()}
    plain = fallback_deck(cards, rates, fmt="draft")
    planned = fallback_deck(cards, rates, fmt="draft", strategy=DeckStrategy(PRIMER, plan_lane="UR"))
    quality = planned["quality"]
    assert quality["colors"] == "UR" and quality["drafted_lane"]
    assert quality["archetype"] == "Izzet Spells & Jace Empower"
    deck = {entry["name"] for entry in planned["main_deck"]}
    assert "Saheeli, Jewel of Avishkar" in deck
    # 2026-10-13 live build: at a lower quality weight 47-50% cards replaced a 58.7% one.
    assert quality["avg_gih"] >= plain["quality"]["avg_gih"] - 0.5
