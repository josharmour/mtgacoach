"""The engine reloads itself between matches when its code changed (2026-10-19).

Merged fixes are on disk at once on the dev Mac (editable install), but a
running engine keeps its old modules until someone clicks Reload engine. The
engine now fingerprints its code at start, and between matches, with the code
changed, sends ``request_engine_reload``; the desktop runs the Reload engine
path, and the armed post-match navigation rides through the reload checkpoint
so the new engine dismisses the result screen and requeues the same queue and
deck. Never mid-game, mid-draft, or with a concede or bug capture in flight.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp import server, standalone_auto_reload
from arenamcp.auto_queue import AutoQueueNavigator
from arenamcp.standalone_auto_queue import _AutoQueueMixin
from arenamcp.standalone_auto_reload import (
    ANSWER_TIMEOUT_S,
    CHECK_INTERVAL_S,
    CodeFingerprint,
    _AutoReloadMixin,
    auto_reload_default,
    compute_fingerprint,
    source_checkout,
)
from arenamcp.standalone_startup import _StartupMixin

OLD = CodeFingerprint("git", "tree-before-merge", "6c88f7f0")
NEW = CodeFingerprint("git", "tree-after-merge", "f7bc5e98")
MATCH = "15e0eb20-a63e-4b88-9c8b-2e2d39188258"
NEXT_MATCH = "73ccac27-ce9c-4a68-a258-263e00e0fcf9"
REASON = "Code changed since engine start (6c88f7f0 -> f7bc5e98)"


# -- change detection ----------------------------------------------------------


def _package(root: Path) -> Path:
    pkg = root / "src" / "arenamcp"
    (pkg / "desktop").mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "1"\n')
    (pkg / "desktop" / "app.py").write_text("x = 1\n")
    return pkg


def test_files_fingerprint_follows_every_engine_source_file(tmp_path):
    pkg = _package(tmp_path)
    start = compute_fingerprint(pkg, kind="files")
    assert start.kind == "files" and start.label == start.value[:8]
    assert compute_fingerprint(pkg, kind="files") == start

    # Bytecode and non-Python files are not the engine's code.
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0" * 40)
    (pkg / "notes.txt").write_text("not code")
    assert compute_fingerprint(pkg, kind="files") == start

    (pkg / "desktop" / "app.py").write_text("x = 22\n")  # size
    edited = compute_fingerprint(pkg, kind="files")
    assert edited.value != start.value
    stat = (pkg / "__init__.py").stat()
    os.utime(pkg / "__init__.py", ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # mtime only
    touched = compute_fingerprint(pkg, kind="files")
    assert touched.value != edited.value
    (pkg / "new_module.py").write_text("")
    assert compute_fingerprint(pkg, kind="files").value != touched.value


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_fingerprint_follows_committed_engine_code_only(tmp_path):
    pkg = _package(tmp_path)
    (tmp_path / "README.md").write_text("docs\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "start")
    start = compute_fingerprint(pkg)
    assert start.kind == "git"
    assert start.label == _git(tmp_path, "rev-parse", "HEAD")[:8]

    # A merge that touches only docs or tests leaves the engine's code alone.
    (tmp_path / "README.md").write_text("more docs\n")
    _git(tmp_path, "commit", "-qam", "docs")
    docs_only = compute_fingerprint(pkg, kind="git")
    assert docs_only.value == start.value and docs_only.label != start.label

    (pkg / "desktop" / "app.py").write_text("x = 2\n")
    _git(tmp_path, "commit", "-qam", "fix")
    assert compute_fingerprint(pkg, kind="git").value != start.value


def test_a_failing_git_never_reads_as_a_change(tmp_path, monkeypatch):
    pkg = _package(tmp_path)
    (tmp_path / ".git").mkdir()

    def no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(standalone_auto_reload.subprocess, "run", no_git)
    # At start, a checkout without a working git falls back to the files ...
    assert compute_fingerprint(pkg).kind == "files"
    # ... but a recheck keeps the kind chosen at start: unknown, not "changed".
    assert compute_fingerprint(pkg, kind="git") is None


# -- gating --------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("ARENAMCP_AUTO_RELOAD", "MTGACOACH_DEV", "MTGACOACH_FRONTEND_FEATURES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    return monkeypatch


def test_on_for_a_source_checkout_off_for_an_installed_package(tmp_path, clean_env):
    checkout = tmp_path / "repo"
    pkg = _package(checkout)
    (checkout / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")  # a worktree's .git file
    assert source_checkout(pkg) == checkout
    assert auto_reload_default(pkg)[0] is True

    installed = _package(tmp_path / "Program Files" / "mtgacoach")
    assert source_checkout(installed) is None
    assert auto_reload_default(installed) == (
        False,
        "installed package; its code changes only through the installer",
    )
    # A non-editable install into a venv that happens to sit inside a checkout.
    in_venv = checkout / ".venv" / "lib" / "python3.12" / "site-packages" / "arenamcp"
    in_venv.mkdir(parents=True)
    assert source_checkout(in_venv) is None


def test_env_override_dev_flag_and_packaged_app(tmp_path, clean_env):
    installed = _package(tmp_path / "installed")
    checkout = tmp_path / "repo"
    in_checkout = _package(checkout)
    (checkout / ".git").mkdir()

    clean_env.setenv("ARENAMCP_AUTO_RELOAD", "1")
    assert auto_reload_default(installed) == (True, "ARENAMCP_AUTO_RELOAD=1")
    clean_env.setenv("ARENAMCP_AUTO_RELOAD", "0")
    assert auto_reload_default(in_checkout) == (False, "ARENAMCP_AUTO_RELOAD=0")
    clean_env.delenv("ARENAMCP_AUTO_RELOAD")

    clean_env.setenv("MTGACOACH_DEV", "1")
    assert auto_reload_default(installed) == (True, "MTGACOACH_DEV=1")
    clean_env.delenv("MTGACOACH_DEV")

    clean_env.setattr(sys, "frozen", True, raising=False)
    assert auto_reload_default(in_checkout)[0] is False


class Coach(_AutoReloadMixin):
    def __init__(self, emit=True):
        self.ui = Mock() if emit else SimpleNamespace(log=Mock())


def test_engine_needs_a_desktop_that_handles_the_request(clean_env, monkeypatch, caplog):
    clean_env.setenv("ARENAMCP_AUTO_RELOAD", "1")
    fingerprint = Mock(return_value=OLD)
    monkeypatch.setattr(standalone_auto_reload, "compute_fingerprint", fingerprint)
    caplog.set_level(logging.INFO, logger="arenamcp.standalone_auto_reload")

    old_desktop = Coach()
    old_desktop._init_auto_reload()
    assert old_desktop._auto_reload_baseline is None
    assert "this desktop app predates automatic reloads" in caplog.text

    clean_env.setenv("MTGACOACH_FRONTEND_FEATURES", "request_engine_reload")
    cli = Coach(emit=False)
    cli._init_auto_reload()
    assert cli._auto_reload_baseline is None
    fingerprint.assert_not_called()

    desktop = Coach()
    desktop._init_auto_reload()
    assert desktop._auto_reload_baseline == OLD
    assert "Auto-reload between matches: on (ARENAMCP_AUTO_RELOAD=1); engine code git 6c88f7f0" in caplog.text

    clean_env.setenv("ARENAMCP_AUTO_RELOAD", "0")
    off = Coach()
    off._init_auto_reload()
    assert off._auto_reload_baseline is None
    assert "Auto-reload between matches: off (ARENAMCP_AUTO_RELOAD=0)" in caplog.text


# -- the between-matches trigger ---------------------------------------------------


class Engine(_AutoReloadMixin):
    """The engine as the trigger sees it: started, autoplay driving, code changed on disk."""

    def __init__(self):
        self.ui = Mock()
        self.ap_status = "AP:ON"
        self.draft_mode = False
        self._startup_finished = True
        self._auto_reload_baseline = OLD
        self._auto_reload_checked_at = 0.0

    def _autopilot_control_status(self):
        return self.ap_status


@pytest.fixture
def disk(monkeypatch):
    """The code on disk; tests change ``disk.current``."""
    holder = SimpleNamespace(current=NEW, calls=0)

    def fingerprint(pkg=None, kind=None):
        holder.calls += 1
        assert kind == OLD.kind
        return holder.current

    monkeypatch.setattr(standalone_auto_reload, "compute_fingerprint", fingerprint)
    return holder


@pytest.fixture
def arena(monkeypatch):
    """Player.log's match completion and the parser's game-end signal."""
    from tests.test_standalone_coaching_loop import FakeServerGameState

    holder = FakeServerGameState()
    monkeypatch.setattr(server, "game_state", holder)
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})

    def complete(match_id):
        monkeypatch.setattr(
            server,
            "_completed_match_for_navigation",
            {"match_id": match_id, "completed_at": time.time(), "match_complete": True, "event_id": "Ladder"},
        )

    holder.complete = complete
    return holder


def requests(engine):
    return [
        c.args[0] for c in engine.ui._emit.call_args_list if c.args[0].get("type") == "request_engine_reload"
    ]


def test_reloads_right_after_the_match_ends_and_its_records_are_written(disk, arena, caplog):
    engine = Engine()
    caplog.set_level(logging.INFO, logger="arenamcp.standalone_auto_reload")
    in_play = {"match_id": MATCH, "turn": {"turn_number": 9}}
    for _ in range(3):
        assert not engine._maybe_request_auto_reload(in_play)
    assert disk.calls == 0  # mid-game never even looks at the code
    arena.complete(MATCH)
    # Arena still shows the result over the finished board, then goes Home.
    assert engine._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 0}})
    assert requests(engine) == [
        {"type": "request_engine_reload", "reason": REASON, "old": "6c88f7f0", "new": "f7bc5e98"}
    ]
    assert f"{REASON}; reloading between matches" in caplog.text
    # One request per engine; the hold keeps navigation still until the desktop answers.
    assert not engine._maybe_request_auto_reload({"match_id": None})
    assert engine._auto_reload_holds({"match_id": None})
    assert len(requests(engine)) == 1


@pytest.mark.parametrize(
    "case",
    [
        "mid_game",
        "bo3_between_games",
        "next_match_started",
        "mulligan_of_next_match",
        "draft",
        "draft_event_driver",
        "still_starting",
    ],
)
def test_never_mid_game_or_mid_draft(disk, arena, case):
    engine = Engine()
    engine._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 3}})  # seen in play
    state = {"match_id": None}
    if case == "mid_game":
        arena.complete("an-earlier-match")
        state = {"match_id": MATCH, "turn": {"turn_number": 7}, "pending_decision": "Declare Attackers"}
    elif case == "bo3_between_games":
        # Game 1 of MATCH ended; the between-games reset shows no match.
        arena.complete("an-earlier-match")
    elif case == "next_match_started":
        arena.complete(MATCH)
        state = {"match_id": NEXT_MATCH, "turn": {"turn_number": 2}}
    elif case == "mulligan_of_next_match":
        arena.complete(MATCH)
        state = {"match_id": NEXT_MATCH, "turn": {"turn_number": 0}, "pending_decision": "Mulligan"}
    elif case == "draft":
        arena.complete(MATCH)
        engine.draft_mode = True
    elif case == "draft_event_driver":
        arena.complete(MATCH)
        engine._draft_event_driver = SimpleNamespace(
            enabled=True, run=SimpleNamespace(event_name="QuickDraft_Y26FRA", finished=False)
        )
    elif case == "still_starting":
        arena.complete(MATCH)
        engine._startup_finished = False
    for _ in range(3):
        assert not engine._maybe_request_auto_reload(state)
    assert requests(engine) == []
    assert disk.calls == 0


@pytest.fixture(autouse=True)
def tests_threads(monkeypatch):
    """Work in flight is judged from the threads this test starts, never another test's leftovers."""
    mine: list[threading.Thread] = []
    real = standalone_auto_reload._busy_thread
    monkeypatch.setattr(standalone_auto_reload, "_busy_thread", lambda: real(mine))
    return mine


