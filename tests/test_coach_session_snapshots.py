"""CoachSession <-> runtime-snapshot handoff: adopt, defer, roll back."""

from __future__ import annotations

import os
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


def test_engine_reload_adopts_compatible_snapshot(session, monkeypatch, tmp_path):
    """Compatible published snapshot: arm the handoff, then reload."""
    target_root = tmp_path / "snapshots" / f"{COMMIT_B[:12]}-deadbeef"
    target_root.mkdir(parents=True)
    plan = SimpleNamespace(
        action="adopt",
        reason="newer compatible engine",
        target_root=str(target_root),
        target_commit=COMMIT_B,
    )
    monkeypatch.setattr(session, "_plan_snapshot_reload", lambda: plan)
    reloads = []
    session.engineReloadRequested.connect(reloads.append)

    _emit_reload(session)

    assert session._pending_snapshot_target == (str(target_root), COMMIT_B)
    assert reloads == ["engine code changed"]


def test_engine_reload_incompatible_requires_visible_restart(session, monkeypatch):
    """Unknown/incompatible protocol: current engine keeps running, UI says why."""
    plan = SimpleNamespace(
        action="require_full_restart",
        reason="published engine protocol [2, 0] is incompatible with UI [1, 0]",
        target_root="/unused",
        target_commit=COMMIT_B,
    )
    monkeypatch.setattr(session, "_plan_snapshot_reload", lambda: plan)
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
    plan = SimpleNamespace(action="reload_unmanaged", reason="", target_root=None, target_commit=None)
    monkeypatch.setattr(session, "_plan_snapshot_reload", lambda: plan)
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


def test_finish_restart_switches_env_to_snapshot_root(session, monkeypatch, tmp_path):
    """Adoption hands the launcher env the absolute versioned root + SHA."""
    session._fake_running = False
    target_root = tmp_path / "snapshots" / f"{COMMIT_B[:12]}-deadbeef"
    target_root.mkdir(parents=True)
    session._pending_restart = {"some": "option"}
    session._pending_snapshot_target = (str(target_root), COMMIT_B)
    started = []
    monkeypatch.setattr(session, "start", lambda **kw: started.append(kw) or True)

    session._finish_restart()

    assert started == [{"some": "option", "engine_reload": True}]
    assert os.environ["MTGACOACH_APP_ROOT"] == str(target_root)
    assert os.environ["MTGACOACH_SNAPSHOT_SHA"] == COMMIT_B
    assert session._pending_restart is None
    assert session._pending_snapshot_target is None


def test_finish_restart_rolls_back_env_when_start_fails(session, monkeypatch, tmp_path):
    """Failed adoption restores the previous root: current engine stays usable."""
    session._fake_running = False
    previous_root = tmp_path / "snapshots" / f"{COMMIT_A[:12]}-cafef00d"
    previous_root.mkdir(parents=True)
    target_root = tmp_path / "snapshots" / f"{COMMIT_B[:12]}-deadbeef"
    target_root.mkdir(parents=True)
    os.environ["MTGACOACH_APP_ROOT"] = str(previous_root)
    os.environ["MTGACOACH_SNAPSHOT_SHA"] = COMMIT_A
    session._pending_restart = {}
    session._pending_snapshot_target = (str(target_root), COMMIT_B)
    calls = []

    def fake_start(**kw):
        calls.append(kw)
        return len(calls) > 1  # first (adopted) start fails, rollback succeeds

    monkeypatch.setattr(session, "start", fake_start)
    errors = []
    session.logEmitted.connect(lambda msg, level: errors.append((msg, level)))

    try:
        session._finish_restart()
    finally:
        os.environ.pop("MTGACOACH_APP_ROOT", None)
        os.environ.pop("MTGACOACH_SNAPSHOT_SHA", None)

    assert len(calls) == 2
    assert errors, "the user must see why the adoption failed"
    assert any("rolling back" in text for text, _level in errors)


def test_finish_restart_clears_env_when_previous_was_checkout(session, monkeypatch, tmp_path):
    """Coming from a plain checkout: a failed adoption returns to no snapshot env."""
    session._fake_running = False
    target_root = tmp_path / "snapshots" / f"{COMMIT_B[:12]}-deadbeef"
    target_root.mkdir(parents=True)
    os.environ.pop("MTGACOACH_APP_ROOT", None)
    os.environ.pop("MTGACOACH_SNAPSHOT_SHA", None)
    session._pending_restart = {}
    session._pending_snapshot_target = (str(target_root), COMMIT_B)
    calls = []

    def fake_start(**kw):
        calls.append(kw)
        return len(calls) > 1

    monkeypatch.setattr(session, "start", fake_start)

    try:
        session._finish_restart()
    finally:
        os.environ.pop("MTGACOACH_APP_ROOT", None)
        os.environ.pop("MTGACOACH_SNAPSHOT_SHA", None)

    assert len(calls) == 2


def test_finish_restart_noop_without_pending(session):
    """No pending restart: _finish_restart must not touch env or start."""
    started = []
    monkeypatch = None
    before_root = os.environ.get("MTGACOACH_APP_ROOT")
    before_sha = os.environ.get("MTGACOACH_SNAPSHOT_SHA")
    orig_start = session.start
    session.start = lambda **kw: started.append(kw) or True  # type: ignore[method-assign]

    session._finish_restart()

    session.start = orig_start  # type: ignore[method-assign]
    del monkeypatch
    assert started == []
    assert os.environ.get("MTGACOACH_APP_ROOT") == before_root
    assert os.environ.get("MTGACOACH_SNAPSHOT_SHA") == before_sha
