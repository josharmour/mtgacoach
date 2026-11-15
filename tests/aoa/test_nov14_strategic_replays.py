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
@pytest.mark.parametrize(
    "mode", ["heard", "pass", "interrupted", "same_words_other_cast", "other_game", "uncited"]
)
def test_heard_public_cast_hands_off_to_writer_while_spell_is_still_on_stack(mode, request):
    import sys
    import threading

    from arenamcp.commentary.client import CommentaryClient
    from arenamcp.commentary.contracts import Observation, Utterance
    from arenamcp.commentary.service import CommentaryService

    # Nov 14 19:39, native public cast seq 326, before the Case entered at
    # 328 or Lunar was cast at 335. Keep only this public spell's identity.
    state = json.loads((Path(__file__).parent / "fixtures/public_20261114_193910.json").read_bytes())
    card = next(c for c in state["zones"]["battlefield"] if c["instance_id"] == 332)
    state["zones"] = {"stack": [card], "battlefield": []}
    state["turn"] = {"turn_number": 7, "phase": "Phase_Main2", "active_player": 2}
    state["commentary_source_sequence"] = 326
    state["commentary_events"] = [
        dict(
            state["commentary_events"][0],
            id=21,
            seq=326,
            payload={"instance_id": 332, "grp_id": 88930, "name": "Case of the Uneaten Feast"},
            received_at=time.monotonic(),
        )
    ]
    emitted = []
    service = CommentaryService(settings=lambda: {}, backend=lambda: None, emit=emitted.append)
    config = CommentaryConfig(mode="commentary", speakers=2, generative=True)
    service.config, service.latest = config, state
    obs = service.adapter.observe(state)
    event = next(e for e in obs.events if e.kind == "action_observed")
    obs = replace(obs, events=(event,))
    service._grounded(obs, config, "analysis_waits_for_gameplay")
    lead = next(e["utterance"] for e in emitted if e["type"] == "commentary_proposal")
    service.delivery(lead["utterance_id"], "started")
    assert not service._heard_cast_calls(obs.epoch, obs.events)
    service.delivery(lead["utterance_id"], "superseded" if mode == "interrupted" else "finished")
    if mode == "same_words_other_cast":
        obs = replace(obs, events=(replace(event, event_id="another-play"),))
    if mode == "other_game":
        obs = replace(obs, epoch=replace(obs.epoch, game_number=2))
    if mode == "uncited":
        other = replace(event, event_id="second-cast", payload=dict(name="Opt", grp_id=1, instance_id=999))
        obs = replace(obs, events=(event, other))
    obs = Observation.from_wire(
        replace(
            obs,
            heard_cast_calls=service._heard_cast_calls(obs.epoch, obs.events),
            recent_commentary=(lead["text"],),
        ).to_wire()
    )
    requests = []

    def rpc(system, ctx, audit):
        requests.append((ctx, audit))
        if audit:
            return {
                "checks": [
                    {
                        "index": 0,
                        "supported": True,
                        "unsupported_claim": "",
                        "adds_insight": True,
                        "scope": "snapshot",
                    }
                ]
            }
        if mode == "uncited":
            return {
                "turns": [
                    {
                        "role": "color_analyst",
                        "text": "The second spell could buy time.",
                        "refs": [ctx["new_events"][1]],
                    }
                ]
            }
        if mode != "heard":
            return {"turns": []}
        return {
            "turns": [
                {
                    "role": "color_analyst",
                    "text": "The Case could buy time as more creatures enter.",
                    "refs": [ctx["new_events"][0], "card:88930"],
                }
            ]
        }

    client = CommentaryClient(audit_replay=True)
    try:
        config = replace(config, python=sys.executable, aoa_source=request.getfixturevalue("source"))
        result = client.generate(obs, config, rpc, threading.Event())
        lines = [Utterance.from_wire(item) for item in result["utterances"]]
    finally:
        client.close()
    assert [audit for _, audit in requests] == ([False, True] if mode == "heard" else [False])
    ctx = requests[0][0]
    if mode in {"heard", "pass", "uncited"}:
        assert ctx["partner_call_heard"] and ctx["partner_call"]["text"] == lead["text"]
        assert ctx["allowed_roles"] == ["color_analyst"] and ctx["editorial_question"]["question"]
        assert not ctx["editorial_question"]["check_these_refs"]  # no preselected rule pair
    else:
        assert "partner_call" not in ctx
    if mode == "heard":
        assert [line["role"] for line in requests[1][0]["lines"]] == ["color_analyst"]
    if mode == "uncited":
        assert result["status"]["state"] == "missing_cast_reference"
    assert [line.role for line in lines] == (["color_analyst"] if mode == "heard" else [])
    assert not any(line.anchor_id for line in lines)
    if lines:
        assert lines[0].evidence_ids == (event.event_id,) and lines[0].snapshot_id == obs.snapshot_id


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
    assert (
        point.startswith("If Case of the Uneaten Feast gains life,")
        and "Lunar Convocation could" in point
        and "without attacking" in point
    )
    assert scope == "cards:332,334"
    public = copy.deepcopy(o.public_state)
    for card in public["zones"]["battlefield"]:
        if card.get("controller_seat_id") == event.seat and card.get("instance_id") != 334:
            card["controller_seat_id"] = 3 - event.seat
    changed = strategic_observation(replace(o, public_state=public), event)
    assert not changed or "Lunar Convocation" not in changed[0]


