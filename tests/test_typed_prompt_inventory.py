"""Typed decisions retain the original inventory and every current card's rules."""

import re
from unittest.mock import Mock

import pytest
from tests.test_card_facts_prompt_render import sample
from tests.test_match_context import card_lookup, obj, state

from arenamcp.action_planner import ActionPlanner
from arenamcp.card_facts.schema import card_facts_to_dict
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.match_context import prepare_match_context, reset_seen_rules


@pytest.fixture(autouse=True)
def clear_seen():
    reset_seen_rules()
    yield
    reset_seen_rules()


@pytest.mark.parametrize("certain", [True, False])
def test_inventory_reconstructs_every_print_count_and_displayed_probability(certain):
    deck = [105, 105, 105, 106, 106, *range(107, 135)]
    s = state(deck, battlefield=[obj(105, 50), obj(107, 51)], library_count=len(deck) - (2 if certain else 3))

    # Keep the legacy and typed labels identical even for distinct prints sharing a name.
    def cards(gid):
        return {**card_lookup(gid), "name": "Shared name" if gid in (105, 106) else f"Card {gid}"}

    args = dict(card_lookup=cards, reveal=[106], library=True)
    original = prepare_match_context(s, **args)
    compact = prepare_match_context(s, compact_decision=True, **args)
    old = original["library_summary"]
    new = compact["library_summary"]
    assert new[: new.index("\n", new.index("\n") + 1)] == old[: old.index("\n", old.index("\n") + 1)]
    assert "Unlisted prints keep their starting-deck counts" in new and len(new) < len(old)
    row = re.compile(r"^(\d+)x .*?\[grp:(\d+)\]", re.M)
    listed = {int(gid): int(n) for n, gid in row.findall(old)}
    starts = {int(gid): int(n) for n, gid in row.findall(compact["deck_reference"])}
    starts.update({int(gid): int(n) for n, gid in row.findall(new)})
    assert {gid: n for gid, n in starts.items() if n} == listed
    assert "2x Shared name [grp:106]" in new and "0x Card 107 [grp:107]" in new
    assert compact["deck_reference"] == original["deck_reference"]
    if certain:
        for n in set(listed.values()):
            assert f"{n}x = {n / sum(listed.values()):.1%}" in new
    else:
        assert "uncertain" in new and "%" not in new


def test_typed_search_keeps_every_option_permanent_and_raw_rule(monkeypatch):
    deck = list(range(201, 245))
    visible = {
        "name": "Fixture Guard",
        "oracle_text": "Vigilance",
        "type_line": "Creature — Test",
        "mana_cost": "{2}",
        "power": "2",
        "toughness": "2",
    }
    s = state(deck, battlefield=[{**obj(201, 50), **visible}], library_count=len(deck) - 1)
    monkeypatch.setattr(
        "arenamcp.match_context._local_card", lambda gid, epoch: visible if gid == 201 else card_lookup(gid)
    )
    options = tuple(DecisionOption(f"sel:{g}", f"Offered card {g}", meta={"grpId": g}) for g in (221, 222))
    decision = PendingDecision((7, 9), "Search", options)
    backend = Mock()
    backend.complete.return_value = '{"option_ids":["sel:221"],"reasoning":"Choose a legal search."}'
    backend.degraded.return_value = False
    backend.available.return_value = True
    planner = ActionPlanner(backend, timeout=5)
    planner.plan_decision_options(decision, s)
    prompt = backend.complete.call_args.args[1]
    assert "Unlisted prints keep their starting-deck counts" in prompt
    assert "Fixture Guard" in prompt and "Vigilance" in prompt
    for op in options:
        assert (
            op.option_id in prompt and op.label in prompt and f"Rules for card {op.meta['grpId']}." in prompt
        )


def test_only_verified_inactive_keyword_body_uses_the_indexed_fact(monkeypatch):
    card, record, _ = sample()
    data = card_facts_to_dict(record)
    reads = []

    def fetch(cards, *, memory_only):
        reads.append(memory_only)
        return {record.rules_hash: data}

    monkeypatch.setattr("arenamcp.card_facts.client.facts_for", fetch)

    def lookup(gid):
        return {**card, "cmc": 3}

    args = dict(card_lookup=lookup, compact_decision=True, source_ids_complete=True)
    seen = state([209, 210], hand=[obj(209, 1)], match_id="fixture")
    current = "Air Cadet [grp:209] | MV 3 | Flying"
    assert current in prepare_match_context(seen, **args)["deck_reference"]
    cleared = state([209, 210], hand=[], match_id="fixture")
    rendered = prepare_match_context(cleared, **args)["deck_reference"]
    assert "\n[grp:209] | MV 3 | Flying" in rendered and "Air Cadet [grp:209]" in rendered
    assert reads and all(reads)
    assert current in prepare_match_context(cleared, reveal=[209], **args)["deck_reference"]
    assert (
        current in prepare_match_context(cleared, **{**args, "source_ids_complete": False})["deck_reference"]
    )
    data["confidence"] = "low"
    assert current in prepare_match_context(cleared, **args)["deck_reference"]

    def unavailable(cards, *, memory_only):
        raise OSError("unavailable")

    monkeypatch.setattr("arenamcp.card_facts.client.facts_for", unavailable)
    assert current in prepare_match_context(cleared, **args)["deck_reference"]
