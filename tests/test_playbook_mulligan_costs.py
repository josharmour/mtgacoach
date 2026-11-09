"""Cached strategy cannot invent free mulligans for a live request."""

import pytest
from tests.deck_fakes import playbook_for

from arenamcp.deck_strategy import DECK_ANALYSIS_PROMPT


def test_generation_prompt_does_not_grant_a_free_brawl_mulligan():
    assert "never promise a free Brawl mulligan" in DECK_ANALYSIS_PROMPT


@pytest.mark.parametrize("free,expected_keep", [(None, 3), (0, 3), (1, 4)])
def test_live_cost_overrides_cached_free_mulligan_claim(free, expected_keep):
    book = playbook_for()
    book.data["decision_rules"][0].update(decisions=["mulligan"], prefer="Take the free Brawl mulligan.")
    state = {
        "format_name": "Brawl Ladder",
        "decision_context": {"type": "mulligan", "mulliganCount": 4},
    }
    if free is not None:
        state["decision_context"]["freeMulliganCount"] = free
    text = book.decision_context(state, request_type="MulliganReq")
    assert f"explicitly granted free mulligans={free or 0}" in text
    assert f"keeps {expected_keep}" in text
    assert text.index("LIVE MULLIGAN COST OVERRIDE") > text.index("Take the free Brawl mulligan")
    assert "actual bottom request remains authoritative" in text
