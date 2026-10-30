"""Recent recaps survive phase transitions; current predictions remain scoped."""

import copy
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_headless_booth import observation, source  # noqa: F401
from test_narration_context import snapshot

from arenamcp.commentary.client import CommentaryClient
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.contracts import Evidence, SpeechCategory, Utterance
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.timeline import historical_scope, position_fingerprint, select_events, valid_for


def test_phase_changes_preserve_position_but_not_timing_predictions():
    state = snapshot()
    a = ObservationAdapter()
    old = a.project(state, True)
    recap = Utterance(
        "past",
        "The spell was cast.",
        SpeechCategory.COMMENTARY,
        old.epoch,
        time.monotonic() + 30,
        evidence_ids=("e",),
        snapshot_id=old.snapshot_id,
        event_bound=True,
    )
    position = replace(recap, utterance_id="hand", event_bound=False, position_id=position_fingerprint(old))
    prediction = replace(recap, utterance_id="prediction", event_bound=False)
    state["turn_info"].update(phase="Combat", step="DeclareAttackers", priority_player=2)
    newer = a.project(state, True)
    assert old.snapshot_id != newer.snapshot_id
    assert valid_for(recap, newer) and valid_for(position, newer) and not valid_for(prediction, newer)
    state["zones"]["my_hand"] = []
    changed = a.project(state, True)
    assert valid_for(recap, changed) and not valid_for(position, changed)
    state["game_number"] = 2
    assert not valid_for(recap, a.project(state, True))


def test_historical_classification_cannot_relax_current_dependencies():
    assert historical_scope("historical", {}, "Opt was cast.")
    assert not historical_scope("historical", {"private:hand:1": {}}, "Opt was cast.")
    assert not historical_scope("historical", {}, "They might respond next.")
    assert not historical_scope("snapshot", {}, "Opt was cast.")


def test_fast_burst_prioritizes_impact_over_newer_routine_events():
    obs = observation()
    now = time.monotonic()
    events = [
        Evidence(f"land-{i}", "land_drop", 1, "s", payload={"name": "Forest"}, received_at=now + i / 1000)
        for i in range(45)
    ]
    cast = Evidence("major", "action_observed", 1, "s", payload={"name": "Opt"}, received_at=now - 10)
    combat = Evidence("combat", "combat", 1, "s", payload={"count": 5}, received_at=now - 5)
    chosen = select_events([cast, combat, *events], obs)
    assert len(chosen) == 4 and {"major", "combat"} <= {e.event_id for e in chosen}
    assert len(select_events([cast, combat, *events], obs, limit=32)) == 32


def test_delayed_journal_keeps_source_turn_and_expires_after_recap_window():
    state = snapshot()
    now = time.monotonic()
    state["commentary_events"] = [
        {
            "id": 1,
            "seq": 1,
            "snapshot_id": "old",
            "match_id": "opening",
            "game_number": 1,
            "kind": "action_observed",
            "seat": 1,
            "payload": {"name": "Known spell"},
            "received_at": now - 20,
            "turn_number": 1,
            "phase": "Main1",
            "active_player": 1,
        }
    ]
    state["turn_info"].update(turn_number=2, phase="Combat", active_player=2)
    event = next(e for e in ObservationAdapter().observe(state).events if e.kind == "action_observed")
    assert event.turn_number == 1 and event.phase == "Main1" and event.active_player == 1
    state["commentary_events"][0]["received_at"] = now - 31
    assert not any(e.kind == "action_observed" for e in ObservationAdapter().observe(state).events)


@pytest.mark.usefixtures("source")
def test_real_worker_produces_historical_recaps_with_original_evidence():
    obs = observation()
    event = replace(
        obs.events[0], received_at=time.monotonic() - 18, turn_number=1, phase="Main1", active_player=1
    )
    obs = replace(obs, events=(event,))
    config = CommentaryConfig(
        mode="commentary",
        generative=True,
        python=sys.executable,
        aoa_source=os.environ.get("AOA_SOURCE", str(Path(__file__).resolve().parents[3] / "arenaonair")),
    )

    def rpc(system, context, audit):
        if audit:
            return {
                "checks": [
                    {"index": i, "supported": True, "unsupported_claim": "", "scope": "historical"}
                    for i in range(len(context["lines"]))
                ]
            }
        key = context["new_events"][0]
        assert context["facts"][key]["turn"] == 1 and context["facts"][key]["phase"] == "Main1"
        return {
            "turns": [
                {"role": "play_by_play", "text": "Opt was cast.", "refs": [key]},
                {"role": "color_analyst", "text": "That cast put Opt onto the stack.", "refs": [key]},
            ]
        }

    client = CommentaryClient()
    try:
        result = client.generate(obs, config, rpc, threading.Event())
        assert len(result["utterances"]) == 2, result["status"]
        lines = [Utterance.from_wire(u) for u in result["utterances"]]
        assert all(line.event_bound and line.evidence_ids == (event.event_id,) for line in lines)
        assert 5 < lines[0].expires_at - time.monotonic() <= 12
        moved = copy.deepcopy(obs.public_state)
        moved["turn_info"]["phase"] = "Combat"
        fresh = replace(obs, public_state=moved, snapshot_id="changed")
        assert all(valid_for(line, fresh) for line in lines)
    finally:
        client.close()


def test_fallback_retains_meaningful_event_for_analysis_without_repeating_call():
    from arenamcp.commentary.headless import deterministic
    from arenamcp.commentary.service import CommentaryService

    obs = observation()
    state = {"match_id": obs.epoch.match_id, "game_number": obs.epoch.game_number, **obs.public_state}
    config = CommentaryConfig(mode="commentary", generative=True)
    service = CommentaryService(settings=lambda: {}, backend=lambda: None, emit=lambda _: None)
    service.config, service.latest = config, state
    event = obs.events[0]
    service.pending_events[event.event_id] = event
    service._fallback(obs, config, "protected")
    assert event.event_id in service.pending_events and event.event_id in service.fallback_events
    assert not service.heard
    item = next(iter(service.proposals.values()))
    service.delivery(item.utterance_id, "finished")
    assert tuple(service.heard) == (item.text,)
    service.last_grounded = (None, -float("inf"))
    service._fallback(obs, config, "protected")
    assert not service.proposals
    delayed = replace(obs, arrived_at=obs.arrived_at + 12)
    line = deterministic(delayed, config)[0]
    assert "You cast Opt" in line.text and line.event_bound


def test_model_schema_is_counted_inside_existing_budget():
    import json

    from arenamcp.commentary.budget import BudgetExceeded, CommentaryBudget
    from arenamcp.commentary.context import generation_format

    budget = CommentaryBudget()
    exchange = budget.begin()
    schema = json.dumps(generation_format(), separators=(",", ":"))
    with pytest.raises(BudgetExceeded, match="context_budget"):
        budget.before_call(exchange, "s", "x" * 5740, audit=False, schema=schema)
    budget.finish(exchange)


def test_compaction_prefers_older_impact_over_latest_land():
    from arenamcp.commentary.context import compact_context
    from arenamcp.commentary.headless import SYSTEM

    facts = {
        "event:cast": {"kind": "spell_cast", "data": {"grp_id": 100}},
        "event:land": {"kind": "land_played", "data": {"name": "Forest"}},
        "card:100": {"rules": "Complete rule."},
    }
    result = compact_context(
        SYSTEM,
        {
            "facts": facts,
            "new_events": ["event:cast", "event:land"],
            "allowed_roles": ["play_by_play", "color_analyst"],
        },
    )
    assert result["new_events"][0] == "event:cast"
    assert result["facts"]["card:100"] == facts["card:100"]
