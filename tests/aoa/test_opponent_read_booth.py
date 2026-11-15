"""Native booth and IPC against the public opponent enchantments in the 19:39 Nov 14 report."""

import json
import sys
import threading
from dataclasses import replace

import pytest
from test_headless_booth import source  # noqa: F401
from test_nov14_strategic_replays import report

from arenamcp.commentary.audited_facts import binding
from arenamcp.commentary.budget import CommentaryBudget
from arenamcp.commentary.client import CommentaryClient
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.context import audit_format, generation_format
from arenamcp.commentary.contracts import Utterance
from arenamcp.commentary.timeline import valid_for


def captured():
    obs = report("193910")
    # The original opponent's public CARDs at instances 332/334 have owner seat 2.
    for card in obs.public_state["zones"]["battlefield"]:
        if card.get("instance_id") in (332, 334):
            card.update(owner_seat_id=2, object_kind="CARD")
    return obs


@pytest.mark.usefixtures("source")
@pytest.mark.parametrize("mode", ["lead", "already_heard", "entry", "audit_denied"])
def test_model_can_answer_the_public_opponent_read_through_native_worker(request, mode):
    obs = captured()
    event = next(
        e for e in obs.events if e.kind == ("spell_resolved" if mode == "entry" else "action_observed")
    )
    lead = "Your opponent casts Lunar Convocation."
    obs = replace(
        obs,
        events=(event,),
        recent_commentary=(lead,) if mode == "already_heard" else (),
        private_state={"hand": [{"name": "NEVER IN PUBLIC READ"}]},
    )
    line = "The Case and Convocation could pressure the opponent without another clean attack."
    calls, captured_reads = [], []
    budget = CommentaryBudget()
    exchange = budget.begin()

    def rpc(system, ctx, audit):
        calls.append(audit)
        schema = audit_format() if audit else generation_format(reply="partner_call" in ctx)
        encoded = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
        assert "NEVER IN PUBLIC READ" not in encoded
        budget.before_call(
            exchange, system, encoded, audit=audit, schema=json.dumps(schema, separators=(",", ":"))
        )
        budget.after_call(exchange)
        keys = [k for k, v in ctx["facts"].items() if v.get("kind") == "opponent_plan_inference"]
        assert len(keys) == 1
        read = ctx["facts"][keys[0]]
        assert [e["grp_id"] for e in read["evidence"]] == [88930, 91759]
        assert read["visibility"] == "public" and read["source"] == "reviewed_public_card_facts"
        assert "Both Case" in read["text"]
        captured_reads.append(read)
        if audit:
            return {
                "checks": [
                    dict(
                        index=i,
                        supported=mode != "audit_denied",
                        unsupported_claim="denied" if mode == "audit_denied" else "",
                        adds_insight=True,
                        scope="historical",
                    )
                    for i in range(len(ctx["lines"]))
                ]
            }
        # The separate N2 source should still fit for this event.
        assert any(
            f.get("kind") == ("card_relationship" if mode == "entry" else "card_threat")
            for f in ctx["facts"].values()
        )
        if mode != "entry":
            assert ctx["partner_call"]["text"] == lead
        return {"turns": [{"role": "color_analyst", "text": line, "refs": [ctx["new_events"][0], keys[0]]}]}

    client = CommentaryClient()
    config = CommentaryConfig(
        mode="commentary",
        generative=True,
        python=sys.executable,
        aoa_source=request.getfixturevalue("source"),
    )
    try:
        result = client.generate(obs, config, rpc, threading.Event())
    finally:
        client.close()
    assert calls == [False, True] and captured_reads[0] == captured_reads[1]
    utterances = [Utterance.from_wire(v) for v in result["utterances"]]
    if mode == "audit_denied":
        assert not utterances
        return
    assert utterances[-1].role == "color_analyst" and utterances[-1].text == line, result["status"]
    if mode == "lead":
        assert [u.text for u in utterances] == [lead, line]
        assert utterances[-1].anchor_id == utterances[0].utterance_id
    else:
        assert len(utterances) == 1
    analysis = utterances[-1]
    assert valid_for(analysis, obs)
    assert not valid_for(analysis, replace(obs, snapshot_id="changed"))
    assert not analysis.event_bound and not analysis.position_id


def test_unknown_source_or_missing_native_dependency_cannot_bind():
    obs = captured()
    key = "state:opponent_read:pair:fixture"
    fact = {"source": "reviewed_public_card_facts", "kind": "opponent_plan_inference", "visibility": "public"}
    tracked = {"facts": {key: fact}, "refs": [key], "deps": {key: fact}}
    assert binding("historical", tracked, {}, obs, "An inference.") == ("snapshot", "")
    for changed in ({"source": "uncited"}, {"visibility": "private"}, {"kind": "untrusted"}):
        new = {**fact, **changed}
        assert (
            binding("historical", {**tracked, "facts": {key: new}, "deps": {key: new}}, {}, obs, "A claim.")
            is None
        )
    assert binding("position", {**tracked, "deps": {}}, {}, obs, "A claim.") is None


def test_local_or_omitted_event_cannot_attach_an_opposing_deck_read():
    from arenamcp.commentary.context import compact_context

    obs = captured()
    from arenamcp.opponent_read import OpponentRead

    fact = next(iter(OpponentRead().observation_facts(obs).values()))
    key = "state:opponent_read:test"
    for seat, filler in [(1, ""), (2, "x" * 10000)]:
        event = {"seat": seat, "kind": "cast", "data": {"grp_id": 91759, "name": filler}}
        result = compact_context("", {"facts": {"event:1": event, key: fact}, "new_events": ["event:1"]})
        assert key not in result["facts"]


def test_large_handoff_must_recompact_for_long_prompt_if_partner_evidence_drops():
    from arenamcp.commentary.context import compact_context, request_size
    from arenamcp.commentary.headless import REPLY, SYSTEM

    obs = captured()
    ctx = {
        "new_events": ["event:1"],
        "facts": {
            "event:1": {
                "kind": "cast",
                "seat": 2,
                "data": {"grp_id": 91759, "name": "Lunar Convocation", "instance_id": 334},
                "player": "Your opponent",
            }
        },
    }
    for card in obs.public_state["zones"]["battlefield"]:
        ctx["facts"][f"state:permanent:{card['instance_id']}"] = card
        ctx["facts"][f"card:{card['grp_id']}"] = {"rules": card["oracle_text"]}
    ctx["facts"]["card:91759"]["rules"] += " " * 7000
    result = compact_context(SYSTEM, ctx, reply_system=REPLY)
    assert not result.get("partner_call")
    assert (
        request_size(SYSTEM, result) + len(json.dumps(generation_format(), separators=(",", ":")).encode())
        <= 6000
    )
