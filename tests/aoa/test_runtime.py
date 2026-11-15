"""Replay integration tests: no live game, audio, models or service requests."""

from __future__ import annotations

import copy
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from arenamcp.commentary.budget import BudgetExceeded, CommentaryBudget
from arenamcp.commentary.client import CommentaryClient
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.contracts import Utterance
from arenamcp.commentary.events import ObservationAdapter, journal_event
from arenamcp.commentary.headless import deterministic
from arenamcp.commentary.service import CommentaryService

FIXTURE = Path(__file__).parent / "fixtures" / "public_snapshot.json"


def snapshot():
    return json.loads(FIXTURE.read_text())


def test_settings_are_opt_in_bounded_and_private_consent_is_boolean():
    assert not CommentaryConfig.from_settings({}).enabled
    c = CommentaryConfig.from_wire(
        {"mode": "commentary", "speakers": 9, "exchanges_per_minute": 99, "private_information": "true"}
    )
    assert c.enabled and c.speakers == 2 and c.exchanges_per_minute == 4
    assert not c.private_information
    assert not replace(c, muted=True).enabled
    assert not replace(c, output_owner="standalone").enabled


def test_budget_has_no_backlog_two_calls_no_repairs_and_charges_cancellation():
    now = [0.0]
    b = CommentaryBudget(lambda: now[0])
    exchange = b.begin(4)
    with pytest.raises(BudgetExceeded):
        b.begin(4)
    assert b.before_call(exchange, "system", "facts", audit=False) == (512, 6)
    now[0] = 2
    b.after_call(exchange)
    assert b.before_call(exchange, "audit", "line", audit=True) == (1024, 4)
    now[0] = 5
    b.finish(exchange)  # cancelled attempts are charged too
    assert exchange.request_seconds == 5 and exchange.output_tokens == 1536
    with pytest.raises(BudgetExceeded):
        b.before_call(exchange, "repair", "facts", audit=False)
    second = b.begin(4)
    b.before_call(second, "", "", audit=False)
    now[0] += 2
    b.finish(second)
    with pytest.raises(BudgetExceeded, match="request_time_budget"):
        b.begin(4)
    now[0] = 70
    assert b.begin(4)


@pytest.mark.parametrize(
    "audit,system,user,reason",
    [(True, "", "", "call_count"), (False, "x" * 6000, "", "context"), (False, "", "🐻" * 1500, "context")],
)
def test_budget_rejects_bad_order_and_dense_unicode(audit, system, user, reason):
    b = CommentaryBudget(lambda: 1)
    with pytest.raises(BudgetExceeded, match=reason):
        b.before_call(b.begin(), system, user, audit=audit)


def test_rate_and_total_deadline_are_enforced():
    now = [0]
    b = CommentaryBudget(lambda: now[0])
    for _ in range(2):
        b.finish(b.begin())
    with pytest.raises(BudgetExceeded):
        b.begin()
    now[0] = 61
    item = b.begin()
    now[0] = 67
    with pytest.raises(BudgetExceeded, match="deadline"):
        b.before_call(item, "", "", audit=False)


def test_server_shape_private_opt_in_and_fingerprint_stability():
    state = snapshot()
    state["turn"] = state.pop("turn_info")
    state["zones"]["my_hand"][0].update(visibility="Visibility_Private", instance_id=45, grp_id=55)
    a = ObservationAdapter(lambda: 10)
    first, second = a.project(state), a.project(state)
    assert first.snapshot_id == second.snapshot_id
    assert first.public_state["turn_info"]["turn_number"] == 4
    assert "PRIVATE_HAND" not in json.dumps(first.to_wire())
    state["local_seat_id"] = 1
    opted = a.project(state, private=True)
    assert opted.private_state["hand"][0]["name"] == "PRIVATE_HAND"
    assert "PRIVATE_FACE" not in json.dumps(opted.to_wire())
    assert "PRIVATE_LIBRARY" not in json.dumps(opted.to_wire())
    state["players"][0]["life_total"] -= 1
    assert a.project(state).snapshot_id != first.snapshot_id


