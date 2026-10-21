"""Requeue after a coach restart, against a cut of the real Player.log (ledger #55, 2026-10-19).

Match 15e0eb20 ended at 10:58:35. Arena went back to its menus (StartHook,
Graph, rank, quest and course calls), the user edited the deck on the Decks
screen (DeckUpsertDeckV3 at 11:05, deck summaries, courses again) and the coach
restarted at 11:06:23. Nothing requeued: that coach still ran c9dfda7 (master
moved to 410a7b5, which has the restart arming, at 11:07:14), so its log says
only "Waiting for this match to finish"; a human clicked Play at 11:06:39
(EventJoin) and the 11:07:19 restart found match 73ccac27 under way.

tests/fixtures/player_log_idle_after_match_20261019.log is Player.log as the
11:06:23 coach saw it (cut at the size its startup anchor logged, 53611619
bytes): the match's queue request and room events, then every line after the
completion, with long payload lines truncated and player names and ids
scrubbed. The scan must read Arena as idle there, and as busy once the real
EventJoin follows.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from tests.test_standalone_auto_queue import Runtime

from arenamcp import server

FIXTURE = Path(__file__).parent / "fixtures" / "player_log_idle_after_match_20261019.log"
MATCH = "15e0eb20-a63e-4b88-9c8b-2e2d39188258"
RESTART = datetime.datetime(2026, 10, 19, 11, 6, 30).timestamp()  # the first auto-queue poll
EVENT_JOIN = (
    '[UnityCrossThreadLogger]==> EventJoin {"id":"069d9002-0f02-4809-8851-16b80cea6054","request":"{\\"EventName'
    '\\":\\"Ladder\\",\\"EntryCurrencyType\\":\\"None\\",\\"EntryCurrencyPaid\\":0,\\"CustomTokenId\\":null,'
    '\\"EventChoice\\":\\"\\",\\"DebugIgnoreEntryLimits\\":false}"}'
)


@pytest.fixture
def real_log(monkeypatch, tmp_path):
    path = tmp_path / "Player.log"
    path.write_bytes(FIXTURE.read_bytes())
    monkeypatch.setattr(server, "_log_path", lambda: path)
    monkeypatch.setattr(server.time, "time", lambda: RESTART)
    monkeypatch.setattr(server, "_last_queue_selection", {})
    return path


def test_the_real_log_reads_idle_after_the_deck_edit(real_log):
    text = real_log.read_text(encoding="utf-8")
    assert "==> DeckUpsertDeckV3" in text and "==> EventGetCoursesV2" in text  # menu traffic after the match
    found = server.idle_completed_match_from_log()
    assert found == {
        "match_id": MATCH,
        "event_id": "Ladder",
        "completed_at": 1792432715.381,
        "match_complete": True,
        "deck_name": "Mono-Red Burn",
        "deck_id": "1543b787-1438-460c-bd4a-e61aa4ea185e",
    }


def test_the_human_play_click_makes_it_busy(real_log):
    with open(real_log, "a", encoding="utf-8") as log:
        log.write(EVENT_JOIN + "\n")
    assert server.idle_completed_match_from_log() is None


@pytest.fixture
def navigator(monkeypatch):
    nav = Mock()
    nav.paused_reason = ""
    nav.process_tick.return_value = True
    nav.note_match_end.return_value = True
    monkeypatch.setattr("arenamcp.auto_queue.AutoQueueNavigator", Mock(return_value=nav))
    monkeypatch.setattr("arenamcp.idle_sleep.SleepInhibitor", Mock())
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    return nav


def test_the_11_06_restart_would_have_requeued_ladder_with_mono_red_burn(real_log, navigator, caplog):
    runtime = Runtime()
    runtime._auto_queue_restart_pending = True
    # The restored checkpoint of the finished match: turn 0, no objects.
    with caplog.at_level(logging.INFO, logger="arenamcp.standalone_auto_queue"):
        assert runtime._poll_auto_queue({"match_id": MATCH, "turn": {"turn_number": 0}})
    navigator.note_match_end.assert_called_once()
    record = navigator.note_match_end.call_args.args[0]
    assert record["match_id"] == MATCH and record["deck_name"] == "Mono-Red Burn"
    assert "requeueing Ladder with 'Mono-Red Burn'" in caplog.text


def test_a_restart_into_a_match_says_why_it_does_not_requeue(real_log, navigator, caplog):
    # 11:07:19: match 73ccac27 restored at turn 1.
    runtime = Runtime()
    runtime._auto_queue_restart_pending = True
    with caplog.at_level(logging.INFO, logger="arenamcp.standalone_auto_queue"):
        runtime._poll_auto_queue(
            {"match_id": "73ccac27-ce9c-4a68-a258-263e00e0fcf9", "turn": {"turn_number": 1}}
        )
    navigator.note_match_end.assert_not_called()
    assert (
        "Auto queue after restart: match 73ccac27-ce9c-4a68-a258-263e00e0fcf9 is being played (turn 1); "
        "requeueing after it ends" in caplog.text
    )


def test_a_refused_requeue_is_logged(real_log, navigator, caplog):
    navigator.note_match_end.return_value = False
    runtime = Runtime()
    runtime._auto_queue_restart_pending = True
    with caplog.at_level(logging.INFO, logger="arenamcp.standalone_auto_queue"):
        runtime._poll_auto_queue({"match_id": MATCH, "turn": {"turn_number": 0}})
    assert f"navigation did not take match {MATCH}" in caplog.text
