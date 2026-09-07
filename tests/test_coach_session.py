from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.coach_session import CoachSession


def test_coach_session_bug_report_saved_event(qapp):
    session = CoachSession()
    received = []
    session.bugReportSaved.connect(lambda path, err: received.append((path, err)))

    event = {
        "type": "bug_report_saved",
        "path": "/path/to/bug_test.json",
        "screenshots": {},
    }
    session._handle_process_event(event)

    assert len(received) == 1
    assert received[0] == ("/path/to/bug_test.json", "")
    session.shutdown()


def test_coach_session_offline_debug_report(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    session = CoachSession()
    received = []
    session.bugReportSaved.connect(lambda path, err: received.append((path, err)))

    # Subprocess is not running -> triggers offline fallback
    session.trigger_debug_report()

    assert len(received) == 1
    path, err = received[0]
    assert err == ""
    assert Path(path).exists()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["reason"] == "Desktop Bug Report (offline)"
    session.shutdown()