def test_transient_cast_and_resolution_keep_distinct_provenance():
    state = snapshot()
    state["commentary_source_sequence"] = 11
    common = dict(
        match_id="fixture-match",
        game_number=1,
        seat=1,
        seq=10,
        snapshot_id="source-10",
        received_at=9,
        payload={"name": "Bear", "to_zone": "battlefield"},
    )
    state["commentary_events"] = [
        {**common, "id": 1, "kind": "action_observed"},
        {**common, "id": 2, "kind": "spell_resolved"},
    ]
    a = ObservationAdapter(lambda: 10)
    obs = a.observe(state)
    assert [e.kind for e in obs.events] == ["action_observed", "spell_resolved", "game_start"]
    assert all(e.snapshot_id == "source-10" for e in obs.events[:2])
    assert not a.observe(state).events
    state["zones"]["stack"] = []
    assert not a.observe(state).events  # disappearance alone never invents resolution
    state["game_number"] = 2
    assert [e.kind for e in a.observe(state).events] == ["game_start"]


def test_journal_is_bounded_public_and_preserves_destination():
    obj = SimpleNamespace(
        instance_id=3,
        grp_id=999,
        controller_seat_id=1,
        owner_seat_id=1,
        to_dict=lambda: {"instance_id": 3, "face_down": True, "name": "SECRET", "grp_id": 999},
    )
    game = SimpleNamespace(
        match_id="m",
        game_number=1,
        game_objects={3: obj},
        zones={4: SimpleNamespace(zone_type=SimpleNamespace(value="Battlefield"))},
        _commentary_journal=[],
        _commentary_event_number=0,
        _raw_gre_sequence=8,
    )
    for _ in range(140):
        journal_event(
            game,
            {
                "type": "zone_transfer",
                "category": "Resolve",
                "instance_id": 3,
                "card": "SECRET",
                "to_zone": 4,
            },
        )
    assert len(game._commentary_journal) == 128
    assert "SECRET" not in json.dumps(game._commentary_journal)
    assert game._commentary_journal[-1]["payload"]["to_zone"] == "battlefield"


def test_replay_boundaries_combat_life_and_end_have_evidence():
    a = ObservationAdapter(lambda: 20)
    state = snapshot()
    a.observe(state)
    state["players"][0]["life_total"] = 8
    state["turn_info"]["turn_number"] = 5
    state["zones"]["battlefield"][0].update(is_attacking=True, controller_seat_id=1)
    obs = a.observe(state)
    assert {e.kind for e in obs.events} == {"turn_start", "life_change", "combat"}
    lines = deterministic(obs, CommentaryConfig(mode="commentary"))
    assert "attacks with 1" in lines[0].text
    assert lines[0].expires_at == 50 and lines[0].event_bound and lines[0].evidence_ids
    state["last_game_result"] = "win"
    assert a.observe(state).events[-1].kind == "game_end"


def test_real_json_worker_deterministic_lifecycle_without_aoa_or_models():
    config = CommentaryConfig(mode="commentary", python=sys.executable)
    client = CommentaryClient()
    obs = ObservationAdapter().observe(snapshot())
    try:
        result = client.generate(
            obs, config, lambda *a: pytest.fail("No inference in deterministic mode"), threading.Event()
        )
        line = Utterance.from_wire(result["utterances"][0])
        assert line.epoch == obs.epoch and line.snapshot_id == obs.snapshot_id
        client.delivered(line.utterance_id, "finished")
        process = client.process
    finally:
        client.close()
    assert process.poll() is not None


def test_proxy_lane_nonwaiting_and_strategy_displaces_commentary():
    from arenamcp.backends.proxy import _BackgroundLane, _CallHandle

    lane = _BackgroundLane()
    optional = _CallHandle("background.commentary")
    assert lane.acquire(optional, 10) == "acquired"
    second = _CallHandle("background.commentary")
    assert lane.acquire(second, 10) == "dropped"
    strategy = _CallHandle("background.strategy")
    lane.protect(strategy)
    assert optional.is_cancelled
    assert lane.acquire(strategy, 0) == "acquired"
    assert lane.acquire(second, 10) == "dropped"
    lane.release(strategy)
    lane.unprotect(strategy)
    assert lane.acquire(second, 0) == "acquired"


