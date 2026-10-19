"""A coach restart never re-records a finished match or overwrites its bundle (#29, #33).

2026-10-19 (standalone.log): match 92c09c7a ended 09:34:55 and its 100 KB
bundle was saved and uploaded. Arena's MatchCompleted then re-bound the
finished match id to the reset game state, and the next checkpoint save
rewrote ~/.arenamcp/last_match.json from "ended" back to "active" (turn 0).
The coach restarted at 09:53:35, resumed that match ("resume_same_session"),
started a packet for it, and at the next match boundary (09:54:09) saved and
uploaded a 0-decision, result=unknown 4 KB bundle over the real one. The
10:06:23 restart did the same to 6175f002 (137 KB -> 3 KB at 10:07:00, on the
boundary 6175f002 -> 0f6e8634). The finalized-match set lived in memory only.

#33: the same file was written in place while the parser thread read it:
"Failed to mark match ended: Expecting value: line 1 column 1 (char 0)"
(10:11:10), "Extra data: line 66 column 2" (2026-10-18 21:13:05).
"""

from __future__ import annotations

import gzip
import json
import secrets
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_game_end_recording import (
    THEIR_TEAM,
    arena,  # noqa: F401  (fixture)
    bundles,  # noqa: F401  (fixture)
    history,
    loop_coach,
    packets,
    result_row,
    script,
)
from tests.test_standalone_coaching_loop import run_loop

import arenamcp.server as server
from arenamcp import gamestate, match_bundle, match_packets
from arenamcp.gamestate import GameState
from arenamcp.gamestate_persistence import load_match_state, mark_match_ended, save_match_state

# --- last_match.json: an ended match stays ended (#29) and writes are atomic (#33)


def _live_game(match_id: str, turn: int, game_number: int | None = 1) -> GameState:
    gs = GameState()
    gs.match_id = match_id
    gs.game_number = game_number
    gs.turn_info.turn_number = turn
    return gs


def test_a_finished_match_re_bound_after_its_reset_is_not_saved_as_active_again():
    """09:34:55: Marked ended -> reset -> MatchCompleted re-binds the id -> checkpoint save."""
    save_match_state(_live_game("92c09c7a", 11))
    assert load_match_state()["match_id"] == "92c09c7a"
    mark_match_ended()
    assert load_match_state() is None

    # The reset game state with the finished id (turn 0, no result, no game number).
    save_match_state(_live_game("92c09c7a", 0, game_number=None))
    assert load_match_state() is None, "the restart at 09:53:35 resumed this finished match"
    saved = json.loads(gamestate.MATCH_STATE_PATH.read_text(encoding="utf-8"))
    assert saved["status"] == "ended" and saved["ended_game_number"] == 1 and "ended_at" in saved

    # The next match is live and resumable.
    save_match_state(_live_game("f1a888a1", 1))
    assert load_match_state()["match_id"] == "f1a888a1"


def test_a_save_carrying_the_game_result_keeps_the_match_end_mark():
    save_match_state(_live_game("92c09c7a", 11))
    mark_match_ended()
    with_result = _live_game("92c09c7a", 11)
    with_result.last_game_result = "win"  # saved "ended" by the result, before the loop takes it
    save_match_state(with_result)
    save_match_state(_live_game("92c09c7a", 0, game_number=None))
    assert load_match_state() is None


def test_a_bo3_game_result_alone_does_not_stop_the_sideboarding_resume():
    """Only mark_match_ended() sticks; a game-scope result gives way when the loop takes it."""
    game_one = _live_game("bo3", 9, game_number=1)
    game_one.last_game_result = "loss"
    save_match_state(game_one)
    save_match_state(_live_game("bo3", 0, game_number=None))
    assert load_match_state()["match_id"] == "bo3"


def test_a_later_game_of_a_match_ended_without_a_scope_resumes_again():
    """An IntermissionReq without a scope marks a Bo3 ended after game 1; game 2 must stay resumable."""
    save_match_state(_live_game("bo3", 9, game_number=1))
    mark_match_ended()
    save_match_state(_live_game("bo3", 2, game_number=2))
    assert load_match_state()["match_id"] == "bo3"