@pytest.fixture
def busy_thread(tests_threads):
    started = []

    def start(name):
        release = threading.Event()
        thread = threading.Thread(target=release.wait, name=name, daemon=True)
        thread.start()
        tests_threads.append(thread)
        started.append((thread, release))
        return release

    yield start
    for thread, release in started:
        release.set()
        thread.join(1)


@pytest.mark.parametrize(
    "work, blocker",
    [
        ("bundle", "the match bundle is still being written"),
        ("advice_score", "the match's advice score is still being written"),
        ("analysis", "post-match analysis is running"),
        ("game_end_signal", "the game end is not recorded yet"),
        ("concede", "an auto-concede is in flight"),
        ("concede_thread", "an auto-concede is in flight"),
        ("bug_capture", "an autopilot bug capture is recording"),
        ("debug_report", "a bug report is being saved"),
        ("navigation_step", "an auto-queue navigation step is running"),
        ("queue_joined", "auto-queue already joined the next queue"),
        ("autoplay_paused", "autoplay is paused"),
    ],
)
def test_waits_for_work_in_flight_then_reloads(disk, arena, busy_thread, caplog, work, blocker):
    engine = Engine()
    caplog.set_level(logging.INFO, logger="arenamcp.standalone_auto_reload")
    engine._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 8}})
    arena.complete(MATCH)
    release = None
    if work == "bundle":
        release = busy_thread("match-bundle-15e0eb20")
    elif work == "advice_score":
        release = busy_thread("auto-advice-score")
    elif work == "analysis":
        engine._post_match_analysis_running = True
    elif work == "game_end_signal":
        arena.game_ended_event.set()
    elif work == "concede":
        engine.concede_in_progress = Mock(return_value=True)
    elif work == "concede_thread":
        release = busy_thread("concede-countdown")
    elif work == "bug_capture":
        engine._autopilot_bug_capture = SimpleNamespace(active=True)
    elif work == "debug_report":
        release = busy_thread("debug-report")
    elif work == "navigation_step":
        engine._auto_queue_navigator = SimpleNamespace(active=True, queue_joined=False, step_running=True)
    elif work == "queue_joined":
        engine._auto_queue_navigator = SimpleNamespace(active=True, queue_joined=True, step_running=False)
    elif work == "autoplay_paused":
        engine.ap_status = "AP:PAUSED"
    for _ in range(3):
        assert not engine._maybe_request_auto_reload({"match_id": None})
    assert requests(engine) == []
    assert caplog.text.count(f"{REASON}; the reload waits: {blocker}") == 1  # said once, not every pass

    if release is not None:
        release.set()
        for thread in threading.enumerate():
            if thread.name in (
                "match-bundle-15e0eb20",
                "auto-advice-score",
                "concede-countdown",
                "debug-report",
            ):
                thread.join(1)
                assert not thread.is_alive()
    engine._post_match_analysis_running = False
    arena.game_ended_event.clear()
    engine.concede_in_progress = Mock(return_value=False)
    engine._autopilot_bug_capture = SimpleNamespace(active=False)
    engine._auto_queue_navigator = SimpleNamespace(active=True, queue_joined=False, step_running=False)
    engine.ap_status = "AP:ON"
    assert engine._maybe_request_auto_reload({"match_id": None})