def test_service_off_or_globally_muted_never_starts_worker():
    for settings, muted in [({}, False), ({"autoplay_speech": "commentary"}, True)]:
        service = CommentaryService(
            settings=lambda settings=settings: settings,
            backend=lambda: None,
            emit=lambda e: None,
            muted=lambda muted=muted: muted,
        )
        service.offer(snapshot())
        assert service.thread is None and service.client.process is None
        service.close()


@pytest.mark.parametrize("change", ["none", "life", "mode", "mute"])
def test_service_revalidates_before_publishing_and_never_blocks_producer(change):
    settings = {"autoplay_speech": "commentary"}
    state = snapshot()
    muted = [False]
    done = threading.Event()
    events = []
    first_generation_events = []

    class Client:
        def start(self, config):
            pass

        def close(self):
            pass

        def delivered(self, *args):
            pass

        def generate(self, obs, config, rpc, cancelled):
            if change == "life":
                updated = copy.deepcopy(state)
                updated["players"][0]["life_total"] = 1
                service.offer(updated)
            elif change == "mode":
                settings["autoplay_speech"] = "essential"
                service.configure()
            elif change == "mute":
                muted[0] = True
            return {"utterances": [u.to_wire() for u in deterministic(obs, config)]}

    def emit(event):
        events.append(event)
        if event["type"] == "commentary_status" and not done.is_set():
            # Freeze this generation before waking the asserting thread.
            # A new life-change observation can legitimately produce fresh
            # narration immediately after the stale opening is rejected.
            first_generation_events.extend(events)
            done.set()

    service = CommentaryService(
        settings=lambda: settings,
        backend=lambda: None,
        emit=emit,
        client_factory=Client,
        muted=lambda: muted[0],
    )
    service.offer(state)
    try:
        assert done.wait(3)
        proposals = [e for e in first_generation_events if e["type"] == "commentary_proposal"]
        assert bool(proposals) == (change == "none")
    finally:
        service.close()
        service.thread.join(2)
    assert not service.thread.is_alive()


def test_actual_gamestate_publishes_journal_and_resets_it():
    from arenamcp.gamestate import GameState
    from arenamcp.gamestate_models import GameObject, Zone, ZoneType

    game = GameState()
    game.match_id = "journal-test"
    game.game_number = 1
    game.game_objects[10] = GameObject(
        instance_id=10, grp_id=11, zone_id=20, owner_seat_id=1, controller_seat_id=1
    )
    game.zones[20] = Zone(zone_id=20, zone_type=ZoneType.BATTLEFIELD, owner_seat_id=1)
    game._add_event(
        {"type": "zone_transfer", "category": "Resolve", "instance_id": 10, "card": "Bear", "to_zone": 20}
    )
    game.publish_snapshot()
    published = game.get_published_snapshot()
    assert published["commentary_events"][-1]["kind"] == "spell_resolved"
    assert published["commentary_events"][-1]["payload"]["to_zone"] == "battlefield"
    game.reset()
    assert not game._commentary_journal


@pytest.mark.parametrize("mode", ["commentary", "essential"])
def test_routine_submission_muted_at_source_but_ui_explanation_remains(mode):
    from tests.fakes import FakeBridge, make_engine

    from arenamcp.action_planner import ActionType, GameAction

    engine = make_engine(FakeBridge())
    engine._config.autoplay_speech = mode
    spoken = []
    notices = []
    engine._speak_fn = lambda *args: spoken.append(args)
    engine._notify = lambda *args: notices.append(args)
    engine._announce_submitted_action(GameAction(action_type=ActionType.CAST_SPELL, card_name="Bear"), {})
    assert not spoken and notices and "Bear" in notices[-1][1]


