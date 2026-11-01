"""Independent reconstructed public-mechanics review; no historical replay."""

from tests.test_entry_draw_search_308 import plan, search
from tests.test_entry_draw_transition_308 import army, innocence
from tests.test_tactical_timing_backlog import card


def token_spell():
    return card(
        74,
        "Test Token Spell",
        "Create a 1/1 white Soldier creature token.",
        "Sorcery",
        "{W}",
    )


def test_noncreature_token_spell_after_new_engine_must_not_silently_omit_chain():
    result = search(hand=[innocence(), token_spell()])
    replay = plan(result, ["Enduring Innocence", "Test Token Spell"])
    assert replay.line is None and replay.unmodelled, (
        f"Unsupported token entry must abstain, not return a zero-draw/no-ping line: {replay!r}"
    )


def test_noncreature_token_spell_with_preexisting_engine_must_abstain():
    result = search(hand=[token_spell()], extra=(innocence(),))
    replay = plan(result, ["Test Token Spell"])
    assert replay.line is None and replay.unmodelled, (
        f"Pre-existing engine availability is unknown even for token entrants: {replay!r}"
    )


def test_existing_army_full_search_retains_growth_after_late_removal():
    result = search(extra=(army(power=2, toughness=2, counters={"+1/+1": 2}),))
    late = plan(result, ["Enduring Innocence", "Test Recruit", "Answer (on Orcish Bowmasters)"])
    early = plan(result, ["Answer (on Orcish Bowmasters)", "Enduring Innocence", "Test Recruit"])
    assert late.line is not None and early.line is not None
    assert late.line._leaf.draws == early.line._leaf.draws == 1
    late_armies = [b for b in late.line._leaf.theirs if b["name"] == "Orc Army"]
    # The early line can kill the smaller Army in subsequent combat, so only
    # the late line's surviving Army is a valid final-leaf growth assertion.
    assert len(late_armies) == 1
    assert late_armies[0]["instance_id"] == 500
    assert late_armies[0]["power"] == late_armies[0]["toughness"] == 3
    assert late_armies[0]["counters"] == {"+1/+1": 3}
