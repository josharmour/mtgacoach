"""Spoken draft commentary: grounded reasons per pick, story beats, and the on/off toggle."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arenamcp.draft_autopick import Lane, PickScore
from arenamcp.draft_narration import DraftNarrator


def card(grp_id, name, colors, gih, *, types="Creature — Wizard", cmc=2.0, oracle="Flying"):
    return SimpleNamespace(
        grp_id=grp_id, name=name, colors=colors, gih_wr=gih, types=types, cmc=cmc, oracle=oracle
    )


class Primer:
    def __init__(self, cards):
        self.cards = {c.grp_id: c for c in cards}
        self.archetypes = [
            {"colors": "WU", "name": "Azorius Scry & Surveil Tempo", "key_commons": ["Tide Scholar"]},
            {"colors": "UB", "name": "Dimir Control"},
        ]
        self.pair_stats = {"WU": {"win_rate": 0.56}, "UB": {"win_rate": 0.567}, "RG": {"win_rate": 0.51}}

    def card(self, grp_id):
        return self.cards.get(grp_id)

    def card_roles(self, name):
        return [
            (a["colors"], role)
            for a in self.archetypes
            for role in ("key_commons",)
            if name in (a.get(role) or [])
        ]


CARDS = [
    card(1, "Sky Ace", "U", 0.62),
    card(2, "Tide Scholar", "U", 0.55),
    card(3, "Bog Snare", "B", 0.58, types="Instant", oracle="Destroy target creature."),
    card(4, "Meadow Squire", "W", 0.53),
    card(5, "Forge Pup", "R", 0.54),
]
PRIMER = Primer(CARDS)


def lane(colors, commitment=0.6):
    return Lane(colors=colors, weights={}, commitment=commitment)


def line(
    narrator, pick, ranking, *, pool=(1, 2), before="U", after="U", pack=1, number=5, size=14, reason=""
):
    return narrator.pick_line(
        names=[PRIMER.card(pick.grp_id).name],
        chosen=[pick],
        ranking=ranking,
        pool=list(pool),
        lane_before=lane(before),
        lane_after=lane(after),
        primer=PRIMER,
        pack_number=pack,
        pick_number=number,
        pack_size=size,
        model_reason=reason,
    )


def score(grp_id, *reasons):
    return PickScore(grp_id=grp_id, name=PRIMER.card(grp_id).name, score=0.0, reasons=list(reasons))


def test_best_card_in_pack_is_said_from_17lands_data():
    narrator = DraftNarrator()
    narrator._lane = "UW"  # no lane story this pick
    text = line(narrator, score(1), [score(1), score(2)], after="WU", before="WU")
    assert text.startswith(
        "Taking Sky Ace: the best card in this pack on 17Lands data, in our white-blue colors."
    )


def test_removal_need_and_archetype_role_are_grounded():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    text = line(
        narrator, score(3, "pool needs removal (0 so far)"), [score(1), score(3)], before="UB", after="UB"
    )
    assert text.startswith("Taking Bog Snare: removal our blue-black deck needs.")
    narrator._lane = "WU"
    text = line(narrator, score(2, "in lane WU"), [score(1), score(2)], before="WU", after="WU")
    assert "a key common for Azorius Scry & Surveil Tempo" in text


def test_unverified_model_synergy_is_never_spoken():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    reason = "Pairs with Sky Ace to empower Jace (synergy unverified)"
    text = line(narrator, score(4), [score(1), score(4)], before="UB", after="UB", reason=reason)
    assert "Sky Ace" not in text.split(":", 1)[1] and "empower" not in text


def test_lane_change_tells_the_story_once():
    narrator = DraftNarrator()
    first = line(narrator, score(3), [score(3)], before="U", after="UB")
    assert "We're leaning blue-black, one of the strongest pairs in this set on 17Lands" in first
    assert "the deck is Dimir Control" in first
    again = line(narrator, score(3), [score(3)], before="UB", after="UB")
    assert "leaning" not in again


def test_end_of_pack_summary_names_best_cards_and_needs():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    text = line(narrator, score(2), [score(2)], pool=(1, 2), before="UB", after="UB", number=14, size=14)
    assert "End of pack 1: we're blue-black with 2 on-color cards, led by Sky Ace and Tide Scholar" in text
    assert "still looking for removal" in text


@pytest.fixture
def primer():
    from tests.test_draft_autoplay import PAIRS, RATINGS

    from arenamcp.set_primer import data_primer

    return data_primer("TST", RATINGS, PAIRS, lambda grp, name: {"oracle_text": f"{name} text", "cmc": 3.0})


def test_driver_commentary_toggle(primer):
    from tests.test_draft_autoplay import PICK_STATE, FakeBridge, driver_for

    for enabled in (False, True):
        bridge = FakeBridge(
            {
                "get_screen": {"ok": True, "draft": True},
                "get_draft_state": dict(PICK_STATE),
                "submit_draft_pick": {"ok": True},
            }
        )
        spoken: list[str] = []
        driver = driver_for(bridge, primer, speak_fn=spoken.append, commentary_fn=lambda e=enabled: e)
        driver._step()
        assert len(spoken) == 1
        if enabled:
            assert spoken[0].startswith("Taking ") and ":" in spoken[0]
        else:
            assert spoken[0] == "Taking Blue Ace."


# ---------------------------------------------------------------------------
# Pick-Two: each card its own reason, grammar for two, honesty about our lane
# (2026-10-07 live PickTwoDraft_FRA: "Taking Twinned Vision and Extended
# Absence: the best card in this pack on 17Lands data." for two cards, and
# Haunted Ridge, a B/R dual, under "a solid card for our blue-black deck").
# ---------------------------------------------------------------------------

LANDS = [
    card(40, "Ash Ridge", "", 0.63, types="Land", cmc=None, oracle="{oT}: Add {oB} or {oR}."),
    card(41, "Tidal Fen", "", 0.54, types="Land", cmc=None, oracle="{oT}: Add {oU} or {oB}."),
]
LAND_PRIMER = Primer(CARDS + LANDS)


def pair_line(narrator, picks, ranking, *, colors="UB", commitment=0.6, reasons=None, primer=PRIMER):
    return narrator.pick_line(
        names=[primer.card(p.grp_id).name for p in picks],
        chosen=picks,
        ranking=ranking,
        pool=[1, 2],
        lane_before=lane(colors, commitment),
        lane_after=lane(colors, commitment),
        primer=primer,
        pack_number=1,
        pick_number=5,
        pack_size=7,
        model_reasons=reasons,
        verb="Choosing",
    )


def test_two_best_cards_are_spoken_in_the_plural():
    narrator = DraftNarrator()
    narrator._lane = "U"
    text = pair_line(
        narrator, [score(1), score(2)], [score(1), score(2), score(4)], colors="U", commitment=0.1
    )
    assert text == "Choosing Sky Ace and Tide Scholar: the two best cards in this pack on 17Lands data."
    assert "the best card in this pack" not in text


def test_pick_two_gives_each_card_its_own_deck_reason():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    picks = [score(1), score(3, "pool needs removal (1 so far)")]
    text = pair_line(narrator, picks, [score(1), score(3), score(4)])
    assert text == (
        "Choosing Sky Ace — the best card in this pack on 17Lands data, in our blue-black colors; "
        "and Bog Snare — removal our blue-black deck needs."
    )


def test_off_lane_cards_are_never_called_a_fit_even_when_the_model_says_so():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    reasons = ["", "Fits our deck perfectly and curves out nicely."]
    text = pair_line(narrator, [score(1), score(5, "in lane UB")], [score(1), score(5)], reasons=reasons)
    assert "Forge Pup — the strongest option left, though it's outside our blue-black colors" in text
    assert "fits our deck" not in text.lower()


def test_lands_are_described_by_the_mana_they_make_for_our_lane():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    picks = [score_in(LAND_PRIMER, 40), score_in(LAND_PRIMER, 41)]
    text = pair_line(narrator, picks, picks + [score(1)], primer=LAND_PRIMER)
    assert "Ash Ridge — only a black source for our blue-black deck" in text
    assert "Tidal Fen — fixes our blue-black mana" in text
    # Lands' inflated GIH never makes them "the best card in this pack".
    assert "17Lands" not in text


def test_model_reason_prefers_the_clause_about_our_deck_and_says_colors():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    reasons = [
        "",
        "Strongest body in the pack: a 1/5 flier. Fits our UB spells lane and adds a needed creature.",
    ]
    text = pair_line(narrator, [score(1), score(2)], [score(1), score(4), score(2)], reasons=reasons)
    assert "Tide Scholar — fits our blue-black spells lane and adds a needed creature" in text


def score_in(primer, grp_id, *reasons):
    return PickScore(grp_id=grp_id, name=primer.card(grp_id).name, score=0.0, reasons=list(reasons))