@pytest.mark.parametrize("blocker", ["protected", "budget", "stale_analysis"])
def test_service_narrates_observed_cast_without_optional_analysis(blocker, monkeypatch):
    import time

    from arenamcp.backends.proxy import _BACKGROUND_LANE, _CallHandle

    settings = {"autoplay_speech": "commentary", "commentary_generative": True}
    events, ready = [], threading.Event()
    state = snapshot()
    state["commentary_events"] = [
        {
            "match_id": "fixture-match",
            "game_number": 1,
            "id": 1,
            "seq": 1,
            "snapshot_id": "cast",
            "received_at": time.monotonic(),
            "kind": "action_observed",
            "seat": 1,
            "payload": {"name": "Bear"},
        }
    ]

    class Client:
        def start(self, config):
            assert blocker != "protected"

        def close(self):
            pass

        def delivered(self, *args):
            pass

        def generate(self, observation, config, rpc, cancelled):
            assert blocker == "stale_analysis"
            changed = copy.deepcopy(state)
            changed["players"][0]["life_total"] -= 1
            service.latest = changed
            return {"utterances": []}

    def emit(event):
        events.append(event)
        if event["type"] == "commentary_proposal":
            ready.set()

    service = CommentaryService(
        settings=lambda: settings, backend=lambda: None, emit=emit, client_factory=Client
    )
    handle = _CallHandle("decision.typed")
    if blocker == "protected":
        _BACKGROUND_LANE.protect(handle)
    if blocker == "budget":
        monkeypatch.setattr(
            service.budget,
            "begin",
            lambda *args: (_ for _ in ()).throw(BudgetExceeded("exchange_rate_or_busy")),
        )
    try:
        service.offer(state)
        assert ready.wait(3)
        proposal = next(e["utterance"] for e in events if e["type"] == "commentary_proposal")
        assert proposal["text"] == "A player casts Bear."
        assert proposal["event_bound"] is True and proposal["evidence_ids"]
    finally:
        _BACKGROUND_LANE.unprotect(handle)
        service.close()
        service.thread.join(2)


@pytest.mark.parametrize("change", ["mute", "mode", "game", "expiry"])
def test_grounded_fallback_keeps_safety_cancellation(change, monkeypatch):
    import time

    settings = {"autoplay_speech": "commentary"}
    events = []
    service = CommentaryService(settings=lambda: settings, backend=lambda: None, emit=events.append)
    state = snapshot()
    service.latest = state
    obs = service.adapter.observe(state)
    if change == "mute":
        service.muted = lambda: True
    elif change == "mode":
        settings["autoplay_speech"] = "actions"
        service.configure()
    elif change == "game":
        service.latest = dict(state, game_number=2)
    else:
        obs = replace(
            obs,
            arrived_at=time.monotonic() - 30,
            events=tuple(replace(e, received_at=time.monotonic() - 30) for e in obs.events),
        )
    service._grounded(obs, CommentaryConfig(mode="commentary"), "test")
    assert not any(e["type"] == "commentary_proposal" for e in events)


def test_grounded_calls_have_no_unbounded_backlog_or_inference_budget_cost():
    import time

    events = []
    service = CommentaryService(
        settings=lambda: {"autoplay_speech": "commentary"}, backend=lambda: None, emit=events.append
    )
    service.latest = snapshot()
    obs = service.adapter.observe(service.latest)
    service.last_grounded = (obs.epoch, time.monotonic())
    service._grounded(obs, service.config, "busy")
    assert not any(e["type"] == "commentary_proposal" for e in events)
    assert events[-1]["data"]["reason"] == "grounded_call_spacing"
    service.last_grounded = (None, -float("inf"))
    service._grounded(replace(obs, events=()), service.config, "busy")
    assert events[-1]["data"]["reason"] == "no_eligible_public_event"


def test_pipe_speech_is_forwarded_once_and_never_implicitly_urgent():
    from unittest.mock import Mock

    from arenamcp.pipe_adapter import PipeAdapter

    ui = object.__new__(PipeAdapter)
    ui._emit = Mock()
    voice = Mock()
    ui._coach = SimpleNamespace(_voice_output=voice)
    ui.speak("Play Forest.")
    voice.speak.assert_called_once_with("Play Forest.", blocking=False)
    ui._emit.assert_not_called()
    ui._coach = None
    ui.speak("Play Forest.")
    assert ui._emit.call_args.args[0]["speech_category"] == "workflow"


