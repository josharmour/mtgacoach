"""Safe engine-reload handoff: arming defers to a real boundary; cancellation wins.

The desktop's Reload Engine request must never abort an active decision.
``prepare_engine_reload`` only arms; the coaching loop checkpoints and exits at
the next between-matches boundary (``_engine_reload_checkpoint_due`` ->
``_complete_engine_reload``), and ``cancel_engine_reload`` — sent by the
desktop when it stops waiting — disarms the request so a late exit can never
stop an engine the user believes is running.

The launcher/dependency halves pin the two remaining round-3 findings: the
snapshot child keeps the interpreter's stdlib/site-packages (real subprocess
through ``scripts/launch_desktop.py``), a lock-bearing snapshot fails visibly
instead of silently falling back to loose requirements, and promoted
console-script shebangs point at the promoted environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from tests.test_standalone_coaching_loop import make_loop_coach, make_state

from arenamcp import standalone_startup
from arenamcp.desktop import dep_env
from arenamcp.desktop.dep_env import (
    ensure_dep_env,
    env_dir_for,
    env_identity,
)
from arenamcp.desktop.runtime_snapshots import SnapshotError

# --------------------------------------------------------------------------
# Arm -> boundary -> complete / cancel handshake (engine side)
# --------------------------------------------------------------------------


@pytest.fixture()
def resume_path(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "engine-resume.json"
    monkeypatch.setattr(standalone_startup, "ENGINE_RESUME_PATH", path)
    return path


@pytest.fixture()
def coach(monkeypatch):
    """A loop coach whose between-matches gate is controlled by the test.

    The gate predicates themselves are covered exhaustively by
    test_auto_reload_20261019; these tests isolate the handshake around them.
    """
    instance = make_loop_coach(make_state(match_id="m1"))
    gate = {"between": "a match is still underway"}
    monkeypatch.setattr(instance, "_auto_reload_blocker", lambda respect_rate_limit=True: "", raising=False)
    monkeypatch.setattr(
        instance, "_auto_reload_between_matches", lambda state: gate["between"], raising=False
    )
    instance.abort_concede = Mock()
    instance.ui._emit = Mock()
    instance.gate = gate  # type: ignore[attr-defined]  # test handle
    return instance


def test_arming_defers_everything_to_the_safe_boundary(coach, resume_path):
    coach.prepare_engine_reload()

    # Nothing was torn down: the engine keeps running, no checkpoint exists
    # yet, and the boundary check reports "not safe yet" mid-match.
    assert coach._running
    assert coach._engine_reload_armed
    assert not resume_path.exists()
    assert not coach._engine_reload_checkpoint_due(coach._mcp.state)
    assert "safe boundary" in coach._last_engine_reload_wait


def test_the_boundary_checkpoints_and_exits_between_matches(coach, resume_path, monkeypatch):
    import arenamcp.gamestate_persistence as gamestate_persistence

    monkeypatch.setattr(gamestate_persistence, "save_match_state", Mock())
    coach.prepare_engine_reload()
    coach.gate["between"] = ""  # the match ended; the loop reaches the boundary

    assert coach._engine_reload_checkpoint_due(coach._mcp.state)

    coach._complete_engine_reload()

    data = json.loads(resume_path.read_text(encoding="utf-8"))
    assert "saved_at" in data and "auto_queue" in data  # checkpoint written atomically
    coach.ui._emit.assert_any_call({"type": "reload_ready"})
    assert coach._restart_requested
    assert not coach._running  # run_forever exits; pipe entrypoint tears down


def test_cancellation_disarms_the_request_and_keeps_the_engine_running(coach, resume_path):
    coach.prepare_engine_reload()
    coach.cancel_engine_reload()

    # Even at a perfectly safe boundary nothing is due any more...
    coach.gate["between"] = ""
    assert not coach._engine_reload_checkpoint_due(coach._mcp.state)
    # ...and completing is a no-op that leaves the current engine up.
    coach._complete_engine_reload()
    assert not resume_path.exists()
    assert coach._running


def test_a_late_cancellation_during_completion_discards_the_checkpoint(coach, resume_path):
    coach.prepare_engine_reload()
    coach.gate["between"] = ""
    assert coach._engine_reload_checkpoint_due(coach._mcp.state)

    # The desktop cancels while the completion is in flight; the engine honours
    # it at the last moment instead of exiting anyway.
    coach._engine_reload_cancelled = True
    coach._complete_engine_reload()

    assert not resume_path.exists()
    assert coach._running


def test_cancellation_landing_inside_save_match_state_commits_the_taken_decision(
    coach, resume_path, monkeypatch
):
    """The reviewer's race: cancel fires mid-checkpoint, after the decision.

    With acknowledged cancel/commit ordering the completion — whose decision
    was taken under the reload decision lock before the cancellation landed —
    finishes definitively (checkpoint + reload_ready + exit request) instead
    of half-cancelling into a stranded handoff. The desktop retains the
    pending restart until this definitive disposition.
    """
    import arenamcp.gamestate_persistence as gamestate_persistence

    def cancel_mid_checkpoint(state):
        # The desktop's cancellation lands while the checkpoint is written —
        # after ``_complete_engine_reload`` took its decision at the boundary.
        coach._engine_reload_cancelled = True
        return Mock()

    monkeypatch.setattr(gamestate_persistence, "save_match_state", cancel_mid_checkpoint)
    coach.prepare_engine_reload()
    coach.gate["between"] = ""
    assert coach._engine_reload_checkpoint_due(coach._mcp.state)

    coach._complete_engine_reload()

    data = json.loads(resume_path.read_text(encoding="utf-8"))
    assert "saved_at" in data and "auto_queue" in data  # checkpoint committed
    coach.ui._emit.assert_any_call({"type": "reload_ready"})
    assert coach._restart_requested  # exit requested: handoff is not stranded
    assert not coach._running


# --------------------------------------------------------------------------
# Desktop side: the deferral timeout cancels the engine request
# --------------------------------------------------------------------------


def test_session_timeout_asks_the_engine_to_cancel_and_waits_for_its_confirmation(qapp, monkeypatch):
    from tests.test_engine_reload import FakeProcess

    from arenamcp.desktop.coach_session import CoachSession

    monkeypatch.setattr("arenamcp.desktop.coach_session.CoachProcess", FakeProcess)
    monkeypatch.setattr("arenamcp.desktop.coach_session.TtsManager", lambda *_: Mock(is_running=True))
    session = CoachSession()
    try:
        sent: list[tuple] = []
        monkeypatch.setattr(session, "send_command", lambda command, *args: sent.append((command, *args)))
        session.start(autopilot=True)
        session.restart()
        assert session._process.stops == [{"command": "prepare_engine_reload"}]
        assert session.last_startup_status["phase"] == "reloading"

        # The deferral timer fires with the engine still running: the engine
        # is asked to disarm, but the pending restart is RETAINED until a
        # definitive disposition so a cancellation racing the engine's own
        # commit can never strand the handoff.
        session._safe_reload_deferred(session._reload_generation)

        assert ("cancel_engine_reload",) in sent  # the engine is told to disarm
        assert session._pending_restart is not None  # retained, not dropped
        assert session._reload_disposition_pending
        assert session.is_running
        assert len(session._process.starts) == 1  # no second engine was spawned

        # The engine acknowledges the cancellation: the current engine keeps
        # running and the UI is released back to ready.
        session._handle_process_event({"type": "engine_reload_cancelled"})

        assert session._pending_restart is None
        assert not session._reload_disposition_pending
        assert session.last_startup_status["phase"] == "ready"
        assert session.is_running
        assert len(session._process.starts) == 1
    finally:
        session.shutdown()


def test_a_stray_cancellation_ack_never_drops_a_committed_handoff(qapp, monkeypatch):
    from tests.test_engine_reload import FakeProcess

    from arenamcp.desktop.coach_session import CoachSession

    monkeypatch.setattr("arenamcp.desktop.coach_session.CoachProcess", FakeProcess)
    monkeypatch.setattr("arenamcp.desktop.coach_session.TtsManager", lambda *_: Mock(is_running=True))
    session = CoachSession()
    try:
        monkeypatch.setattr(session, "send_command", lambda command, *args: None)
        session.start(autopilot=True)
        session.restart()

        # The engine commits at the boundary (reload_ready)…
        session._handle_process_event({"type": "reload_ready"})
        assert session._pending_restart is not None

        # …and only afterwards does a cancellation acknowledgement arrive.
        # Without an awaited cancellation disposition it must be ignored: the
        # committed handoff proceeds when the process exits.
        session._handle_process_event({"type": "engine_reload_cancelled"})
        assert session._pending_restart is not None

        session._process.finish()
        qapp.processEvents()
        assert len(session._process.starts) == 2  # the restart happened
    finally:
        session.shutdown()


def test_unconfirmed_cancellation_is_abandoned_visibly_after_the_grace_window(qapp, monkeypatch):
    from tests.test_engine_reload import FakeProcess

    from arenamcp.desktop.coach_session import CoachSession

    monkeypatch.setattr("arenamcp.desktop.coach_session.CoachProcess", FakeProcess)
    monkeypatch.setattr("arenamcp.desktop.coach_session.TtsManager", lambda *_: Mock(is_running=True))
    session = CoachSession()
    try:
        monkeypatch.setattr(session, "send_command", lambda command, *args: None)
        session.start(autopilot=True)
        session.restart()
        generation = session._reload_generation
        session._safe_reload_deferred(generation)
        assert session._pending_restart is not None

        # The engine never confirms either disposition: the second-window
        # fallback abandons the reload visibly instead of leaving the UI in a
        # permanent "reloading" limbo.
        session._reload_disposition_expired(generation)

        assert session._pending_restart is None
        assert not session._reload_disposition_pending
        assert session.last_startup_status["phase"] == "ready"
        assert session.is_running
        assert len(session._process.starts) == 1

        # A late ack after abandonment changes nothing.
        session._handle_process_event({"type": "engine_reload_cancelled"})
        assert session._pending_restart is None
    finally:
        session.shutdown()


# --------------------------------------------------------------------------
# Launcher: the snapshot child keeps stdlib/site-packages (real subprocess)
# --------------------------------------------------------------------------


def test_launcher_child_keeps_interpreter_stdlib_and_resolves_snapshot_imports(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    launcher = repo / "scripts" / "launch_desktop.py"
    snapshot = tmp_path / "snapshots" / ("c" * 12 + "-src")
    pkg = snapshot / "src" / "arenamcp" / "desktop"
    pkg.mkdir(parents=True)
    (snapshot / "src" / "arenamcp" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # An inert app: proves stdlib imports survive and arenamcp resolves to the
    # snapshot, without starting any UI.
    (pkg / "app.py").write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "\n"
        "def main():\n"
        "    stdlib_dir = os.path.dirname(os.__file__)\n"
        "    assert stdlib_dir in sys.path, sys.path\n"
        "    import fractions  # stdlib must survive the snapshot path insertion\n"
        "    import arenamcp\n"
        "    root = os.environ['MTGACOACH_SNAPSHOT_ROOT']\n"
        "    expected = os.path.join(root, 'src', 'arenamcp', '__init__.py')\n"
        "    assert arenamcp.__file__ == expected, arenamcp.__file__\n"
        "    print('LAUNCHER-CHILD-OK')\n"
        "    return 0\n",
        encoding="utf-8",
    )

    env = {key: value for key, value in os.environ.items() if not key.startswith(("MTGACOACH_", "PYTHON"))}
    env.update(
        {
            "MTGACOACH_LAUNCH_CHILD": "1",
            "MTGACOACH_SNAPSHOT_ROOT": str(snapshot),
            "MTGACOACH_SNAPSHOT_SHA": "c" * 40,
            "MTGACOACH_RUNTIME_ROOT": str(tmp_path / "runtime"),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(launcher)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "LAUNCHER-CHILD-OK" in proc.stdout


# --------------------------------------------------------------------------
# Dependencies: visible failure on lock problems; relocated entrypoints work
# --------------------------------------------------------------------------


def _snapshot_with_lock(snapshot_root: Path) -> None:
    (snapshot_root / "uv.lock").write_text("lock content\n", encoding="utf-8")


def test_missing_uv_with_a_lock_fails_visibly_instead_of_loose_fallback(snapshot_root, tmp_path, monkeypatch):
    _snapshot_with_lock(snapshot_root)
    monkeypatch.setattr(dep_env.shutil, "which", lambda name: None)

    with pytest.raises(SnapshotError, match="uv"):
        ensure_dep_env(snapshot_root, runtime_root=tmp_path / "runtime")

    # Nothing was promoted under the identity, so nothing can masquerade as a
    # pinned environment built from loose requirements.
    assert not env_dir_for(env_identity(snapshot_root), tmp_path / "runtime").exists()


def test_failed_lock_export_fails_visibly_instead_of_loose_fallback(snapshot_root, tmp_path, monkeypatch):
    _snapshot_with_lock(snapshot_root)
    monkeypatch.setattr(dep_env.shutil, "which", lambda name: "/usr/bin/uv")
    runs = FakeRuns(fail_step="export")
    monkeypatch.setattr(dep_env.subprocess, "run", runs)

    with pytest.raises(SnapshotError, match="export failed"):
        ensure_dep_env(snapshot_root, runtime_root=tmp_path / "runtime")

    assert not env_dir_for(env_identity(snapshot_root), tmp_path / "runtime").exists()


def test_promoted_entrypoint_shebangs_point_at_the_promoted_python(tmp_path):
    staging = tmp_path / "staging"
    promoted = tmp_path / "promoted"  # does not exist yet: rename happens later
    (staging / "bin").mkdir(parents=True)
    (staging / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    staged_python = staging / "bin" / "python"
    pip3 = staging / "bin" / "pip3"
    pip3.write_text(
        f"#!{staged_python}\nimport sys\nfrom pip._internal.cli.main import main\nsys.exit(main())\n",
        encoding="utf-8",
    )
    # An env-style launcher must be left alone: it never pointed at staging.
    activate = staging / "bin" / "activate.fish"
    activate.write_text("#!/usr/bin/env fish\nset -gx VIRTUAL_ENV x\n", encoding="utf-8")

    dep_env._repoint_entrypoint_shebangs(staging, promoted)

    text = pip3.read_text(encoding="utf-8")
    assert text.startswith(f"#!{promoted / 'bin' / 'python'}")
    assert "from pip._internal.cli.main import main" in text  # body preserved
    assert activate.read_text(encoding="utf-8").startswith("#!/usr/bin/env fish")


# --------------------------------------------------------------------------
# Shared fakes (mirrors tests/test_dep_env.py without importing its fixtures)
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRuns:
    """Records subprocess invocations; simulates venv/ensurepip/pip/probe."""

    def __init__(self, fail_step: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_step = fail_step

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.fail_step == "export" and "-o" in argv:
            raise subprocess.CalledProcessError(1, argv, stderr=b"uv export boom")
        if "-o" in argv:
            out = Path(argv[argv.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("pinned-from-lock==1\n", encoding="utf-8")

        def step_result(rc: int, err: bytes):
            if rc != 0:
                raise subprocess.CalledProcessError(rc, argv, stderr=err)
            return _Result(rc, b"", err)

        if "-m" in argv and "venv" in argv:
            target = Path(argv[-1])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            return _Result(0, b"", b"")
        if "-m" in argv and "ensurepip" in argv:
            return _Result(0, b"", b"")
        if "-m" in argv and "pip" in argv:
            return _Result(0, b"", b"")
        return _Result(0, b"ok", b"")


@pytest.fixture()
def snapshot_root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshots" / ("d" * 12 + "-src")
    (root / "src" / "arenamcp").mkdir(parents=True)
    (root / "requirements.txt").write_text("PySide6==6.7.0\n", encoding="utf-8")
    (root / "src" / "arenamcp" / "__init__.py").write_text("", encoding="utf-8")
    return root
