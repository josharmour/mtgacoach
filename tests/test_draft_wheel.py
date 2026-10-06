"""Wheel-aware picks: take the card that won't come back when the better one will."""

from __future__ import annotations

from types import SimpleNamespace

from arenamcp.draft_autopick import PickScore, wheel_adjust, wheel_chance


def primer(**alsa):
    cards = {i: SimpleNamespace(name=name, alsa=value) for i, (name, value) in enumerate(alsa.items(), 1)}
    return SimpleNamespace(card=lambda grp_id: cards.get(grp_id))


def ranked(*scores):
    return [PickScore(grp_id=i, name=f"Card{i}", score=s, reasons=[]) for i, s in enumerate(scores, 1)]


def test_wheel_chance_follows_average_last_seen():
    card = SimpleNamespace(alsa=11.0)
    assert wheel_chance(card, 9) > 0.9
    assert wheel_chance(SimpleNamespace(alsa=3.0), 9) < 0.01
    assert wheel_chance(SimpleNamespace(alsa=None), 9) == 0.0


def test_takes_the_card_that_wont_wheel_when_the_best_one_will():
    # Card1 is slightly better but is usually still around at pick 10+; Card2 goes by pick 3.
    p = primer(Card1=11.5, Card2=2.5, Card3=6.0)
    out = wheel_adjust(ranked(0.9, 0.7, 0.1), list(range(1, 15)), p, pick_number=2)
    assert out[0].grp_id == 2 and "Card1 likely wheels" in out[0].reasons[-1]
    assert "pick 10" in out[0].reasons[-1]


def test_keeps_the_best_card_when_the_gap_is_large_or_it_wont_wheel():
    p = primer(Card1=11.5, Card2=2.5)
    assert wheel_adjust(ranked(2.5, 0.2), list(range(1, 15)), p, pick_number=2)[0].grp_id == 1
    p = primer(Card1=2.0, Card2=2.5)
    assert wheel_adjust(ranked(0.9, 0.7), list(range(1, 15)), p, pick_number=2)[0].grp_id == 1


def test_no_wheel_once_the_pack_will_not_come_back():
    p = primer(Card1=14.0, Card2=2.5)
    # 8 cards left with 8 drafters: nothing returns to us.
    assert wheel_adjust(ranked(0.9, 0.7), list(range(1, 9)), p, pick_number=7)[0].grp_id == 1
    # Pick-two drafts take two cards per pass; the adjustment stays out of them.
    assert (
        wheel_adjust(ranked(0.9, 0.7), list(range(1, 15)), p, pick_number=2, picks_per_pass=2)[0].grp_id == 1
    )


def test_four_player_pods_wheel_sooner():
    p = primer(Card1=7.0, Card2=2.0)
    out = wheel_adjust(ranked(0.8, 0.7), list(range(1, 15)), p, pick_number=2, players=4)
    assert out[0].grp_id == 2 and "pick 6" in out[0].reasons[-1]
