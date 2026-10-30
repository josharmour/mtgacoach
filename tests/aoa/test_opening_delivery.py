"""Real speech arbitration with deterministic callbacks; no audio or live game."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_narration_context import snapshot

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.contracts import Evidence, Utterance
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.headless import deterministic
from arenamcp.commentary.rationale import attach_rationales, purpose, remember_explanation
from arenamcp.commentary.service import CommentaryService
from arenamcp.commentary.status import status_label
from arenamcp.commentary.timeline import valid_for
from arenamcp.desktop.speech_arbiter import SpeechArbiter


def setup(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("arenamcp.commentary.service.time.monotonic", lambda: now[0])
    state = snapshot()
    played, events = [], []
    settings = {"autoplay_speech": "commentary", "commentary_private_information": True}
    service = CommentaryService(settings=lambda: settings, backend=lambda: None, emit=lambda e: None)
    service.latest = state
    service.adapter = ObservationAdapter(lambda: now[0])
    obs = service.adapter.observe(state, True)
    service.pending_events = {e.event_id: e for e in obs.events}
    arbiter = SpeechArbiter(
        start=played.append,
        stop=lambda: None,
        clock=lambda: now[0],
        status=lambda uid, s: service.delivery(uid, s, arbiter.reasons[uid]),
        valid=lambda u: valid_for(u, service.adapter.project(service.latest, True)),
    )
    arbiter.reset(obs.epoch)

    def emit(event):
        events.append(event)
        if event["type"] == "commentary_proposal":
            arbiter.submit(Utterance.from_wire(event["utterance"]))

    service.emit = emit
    return service, arbiter, obs, now, played, events


def finish(arbiter):
    item = arbiter.active
    assert item is not None
    arbiter.playback_status(item.utterance_id, "started", epoch=item.epoch)
    arbiter.playback_status(item.utterance_id, "finished", epoch=item.epoch)


def test_interrupted_opening_retries_then_both_speakers_finish(monkeypatch):
    s, a, obs, now, played, events = setup(monkeypatch)
    opening = next(e for e in obs.events if e.kind == "deck_overview")
    s._fallback(obs, s.config, "opening_deck_overview")
    a.clear("engine_stop")  # Exact report sequence: both stopped before completion.
    assert not s.heard and opening.event_id in s.pending_events
    assert s.last_grounded[0] is None
    now[0] += 2.1
    s.latest = copy.deepcopy(s.latest)
    s.latest["turn_info"].update(turn_number=2, phase="Combat")
    s._fallback(obs, s.config, "opening_deck_overview")
    assert a.active.utterance_id.endswith(":retry2")
    finish(a)
    assert a.active.role == "color_analyst"
    finish(a)
    assert len(s.heard) == 2 and a.active is None
    now[0] += 3
    s._fallback(obs, s.config, "opening_deck_overview")
    assert len([e for e in events if e["type"] == "commentary_proposal"]) == 4


def test_only_interrupted_second_role_is_retried(monkeypatch):
    s, a, obs, now, played, events = setup(monkeypatch)
    s._fallback(obs, s.config, "opening_deck_overview")
    finish(a)
    a.clear("speed_changed")
    now[0] += 3
    s._fallback(obs, s.config, "opening_deck_overview")
    assert a.active.role == "color_analyst" and a.active.anchor_id is None
    finish(a)
    assert [p.role for p in played] == ["play_by_play", "color_analyst", "color_analyst"]
    assert len(s.heard) == 2


@pytest.mark.parametrize("reason", ["user_stop", "mode_changed", "shutdown", "epoch_changed", "muted"])
def test_opening_does_not_retry_explicit_stop_or_disabled_output(monkeypatch, reason):
    s, a, obs, now, played, events = setup(monkeypatch)
    s._fallback(obs, s.config, "opening_deck_overview")
    a.set_muted(True) if reason == "muted" else a.clear(reason)
    now[0] += 3
    s._fallback(obs, s.config, "opening_deck_overview")
    assert len(played) == 1


def test_stop_before_opening_arrives_suppresses_that_game(monkeypatch):
    s, a, obs, now, played, events = setup(monkeypatch)
    s.suppress_opening("user_stop")
    s._fallback(obs, s.config, "opening_deck_overview")
    assert not played


def test_retry_is_bounded_by_attempts_and_original_event_age(monkeypatch):
    s, a, obs, now, played, events = setup(monkeypatch)
    for _ in range(6):
        s._fallback(obs, s.config, "opening_deck_overview")
        a.clear("engine_stop")
        now[0] += 3
    assert len(played) == 3
    s.openings.clear()
    now[0] = 191
    s._fallback(replace(obs, arrived_at=191), s.config, "opening_deck_overview")
    assert len(played) == 3


def rationale_state(monkeypatch, explanation):
    now = 100.0
    monkeypatch.setattr("arenamcp.commentary.rationale.time.monotonic", lambda: now)
    state = snapshot()
    state.pop("commentary_deck_overview")
    state["commentary_source_sequence"] = 5
    option = SimpleNamespace(meta={"actionType": "ActionType_Cast", "instanceId": 10, "grpId": 100})
    decision = SimpleNamespace(request_type="ActionsAvailable", find=lambda _: option)
    engine = SimpleNamespace()
    remember_explanation(engine, decision, ["idx:1"], explanation, state)
    state["commentary_action_explanations"] = engine._commentary_explanations
    obs = ObservationAdapter(lambda: now + 1).project(state, True)
    event = Evidence(
        "cast",
        "action_observed",
        6,
        "observed",
        seat=1,
        received_at=101,
        turn_number=1,
        payload={"instance_id": 10, "grp_id": 100, "name": "Known spell"},
    )
    return state, obs, event, engine, decision


@pytest.mark.parametrize(
    "explanation,expected",
    [
        (
            "Casting Boltwave. Boltwave pumps Slickshot to 3/2 and chips opponent to 17, advancing the prowess race while keeping Burst Lightning as reach.",
            "build up a threat",
        ),
        (
            "Casting Giant Cindermaw. Cindermaw gives a 4/3 trampler to block the 4/1 Saboteur and its no-lifegain static.",
            "strengthen the defense and deny lifegain",
        ),
    ],
)
def test_displayed_rationale_becomes_retrospective_second_speaker(monkeypatch, explanation, expected):
    state, obs, event, *_ = rationale_state(monkeypatch, explanation)
    attached = attach_rationales([event], state, True, 101)
    lines = deterministic(
        replace(obs, events=attached), CommentaryConfig(mode="commentary", private_information=True)
    )
    assert len(lines) == 2 and expected in lines[1].text
    assert "17" not in lines[1].text and "3/2" not in lines[1].text and "Burst Lightning" not in lines[1].text
    changed = copy.deepcopy(state)
    changed["turn_info"].update(turn_number=2, phase="Combat")
    changed["zones"]["my_hand"] = []
    fresh = ObservationAdapter(lambda: 102).project(changed, True)
    played, delivered = [], []
    a = SpeechArbiter(
        start=played.append,
        stop=lambda: None,
        status=lambda u, s: delivered.append((u, s)),
        clock=lambda: 102,
        valid=lambda u: valid_for(u, fresh),
    )
    a.reset(obs.epoch)
    for line in lines:
        a.submit(line)
    finish(a)
    assert a.active.role == "color_analyst"
    finish(a)
    assert len([s for _, s in delivered if s == "finished"]) == 2


@pytest.mark.parametrize(
    "change",
    [
        "private_off",
        "opponent",
        "wrong_game",
        "wrong_turn",
        "old",
        "future",
        "ambiguous",
        "old_sequence",
        "wrong_card",
    ],
)
def test_rationale_cannot_cross_identity_privacy_or_time_boundaries(monkeypatch, change):
    state, obs, event, engine, decision = rationale_state(
        monkeypatch, "Casting Known spell. Develop mana for bigger plays."
    )
    record = dict(state["commentary_action_explanations"][0])
    if change == "opponent":
        event = replace(event, seat=2)
    if change == "wrong_game":
        record["game_number"] = 2
    if change == "wrong_turn":
        record["turn"] = 2
    if change == "old":
        record["received_at"] = 20
    if change == "future":
        record["received_at"] = 110
    if change == "old_sequence":
        record["sequence"] = 6
    if change == "wrong_card":
        record["grp_id"] = 999
    state["commentary_action_explanations"] = [record] * (2 if change == "ambiguous" else 1)
    attached = attach_rationales([event], state, change != "private_off", 101)
    assert "coach_intent" not in attached[0].payload


def test_pass_and_planned_action_never_become_observed_rationale(monkeypatch):
    state, obs, event, engine, decision = rationale_state(monkeypatch, "Casting Known spell.")
    assert not purpose("Casting Known spell.")
    decision.find = lambda _: SimpleNamespace(meta={"actionType": "ActionType_Pass"})
    before = engine._commentary_explanations
    remember_explanation(engine, decision, ["pass"], "Passing to protect the board.", state)
    assert engine._commentary_explanations == before
    assert not purpose("Casting Known spell. This is a mistake; do not use it to protect the threat.")


def test_current_board_analysis_survives_hand_and_phase_but_not_board_change():
    from test_outcomes import add, game, observation

    g = game()
    add(g, 100, "Big creature", kind="Destroy", source=2)
    obs = observation(g)
    second = deterministic(obs, CommentaryConfig(mode="commentary"))[1]
    changed = replace(
        obs,
        snapshot_id="next",
        private_state={"hand": ["new"]},
        public_state={**obs.public_state, "turn_info": {"phase": "End"}},
    )
    assert valid_for(second, changed)
    changed = replace(
        changed, public_state={**changed.public_state, "zones": {"battlefield": [{"name": "new creature"}]}}
    )
    assert not valid_for(second, changed)


def test_waiting_labels_identify_local_cause_without_server_claims():
    assert "pause between" in status_label({"state": "waiting_capacity", "reason": "grounded_call_spacing"})
    assert "decision has priority" in status_label(
        {"state": "grounded", "reason": "analysis_waits_for_gameplay"}
    )
    assert "request limit" in status_label({"state": "analysis_deferred", "reason": "exchange_rate_or_busy"})
    assert "capacity" not in status_label({"state": "grounded"})


def test_intent_names_only_observed_public_synergy_without_damage_predictions():
    card = {
        "name": "Slickshot Show-Off",
        "oracle_text": "Flying, haste. Whenever you cast a noncreature spell, Slickshot gets +2/+0 until end of turn.",
    }
    text = "Casting Boltwave. Boltwave pumps Slickshot to 3/2 and chips opponent to 17."
    line = purpose(text, [card])
    assert "Slickshot Show-Off" in line and "3/2" not in line and "17" not in line
    assert "Slickshot" not in purpose(text, [{**card, "face_down": True}])


def test_pipe_forwards_only_displayed_submission_cache_to_local_commentary():
    from unittest.mock import Mock

    from arenamcp.pipe_adapter import PipeAdapter

    adapter = object.__new__(PipeAdapter)
    cache = ({"explanation": "Casting a spell. Develop mana for bigger plays."},)
    adapter._coach = SimpleNamespace(_autopilot=SimpleNamespace(_commentary_explanations=cache))
    adapter._commentary = Mock()
    adapter._emit = Mock()
    state = snapshot()
    adapter.emit_game_state(state)
    offered = adapter._commentary.offer.call_args.args[0]
    assert offered["commentary_action_explanations"] == cache
    assert "commentary_action_explanations" not in state


def test_pending_current_prediction_still_cancels_before_second_voice():
    from test_headless_booth import observation as cast_observation

    obs = cast_observation()
    event = replace(obs.events[0], payload={**obs.events[0].payload, "name": "Counterspell"})
    card = {"grp_id": 1, "instance_id": 10, "name": "Counterspell", "oracle_text": "Counter target spell."}
    obs = replace(obs, events=(event,), public_state={**obs.public_state, "zones": {"stack": [card]}})
    lines = deterministic(obs, CommentaryConfig(mode="commentary"))
    current = [obs]
    a = SpeechArbiter(
        start=lambda u: None,
        stop=lambda: None,
        status=lambda *a: None,
        valid=lambda u: valid_for(u, current[0]),
    )
    a.reset(obs.epoch)
    for u in lines:
        a.submit(u)
    current[0] = replace(
        obs, snapshot_id="resolved", public_state={**obs.public_state, "zones": {"stack": []}}
    )
    finish(a)
    assert a.active is None and a._states[lines[1].utterance_id] == "superseded"


def test_settings_echo_does_not_interrupt_a_running_introduction(qapp, monkeypatch):
    from test_controller import Tts

    from arenamcp.desktop import commentary_controller

    settings = {
        "tts_provider": "kokoro",
        "autoplay_speech": "commentary",
        "commentary_private_information": True,
    }
    monkeypatch.setattr(commentary_controller, "get_settings", lambda: settings)
    tts, sent = Tts(), []
    c = commentary_controller.CommentaryController(tts, sent.append, lambda text: None)
    try:
        c.observe(snapshot())
        obs = c.adapter.observe(snapshot(), True)
        lines = deterministic(obs, c.config)
        for line in lines:
            c.proposal(line.to_wire())
        c.configure(c.config.to_wire())
        assert c.observation is not None and not tts.stop_speech.called
        for line in lines:
            tts.speechStatus.emit(line.utterance_id, "started")
            tts.speechStatus.emit(line.utterance_id, "finished")
        assert sum(e.get("state") == "finished" for e in sent) == 2
    finally:
        c.close()
