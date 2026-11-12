from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from arenamcp import __version__
from arenamcp.desktop import app as desktop_app


@pytest.mark.parametrize(
    "entrypoint", ["/repo/src/arenamcp/desktop/__main__.py", "/venv/bin/mtgacoach-desktop"]
)
def test_posix_restart_uses_launcher_handoff_with_clean_env(monkeypatch, tmp_path, entrypoint):
    """Posix Full Restart execs the launcher (which re-resolves the selection
    once) with snapshot pins and PYTHONPATH scrubbed -- never ``python -m``
    inheriting the previous selection's environment."""
    release = MagicMock()
    execute = MagicMock()
    stale_root = tmp_path / "stale" / "snapshots" / "old"
    stale_launcher = stale_root / "scripts" / "launch_desktop.py"
    stale_launcher.parent.mkdir(parents=True)
    stale_launcher.touch()
    monkeypatch.setenv("MTGACOACH_APP_ROOT", str(stale_root))
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", "a" * 40)
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_ROOT", "/stale/snapshots/old")
    monkeypatch.setenv("MTGACOACH_LAUNCH_CHILD", "1")
    monkeypatch.setenv("PYTHONPATH", "/stale/snapshots/old/src")
    monkeypatch.setattr(desktop_app, "_release_single_instance_lock", release)
    monkeypatch.setattr(desktop_app, "_write_log", lambda message: None)
    monkeypatch.setattr(desktop_app.os, "execve", execute)
    monkeypatch.setattr(desktop_app.sys, "platform", "darwin")
    monkeypatch.setattr(desktop_app.sys, "executable", "/local venv/bin/python")
    monkeypatch.setattr(desktop_app.sys, "argv", [entrypoint, "--custom-option"])
    monkeypatch.setattr(desktop_app.sys, "frozen", False, raising=False)

    desktop_app.relaunch_application()

    release.assert_called_once()
    args, kwargs = execute.call_args
    assert args[0] == "/local venv/bin/python"
    command = args[1]
    assert command[0] == "/local venv/bin/python"
    # The launcher comes from the CURRENT app root (the stale snapshot's own
    # copy), so the next launch re-resolves the newest published selection.
    assert Path(command[1]) == stale_launcher
    assert command[2:] == ["--custom-option"]
    env = kwargs.get("env", args[2] if len(args) > 2 else {})
    for key in (
        "MTGACOACH_SNAPSHOT_SHA",
        "MTGACOACH_SNAPSHOT_ROOT",
        "MTGACOACH_LAUNCH_CHILD",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
    ):
        assert key not in env


@pytest.mark.parametrize("frozen", [False, True])
def test_windows_restart_relaunches_once_without_duplicating_executable(monkeypatch, frozen):
    import subprocess

    launch = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", "a" * 40)
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_ROOT", "/stale/snapshots/old")
    monkeypatch.setenv("MTGACOACH_LAUNCH_CHILD", "1")
    monkeypatch.setenv("PYTHONPATH", "/stale/snapshots/old/src")
    monkeypatch.setattr(desktop_app, "_release_single_instance_lock", MagicMock())
    monkeypatch.setattr(desktop_app, "_write_log", lambda message: None)
    monkeypatch.setattr(desktop_app.sys, "platform", "win32")
    monkeypatch.setattr(desktop_app.sys, "executable", "coach.exe")
    monkeypatch.setattr(desktop_app.sys, "argv", ["coach.exe", "--custom-option"])
    monkeypatch.setattr(desktop_app.sys, "frozen", frozen, raising=False)

    with pytest.raises(SystemExit) as result:
        desktop_app.relaunch_application()

    assert result.value.code == 0
    args, kwargs = launch.call_args
    command = args[0]
    if frozen:
        # A frozen executable relaunches itself; argv extras carry over.
        assert command == ["coach.exe", "--custom-option"]
    else:
        # Source runs hand off to the launcher for a fresh selection.
        assert command[0] == "coach.exe"
        assert Path(command[1]).name == "launch_desktop.py"
        assert command[2:] == ["--custom-option"]
        env = kwargs["env"]
        for key in (
            "MTGACOACH_SNAPSHOT_SHA",
            "MTGACOACH_SNAPSHOT_ROOT",
            "MTGACOACH_LAUNCH_CHILD",
            "PYTHONPATH",
        ):
            assert key not in env


def test_restore_existing_instance_window_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(desktop_app.os, "name", "posix")
    assert desktop_app._restore_existing_instance_window() is False


def test_restore_existing_instance_window_finds_by_title(monkeypatch) -> None:
    import ctypes

    mock_user32 = MagicMock()
    mock_user32.FindWindowW.side_effect = lambda class_name, title: (
        12345 if title == f"mtgacoach v{__version__}" else 0
    )

    # Runs on every host: stand in for Windows and its user32.
    monkeypatch.setattr(desktop_app.os, "name", "nt")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=mock_user32), raising=False)

    assert desktop_app._restore_existing_instance_window() is True
    mock_user32.ShowWindow.assert_called_once_with(12345, 9)
    mock_user32.SetForegroundWindow.assert_called_once_with(12345)


@pytest.mark.skipif(os.name != "nt", reason="Windows ctypes test")
def test_windows_single_instance_lock_roundtrip(monkeypatch) -> None:
    monkeypatch.setattr(desktop_app, "_INSTANCE_MUTEX", None)
    try:
        acquired = desktop_app._acquire_single_instance_lock()
        assert acquired is True
    finally:
        desktop_app._release_single_instance_lock()
