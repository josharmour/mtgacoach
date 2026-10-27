"""Splitting artifact rules mechanisms must preserve their strategic weight."""

import pytest

from arenamcp.draft_deck_plan import Hypothesis, Ledger, engine_gain
from arenamcp.draft_playbook import ArchetypePlan, Playbook
from arenamcp.set_primer import SetCard, SetPrimer


@pytest.mark.parametrize(
    "mechanic,enabler_type,enabler_text,payoff_text,noun",
    [
        (
            "artifact_presence",
            "Artifact",
            "",
            "{T}, Sacrifice another artifact: Draw a card.",
            "artifacts",
        ),
        (
            "artifact_tokens",
            "Sorcery",
            "Create a Treasure token.",
            "If one or more artifact tokens would be created under your control, twice that many of those tokens are created instead.",
            "artifact token makers",
        ),
    ],
)
def test_split_artifact_mechanisms_keep_broad_engine_threshold_and_weight(
    mechanic, enabler_type, enabler_text, payoff_text, noun
):
    def card(grp_id, types, text):
        return SetCard(grp_id, f"Card {grp_id}", "R", "U", types, 2, text)

    cards = {g: card(g, enabler_type, enabler_text) for g in range(1, 5)}
    cards.update({g: card(g, "Creature", payoff_text) for g in (10, 11)})
    primer = SetPrimer("TST", cards=cards, archetypes=[{"colors": "UR", "name": "Artifacts"}])
    book = Playbook(primer)
    # Two generic payoffs did not establish an artifact engine before the
    # rules labels were split; the new tags must not lower that bar.
    assert mechanic not in book.archetype("UR").engine
    cards[12] = card(12, "Creature", payoff_text)
    book = Playbook(primer)
    plan = book.archetype("UR")
    assert mechanic in plan.engine

    # Both artifact subtypes remain broad enablers, with the same weight as
    # a true artifact-entry enabler, rather than suddenly getting full credit.
    entry_plan = ArchetypePlan("UR", "Entry", 2, engine=("artifact_entry",))
    split_plan = ArchetypePlan("UR", "Split", 2, engine=(mechanic,))
    assert (
        book.fit(1, split_plan, roles=False, key_lists=False)[0]
        == book.fit(1, entry_plan, roles=False, key_lists=False)[0]
    )
    hypothesis = Hypothesis(split_plan, 1.0, 1.0, Ledger("UR", enablers={mechanic: 4}))
    gain, reason = engine_gain(book.tag(10), hypothesis)
    assert gain > 0
    assert reason == f"a payoff for our 4 {noun}"