def test_match_state_writes_are_atomic_while_another_thread_reads(monkeypatch):
    path = gamestate.MATCH_STATE_PATH
    gs = _live_game("m-atomic", 3)
    # A big checkpoint, like the 100 KB live one, so a torn read is likely.
    monkeypatch.setattr(gs, "export_checkpoint", lambda: {"raw_gre_events": ["x" * 200] * 400})
    save_match_state(gs)
    errors: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except ValueError as error:
                errors.append(str(error))

    def marker() -> None:
        while not stop.is_set():
            mark_match_ended()

    threads = [threading.Thread(target=reader), threading.Thread(target=marker)]
    for thread in threads:
        thread.start()
    try:
        for turn in range(150):
            gs.turn_info.turn_number = turn
            save_match_state(gs)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
    assert errors == []
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


# --- finalized games survive the restart (#29)


def test_finalized_games_are_remembered_across_a_restart(monkeypatch):
    packet = match_packets.start_match_packet("92c09c7a")
    assert packet is not None
    packet.result = "win"
    assert packet.save() is not None
    match_packets.stop_match_packet()

    monkeypatch.setattr(match_packets, "_finalized_match_ids", set())  # a new coach process
    assert match_packets.is_match_finalized("92c09c7a")
    assert match_packets.start_match_packet("92c09c7a") is None
    assert match_packets.start_match_packet("92c09c7a-g2") is not None  # game 2 of a Bo3 is its own game
    assert json.loads(match_packets.FINALIZED_PATH.read_text(encoding="utf-8"))["match_ids"] == ["92c09c7a"]


def test_the_finalized_list_is_bounded(monkeypatch):
    monkeypatch.setattr(match_packets, "MAX_FINALIZED", 3)
    for n in range(5):
        match_packets.mark_match_finalized(f"m{n}")
    assert json.loads(match_packets.FINALIZED_PATH.read_text(encoding="utf-8"))["match_ids"] == [
        "m2",
        "m3",
        "m4",
    ]


@pytest.mark.parametrize("boundary_to", ["0f6e8634-next-match", None], ids=["next-match", "back-to-menu"])
def test_a_restarted_coach_never_rerecords_the_finished_match_at_the_next_boundary(
    monkeypatch,
    arena,  # noqa: F811
    bundles,  # noqa: F811
    boundary_to,
):
    """Both paths of the field report: the restored match id (09:53:41 "Started match packet
    recording") and the next match boundary (10:07:00 packet decisions=0 + 3 KB bundle)."""
    a, b = "6175f002-4914-4457", boundary_to
    first = loop_coach(arena)
    script(
        first,
        {
            1: lambda: (arena.start_match(a, "Rollo"), arena.play(a, 1, 3)),
            2: lambda: arena.play(a, 1, 15),
            3: lambda: (
                arena.end_game(a, 1, THEIR_TEAM),
                arena.complete_match(
                    a, "Rollo", [result_row(THEIR_TEAM), result_row(THEIR_TEAM, "MatchScope_Match")]
                ),
            ),
        },
    )
    run_loop(monkeypatch, first, iterations=4)
    assert bundles == [(a, "loss")]
    assert [p["result"] for p in packets(a)] == ["loss"]

    # The coach restarts (10:06:23): new process, the finished match restored at turn 0.
    monkeypatch.setattr(match_packets, "_finalized_match_ids", set())
    monkeypatch.setattr(match_packets, "_current_packet", None)
    monkeypatch.setattr(match_bundle, "_bundled_match_ids", set())
    arena.gs.reset()
    arena.gs.match_id = a
    second = loop_coach(arena)
    if b:
        script(second, {3: lambda: (arena.start_match(b, "Next"), arena.play(b, 1, 1))})
    else:
        script(second, {3: lambda: setattr(arena.gs, "match_id", None)})
    run_loop(monkeypatch, second, iterations=5)

    assert getattr(getattr(second, "_match_bundle_ctx", None), "match_id", None) != a
    assert [p["result"] for p in packets(a)] == ["loss"], "no empty packet for the finished match"
    assert [entry for entry in bundles if entry[0] == a] == [(a, "loss")], "no second bundle for it"
    assert history() == {a: ("loss", "Rollo")}
    if b:
        assert match_packets.get_current_packet().match_id == b  # the next match records normally


