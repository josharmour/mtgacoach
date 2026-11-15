import json
from dataclasses import replace

from test_controller import controller as booth_controller  # noqa: F401
from test_controller import line

from arenamcp import speech_journal
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.match_bundle import _bug_report_entry


def test_report_includes_words_for_played_and_dropped_lines(booth_controller, monkeypatch, tmp_path):  # noqa: F811
    control, tts, _, _ = booth_controller
    monkeypatch.setattr(speech_journal, "LOG_FILE", tmp_path / "coach.log")
    control.config = replace(control.config, private_information=True)
    first = line(control)
    control.proposal(first.to_wire(), public_only=False)
    tts.speechStatus.emit(first.utterance_id, "started")
    tts.speechStatus.emit(first.utterance_id, "finished")
    rejected = replace(
        first, snapshot_id="obsolete", utterance_id="obsolete", text="A missed point.", event_bound=False
    )
    control.proposal(rejected.to_wire(), public_only=False)
    rows = speech_journal.for_game({"match_id": first.epoch.match_id, "game_number": first.epoch.game_number})
    assert [r["stage"] for r in rows if r.get("uid") == first.utterance_id] == [
        "proposed",
        "accepted",
        "render_requested",
        "started",
        "finished",
    ]
    assert next(r for r in rows if r["stage"] == "render_requested")["text"] == first.text
    assert rows[-1]["stage"] == "superseded" and rows[-1]["reason"] == "expired_or_changed"
    assert any(r.get("text") == rejected.text for r in rows)
    assert not speech_journal.for_game({"match_id": "other", "game_number": 1})
    report = tmp_path / "bug_game.json"
    report.write_text(json.dumps({"speech_transcript": rows}))
    assert "speech_transcript" not in _bug_report_entry(report)["report"]


def test_visible_library_and_revealed_opposing_hand_follow_the_current_snapshot():
    adapter = ObservationAdapter()
    row = {"name": "Known", "grp_id": 100, "owner_seat_id": 1}
    snap = {
        "match_id": "m",
        "game_number": 1,
        "local_seat_id": 1,
        "zones": {"library_count": 32},
        "library_visible": [row, dict(row, owner_seat_id=2), dict(row, face_down=True)],
        "revealed_cards": [dict(row, owner_seat_id=2, zone="hand"), dict(row, zone="graveyard")],
    }
    view = adapter.project(snap, True)
    assert view.private_state["library"]["visible_cards"] == [{"name": "Known", "grp_id": 100}]
    assert view.private_state["library"]["size"] == 32
    assert len(view.private_state["revealed_opposing_hand"]["cards"]) == 1
    assert "revealed_opposing_hand" not in adapter.project(dict(snap, revealed_cards=[]), True).private_state
    assert not adapter.project(snap, False).private_state


def test_queued_observation_uses_arrival_time_and_omits_unconfirmed_reveals():
    adapter = ObservationAdapter(clock=lambda: 20)
    frame = {
        "match_id": "game",
        "game_number": 1,
        "local_seat_id": 1,
        "turn_info": {"turn_number": 2},
        "players": [{"seat_id": 1, "life_total": 20}],
        "zones": {},
    }
    adapter.observe(frame, True, arrived_at=10)
    update = dict(
        frame,
        players=[{"seat_id": 1, "life_total": 16}],
        _bridge_connected=True,
        _bridge_game_state_id=7,
        _log_game_state_id=6,
        library_visible=[{"name": "Old card", "grp_id": 123, "owner_seat_id": 1}],
        revealed_cards=[{"name": "Old card", "grp_id": 123, "zone": "hand"}],
    )
    obs = adapter.observe(update, True, arrived_at=12)
    assert next(e for e in obs.events if e.kind == "life_change").received_at == 12
    assert "library" not in obs.private_state and "revealed_opposing_hand" not in obs.private_state
