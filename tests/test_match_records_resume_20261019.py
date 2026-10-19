"""A coach restart mid-game keeps the game's whole packet, bundle and opponent (ledger #72, 2026-10-19).

Match 115944ce (Jurema, G/W anthem): the coach restarted at 11:14:23 on T8.
The packet saved at 11:16:10 started at 11:14:29 with 11 decisions (T8-T10);
T1-T7 lived only in the old process. Its bundle sliced the coach log and
Player.log from the restart, and opponent_name was None: the room event that
names the opponent precedes the resume offset and the game-state checkpoint
did not carry the name.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import arenamcp.match_packets as mp
from arenamcp import match_bundle
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.gamestate import GameState
from arenamcp.match_packets import get_current_packet, start_match_packet, stop_match_packet
from arenamcp.request_tracker import decision_fingerprint
from arenamcp.standalone_postmatch import (
    _clear_live_bundle_context,
    _resume_bundle_context,
    _save_live_bundle_context,
)

GAME = "115944ce-5273-4596-a336-a43a9f70db4e"


def _restart() -> None:
    """A new coach process: nothing in memory."""
    mp._current_packet = None
    mp._finalized_match_ids.clear()


def _typed(turn: int) -> PendingDecision:
    return PendingDecision(
        request_id=(turn, 1),
        request_type="SelectTargets",
        options=(DecisionOption("tgt:299", "Trostani, Three Whispers"), DecisionOption("tgt:1", "Opponent")),
    )


def test_a_restart_resumes_the_games_packet_and_saves_it_whole():
    packet = start_match_packet(GAME)
    started = packet.start_time
    for turn in range(1, 8):
        packet.add_executed_action("Cast", "Shock", detail=f"T{turn}", turn=turn)
    early = _typed(6)
    packet.add_decision(early, ["tgt:299"])
    packet.add_combat_decision(
        "DeclareAttackers", chosen=["Slickshot Show-Off"], options=["Slickshot Show-Off"]
    )

    _restart()  # 11:14:23
    resumed = start_match_packet(GAME)
    assert resumed is get_current_packet() and resumed is not packet
    assert resumed.start_time == started
    assert len(resumed.decisions) == 9
    # The pending typed decision still settles by its fingerprint after the restart.
    resumed.update_outcome(decision_fingerprint(early), "ADVANCED")
    resumed.add_executed_action("Cast", "Iroh's Demonstration", turn=8)

    resumed.result = "loss"
    path = stop_match_packet().save()
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert saved["start_time"] == started
    assert len(saved["decisions"]) == 10
    assert [d["outcome"] for d in saved["decisions"] if "pending_decision" in d][0] == "ADVANCED"
    # The journal is gone and the finished game is never resumed again.
    assert not list((mp.PACKETS_DIR / mp.LIVE_SUBDIR).glob("*.jsonl"))
    _restart()
    assert start_match_packet(GAME) is None


def test_a_line_torn_by_the_dying_process_is_skipped():
    packet = start_match_packet(GAME)
    packet.add_executed_action("Cast", "Shock", turn=1)
    with open(packet.journal, "a", encoding="utf-8") as journal:
        journal.write('{"entry": {"executed_action": {"action_type": "Ca')
    _restart()
    resumed = start_match_packet(GAME)
    assert len(resumed.decisions) == 1
    resumed.add_executed_action("Cast", "Lightning Strike", turn=2)
    _restart()
    assert len(start_match_packet(GAME).decisions) == 2


def test_another_games_journal_is_not_resumed_and_old_ones_are_pruned():
    start_match_packet("older-game").add_executed_action("Cast", "Shock", turn=1)
    stale = mp._journal_path("older-game")
    old = time.time() - mp.LIVE_MAX_AGE_S - 60
    import os

    os.utime(stale, (old, old))
    _restart()
    packet = start_match_packet(GAME)
    assert packet.decisions == []
    assert not stale.exists()


def test_the_checkpoint_carries_the_opponents_name():
    gs = GameState()
    gs.match_id = GAME
    gs.opponent_name = "Jurema"
    restored = GameState()
    assert restored.restore_checkpoint(gs.export_checkpoint())
    assert restored.opponent_name == "Jurema"
    assert restored.get_snapshot()["opponent_name"] == "Jurema"


# --- the bundle's log offsets --------------------------------------------------------------


def _logs(tmp_path: Path, coach: int, player: int) -> tuple[Path, Path]:
    coach_log, player_log = tmp_path / "standalone.log", tmp_path / "Player.log"
    coach_log.write_bytes(b"c" * coach)
    player_log.write_bytes(b"p" * player)
    return coach_log, player_log


def test_a_restart_resumes_the_bundle_context_of_the_same_game(tmp_path, caplog):
    coach_log, player_log = _logs(tmp_path, 1000, 5000)
    first = match_bundle.begin_match(GAME, coach_log_path=coach_log, player_log_path=player_log)
    _save_live_bundle_context(first)
    _logs(tmp_path, 9000, 70000)  # the game went on; then the coach restarted
    later = match_bundle.begin_match(GAME, coach_log_path=coach_log, player_log_path=player_log)
    later.started_at = first.started_at + 400
    with caplog.at_level("INFO", logger="arenamcp.standalone_postmatch"):
        resumed = _resume_bundle_context(later)
    assert (resumed.coach_log_start, resumed.player_log_start) == (1000, 5000)
    assert resumed.started_at == first.started_at
    assert "resumed 115944ce-5273-4596-a336-a43a9f70db4e's context from before the restart" in caplog.text
    _clear_live_bundle_context(GAME)
    fresh = match_bundle.begin_match(GAME, coach_log_path=coach_log, player_log_path=player_log)
    assert _resume_bundle_context(fresh).coach_log_start == 9000


def test_another_game_or_a_rotated_log_starts_over(tmp_path):
    coach_log, player_log = _logs(tmp_path, 1000, 5000)
    _save_live_bundle_context(
        match_bundle.begin_match(GAME, coach_log_path=coach_log, player_log_path=player_log)
    )
    other = match_bundle.begin_match("73ccac27", coach_log_path=coach_log, player_log_path=player_log)
    assert _resume_bundle_context(other).coach_log_start == 1000  # its own offsets, which happen to match
    _logs(tmp_path, 1500, 300)  # Arena relaunched: a new, shorter Player.log
    rotated = match_bundle.begin_match(GAME, coach_log_path=coach_log, player_log_path=player_log)
    assert _resume_bundle_context(rotated).player_log_start == 300
    game_two = match_bundle.begin_match(
        GAME, coach_log_path=coach_log, player_log_path=player_log, game_number=2
    )
    assert _resume_bundle_context(game_two).coach_log_start == 1500