def test_unchanged_code_never_reloads_and_is_checked_sparingly(disk, arena, monkeypatch):
    disk.current = CodeFingerprint("git", OLD.value, "b0b0b0b0")  # a docs-only merge: same engine tree
    engine = Engine()
    engine._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 4}})
    arena.complete(MATCH)
    for _ in range(20):
        assert not engine._maybe_request_auto_reload({"match_id": None})
    assert disk.calls == 1  # once per CHECK_INTERVAL_S, not every 0.3 s pass
    engine._auto_reload_checked_at = time.monotonic() - CHECK_INTERVAL_S - 1
    disk.current = None  # git failed: unknown is not "changed"
    assert not engine._maybe_request_auto_reload({"match_id": None})
    assert disk.calls == 2 and requests(engine) == []


def test_one_reload_per_three_minutes_across_engines(disk, arena, caplog):
    caplog.set_level(logging.INFO, logger="arenamcp.standalone_auto_reload")
    first = Engine()
    first._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 4}})
    arena.complete(MATCH)
    assert first._maybe_request_auto_reload({"match_id": None})
    saved = json.loads(json.dumps(first._auto_reload_export()))  # through the checkpoint file

    # Another merge lands while the new engine starts; it is between the same matches.
    second = Engine()
    second._auto_reload_resume_from(saved)
    second._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 0}})
    assert not second._maybe_request_auto_reload({"match_id": None})
    assert requests(second) == []
    assert "the reload waits: the last automatic reload was 0s ago (at most one per 3 min)" in caplog.text

    second._auto_reload_last_wall = time.time() - 181
    assert second._maybe_request_auto_reload({"match_id": None})


