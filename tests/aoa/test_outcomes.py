"""Outcome bursts, factual analyst contributions and freshness without live services."""

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arenamcp.commentary.budget import BudgetExceeded, CommentaryBudget
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.events import ObservationAdapter, journal_event
from arenamcp.commentary.headless import deterministic
from arenamcp.commentary.timeline import valid_for


def game():
    zones = {
        i: SimpleNamespace(zone_type=SimpleNamespace(value=t))
        for i, t in enumerate(["Library", "Graveyard", "Battlefield", "Stack", "Exile"])
    }
    g = SimpleNamespace(
        match_id="test",
        game_number=1,
        zones=zones,
        game_objects={},
        _commentary_journal=[],
        _commentary_event_number=0,
        _raw_gre_sequence=10,
    )
    g.turn_info = SimpleNamespace(turn_number=14, phase="Main1", active_player=1)
    return g


def add(g, iid, name, *, kind="Mill", source=0, destination=1, hidden=False):
    row = {
        "instance_id": iid,
        "name": name,
        "grp_id": iid,
        "type_line": "Creature",
        "mana_cost": "{7}",
        "face_down": hidden,
    }
    g.game_objects[iid] = SimpleNamespace(
        instance_id=iid, grp_id=iid, controller_seat_id=2, owner_seat_id=2, to_dict=lambda: row
    )
    journal_event(
        g,
        {
            "type": "zone_transfer",
            "category": kind,
            "instance_id": iid,
            "card": name,
            "from_zone": source,
            "to_zone": destination,
        },
    )


def observation(g, age=0):
    state = {
        "match_id": "test",
        "game_number": 1,
        "local_seat_id": 2,
        "player_names": {1: "Rival", 2: "Hero"},
        "players": [],
        "zones": {"battlefield": []},
        "turn": {"turn_number": 14},
        "commentary_events": g._commentary_journal,
    }
    for e in state["commentary_events"]:
        e["received_at"] = time.monotonic() - age
    return ObservationAdapter().observe(state)


def test_twenty_mills_are_one_bounded_outcome_and_two_nonduplicate_roles():
    g = game()
    for i in range(20):
        add(g, i + 100, f"Known card {i}")
    assert len(g._commentary_journal) == 1
    assert g._commentary_journal[0]["payload"]["count"] == 20
    assert len(g._commentary_journal[0]["payload"]["cards"]) == 12
    obs = observation(g, 40)
    lines = deterministic(obs, CommentaryConfig(mode="commentary", speakers=2))
    assert len(lines) == 2 and lines[0].role == "play_by_play" and lines[1].role == "color_analyst"
    assert "milled 20" in lines[0].text and "stocked the graveyard" in lines[1].text
    assert lines[1].anchor_id == lines[0].utterance_id and lines[1].text != lines[0].text
    assert all(valid_for(l, replace(obs, snapshot_id="new-phase")) for l in lines)
    assert not any(e.kind == "outcome" for e in observation(g, 61).events)


def test_removed_creatures_get_current_board_analysis_not_an_invented_win():
    g = game()
    add(g, 100, "Big creature", kind="Destroy", source=2)
    obs = observation(g)
    lines = deterministic(obs, CommentaryConfig(mode="commentary", speakers=2))
    assert "left the battlefield" in lines[0].text
    assert "no creatures left" in lines[1].text
    assert lines[0].event_bound and not lines[1].event_bound and lines[1].position_id
    newer = replace(
        obs, public_state={**obs.public_state, "zones": {"battlefield": [{"name": "New creature"}]}}
    )
    assert valid_for(lines[0], newer) and not valid_for(lines[1], newer)


def test_explicit_counter_outcome_not_stack_disappearance():
    g = game()
    add(g, 100, "World Breaker", kind="Countered", source=3)
    lines = deterministic(observation(g), CommentaryConfig(mode="commentary", speakers=2))
    assert "was countered" in lines[0].text and "creature spell" in lines[1].text
    g = game()
    add(g, 100, "Spell", kind="Resolve", source=3)
    assert not deterministic(observation(g), CommentaryConfig(mode="commentary"))[0].text.startswith(
        "Spell was countered"
    )


def test_hidden_exile_never_names_the_card():
    g = game()
    add(g, 100, "SECRET", kind="Exile", source=2, destination=4, hidden=True)
    lines = deterministic(observation(g), CommentaryConfig(mode="commentary", speakers=2))
    assert "SECRET" not in str(g._commentary_journal) and all("SECRET" not in l.text for l in lines)


def test_background_plan_and_commentary_overlap_but_urgent_decision_preempts():
    from arenamcp.backends.proxy import _BackgroundLane, _CallHandle

    lane = _BackgroundLane()
    plan = _CallHandle("background.game_plan")
    talk = _CallHandle("background.commentary")
    lane.protect(plan)
    assert lane.acquire(plan, 0) == "acquired"
    assert lane.commentary_available() and lane.acquire(talk, 0) == "acquired"
    assert lane.acquire(_CallHandle("background.commentary"), 0) == "dropped"
    urgent = _CallHandle("decision.typed")
    lane.protect(urgent)
    assert talk.is_cancelled and not lane.commentary_available() and not plan.is_cancelled
    lane.release(talk)
    lane.unprotect(urgent)
    assert lane.commentary_available()
    lane.release(plan)


