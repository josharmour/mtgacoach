"""Public slices of the two Nov 14 captures. No player names, hand, deck or original match IDs."""

import copy
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_headless_booth import source  # noqa: F401

from arenamcp.commentary.audited_facts import binding, value
from arenamcp.commentary.budget import BudgetExceeded
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.context import cast_entry_conflict, parse_completed_json, valid_verdict
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.headless import HeadlessBooth
from arenamcp.commentary.overview_quality import speakable_overview
from arenamcp.commentary.timeline import position_fingerprint


def report(stamp):
    path = Path(__file__).parent / "fixtures" / f"public_20261114_{stamp}.json"
    state = json.loads(path.read_text())
    for event in state["commentary_events"]:
        event["received_at"] = time.monotonic()
    return ObservationAdapter().observe(state)


@pytest.mark.usefixtures("source")
@pytest.mark.parametrize("stamp,name", [("193910", "Lunar Convocation"), ("195635", "Seam Rip")])
def test_dense_report_board_supplies_event_rules_and_both_players_permanents(stamp, name):
    o = report(stamp)
    o = replace(o, events=tuple(e for e in o.events if e.kind == "action_observed"))
    event = o.events[0]
    requests = []

    def rpc(system, ctx, audit):
        requests.append(ctx)
        return {"turns": []}

    booth = HeadlessBooth(CommentaryConfig(mode="commentary", generative=True), rpc, time.monotonic)
    booth.generate(o)
    facts = requests[0]["facts"]
    assert facts[requests[0]["new_events"][0]]["data"]["name"] == name
    assert facts[f"card:{event.payload['grp_id']}"]["rules"]
    assert {v["controller_seat_id"] for k, v in facts.items() if k.startswith("state:permanent:")} == {1, 2}
    assert not any(k.startswith("private:") for k in facts)
    key = f"state:permanent:{event.payload['instance_id']}"
    record = dict(refs=[key], facts={key: facts[key]}, deps={key: facts[key]})
    kind, scope = binding("position", record, {}, o, "This could change the race.")
    assert kind == "position"
    before = position_fingerprint(o, scope)
    public = copy.deepcopy(o.public_state)
    public["zones"]["stack"] = []
    public["turn_info"]["phase"] = "Combat"
    public["players"][0]["life_total"] -= 1
    other = next(
        c for c in public["zones"]["battlefield"] if c["instance_id"] != event.payload["instance_id"]
    )
    other["is_tapped"] = not other.get("is_tapped")
    after = replace(o, public_state=public, private_state={"hand": [{"name": "unrelated"}]})
    assert position_fingerprint(after, scope) == before
    focal = next(
        c for c in public["zones"]["battlefield"] if c["instance_id"] == event.payload["instance_id"]
    )
    focal["controller_seat_id"] = 3 - focal["controller_seat_id"]
    assert position_fingerprint(after, scope) != before
    public["zones"]["battlefield"].remove(focal)
    assert position_fingerprint(after, scope) != before


def test_field_completeness_and_unknown_refs_are_not_timeless():
    o = report("195635")
    fact = value(o, "field")
    key = "state:battlefield"
    rec = dict(refs=[key], facts={key: fact}, deps={key: fact})
    kind, scope = binding("position", rec, {}, o, "This could change the race.")
    assert kind == "position" and fact["complete"]
    public = copy.deepcopy(o.public_state)
    public["zones"]["graveyard"] = [{"instance_id": i + 500} for i in range(33)]
    changed = replace(o, public_state=public)
    assert not value(changed, "field")["complete"]
    assert position_fingerprint(o, scope) != position_fingerprint(changed, scope)
    for category in ("snapshot", "position", "historical"):
        for broken in (
            dict(rec, refs=[key, "heard:missing"]),
            dict(rec, deps={"future:read": {}}),
            {},
            dict(rec, facts={key: None}),
        ):
            assert binding(category, broken, {}, o, "This could change the race.") is None


def test_arithmetic_binds_life_even_for_past_tense_lines():
    o = report("193910")
    e = o.events[-1]
    k = "event:x"
    facts = {
        k: {"kind": "cast"},
        "card:9": {"source": "local_card_database", "hypothetical_arithmetic": {"players": []}},
    }
    rec = dict(refs=list(facts), facts=facts, deps={})
    assert binding("historical", rec, {k: e}, o, "They cast a spell.") == ("snapshot", "")
    assert binding("position", rec, {k: e}, o, "They could lose life.") == ("position", "audit:identity,life")
    assert binding("historical", rec, {}, o, "They cast a spell.") is None


def test_reasoning_and_malformed_verdicts_cannot_approve():
    check = dict(index=0, supported=True, unsupported_claim="", adds_insight=True, scope="position")
    assert valid_verdict({"checks": [check]}, 1)
    for bad in (
        dict(check, index=False),
        dict(check, scope="timeless"),
        dict(check, extra="yes"),
        dict(check, adds_insight=1),
        {k: v for k, v in check.items() if k != "scope"},
    ):
        assert not valid_verdict({"checks": [bad]}, 1)
    with pytest.raises(BudgetExceeded, match="invalid_json"):
        parse_completed_json('{"checks":[],"checks":[true]}')
    assert cast_entry_conflict(
        "Opponent drops Lunar Convocation into play right before ending their turn seven.",
        {"event:1": {"kind": "cast"}},
    )
    assert not cast_entry_conflict("Opponent casts Lunar Convocation.", {"event:1": {"kind": "cast"}})


def test_report_overview_fails_as_a_whole_including_the_latin_truncation():
    for bad in (
        "Our deck develops enchantments. Sheltered only keeps things gone while it's附着",
        "Our deck develops enchantments. Sheltered only keeps things gone while it's",
        "(Spoken summary:) The first half looks complete.",
    ):
        assert not speakable_overview(bad)


def test_replay_fallback_explains_pressure_without_declaring_a_trigger():
    from arenamcp.commentary.strategy import strategic_observation

    o = report("193910")
    event = next(e for e in o.events if e.kind == "action_observed")
    point, scope = strategic_observation(o, event)
    assert "Lunar Convocation" in point and "could" in point and "without attacking" in point
    assert scope == "cards:332,334"
    public = copy.deepcopy(o.public_state)
    for card in public["zones"]["battlefield"]:
        if card.get("controller_seat_id") == event.seat and card.get("instance_id") != 334:
            card["controller_seat_id"] = 3 - event.seat
    changed = strategic_observation(replace(o, public_state=public), event)
    assert not changed or "Lunar Convocation" not in changed[0]