def test_karn_and_unattributed_objects_do_not_supply_an_unconditional_lifegain_engine():
    from arenamcp.commentary.context import compact_context
    from arenamcp.commentary.strategy import strategic_observation

    a = report("193910")
    event = next(e for e in a.events if e.kind == "action_observed")
    b = report("195635")
    karn = next(c for c in b.public_state["zones"]["battlefield"] if c["name"] == "Karn, Argent Defender")
    public = copy.deepcopy(a.public_state)
    public["zones"]["battlefield"].append(karn)
    point, _ = strategic_observation(replace(a, public_state=public), event)
    assert point.startswith("If Case of the Uneaten Feast gains life,")
    ctx = {
        "facts": {"event:1": {"kind": "cast", "seat": 2, "data": {"grp_id": 91759}}},
        "new_events": ["event:1"],
    }
    for c in public["zones"]["battlefield"]:
        ctx["facts"][f"state:permanent:{c['instance_id']}"] = c
        ctx["facts"][f"card:{c['grp_id']}"] = {"rules": c.get("oracle_text")}
    angle = compact_context("", ctx)["editorial_question"]
    assert angle["check_these_refs"] == [] and "What changed" in angle["question"]
    for c in public["zones"]["battlefield"]:
        if c.get("name") == "Lunar Convocation":
            c.pop("instance_id")
    changed = strategic_observation(replace(a, public_state=public), event)
    assert not changed or "Lunar Convocation" not in changed[0]


@pytest.mark.parametrize("value", ["N/A.", "To be written.", "Omitted."])
def test_cached_placeholder_rejected_at_speech_boundary(value):
    assert not speakable_overview(value)