def test_cancelled_attempts_keep_costs_but_leave_airtime_for_a_useful_exchange():
    now = [0.0]
    b = CommentaryBudget(lambda: now[0])
    for _ in range(2):
        x = b.begin()
        b.before_call(x, "s", "u", audit=False)
        now[0] += 0.2
        b.finish(x, usable=False)
    assert sum(x.output_tokens for x in b.history) == 1024
    b.finish(b.begin(), usable=True)
    b.finish(b.begin(), usable=True)
    with pytest.raises(BudgetExceeded):
        b.begin()


def test_cancelled_holder_does_not_block_next_exchange_or_release_new_owner():
    from arenamcp.backends.proxy import _BackgroundLane, _CallHandle

    lane = _BackgroundLane()
    old, new = _CallHandle("background.commentary"), _CallHandle("background.commentary")
    assert lane.acquire(old, 0) == "acquired"
    old.cancel("superseded")
    assert lane.commentary_available() and lane.acquire(new, 0) == "acquired"
    lane.release(old)
    assert not lane.commentary_available()
    lane.release(new)
    assert lane.commentary_available()


def test_no_call_empty_poll_does_not_consume_model_airtime():
    b = CommentaryBudget()
    for _ in range(6):
        b.finish(b.begin(), usable=False)
    assert not b.history
    b.finish(b.begin(), usable=True)
    assert len(b.history) == 1


def test_game_end_outweighs_recent_mill():
    from arenamcp.commentary.contracts import Evidence

    g = game()
    add(g, 100, "Known card")
    obs = observation(g)
    end = Evidence("end", "game_end", obs.source_sequence, obs.snapshot_id, received_at=obs.arrived_at)
    obs = replace(obs, events=(*obs.events, end))
    assert deterministic(obs, CommentaryConfig(mode="commentary"))[0].evidence_ids == ("end",)


def test_outcome_bursts_do_not_merge_across_causes_or_messages():
    g = game()
    add(g, 100, "First")
    g._raw_gre_sequence += 1
    add(g, 101, "Second")
    add(g, 102, "Third", kind="Exile", source=2, destination=4)
    assert len(g._commentary_journal) == 3
    assert all(e["payload"]["count"] == 1 for e in g._commentary_journal)


def test_diagnostics_are_bounded_and_never_forward_exception_secrets():
    from arenamcp.backends.proxy import BackendCallCancelled, FirstTokenTimeout
    from arenamcp.commentary.budget import broker_failure_code
    from arenamcp.commentary.service import CommentaryService

    assert broker_failure_code(BackendCallCancelled("private text", reason="superseded")) == "superseded"
    assert broker_failure_code(FirstTokenTimeout("private text")) == "first_token_timeout"
    assert (
        broker_failure_code(RuntimeError("https://secret@example.invalid/private"))
        == "admission_or_request_failed"
    )
    emitted = []
    service = CommentaryService(settings=lambda: {}, backend=lambda: None, emit=emitted.append)
    for _ in range(4):
        service.status("analysis_empty", reason="superseded")
        service.status("waiting_capacity", reason="grounded_call_spacing")
    assert sum(e["data"]["state"] == "analysis_empty" for e in emitted) == 1


def test_actual_worker_keeps_outcome_evidence_audit_duo_and_recap_window():
    import os
    import sys
    import threading
    from pathlib import Path

    from arenamcp.commentary.client import CommentaryClient
    from arenamcp.commentary.contracts import Utterance

    source = Path(os.environ.get("AOA_SOURCE", Path(__file__).resolve().parents[3] / "arenaonair"))
    if not (source / "src/arenaonair/llm_booth.py").is_file():
        pytest.skip("Independent ArenaOnAir source required")
    g = game()
    for i in range(20):
        add(g, 100 + i, f"Known card {i}")
    obs = observation(g, 40)
    # The real worker is tested; model generation and audit are synthetic and local.
    calls = []

    def rpc(system, context, audit):
        calls.append(audit)
        if audit:
            return {
                "checks": [
                    {"index": i, "supported": True, "unsupported_claim": "", "scope": "historical"}
                    for i in range(len(context["lines"]))
                ]
            }
        key = next(k for k in context["new_events"] if context["facts"][k].get("kind") == "outcome")
        fact = context["facts"][key]
        assert fact["data"]["count"] == 20 and fact["turn"] == 14
        assert fact["player"] == "Hero" and "cite that event for a historical call" in system
        return {
            "turns": [
                {"role": "play_by_play", "text": "Hero milled twenty cards.", "refs": [key]},
                {"role": "color_analyst", "text": "Known card 0 was among the cards milled.", "refs": [key]},
            ]
        }

    config = CommentaryConfig(
        mode="commentary", generative=True, python=sys.executable, aoa_source=str(source)
    )
    client = CommentaryClient()
    try:
        result = client.generate(obs, config, rpc, threading.Event())
        assert calls == [False, True], result["status"]
        assert len(result["utterances"]) == 2, result["status"]
        first, second = [Utterance.from_wire(u) for u in result["utterances"]]
        assert first.role == "play_by_play" and second.role == "color_analyst"
        assert second.anchor_id == first.utterance_id and first.voice != second.voice
        assert all(l.event_bound and 10 < l.expires_at - time.monotonic() <= 20 for l in (first, second))
        assert all(valid_for(l, replace(obs, snapshot_id="next-phase")) for l in (first, second))
    finally:
        client.close()