def test_an_unanswered_request_turns_automatic_reloads_off(disk, arena, caplog):
    engine = Engine()
    engine._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 4}})
    arena.complete(MATCH)
    assert engine._maybe_request_auto_reload({"match_id": None})
    assert engine._auto_reload_holds({"match_id": None})
    # A match that starts meanwhile is played, not held.
    assert not engine._auto_reload_holds({"match_id": NEXT_MATCH, "turn": {"turn_number": 1}})
    engine._auto_reload_requested_at = time.monotonic() - ANSWER_TIMEOUT_S - 1
    with caplog.at_level(logging.WARNING, logger="arenamcp.standalone_auto_reload"):
        assert not engine._auto_reload_holds({"match_id": None})
    assert "automatic reloads are off until the engine restarts" in caplog.text
    engine._auto_reload_last_wall = 0.0
    assert not engine._maybe_request_auto_reload({"match_id": None})  # no loop
    assert len(requests(engine)) == 1


# -- auto-queue continuity through the reload checkpoint ---------------------------


class Runtime(_StartupMixin, _AutoQueueMixin, _AutoReloadMixin):
    """Old and new engine: checkpoint, auto-queue and auto-reload mixins, nothing else."""

    def __init__(self):
        self.settings = Mock()
        self.ui = Mock()
        self._coach = SimpleNamespace(_backend=Mock(), _deck_playbook=None, _deck_strategy=None)
        self._mcp = Mock()
        self._autopilot = Mock()
        self._autopilot_enabled = True
        self._running = True
        self._auto_queue_enabled = True
        self._auto_queue_since = time.time()
        self._auto_queue_restart_pending = True

    def _autopilot_control_status(self):
        return "AP:ON" if self._autopilot_enabled else "AP:OFF"

    @staticmethod
    def _normalize_turn_snapshot(snapshot):
        return snapshot


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "engine_resume.json"
    monkeypatch.setattr("arenamcp.standalone_startup.ENGINE_RESUME_PATH", path)
    monkeypatch.setenv("ARENAMCP_ENGINE_RELOAD", "1")
    monkeypatch.setattr(server, "watcher", SimpleNamespace(file_position=4096, log_path="Player.log"))
    monkeypatch.setattr("arenamcp.gamestate_persistence.save_match_state", Mock())
    monkeypatch.setattr("arenamcp.idle_sleep.SleepInhibitor", Mock())
    # Navigation steps (screenshots, clicks) and the bridge socket stay out of these tests.
    monkeypatch.setattr(AutoQueueNavigator, "_step", lambda self, generation, aborted: None)
    monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", lambda: Mock(connected=False))
    log_scan = Mock(return_value=None)
    monkeypatch.setattr(server, "idle_completed_match_from_log", log_scan)
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    return SimpleNamespace(path=path, log_scan=log_scan)


