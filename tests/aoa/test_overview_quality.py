"""Saved and externally supplied overviews cannot bypass speech script hygiene."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.context import deck_overview
from arenamcp.commentary.contracts import Evidence
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.headless import deterministic
from arenamcp.commentary.overview_quality import speakable_overview

BAD = [
    "This protects the creature while it’s附着",
    "The creature is защищён.",
    "The card is προστασία.",
    "Safe first sentence. The analyst says 附着",
    "Hidden\x00control",
    "Hidden\u202econtrol",
    "Hidden\u200bcontrol",
    "Bad\ud800text",
    "Two\nlines",
    "A\tcard",
    "\u0301Orphan accent",
    "",
    "   ",
    ".?!",
    "123",
    "a" * 501,
    None,
    42,
    {},
    [],
]
GOOD = [
    "This deck develops mana. Protection keeps threats alive.",
    "Éowyn’s plan uses Æther—then attacks (with +1/+1 counters).",
    "Lim-Dûl and E\u0301owyn: a 2/2, +1/+1, {W}, and 50% chance?",
    "“Draw a card,” then attack… Don’t assume the spell resolves!",
]


def observed(summary):
    observation = ObservationAdapter().project({"match_id": "m", "game_number": 1, "local_seat_id": 1})
    event = Evidence(
        "overview",
        "deck_overview",
        observation.source_sequence,
        observation.snapshot_id,
        payload={"identity": "d", "summary": summary},
        visibility="private",
        received_at=observation.arrived_at,
    )
    return replace(observation, events=(event,))


@pytest.mark.parametrize("summary", BAD)
def test_bad_summary_refused_at_both_boundaries_without_mutation(summary):
    book = SimpleNamespace(identity="d", data={"spoken_summary": summary})
    coach = SimpleNamespace(_coach=SimpleNamespace(_deck_playbook=book), _deck_analysis_key=("m", "d"))
    before = copy.deepcopy(book.data)
    assert not speakable_overview(summary)
    assert deck_overview(coach, {"match_id": "m"}) is None
    # Direct Evidence deliberately bypasses context/adapter input filtering.
    for speakers in (1, 2):
        config = CommentaryConfig(mode="commentary", private_information=True, speakers=speakers)
        assert deterministic(observed(summary), config) == []
    assert book.data == before


@pytest.mark.parametrize("summary", GOOD)
def test_latin_names_and_typography_are_preserved(summary):
    assert speakable_overview(summary)
    book = SimpleNamespace(identity="d", data={"spoken_summary": summary})
    coach = SimpleNamespace(_coach=SimpleNamespace(_deck_playbook=book), _deck_analysis_key=("m", "d"))
    assert deck_overview(coach, {"match_id": "m"})["summary"] == summary
    config = CommentaryConfig(mode="commentary", private_information=True, speakers=1)
    assert deterministic(observed(summary), config)[0].text == "Our deck plan: " + summary


def test_hygiene_does_not_claim_semantic_correctness():
    assert speakable_overview("Every creature is a land.")