def test_larger_audit_reservation_preserves_minute_output_ceiling_and_deadline():
    now = [0.0]
    budget = CommentaryBudget(lambda: now[0])
    for _ in range(2):
        exchange = budget.begin(4)
        assert budget.before_call(exchange, "", "", audit=False)[0] == 512
        budget.after_call(exchange)
        assert budget.before_call(exchange, "", "", audit=True)[0] == 1024
        assert exchange.deadline == now[0] + 6
        budget.finish(exchange)
    assert sum(item.output_tokens for item in budget.history) == 3072
    third = budget.begin(4)
    with pytest.raises(BudgetExceeded, match="output_budget"):
        budget.before_call(third, "", "", audit=False)
    assert third.calls == 0
    budget.finish(third, usable=False)
    now[0] = 61
    fresh = budget.begin(4)
    budget.before_call(fresh, "", "", audit=False)
    budget.after_call(fresh)
    now[0] = 67
    with pytest.raises(BudgetExceeded, match="call_count_or_deadline"):
        budget.before_call(fresh, "", "", audit=True)


def test_both_calls_remain_charged_until_the_later_audit_expires():
    now = [0.0]
    budget = CommentaryBudget(lambda: now[0])
    for start in (0, 2):
        now[0] = start
        exchange = budget.begin(4)
        budget.before_call(exchange, "", "", audit=False)
        budget.after_call(exchange)
        now[0] += 1
        budget.before_call(exchange, "", "", audit=True)
        budget.finish(exchange)
    now[0] = 60.1
    early = budget.begin(4)
    with pytest.raises(BudgetExceeded, match="output_budget"):
        budget.before_call(early, "", "", audit=False)
    budget.finish(early, usable=False)
    now[0] = 61
    fresh = budget.begin(4)
    assert budget.before_call(fresh, "", "", audit=False)[0] == 512


def test_generation_requires_room_for_audit_after_a_partial_exchange():
    now = [0.0]
    budget = CommentaryBudget(lambda: now[0])
    partial = budget.begin(4)
    budget.before_call(partial, "", "", audit=False)
    budget.finish(partial, usable=False)
    now[0] = 5
    complete = budget.begin(4)
    budget.before_call(complete, "", "", audit=False)
    budget.after_call(complete)
    budget.before_call(complete, "", "", audit=True)
    budget.finish(complete)
    third = budget.begin(4)
    with pytest.raises(BudgetExceeded, match="output_budget"):
        budget.before_call(third, "", "", audit=False)
    assert third.calls == third.output_tokens == 0
    budget.finish(third, usable=False)
    assert sum(item.output_tokens for item in budget.history) == 2048
    # The failed attempt ages out. Exactly one more complete exchange fits.
    now[0] = 60
    fresh = budget.begin(4)
    budget.before_call(fresh, "", "", audit=False)
    budget.after_call(fresh)
    budget.before_call(fresh, "", "", audit=True)
    assert sum(item.output_tokens for item in budget.history) == 3072