def _armed_old_engine(monkeypatch, published=None):
    """The old engine seconds after MATCH ended: navigation armed, the result dismissed."""
    monkeypatch.setattr(
        server, "game_state", SimpleNamespace(get_published_snapshot=lambda: published or {"match_id": None})
    )
    old = Runtime()
    navigator = AutoQueueNavigator(backend=Mock(), get_game_state=lambda: {"match_id": None})
    navigator.set_enabled(True)
    assert navigator.note_match_end(
        {"match_id": MATCH, "event_id": "Ladder", "deck_name": "Mono-Red Burn"}, confirmed_match_end=True
    )
    navigator._stage = "home"
    navigator._history.append({"action": "continue", "label": "Click to Continue"})
    navigator._clicks = 1
    old._auto_queue_navigator = navigator
    old._auto_reload_last_wall = time.time()
    return old, navigator


def test_navigation_continues_in_the_new_engine_as_if_no_reload_happened(checkpoint, monkeypatch):
    old, old_navigator = _armed_old_engine(monkeypatch)
    old.prepare_engine_reload()
    assert not old._running and not old_navigator.enabled  # the old engine lets go of Arena
    old.ui._emit.assert_called_with({"type": "reload_ready"})
    saved = json.loads(checkpoint.path.read_text())
    assert saved.get("match_id") is None  # between matches: no match part ...
    record = saved["auto_queue"]  # ... but the navigation is saved
    assert record["ended_match_id"] == MATCH and record["stage"] == "home"
    assert record["expected_queue"] == {"event_id": "Ladder", "deck_name": "Mono-Red Burn"}
    assert record["last_event_id"] == "Ladder"
    # The toggles are untouched: nothing turned auto-queue or autoplay off in settings.
    assert ("auto_queue_enabled", False) not in [c.args for c in old.settings.set.call_args_list]
    assert ("autopilot_enabled", False) not in [c.args for c in old.settings.set.call_args_list]

    new = Runtime()  # started with --autopilot; auto_queue_enabled read from settings
    new._load_engine_resume()
    assert not checkpoint.path.exists()  # consumed once
    assert new._auto_reload_last_wall == pytest.approx(old._auto_reload_last_wall)
    assert new._autopilot_enabled  # autoplay was driving, not paused
    # Arena still shows the finished match (turn 0 after the reset).
    assert new._poll_auto_queue({"match_id": MATCH, "turn": {"turn_number": 0}})
    navigator = new._auto_queue_navigator
    assert navigator is not old_navigator
    assert navigator.active and navigator.enabled and not navigator.paused_reason
    assert navigator._ended_match_id == MATCH
    assert navigator._expected_queue == {"event_id": "Ladder", "deck_name": "Mono-Red Burn"}
    assert navigator._last_event_id == "Ladder"
    assert navigator.get_debug_info()["stage"] == "home"
    assert navigator.get_debug_info()["recent_actions"] == [
        {"action": "continue", "label": "Click to Continue"}
    ]
    assert (
        navigator.get_debug_info()["status"]
        == "Engine reloaded; continuing navigation for the finished match"
    )
    checkpoint.log_scan.assert_not_called()  # no Player.log guesswork needed

    # The new engine's log replay re-reads MATCH's completion: it must not re-arm from "results".
    monkeypatch.setattr(
        server,
        "_completed_match_for_navigation",
        {"match_id": MATCH, "completed_at": time.time(), "match_complete": True},
    )
    assert new._poll_auto_queue({"match_id": MATCH, "turn": {"turn_number": 0}})
    assert navigator.get_debug_info()["stage"] == "home"
    # The next match hands control back to autoplay, exactly as before.
    assert not new._poll_auto_queue({"match_id": NEXT_MATCH, "turn": {"turn_number": 1}})
    assert not navigator.active


