"""Engine reload keeps the desktop alive and never overlaps Arena controllers."""

from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QProcess, Signal

from arenamcp.desktop.coach_process import CoachProcess
from arenamcp.desktop.coach_session import CoachSession


class FakeProcess(QObject):
    event_received = Signal(object)
    stderr_line = Signal(str)
    exited = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_running = False
        self.starts = []
        self.stops = []

    def start(self, **options):
        self.is_running = True
        self.starts.append(options)

    def stop_async(self, **options):
        self.stops.append(options)

    def stop(self):
        self.finish()

    def finish(self, code=0):
        self.is_running = False
        self.exited.emit(code)


@pytest.fixture
def session(qapp, monkeypatch):
    monkeypatch.setattr("arenamcp.desktop.coach_session.CoachProcess", FakeProcess)
    monkeypatch.setattr("arenamcp.desktop.coach_session.TtsManager", lambda *_: Mock(is_running=True))
    session = CoachSession()
    yield session
    session.shutdown()


def test_reload_waits_for_exit_and_preserves_runtime_controls(session, qapp):
    session.start(autopilot=False, dry_run=True, afk=False)
    session._handle_process_event({"type": "status", "key": "AUTOPILOT", "value": "AP:ON"})
    session._handle_process_event({"type": "status", "key": "AFK", "value": "ON"})
    session._handle_process_event({"type": "game_state", "data": {"match_id": "in-progress"}})
    session.restart()
    session.restart()  # Repeated clicks must not schedule another child.

    assert session._process.stops == [{"command": "prepare_engine_reload"}]
    assert len(session._process.starts) == 1
    assert session.last_startup_status["phase"] == "reloading"
    assert session.last_game_state == {"match_id": "in-progress"}
    session._handle_process_event({"type": "startup_status", "phase": "ready", "ready": True})
    session._handle_process_event({"type": "speak_request", "text": "Obsolete advice"})
    assert session.last_startup_status["phase"] == "reloading"
    session._tts.request_speech.assert_not_called()

    session._process.finish()
    assert len(session._process.starts) == 1
    qapp.processEvents()
    assert session._process.starts[-1] == {
        "autopilot": True,
        "dry_run": True,
        "afk": True,
        "engine_reload": True,
    }
    assert len(session._process.starts) == 2


def test_shutdown_cancels_pending_reload(session, qapp):
    session.start()
    session.restart()
    session._process.finish()
    session.shutdown()
    qapp.processEvents()
    assert len(session._process.starts) == 1


def test_startup_events_are_retained_and_failure_is_visible(session, monkeypatch):
    statuses = []
    session.startupStatusChanged.connect(statuses.append)
    event = {"phase": "initializing_client", "message": "Preparing LLM client", "ready": False}
    session._handle_process_event({"type": "startup_status", "data": event})
    assert session.last_startup_status == event
    assert statuses == [event]

    def fail(**_options):
        raise RuntimeError("missing Python")

    monkeypatch.setattr(session._process, "start", fail)
    session.start()
    assert session.last_startup_status["phase"] == "error"
    assert "missing Python" in session.last_startup_status["message"]


@pytest.fixture
def child_runtime(tmp_path, monkeypatch):
    """Use a tiny local child; never launch the real coach or contact a model."""
    package = tmp_path / "src" / "arenamcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "standalone.py").write_text(
        "import json, os, sys, time\n"
        "print(json.dumps({'reload': os.getenv('ARENAMCP_ENGINE_RELOAD'), 'proactive': os.getenv('ARENAMCP_PROACTIVE_ONLY'), 'args': sys.argv[1:]}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    if json.loads(line).get('cmd') == 'prepare_engine_reload':\n"
        "        time.sleep(0.05)\n"
        "        break\n"
    )
    monkeypatch.setattr("arenamcp.desktop.coach_process.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("arenamcp.desktop.coach_process.get_runtime_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        "arenamcp.desktop.coach_process.find_python_executable", lambda: (sys.executable, "test")
    )
    return tmp_path


@pytest.mark.parametrize("engine_reload, expected", [(True, "1"), (False, None)])
def test_child_env_and_no_overlap_during_reload(qapp, child_runtime, monkeypatch, engine_reload, expected):
    monkeypatch.setenv("ARENAMCP_ENGINE_RELOAD", "stale")
    process = CoachProcess()
    events = []
    exits = []
    process.event_received.connect(events.append)
    process.exited.connect(exits.append)
    try:
        process.start(autopilot=True, dry_run=True, afk=True, engine_reload=engine_reload)
        child = process._process
        process.stop_async(command="prepare_engine_reload")
        assert process.is_running
        process.start()  # Must not replace the child while checkpointing.
        assert process._process is child
        assert child.waitForFinished(3000)
        assert not process.is_running
        assert exits == [0]
        assert events == [
            {"reload": expected, "proactive": "1", "args": ["--pipe", "--autopilot", "--dry-run", "--afk"]}
        ]
    finally:
        process.stop()


def test_stale_exit_signal_does_not_drop_current_child(qapp, child_runtime):
    process = CoachProcess()
    stale = QProcess()
    stale.finished.connect(process._on_finished)
    try:
        process.start()
        child = process._process
        stale.finished.emit(0, QProcess.NormalExit)
        assert process._process is child
        assert process.is_running
    finally:
        process.stop()
