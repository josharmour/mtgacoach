"""A completed grounded lead can hand one specific public event to the other speaker."""

import sys
import threading
import time
from dataclasses import replace

import pytest
from test_headless_booth import source  # noqa: F401

from arenamcp.commentary.client import CommentaryClient
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.contracts import Observation, Utterance
from arenamcp.commentary.service import CommentaryService
from arenamcp.commentary.timeline import valid_for


def prepare_lead(kind, *, private=False):
    emitted = []
    service = CommentaryService(settings=lambda: {}, backend=lambda: None, emit=emitted.append)
    config = CommentaryConfig(mode="commentary", speakers=2, generative=True, private_information=private)
    state = {
        "match_id": "handoff",
        "game_number": 1,
        "local_seat_id": 1,
        "turn": {"turn_number": 4, "active_player": 2, "phase": "Phase_Combat"},
        "players": [{"seat_id": 1, "life_total": 12}, {"seat_id": 2, "life_total": 16}],
        "zones": {"battlefield": []},
        "commentary_source_sequence": 7,
    }
    service.adapter.observe(state, private)
    card = {
        "name": "Harbor Sentry",
        "grp_id": 431,
        "instance_id": 961,
        "type_line": "Creature — Human",
        "card_types": ["Creature"],
        "is_attacking": True,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "power": 3,
        "toughness": 3,
    }
    state.update(commentary_source_sequence=8)
    if kind == "combat":
        state["zones"] = {"battlefield": [card]}
    elif kind == "life_change":
        state["players"] = [{"seat_id": 1, "life_total": 5}, {"seat_id": 2, "life_total": 16}]
    elif kind == "outcome":
        state["zones"] = {"graveyard": [card]}
        state["commentary_events"] = [
            {
                "id": 2,
                "kind": "outcome",
                "match_id": "handoff",
                "game_number": 1,
                "seq": 8,
                "seat": 2,
                "snapshot_id": "8",
                "received_at": time.monotonic(),
                "payload": {
                    "category": "Destroy",
                    "from_zone": "battlefield",
                    "to_zone": "graveyard",
                    "cards": [card],
                    "count": 1,
                },
            }
        ]
    service.config, service.latest = config, state
    obs = service.adapter.observe(state, private)
    event = next(e for e in obs.events if e.kind == kind)
    obs = replace(obs, events=(event,))
    service._grounded(obs, config, "analysis_waits_for_gameplay")
    message = next(e for e in emitted if e["type"] == "commentary_proposal")
    assert message["public_only"] == (not private)
    return service, config, obs, event, message["utterance"]


@pytest.mark.parametrize(
    "kind,mode",
    [
        ("combat", "heard"),
        ("life_change", "heard"),
        ("outcome", "heard"),
        ("outcome", "owner"),
        ("outcome", "interrupted"),
        ("outcome", "forged"),
        ("outcome", "uncited"),
        ("outcome", "failed_audit"),
    ],
)
def test_grounded_handoff_in_normal_worker(kind, mode, request):
    service, config, obs, event, lead = prepare_lead(kind, private=mode == "owner")
    uid = lead["utterance_id"]
    service.delivery(uid, "started")
    assert service._heard_public_calls(obs.epoch, obs.events) == ()
    service.delivery(uid, "superseded" if mode == "interrupted" else "finished")
    heard = service._heard_public_calls(obs.epoch, obs.events)
    if mode == "forged":
        heard = ((event.event_id, "private canary"),)
    if mode == "uncited":
        other = replace(
            event,
            kind="life_change",
            event_id="other-life",
            seat=1,
            payload={"from": 12, "to": 11, "delta": -1},
        )
        obs = replace(obs, events=(event, other))
    obs = Observation.from_wire(replace(obs, heard_public_calls=heard).to_wire())
    calls = []

    def rpc(system, ctx, audit):
        calls.append((ctx, audit))
        if audit:
            return {
                "checks": [
                    {
                        "index": 0,
                        "supported": True,
                        "unsupported_claim": "",
                        "adds_insight": mode != "failed_audit",
                        "scope": "snapshot",
                    }
                ]
            }
        if mode in {"interrupted", "forged"}:
            return {"turns": []}
        ref = (
            next(r for r in ctx["new_events"] if r not in ctx["partner_call"]["refs"])
            if mode == "uncited"
            else ctx["partner_call"]["refs"][0]
        )
        text = {
            "combat": "That attack could leave fewer blockers for the return swing.",
            "outcome": "Losing that creature could open the next attack.",
            "life_change": "That smaller life cushion could make blocking more attractive.",
        }[kind]
        return {"turns": [{"role": "color_analyst", "text": text, "refs": [ref]}]}

    client = CommentaryClient()
    try:
        result = client.generate(
            obs,
            replace(config, python=sys.executable, aoa_source=request.getfixturevalue("source")),
            rpc,
            threading.Event(),
        )
    finally:
        client.close()
    ctx = calls[0][0]
    if mode in {"interrupted", "forged"}:
        assert "partner_call" not in ctx and "private canary" not in str(ctx)
    else:
        assert ctx["partner_call_heard"] and ctx["partner_call"]["text"] == lead["text"]
        assert ctx["allowed_roles"] == ["color_analyst"] and ctx["editorial_question"]["question"]
    lines = [Utterance.from_wire(row) for row in result["utterances"]]
    if mode in {"heard", "owner"}:
        assert [audit for _, audit in calls] == [False, True]
        assert [line.role for line in lines] == ["color_analyst"]
        assert not lines[0].anchor_id and lines[0].evidence_ids == (event.event_id,)
        assert valid_for(lines[0], obs) and not valid_for(lines[0], replace(obs, snapshot_id="changed"))
    else:
        assert not lines
    if mode == "uncited":
        assert result["status"]["state"] == "missing_partner_reference"


def test_original_public_receipt_binds_payload_epoch_and_consent():
    service, config, obs, event, lead = prepare_lead("outcome", private=True)
    service.delivery(lead["utterance_id"], "finished")
    assert service._heard_public_calls(obs.epoch, obs.events)
    assert not service._heard_public_calls(replace(obs.epoch, game_number=2), obs.events)
    assert not service._heard_public_calls(obs.epoch, (replace(event, visibility="private"),))
    event.payload["cards"][0]["type_line"] = "Artifact"
    assert not service._heard_public_calls(obs.epoch, obs.events)
    service.configure()
    assert not service.heard_event_calls


def test_expired_delivery_discards_grounded_evidence_without_a_late_handoff():
    service, config, obs, event, lead = prepare_lead("outcome")
    uid = lead["utterance_id"]
    service.pending_config, service.pending_epoch = config, obs.epoch
    service.inflight[uid] = time.monotonic() - 1
    # The producer already observed this state. Let the next normal empty poll
    # expire the unacknowledged proposal, then stop without starting the client.
    service.status = lambda *args, **kwargs: service.close()
    service.wake.set()
    service._run()
    assert uid not in service.proposals and uid not in service.grounded_ids
    service.delivery(uid, "finished")
    assert not service._heard_public_calls(obs.epoch, (event,))