def test_a_paused_navigation_stays_paused(checkpoint, monkeypatch):
    old, navigator = _armed_old_engine(monkeypatch)
    navigator._pause("Could not verify the next Arena navigation step")
    old.prepare_engine_reload()
    new = Runtime()
    new._load_engine_resume()
    new._poll_auto_queue({"match_id": None})
    info = new._auto_queue_navigator.get_debug_info()
    assert info["active"] and info["stage"] == "paused"
    assert info["paused_reason"] == "Could not verify the next Arena navigation step"
    assert info["status"].endswith("Toggle auto queue off/on to retry this navigation.")


def test_a_match_that_started_during_the_reload_is_played(checkpoint, monkeypatch, caplog):
    old, _ = _armed_old_engine(monkeypatch)
    old.prepare_engine_reload()
    new = Runtime()
    new._load_engine_resume()
    with caplog.at_level(logging.INFO, logger="arenamcp.standalone_auto_queue"):
        new._poll_auto_queue({"match_id": NEXT_MATCH, "turn": {"turn_number": 2}})
    assert not new._auto_queue_navigator.active
    assert f"match {NEXT_MATCH} started after match {MATCH} ended; autoplay is in control" in caplog.text
    # The restart path then says so too, and requeues after this match ends.
    assert f"match {NEXT_MATCH} is being played (turn 2)" in caplog.text


def test_an_expired_navigation_record_falls_back_to_the_restart_path(checkpoint, monkeypatch):
    old, _ = _armed_old_engine(monkeypatch)
    old.prepare_engine_reload()
    new = Runtime()
    new._load_engine_resume()
    new._auto_queue_resume["saved_at"] = time.time() - 601
    new._poll_auto_queue({"match_id": None})
    assert not new._auto_queue_navigator.active
    checkpoint.log_scan.assert_called_once()  # _arm_auto_queue_after_restart's Player.log check