def test_broker_distinguishes_missing_final_content_without_repair():
    from arenamcp.backends.proxy import ProxyBackend
    from arenamcp.commentary.budget import broker_failure_code

    class EmptyFinal(ProxyBackend):
        def __init__(self):
            self.calls = []

        def complete(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return ""

    backend = EmptyFinal()
    service = CommentaryService(
        settings=lambda: {}, backend=lambda: backend, emit=lambda _: None, client_factory=lambda: None
    )
    exchange = service.budget.begin()
    service.budget.before_call(exchange, "generation", "facts", audit=False)
    service.budget.after_call(exchange)
    with pytest.raises(BudgetExceeded, match="incomplete_final_response") as caught:
        service._backend_request(exchange, "audit", {"lines": []}, True)
    assert broker_failure_code(caught.value) == "incomplete_final_response"
    assert len(backend.calls) == 1
    assert backend.calls[0][0][2] == 1024
    assert backend.calls[0][1]["enable_thinking"] is False
    assert exchange.calls == 2 and exchange.call_started is None


def test_compact_heard_replies_reserve_three_complete_pairs_at_rate_four():
    budget = CommentaryBudget(lambda: 0)
    for _ in range(3):
        exchange = budget.begin(4)
        assert budget.before_call(exchange, "", "", audit=False, single_reply=True)[0] == 512
        budget.after_call(exchange)
        assert budget.before_call(exchange, "", "", audit=True, single_reply=True)[0] == 512
        budget.finish(exchange)
    assert sum(exchange.output_tokens for exchange in budget.history) == 3072
    fourth = budget.begin(4)
    with pytest.raises(BudgetExceeded, match="output_budget"):
        budget.before_call(fourth, "", "", audit=False, single_reply=True)
    assert fourth.calls == 0


def test_smaller_audit_cannot_accept_expanded_roles_or_refund_a_cancelled_call():
    budget = CommentaryBudget(lambda: 0)
    exchange = budget.begin(4)
    budget.before_call(exchange, "", "", audit=False, single_reply=True)
    budget.after_call(exchange)
    with pytest.raises(BudgetExceeded, match="audit_shape_changed"):
        budget.before_call(exchange, "", "", audit=True)
    budget.finish(exchange, usable=False)
    assert exchange.calls == 1 and exchange.output_tokens == 512
    normal = budget.begin(4)
    budget.before_call(normal, "", "", audit=False)
    budget.after_call(normal)
    # A generic generation originally had room for two verdicts. Its broader
    # audit retains full reasoning headroom even if only one analyst line survived.
    assert budget.before_call(normal, "", "", audit=True, single_reply=True)[0] == 1024
    budget.finish(normal)
    compact = budget.begin(4)
    assert budget.before_call(compact, "", "", audit=False, single_reply=True)[0] == 512
    budget.after_call(compact)
    assert budget.before_call(compact, "", "", audit=True, single_reply=True)[0] == 512
    budget.finish(compact, usable=False)
    assert sum(item.output_tokens for item in budget.history) == 3072


@pytest.mark.parametrize(
    "heard,roles,lead_role,audit_roles,expected_limit",
    [
        (False, ["color_analyst"], "play_by_play", ["play_by_play", "color_analyst"], 1024),
        ("yes", ["color_analyst"], "play_by_play", ["color_analyst"], 1024),
        (True, ["play_by_play", "color_analyst"], "play_by_play", ["color_analyst"], 1024),
        (True, ["color_analyst"], "color_analyst", ["color_analyst"], 1024),
        (True, ["color_analyst"], "play_by_play", ["color_analyst"], 512),
        (True, ["color_analyst"], "play_by_play", ["play_by_play", "color_analyst"], None),
        (True, ["color_analyst"], "play_by_play", ["play_by_play"], None),
    ],
)
def test_broker_binds_audit_allowance_to_original_heard_reply(
    heard, roles, lead_role, audit_roles, expected_limit, caplog
):
    from unittest.mock import Mock

    from arenamcp.backends.proxy import ProxyBackend

    backend = Mock(spec=ProxyBackend)
    backend.complete.return_value = "{}"
    service = CommentaryService(settings=lambda: {}, backend=lambda: backend, emit=lambda _: None)
    exchange = service.budget.begin()
    generation = {
        "allowed_roles": roles,
        "partner_call_heard": heard,
        "partner_call": {"role": lead_role, "refs": ["event:1"], "text": "An attacker was declared."},
    }
    service._backend_request(exchange, "author", generation, False)
    lines = {"lines": [{"role": r} for r in audit_roles]}
    if expected_limit is None:
        with pytest.raises(BudgetExceeded, match="audit_shape_changed"):
            service._backend_request(exchange, "audit", lines, True)
        assert backend.complete.call_count == 1 and exchange.output_tokens == 512
    else:
        service._backend_request(exchange, "audit", lines, True)
        assert backend.complete.call_args.args[2] == expected_limit
        schema = backend.complete.call_args.kwargs["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["checks"]["maxItems"] == (1 if expected_limit == 512 else 2)
        assert exchange.output_tokens == 512 + expected_limit
    receipts = [m for m in caplog.messages if "single analyst audit" in m]
    assert receipts == (
        ["Commentary single analyst audit requested max_tokens=512"] if expected_limit == 512 else []
    )
