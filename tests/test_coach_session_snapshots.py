"""CoachSession <-> runtime-snapshot handoff: adopt, defer, roll back.

The cross-version test at the bottom launches *real* interpreter subprocesses
against really published snapshot directories and asserts which code they
import -- process/import paths, not string fixtures.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.coach_session import CoachSession

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


@pytest.fixture()
def session(qapp, monkeypatch):
    """A CoachSession that believes an engine is running and never spawns one."""
    sess = CoachSession()
    sess._fake_running = True
    monkeypatch.setattr(CoachSession, "is_running", property(lambda self: self._fake_running))
    sess._pending_restart = None
    sess._shutting_down = False
    yield sess
    sess._pending_restart = None
    sess._shutting_down = True


def _emit_reload(session, reason="engine code changed"):
    session._handle_process_event({"type": "request_engine_reload", "reason": reason})


def _plan(action, root=None, commit=COMMIT_B, reason=""):
    return SimpleNamespace(
        action=action, reason=reason, target_root=root, target_commit=commit
    )


# ---------------------------------------------------------------------------
# reload planning at the safe checkpoint


def test_engine_reload_adopts_compatible_snapshot(session, monkeypatch, tmp_path):
    """Compatible published snapshot: arm the handoff, then reload."""
    target_root = tmp_path / "snapshots" / f"{COMMIT_B[:12]}-deadbeef"
    target_root.mkdir(parents=True)
    monkeypatch.setattr(
        session,
        "_plan_snapshot_reload",
        lambda: _plan("adopt", root=str(target_root), reason="newer compatible engine"),
    )
    reloads = []
    session.engineReloadRequested.connect(reloads.append)

    _emit_reload(session)

    assert session._pending_snapshot_target == (str(target_root), COMMIT_B, str(target_root))
    assert reloads == ["engine code changed"]


def test_engine_reload_incompatible_requires_visible_restart(session, monkeypatch):
    """Unknown/incompatible protocol: current engine keeps running, UI says why."""
    monkeypatch.setattr(
        session,
        "_plan_snapshot_reload",
        lambda: _plan(
            "require_full_restart",
            reason="published engine protocol [2, 0] is incompatible with UI [1, 0]",
        ),
    )
    statuses = []
    session.startupStatusChanged.connect(statuses.append)
    reloads = []
    session.engineReloadRequested.connect(reloads.append)

    _emit_reload(session)

    assert session._pending_snapshot_target is None
    assert reloads == [], "must never silently reload into an incompatible engine"
    assert statuses[-1]["phase"] == "restart_required"
    assert statuses[-1]["ready"] is True
    assert "incompatible" in statuses[-1]["message"]


def test_engine_reload_unmanaged_behaves_like_before(session, monkeypatch):
    """No snapshot infrastructure: plain engine reload, no handoff armed."""
    monkeypatch.setattr(session, "_plan_snapshot_reload", lambda: _plan("reload_unmanaged"))
    reloads = []
    session.engineReloadRequested.connect(reloads.append)

    _emit_reload(session)

    assert session._pending_snapshot_target is None
    assert len(reloads) == 1


def test_engine_reload_suppressed_while_restart_pending(session, monkeypatch):
    """A reload already under way wins: never a second engine."""
    called = []
    monkeypatch.setattr(session, "_plan_snapshot_reload", lambda: called.append(1))
    session._pending_restart = {}
    reloads = []
    session.engineReloadRequested.connect(reloads.append)

    _emit_reload(session)

    assert called == []
    assert reloads == []


# ---------------------------------------------------------------------------
# selection identity moves as one unit (SHA + APP_ROOT + SNAPSHOT_ROOT)


def _identity():
    return (
        os.environ.get("MTGACOACH_APP_ROOT"),
        os.environ.get("MTGACOACH_SNAPSHOT_SHA"),
        os.environ.get("MTGACOACH_SNAPSHOT_ROOT"),
    )


def test_finish_restart_switches_whole_identity(session, monkeypatch, tmp_path):
    """Adoption moves APP_ROOT + SNAPSHOT_SHA + SNAPSHOT_ROOT together.

    Regression guard for the reviewer finding: a new SHA paired with a stale
    SNAPSHOT_ROOT let readers trust the wrong tree.
    """
    session._fake_running = False
    stale_root = tmp_path / "snapshots" / "old"
    target_root = tmp_path / "snapshots" / f"{COMMIT_B[:12]}-deadbeef"
    stale_root.mkdir(parents=True)
    target_root.mkdir(parents=True)
    os.environ["MTGACOACH_APP_ROOT"] = str(stale_root)
    os.environ["MTGACOACH_SNAPSHOT_SHA"] = COMMIT_A
    os.environ["MTGACOACH_SNAPSHOT_ROOT"] = str(stale_root)
    session._pending_restart = {"some": "option"}
    session._pending_snapshot_target = (str(target_root), COMMIT_B, str(target_root))
    started = []
    monkeypatch.setattr(session, "start", lambda **kw: started.append(kw) or True)

    try:
        session._finish_restart()
        identity = _identity()
    finally:
        for key in ("MTGACOACH_APP_ROOT", "MTGACOACH_SNAPSHOT_SHA", "MTGACOACH_SNAPSHOT_ROOT"):
            os.environ.pop(key, None)

    assert started == [{"some": "option", "engine_reload": True}]
    assert identity == (str(target_root), COMMIT_B, str(target_root))
    assert session._pending_restart is None
    assert session._pending_snapshot_target is None


def test_finish_restart_rolls_back_whole_identity_when_start_fails(session, monkeypatch, tmp_path):
    """Failed adoption restores the previous root: current engine stays usable."""
    session._fake_running = False
    previous_root = tmp_path / "snapshots" / f"{COMMIT_A[:12]}-cafef00d"
    target_root = tmp_path / "snapshots" / f"{COMMIT_B[:12]}-deadbeef"
    previous_root.mkdir(parents=True)
    target_root.mkdir(parents=True)
    os.environ["MTGACOACH_APP_ROOT"] = str(previous_root)
    os.environ["MTGACOACH_SNAPSHOT_SHA"] = COMMIT_A
    os.environ["MTGACOACH_SNAPSHOT_ROOT"] = str(previous_root)
    session._pending_restart = {}
    session._pending_snapshot_target = (str(target_root), COMMIT_B, str(target_root))
    calls = []

    def fake_start(**kw):
        calls.append(kw)
        return len(calls) > 1  # first (adopted) start fails, rollback succeeds

    monkeypatch.setattr(session, "start", fake_start)
    errors = []
    session.logEmitted.connect(lambda msg, level: errors.append((msg, level)))

    try:
        session._finish_restart()
        identity_after = _identity()
    finally:
        for key in ("MTGACOACH_APP_ROOT", "MTGACOACH_SNAPSHOT_SHA", "MTGACOACH_SNAPSHOT_ROOT"):
            os.environ.pop(key, None)

    assert len(calls) == 2
    assert identity_after == (str(previous_root), COMMIT_A, str(previous_root))
    assert errors, "the user must see why the adoption failed"
    assert any("rolling back" in text for text, _level in errors)


def test_finish_restart_clears_identity_when_previous_was_checkout(session, monkeypatch, tmp_path):
    """Coming from a plain checkout: a failed adoption returns to no snapshot env."""
    session._fake_running = False
    target_root = tmp_path / "snapshots" / f"{COMMIT_B[:12]}-deadbeef"
    target_root.mkdir(parents=True)
    for key in ("MTGACOACH_APP_ROOT", "MTGACOACH_SNAPSHOT_SHA", "MTGACOACH_SNAPSHOT_ROOT"):
        os.environ.pop(key, None)
    session._pending_restart = {}
    session._pending_snapshot_target = (str(target_root), COMMIT_B, str(target_root))
    calls = []

    def fake_start(**kw):
        calls.append(kw)
        return len(calls) > 1

    monkeypatch.setattr(session, "start", fake_start)

    try:
        session._finish_restart()
        identity_after = _identity()
    finally:
        for key in ("MTGACOACH_APP_ROOT", "MTGACOACH_SNAPSHOT_SHA", "MTGACOACH_SNAPSHOT_ROOT"):
            os.environ.pop(key, None)

    assert len(calls) == 2
    assert identity_after == (None, None, None)


def test_finish_restart_noop_without_pending(session):
    """No pending restart: _finish_restart must not touch env or start."""
    started = []
    before = _identity()
    orig_start = session.start
    session.start = lambda **kw: started.append(kw) or True  # type: ignore[method-assign]

    session._finish_restart()

    session.start = orig_start  # type: ignore[method-assign]
    assert started == []
    assert _identity() == before


# ---------------------------------------------------------------------------
# READY/handshake-gated rollback


def test_ready_watchdog_rolls_back_when_adopted_engine_never_ready(session, monkeypatch):
    """Spawn-alone success is never adoption success: no READY -> rollback."""
    session._fake_running = False
    previous = ("/prev/root", COMMIT_A, "/prev/root")
    os.environ["MTGACOACH_APP_ROOT"] = "/adopted/root"
    os.environ["MTGACOACH_SNAPSHOT_SHA"] = COMMIT_B
    os.environ["MTGACOACH_SNAPSHOT_ROOT"] = "/adopted/root"
    session._last_startup_status = {"ready": False}
    session._last_start_options = {"autopilot": True}
    started = []
    monkeypatch.setattr(session, "start", lambda **kw: started.append(kw) or True)

    try:
        session._arm_ready_watchdog(previous)
        session._ready_watchdog_fired(session._adoption_generation, COMMIT_B, previous)
        identity_after = _identity()
    finally:
        for key in ("MTGACOACH_APP_ROOT", "MTGACOACH_SNAPSHOT_SHA", "MTGACOACH_SNAPSHOT_ROOT"):
            os.environ.pop(key, None)

    assert started == [{"autopilot": True, "engine_reload": True}]
    assert identity_after == previous


def test_ready_watchdog_keeps_adopted_engine_once_ready(session, monkeypatch):
    """A READY adopted engine is a success: no rollback start."""
    session._fake_running = False
    previous = ("/prev/root", COMMIT_A, "/prev/root")
    os.environ["MTGACOACH_SNAPSHOT_SHA"] = COMMIT_B
    session._last_startup_status = {"ready": True}
    started = []
    monkeypatch.setattr(session, "start", lambda **kw: started.append(kw) or True)

    try:
        session._arm_ready_watchdog(previous)
        session._ready_watchdog_fired(session._adoption_generation, COMMIT_B, previous)
    finally:
        os.environ.pop("MTGACOACH_SNAPSHOT_SHA", None)

    assert started == []


def test_ready_watchdog_stops_running_engine_then_rolls_back_on_exit(session, monkeypatch):
    """Running adopted engine that never becomes READY: graceful stop first,
    then the exited handler restarts the previous identity -- never two engines."""
    session._fake_running = True
    previous = ("/prev/root", COMMIT_A, "/prev/root")
    os.environ["MTGACOACH_APP_ROOT"] = "/adopted/root"
    os.environ["MTGACOACH_SNAPSHOT_SHA"] = COMMIT_B
    os.environ["MTGACOACH_SNAPSHOT_ROOT"] = "/adopted/root"
    session._last_startup_status = {"ready": False}
    session._last_start_options = {"afk": True}
    stops = []

    monkeypatch.setattr(session._process, "stop_async", lambda *a, **kw: stops.append(1))

    try:
        session._arm_ready_watchdog(previous)
        session._ready_watchdog_fired(session._adoption_generation, COMMIT_B, previous)
        assert stops == [1], "the adopted engine must be stopped gracefully first"
        assert session._pending_rollback is not None

        # The engine exits; the exited handler performs the rollback start.
        session._fake_running = False
        session._handle_exited(0)
        identity_after = _identity()
    finally:
        for key in ("MTGACOACH_APP_ROOT", "MTGACOACH_SNAPSHOT_SHA", "MTGACOACH_SNAPSHOT_ROOT"):
            os.environ.pop(key, None)

    assert identity_after == previous


def test_ready_watchdog_ignores_stale_generation(session, monkeypatch):
    """A newer adoption supersedes an older watchdog: no double rollback."""
    session._fake_running = False
    previous = ("/prev/root", COMMIT_A, "/prev/root")
    session._last_startup_status = {"ready": False}
    started = []
    monkeypatch.setattr(session, "start", lambda **kw: started.append(kw) or True)

    session._arm_ready_watchdog(previous)
    stale_generation = session._adoption_generation - 1
    session._ready_watchdog_fired(stale_generation, COMMIT_B, previous)

    assert started == []


# ---------------------------------------------------------------------------
# cross-version real subprocess import paths


def _publish_snapshot(tmp_path, label, engine_value, snaps_dir):
    """Build a real git repo, stage + promote + publish it; return its root."""
    from arenamcp.desktop import runtime_snapshots as rs

    root = tmp_path / f"repo-{label}"
    (root / "src" / "arenamcp" / "desktop").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "mtgacoach"\n', encoding="utf-8")
    (root / "src" / "arenamcp" / "__init__.py").write_text('__version__ = "1"\n', encoding="utf-8")
    (root / "src" / "arenamcp" / "desktop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "arenamcp" / "desktop" / "engine.py").write_text(
        f"ENGINE_VALUE = {engine_value}\n", encoding="utf-8"
    )
    (root / "src" / "arenamcp" / "desktop" / "app.py").write_text(
        f"APP_LABEL = '{label}'\n", encoding="utf-8"
    )
    for args in (("init", "-q"), ("add", "-A")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@e.c", "-c", "user.name=t", "commit", "-qm", label],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    staging = rs.stage_snapshot(root, commit, staging_parent=snaps_dir)
    final = rs.promote_snapshot(staging, snaps_dir)
    rs.publish_pointer(final, snaps_dir)
    return final, commit


_PHASE_B_PROBE = (
    "import os, sys\n"
    "snapshot_root = os.environ['MTGACOACH_SNAPSHOT_ROOT']\n"
    "sys.path[:] = [os.path.join(snapshot_root, 'src')]\n"
    "import arenamcp, arenamcp.desktop.app as app\n"
    "print(app.APP_LABEL, arenamcp.__file__, app.__file__)\n"
)


def test_phase_b_child_imports_only_from_its_snapshot(tmp_path):
    """Real subprocesses: each snapshot serves its own code; a hostile
    PYTHONPATH pointing at the checkout cannot leak a single module in."""
    snaps = tmp_path / "snaps"
    snap_old, commit_old = _publish_snapshot(tmp_path, "oldgen", 1, snaps)
    snap_new, commit_new = _publish_snapshot(tmp_path, "newgen", 2, snaps)
    assert commit_old != commit_new


    # The mutable checkout this test process is running from -- hostile env.
    checkout_src = str(tmp_path / "hostile-checkout-src")
    os.makedirs(checkout_src, exist_ok=True)
    (Path(checkout_src) / "arenamcp").mkdir()
    (Path(checkout_src) / "arenamcp" / "__init__.py").write_text(
        "APP_LABEL = 'LEAKED'\n", encoding="utf-8"
    )

    base_env = dict(os.environ)
    base_env["PYTHONPATH"] = checkout_src  # adversarial: must be ignored by phase B

    results = {}
    for label, snap in (("oldgen", snap_old), ("newgen", snap_new)):
        env = dict(base_env)
        env["MTGACOACH_SNAPSHOT_SHA"] = commit_old if label == "oldgen" else commit_new
        env["MTGACOACH_SNAPSHOT_ROOT"] = str(snap)
        proc = subprocess.run(
            [sys.executable, "-c", _PHASE_B_PROBE],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
        app_label, arenamcp_file, app_file = proc.stdout.split()
        results[label] = (app_label, arenamcp_file, app_file)

    # Each child imported ITS OWN snapshot's code, never the checkout's.
    assert results["oldgen"][0] == "oldgen"
    assert results["newgen"][0] == "newgen"
    for label, snap in (("oldgen", snap_old), ("newgen", snap_new)):
        app_label, arenamcp_file, app_file = results[label]
        assert arenamcp_file.startswith(str(snap)), (label, arenamcp_file)
        assert app_file.startswith(str(snap)), (label, app_file)
        assert "LEAKED" not in app_label


def test_phase_b_children_get_distinct_engine_versions(tmp_path):
    """Old UI process + new engine snapshot: the child really runs new code."""
    snaps = tmp_path / "snaps"
    _snap_old, commit_old = _publish_snapshot(tmp_path, "oldgen", 1, snaps)
    snap_new, commit_new = _publish_snapshot(tmp_path, "newgen", 2, snaps)

    engine_probe = (
        "import os, sys\n"
        "sys.path[:] = [os.path.join(os.environ['MTGACOACH_SNAPSHOT_ROOT'], 'src')]\n"
        "import arenamcp.desktop.engine as engine\n"
        "print(engine.ENGINE_VALUE)\n"
    )
    values = {}
    for label, snap, commit in (("old", _snap_old, commit_old), ("new", snap_new, commit_new)):
        env = dict(os.environ)
        env["MTGACOACH_SNAPSHOT_SHA"] = commit
        env["MTGACOACH_SNAPSHOT_ROOT"] = str(snap)
        proc = subprocess.run(
            [sys.executable, "-c", engine_probe],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
        values[label] = int(proc.stdout.strip())

    assert values == {"old": 1, "new": 2}