def test_nothing_armed_saves_no_navigation(checkpoint, monkeypatch):
    monkeypatch.setattr(
        server, "game_state", SimpleNamespace(get_published_snapshot=lambda: {"match_id": MATCH})
    )
    old = Runtime()
    navigator = AutoQueueNavigator(backend=Mock(), get_game_state=dict)
    navigator.set_enabled(True)  # waiting for this match to finish
    old._auto_queue_navigator = navigator
    old.prepare_engine_reload()
    saved = json.loads(checkpoint.path.read_text())
    assert saved["auto_queue"] is None and saved["match_id"] == MATCH


# -- the coaching loop -------------------------------------------------------------


def _loop_engine(state):
    from tests.test_standalone_coaching_loop import make_loop_coach

    coach = make_loop_coach(state)
    coach._startup_finished = True
    coach._auto_reload_baseline = OLD
    coach._auto_reload_checked_at = 0.0
    coach.emitted = []
    coach.ui._emit = coach.emitted.append
    return coach


def test_the_loop_never_requests_a_reload_mid_game(monkeypatch, disk, arena):
    from tests.test_standalone_coaching_loop import make_state, run_loop

    arena.complete("an-earlier-match")
    coach = _loop_engine(make_state(MATCH, turn=6))
    run_loop(monkeypatch, coach, iterations=5)
    assert [e for e in coach.emitted if e.get("type") == "request_engine_reload"] == []
    assert disk.calls == 0


def test_the_loop_requests_after_the_match_and_then_holds_navigation(monkeypatch, disk, arena):
    from tests.test_standalone_coaching_loop import make_state, run_loop

    coach = _loop_engine(make_state(MATCH, turn=6))
    passes = {"n": 0}

    def arena_moves_on():
        passes["n"] += 1
        if passes["n"] == 3:  # the match ends: the finished board under its result screen
            arena.complete(MATCH)
            coach._mcp.state = make_state(MATCH, turn=0)

    coach._mcp.poll_hooks.append(arena_moves_on)

    def sent():
        return [e for e in coach.emitted if e.get("type") == "request_engine_reload"]

    polls_after_request = []
    original = coach._poll_auto_queue

    def poll(state):
        polls_after_request.append(bool(sent()))
        return original(state)

    coach._poll_auto_queue = poll
    run_loop(monkeypatch, coach, iterations=7)
    assert passes["n"] == 7
    assert sent() == [
        {"type": "request_engine_reload", "reason": REASON, "old": "6c88f7f0", "new": "f7bc5e98"}
    ]
    # Mid-game the code was never even fingerprinted; after the request no
    # navigation step starts while the desktop swaps the engine.
    assert disk.calls == 1
    assert polls_after_request and not any(polls_after_request)


# -- the desktop -----------------------------------------------------------------------


@pytest.fixture
def session(qapp, monkeypatch):
    from tests.test_engine_reload import FakeProcess

    from arenamcp.desktop.coach_session import CoachSession

    monkeypatch.setattr("arenamcp.desktop.coach_session.CoachProcess", FakeProcess)
    monkeypatch.setattr("arenamcp.desktop.coach_session.TtsManager", lambda *_: Mock(is_running=True))
    session = CoachSession()
    yield session
    session.shutdown()


def test_the_request_runs_the_reload_engine_handoff(session, qapp):
    logs = []
    session.logEmitted.connect(lambda message, role: logs.append(message))
    # As the window wires it: the request takes the Reload engine button's slot.
    session.engineReloadRequested.connect(lambda _reason: session.restart())
    session.start(autopilot=True)
    session._handle_process_event({"type": "status", "key": "AUTOPILOT", "value": "AP:ON"})
    request = {"type": "request_engine_reload", "reason": REASON, "old": "6c88f7f0", "new": "f7bc5e98"}
    session._handle_process_event(request)
    session._handle_process_event(request)  # a repeat during the reload: never a second engine
    assert session._process.stops == [{"command": "prepare_engine_reload"}]
    assert f"{REASON}; reloading the engine between matches." in logs
    assert session.last_startup_status["phase"] == "reloading"
    session._process.finish()
    qapp.processEvents()
    assert session._process.starts[-1] == {
        "autopilot": True,
        "dry_run": False,
        "afk": False,
        "engine_reload": True,
    }
    assert len(session._process.starts) == 2