# --- the packet names the opponent (#47)


def test_the_packet_names_the_opponent_the_match_history_names(monkeypatch, arena, bundles):  # noqa: F811
    """2026-10-19: every packet said opponent_name None; the history said "aaronkp" (a51dea64)."""
    a = "a51dea64-0c1d"
    coach = loop_coach(arena)
    script(
        coach,
        {
            1: lambda: (arena.start_match(a, "aaronkp"), arena.play(a, 1, 3)),
            2: lambda: arena.end_game(a, 1, THEIR_TEAM),
        },
    )
    run_loop(monkeypatch, coach, iterations=3)
    assert history() == {a: ("loss", "aaronkp")}
    assert [p["opponent_name"] for p in packets(a)] == ["aaronkp"]


def test_the_match_boundary_fallback_also_names_the_opponent(monkeypatch, arena, bundles):  # noqa: F811
    """A game end the signal never brought, recorded when the next match starts."""
    a, b = "a-no-signal", "b-next"
    coach = loop_coach(arena)
    view = coach._mcp.get_game_state
    # server.get_game_state carries the room event's opponent name.
    coach._mcp.get_game_state = lambda: {**view(), "opponent_name": arena.gs.opponent_name}
    script(
        coach,
        {
            1: lambda: (arena.start_match(a, "aaronkp"), arena.play(a, 1, 3)),
            2: lambda: arena.play(a, 1, 7),
            3: lambda: (arena.start_match(b, "Someone"), arena.play(b, 1, 1)),
        },
    )
    run_loop(monkeypatch, coach, iterations=4)
    assert [p["opponent_name"] for p in packets(a)] == ["aaronkp"]


# --- a saved bundle is never replaced by an emptier one (#29)


def _packet_file(tmp_path: Path, decisions: int) -> Path:
    path = tmp_path / f"packet_{decisions}.json"
    path.write_text(json.dumps({"match_id": "m", "decisions": [{"n": i} for i in range(decisions)]}))
    return path


def _process(ctx, tmp_path, opener, *, decisions: int, result: str, advice=None):
    return match_bundle.process_match(
        ctx,
        settings={"share_match_logs": True, "license_key": "sk-test", "install_id": "inst"},
        result=result,
        packet_path=_packet_file(tmp_path, decisions),
        advice_history=advice if advice is not None else [{"advice": "x"}],
        bundle_dir=tmp_path / "bundles",
        base_url="https://e.test",
        opener=opener,
        sleep=lambda s: None,
    )


@pytest.mark.parametrize(
    "decisions,result,advice",
    [
        (0, "unknown", None),  # the 09:54:09 / 10:07:00 junk bundle
        (0, "loss", None),  # fewer decisions
        (31, "unknown", None),  # same decisions, the known result lost
        (31, "loss", []),  # same decisions and result, less recorded
    ],
)
def test_a_saved_fuller_bundle_is_kept_and_the_emptier_one_never_uploaded(
    tmp_path, monkeypatch, caplog, decisions, result, advice
):
    monkeypatch.setattr(match_bundle, "_bundled_match_ids", set())
    calls: list = []

    def opener(url, data, headers, timeout):
        calls.append(data)
        return 200, '{"status": "stored"}'

    ctx = match_bundle.MatchBundleContext(match_id="92c09c7a")
    advice_rich = [{"advice": secrets.token_hex(500), "n": i} for i in range(20)]
    saved = _process(ctx, tmp_path, opener, decisions=31, result="loss", advice=advice_rich)
    assert saved is not None and len(calls) == 1
    original = saved.read_bytes()

    monkeypatch.setattr(match_bundle, "_bundled_match_ids", set())  # the restarted coach
    with caplog.at_level("INFO", logger="arenamcp.match_bundle"):
        again = _process(ctx, tmp_path, opener, decisions=decisions, result=result, advice=advice)
    assert again is None
    assert saved.read_bytes() == original
    assert len(calls) == 1
    assert "[BUNDLE] skipped: kept the saved bundle for 92c09c7a" in caplog.text


