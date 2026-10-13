"""Unrated rares and mythics: pick order sets the prior, a small sample only nudges it.

2026-10-12 P2p1 (bug_20261012_232641): Darklight Phoenix, a black mythic in our
BR lane that drafters take around pick 2.3, had no 17lands GIH rating (190 games
in hand) and a 52.3% game win rate over 238 games. The ranking read that win rate
at face value (z -1.07), put it at #5, and vetoed the model's pick of it for the
common Apex Witchstalker. 238 games carry a +-3.2 point sampling error, more than
the 2.1-point spread between FRA cards.
"""

from __future__ import annotations

from arenamcp.draft_autopick import _set_stats, card_value, rank_pack
from arenamcp.set_primer import SetCard, SetPrimer


def _set(*extra: SetCard) -> SetPrimer:
    cards = {}
    for i in range(40):
        alsa = 1.5 + i * 0.2  # picks 1.5 .. 9.3
        cards[i + 1] = SetCard(
            i + 1,
            f"Rated {i}",
            "B",
            "C",
            "Creature",
            cmc=3,
            gih_wr=0.55,
            baseline=round(1.4 - 0.35 * alsa, 3),
            games=3000,
            game_wr=0.51 + (i % 5) * 0.015,
            play_rate=0.75,
            alsa=alsa,
        )
    cards.update({card.grp_id: card for card in extra})
    return SetPrimer(set_code="TST", cards=cards)


PHOENIX = SetCard(
    100,
    "Darklight Phoenix",
    "B",
    "M",
    "Creature - Phoenix",
    cmc=4,
    games=238,
    game_wr=0.5225,
    play_rate=0.67,
    alsa=2.3,
)


def test_early_picked_mythic_with_a_small_mediocre_sample_stays_above_average():
    primer = _set(PHOENIX)
    stats = _set_stats(primer)
    assert stats.alsa_fit is not None and stats.alsa_fit[1] < 0
    score, reason, rated = card_value(PHOENIX, stats)
    assert not rated
    assert score > 0, reason
    assert "over 238 games" in reason and "around pick 2.3" in reason
    # Face value (the old reading) was well below average.
    assert (PHOENIX.game_wr - stats.game_wr_mean) / stats.game_wr_sd < -0.8


def test_the_mythic_outranks_an_average_common_of_its_color():
    primer = _set(PHOENIX)
    average_common = next(card for card in primer.cards.values() if abs(card.baseline) < 0.1)
    picks = rank_pack([average_common.grp_id, PHOENIX.grp_id], [], primer)
    assert picks[0].grp_id == PHOENIX.grp_id


def test_a_late_picked_rare_starts_below_average():
    late = SetCard(101, "Bulk Rare", "B", "R", "Creature", cmc=4, games=0, play_rate=0.6, alsa=8.5)
    score, reason, _ = card_value(late, _set_stats(_set(late)))
    assert score < 0 and "around pick 8.5" in reason


def test_a_large_sample_still_moves_the_estimate():
    strong = SetCard(
        102, "Proven Mythic", "B", "M", "Creature", cmc=4, games=3000, game_wr=0.60, play_rate=0.9, alsa=6.0
    )
    weak = SetCard(
        103, "Exposed Mythic", "B", "M", "Creature", cmc=4, games=3000, game_wr=0.49, play_rate=0.9, alsa=2.0
    )
    stats = _set_stats(_set(strong, weak))
    assert card_value(strong, stats)[0] >= 1.0  # late pick order, but 3000 games say it is great
    assert card_value(weak, stats)[0] < 0  # early pick order, but 3000 games say it is not


def test_without_pick_order_data_the_rarity_prior_remains():
    bare = SetCard(104, "Scarce Mythic", "B", "M", "Creature", games=400)
    assert card_value(bare, _set_stats(_set(bare))) == (0.2, "unrated rare; rarity prior", False)