def test_worker_preserves_preemption_diagnostic_without_private_error():
    import os
    import sys
    import threading
    from pathlib import Path

    from test_headless_booth import observation as cast_observation

    from arenamcp.backends.proxy import BackendCallCancelled
    from arenamcp.commentary.client import CommentaryClient

    source = Path(os.environ.get("AOA_SOURCE", Path(__file__).resolve().parents[3] / "arenaonair"))
    if not (source / "src/arenaonair/llm_booth.py").is_file():
        pytest.skip("Independent ArenaOnAir source required")
    config = CommentaryConfig(
        mode="commentary", generative=True, python=sys.executable, aoa_source=str(source)
    )
    client = CommentaryClient()

    def rpc(*args):
        raise BackendCallCancelled("private request text", reason="superseded")

    try:
        result = client.generate(cast_observation(), config, rpc, threading.Event())
        assert not result["utterances"] and result["status"]["reason"] == "superseded"
        assert "private request text" not in str(result)
    finally:
        client.close()


def test_real_parser_objects_keep_public_milled_card_names_and_types():
    from arenamcp.gamestate import GameObject

    g = game()
    obj = GameObject(
        instance_id=100,
        grp_id=10,
        zone_id=1,
        owner_seat_id=2,
        controller_seat_id=2,
        visibility="Visibility_Public",
        card_types=["Creature"],
    )
    assert "name" not in obj.to_dict()
    g.game_objects[100] = obj
    journal_event(
        g,
        {
            "type": "zone_transfer",
            "category": "Mill",
            "instance_id": 100,
            "card": "Known creature",
            "from_zone": 0,
            "to_zone": 1,
        },
    )
    lines = deterministic(observation(g), CommentaryConfig(mode="commentary", speakers=2))
    assert len(lines) == 2 and "Known creature" in lines[1].text
    assert g._commentary_journal[0]["payload"]["cards"][0]["type_line"] == "Creature"


def test_coalescing_does_not_mutate_already_published_record():
    g = game()
    add(g, 100, "First")
    published = list(g._commentary_journal)
    add(g, 101, "Second")
    assert published[0]["payload"]["count"] == 1
    assert g._commentary_journal[0]["payload"]["count"] == 2


def test_fallback_pending_spell_analysis_expires_with_snapshot():
    from test_headless_booth import observation as cast_observation

    obs = cast_observation()
    event = replace(obs.events[0], payload={**obs.events[0].payload, "name": "Counterspell"})
    card = {"grp_id": 1, "instance_id": 10, "name": "Counterspell", "oracle_text": "Counter target spell."}
    obs = replace(obs, events=(event,), public_state={**obs.public_state, "zones": {"stack": [card]}})
    lines = deterministic(obs, CommentaryConfig(mode="commentary", speakers=2))
    assert len(lines) == 2 and "threatens" in lines[1].text
    assert valid_for(lines[0], replace(obs, snapshot_id="new-step"))
    assert not valid_for(lines[1], replace(obs, snapshot_id="new-step"))
    resolved = replace(obs, public_state={**obs.public_state, "zones": {"graveyard": [card]}})
    assert len(deterministic(resolved, CommentaryConfig(mode="commentary", speakers=2))) == 1


def test_parser_zone_transfer_annotations_feed_one_outcome_per_public_burst():
    from arenamcp.gamestate import GameObject, GameState, Zone, ZoneType

    g = GameState()
    g.match_id, g.game_number = "synthetic", 1
    g._resolve_card_name = lambda gid: f"Known card {gid}"
    g.zones = {1: Zone(1, ZoneType.LIBRARY), 2: Zone(2, ZoneType.GRAVEYARD)}
    g.game_objects = {
        i: GameObject(
            instance_id=i,
            grp_id=i,
            zone_id=2,
            owner_seat_id=1,
            controller_seat_id=1,
            visibility="Visibility_Public",
            card_types=["Creature"],
        )
        for i in range(100, 110)
    }
    g._process_annotations(
        [
            {
                "type": ["AnnotationType_ZoneTransfer"],
                "affectorId": 999,
                "affectedIds": list(g.game_objects),
                "details": [
                    {"key": "zone_src", "valueInt32": [1]},
                    {"key": "zone_dest", "valueInt32": [2]},
                    {"key": "category", "valueString": ["Mill"]},
                ],
            }
        ]
    )
    assert len(g._commentary_journal) == 1
    payload = g._commentary_journal[0]["payload"]
    assert payload["count"] == 10 and payload["source_id"] == 999
    assert len(payload["cards"]) == 10 and payload["cards"][0]["name"] == "Known card 100"