@pytest.mark.usefixtures("source")
@pytest.mark.parametrize(
    "mode", ["approved", "uncited_analyst", "audit_denied", "wrong_role", "silence", "already_called"]
)
def test_cast_partner_reply_passes_full_native_group_audit(mode):
    o = report("193910")
    event = next(e for e in o.events if e.kind == "action_observed")
    lead = "Your opponent casts Lunar Convocation."
    o = replace(o, events=(event,), recent_commentary=(lead,) if mode == "already_called" else ())
    audited = []

    def rpc(system, ctx, audit):
        if audit:
            audited.extend(ctx["lines"])
            return {
                "checks": [
                    dict(
                        index=i,
                        supported=mode != "audit_denied",
                        adds_insight=True,
                        unsupported_claim="unsupported" if mode == "audit_denied" else "",
                        scope="position",
                    )
                    for i in range(len(audited))
                ]
            }
        assert ctx["partner_call"]["text"] == lead
        assert ctx["allowed_roles"] == ["color_analyst"]
        if mode == "silence":
            return {"turns": []}
        refs = list(ctx["editorial_question"]["check_these_refs"])
        if mode != "uncited_analyst":
            refs.insert(0, ctx["new_events"][0])
        else:
            assert refs and not set(refs).intersection(ctx["partner_call"]["refs"])
        return {
            "turns": [
                {
                    "role": "play_by_play" if mode == "wrong_role" else "color_analyst",
                    "text": "If the Case gains life, blockers alone might not stabilize the race.",
                    "refs": refs,
                }
            ]
        }

    b = HeadlessBooth(CommentaryConfig(mode="commentary", speakers=2, generative=True), rpc, time.monotonic)
    lines, _ = b.generate(o)
    if mode in ("uncited_analyst", "wrong_role", "silence"):
        assert not lines and not audited
    elif mode == "audit_denied":
        assert not lines and [x["role"] for x in audited] == ["play_by_play", "color_analyst"]
    else:
        assert [x["text"] for x in audited] == [item.text for item in lines]
        if mode == "already_called":
            assert len(lines) == 1 and lines[0].role == "color_analyst"
        else:
            assert len(lines) == 2 and lines[0].text == lead and lines[1].anchor_uid == lines[0].uid


COST_RULE = (
    "Whenever Leatherhead deals combat damage to a player, you may remove a counter from her. "
    "When you do, destroy target artifact or enchantment that player controls."
)


@pytest.mark.parametrize(
    "text, withheld",
    [
        ("Losing the threat that stripped artifacts or enchantments on hit hurts.", True),
        ("She cannot remove a counter but still destroys artifacts on hit.", True),
        ("She removed a counter and destroyed the artifact on hit.", True),
        ("After hitting a player, spending a counter could destroy that player's artifact.", False),
        ("It can remove a counter on hit.", False),
    ],
)
def test_paid_combat_trigger_in_heard_nov14_trade(text, withheld):
    from arenamcp.commentary.context import omits_on_hit_counter_cost

    assert omits_on_hit_counter_cost(text, {"card:123": {"rules": COST_RULE}}) is withheld
    assert not omits_on_hit_counter_cost(text, {"card:123": {"rules": "Trample"}})


@pytest.mark.usefixtures("source")
@pytest.mark.parametrize(
    "line,allowed",
    [
        ("Losing the threat that stripped artifacts or enchantments on hit hurts.", False),
        ("Spending a counter after hitting a player could destroy that player's artifact.", True),
    ],
)
def test_counter_payment_through_native_audit(line, allowed):
    from test_headless_booth import observation, response

    o = observation()
    o.public_state["zones"]["stack"][0].update(name="Leatherhead, Swamp Stalker", oracle_text=COST_RULE)
    o.events[0].payload["name"] = "Leatherhead, Swamp Stalker"
    calls = []

    def rpc(system, ctx, audit):
        calls.append(audit)
        result = response(ctx, audit)
        if not audit:
            result["turns"][0]["text"] = "Leatherhead goes onto the stack."
            result["turns"][1].update(text=line, refs=[*ctx["new_events"], "card:1"])
        return result

    booth = HeadlessBooth(CommentaryConfig(mode="commentary", generative=True), rpc, time.monotonic)
    lines, _ = booth.generate(o)
    assert calls == [False, True]
    assert (line in [item.text for item in lines]) is allowed


def test_compared_attackers_have_separate_on_hit_abilities():
    from arenamcp.commentary.context import omits_on_hit_counter_cost

    refs = {
        "card:1": {"name": "Leatherhead, Swamp Stalker", "rules": COST_RULE},
        "card:2": {
            "name": "Caustic Wasps",
            "rules": "Whenever Caustic Wasps deals combat damage to a player, you may destroy target artifact that player controls.",
        },
    }
    assert not omits_on_hit_counter_cost(
        "Caustic Wasps can destroy an artifact on hit, unlike the other threat.", refs
    )
    assert omits_on_hit_counter_cost("Leatherhead destroys artifacts on hit, like Caustic Wasps.", refs)
    assert omits_on_hit_counter_cost(
        "Caustic Wasps can destroy an artifact on hit, and Leatherhead destroys artifacts on hit.", refs
    )