def test_a_fuller_bundle_still_replaces_an_emptier_saved_one(tmp_path, monkeypatch):
    monkeypatch.setattr(match_bundle, "_bundled_match_ids", set())
    ok = lambda url, data, headers, timeout: (200, "{}")  # noqa: E731
    ctx = match_bundle.MatchBundleContext(match_id="m-grow")
    _process(ctx, tmp_path, ok, decisions=0, result="unknown")
    monkeypatch.setattr(match_bundle, "_bundled_match_ids", set())
    path = _process(ctx, tmp_path, ok, decisions=12, result="win")
    assert path is not None
    assert len(json.loads(gzip.decompress(path.read_bytes()))["packet"]["decisions"]) == 12


def test_a_duplicate_upload_says_the_server_kept_its_first_copy(caplog):
    with caplog.at_level("INFO", logger="arenamcp.match_bundle"):
        assert match_bundle.upload_bundle(
            b"gz",
            match_id="92c09c7a",
            license_key="k",
            install_id="i",
            base_url="https://e.test",
            opener=lambda *a: (200, '{"status": "duplicate", "match_id": "92c09c7a"}'),
        )
    assert "92c09c7a was already uploaded; the server kept its first copy" in caplog.text
    assert "[BUNDLE] uploaded" not in caplog.text


# --- the resume path itself (server.start_watching)


class _Watcher:
    created: list[dict] = []

    def __init__(self, **kwargs):
        _Watcher.created.append(kwargs)
        self.file_position = 0
        self.log_path = None

    def start(self):
        pass


def _completed_line(match_id: str) -> str:
    return json.dumps(
        {
            "timestamp": str(int(time.time() * 1000)),
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {
                    "gameRoomConfig": {"matchId": match_id, "reservedPlayers": []},
                    "stateType": "MatchGameRoomStateType_MatchCompleted",
                }
            },
        }
    )


@pytest.mark.parametrize("completed", [True, False], ids=["completed-in-log", "still-live"])
def test_resume_skips_a_saved_match_the_log_shows_completed(tmp_path, monkeypatch, completed):
    """Legacy last_match.json files (written "active" after the end) must not resume either."""
    log = (tmp_path / "Player.log").resolve()
    room = {"matchGameRoomStateChangedEvent": {"gameRoomInfo": {"gameRoomConfig": {"matchId": "6175f002"}}}}
    lines = [json.dumps(room)]
    if completed:
        lines.append(_completed_line("6175f002"))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stat = log.stat()
    gamestate.MATCH_STATE_PATH.write_text(
        json.dumps(
            {
                "match_id": "6175f002",
                "local_seat_id": 1,
                "log_offset": 10,
                "turn_number": 0,
                "timestamp": time.time(),
                "status": "active",
                "log_identity": {"path": str(log), "size": stat.st_size, "mtime": stat.st_mtime},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MTGA_LOG_PATH", str(log))
    monkeypatch.setattr(server, "MTGALogWatcher", _Watcher)
    monkeypatch.setattr(server, "watcher", None)
    parser = SimpleNamespace(register_handler=lambda *a: None, set_default_handler=lambda *a: None)
    monkeypatch.setattr(server, "parser", parser)
    gs = GameState()
    monkeypatch.setattr(server, "game_state", gs)
    _Watcher.created.clear()

    server.start_watching()

    created = _Watcher.created[-1]
    if completed:
        assert gs.match_id is None and created["resume_offset"] is None and created["resume_match_id"] is None
    else:
        assert gs.match_id == "6175f002" and created["resume_offset"] == 10