def test_the_window_routes_the_request_to_the_reload_engine_slot(qapp, monkeypatch):
    from arenamcp.desktop.main_window import MainWindow

    win = MainWindow()
    reload_engine = Mock()
    monkeypatch.setattr(win._session, "restart", reload_engine)
    try:
        win._session.engineReloadRequested.emit(REASON)
        reload_engine.assert_called_once_with()
        assert not win._closed
    finally:
        win.close()


@pytest.fixture
def child_runtime(tmp_path, monkeypatch):
    """A tiny child that reports its environment; never the real coach."""
    package = tmp_path / "src" / "arenamcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "standalone.py").write_text(
        "import json, os\n"
        "print(json.dumps({'features': os.getenv('MTGACOACH_FRONTEND_FEATURES')}), flush=True)\n"
    )
    monkeypatch.setattr("arenamcp.desktop.coach_process.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("arenamcp.desktop.coach_process.get_runtime_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        "arenamcp.desktop.coach_process.find_python_executable", lambda: (sys.executable, "test")
    )
    return tmp_path


def test_the_desktop_tells_the_engine_it_handles_reload_requests(qapp, child_runtime, monkeypatch):
    from arenamcp.desktop.coach_process import CoachProcess

    monkeypatch.delenv("MTGACOACH_FRONTEND_FEATURES", raising=False)
    process = CoachProcess()
    events = []
    process.event_received.connect(events.append)
    try:
        process.start()
        assert process._process.waitForFinished(5000)
        qapp.processEvents()
        assert {"features": "request_engine_reload"} in events
    finally:
        process.stop()


def test_navigation_waits_for_the_records_of_a_pending_reload(disk, arena, busy_thread, caplog, monkeypatch):
    """#190 (16:26:39): the change waited on the advice score while auto-queue joined the next
    queue, and the next match ran on the old code."""
    engine = Engine()
    caplog.set_level(logging.INFO, logger="arenamcp.standalone_auto_reload")
    engine._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 8}})
    arena.complete(MATCH)
    release = busy_thread("auto-advice-score")
    assert not engine._auto_reload_holds({"match_id": None})  # nothing found yet
    assert not engine._maybe_request_auto_reload({"match_id": None})  # found; the score is written
    for _ in range(3):
        assert engine._auto_reload_holds({"match_id": None})
    assert requests(engine) == []
    assert caplog.text.count("Auto-queue navigation waits for the pending engine reload") == 1
    release.set()
    for thread in threading.enumerate():
        if thread.name == "auto-advice-score":
            thread.join(1)
    assert engine._auto_reload_holds({"match_id": None})  # the request goes out at once
    assert len(requests(engine)) == 1


@pytest.mark.parametrize(
    "navigator",
    [
        SimpleNamespace(active=True, queue_joined=True, step_running=False),
        SimpleNamespace(active=True, queue_joined=False, step_running=True),
    ],
)
def test_no_work_hold_for_navigation_itself_or_mid_game(disk, arena, navigator):
    engine = Engine()
    engine._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 8}})
    arena.complete(MATCH)
    engine._auto_queue_navigator = navigator
    assert not engine._maybe_request_auto_reload({"match_id": None})
    assert not engine._auto_reload_holds({"match_id": None})
    assert not engine._auto_reload_holds({"match_id": NEXT_MATCH, "turn": {"turn_number": 1}})


def test_the_work_hold_gives_up_after_its_limit(disk, arena, busy_thread, caplog, monkeypatch):
    engine = Engine()
    caplog.set_level(logging.INFO, logger="arenamcp.standalone_auto_reload")
    engine._maybe_request_auto_reload({"match_id": MATCH, "turn": {"turn_number": 8}})
    arena.complete(MATCH)
    busy_thread("match-bundle-15e0eb20")
    engine._maybe_request_auto_reload({"match_id": None})
    assert engine._auto_reload_holds({"match_id": None})
    clock = time.monotonic() + standalone_auto_reload.WORK_HOLD_S + 1
    monkeypatch.setattr(standalone_auto_reload.time, "monotonic", lambda: clock)
    assert not engine._auto_reload_holds({"match_id": None})
    assert not engine._auto_reload_holds({"match_id": None})
    assert caplog.text.count("stops waiting for the engine reload") == 1
    assert requests(engine) == []
