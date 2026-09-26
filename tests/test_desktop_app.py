from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from arenamcp import __version__
from arenamcp.desktop import app as desktop_app


def test_restart_exit_code_defined() -> None:
    assert hasattr(desktop_app, "RESTART_EXIT_CODE")
    assert desktop_app.RESTART_EXIT_CODE == 42


@pytest.mark.parametrize("entrypoint", ["/repo/src/arenamcp/desktop/__main__.py", "/venv/bin/mtgacoach-desktop"])
def test_posix_restart_uses_module_not_a_package_file(monkeypatch, entrypoint):
    release = MagicMock()
    execute = MagicMock()
    monkeypatch.setattr(desktop_app, "_release_single_instance_lock", release)
    monkeypatch.setattr(desktop_app, "_write_log", lambda message: None)
    monkeypatch.setattr(desktop_app.os, "execv", execute)
    monkeypatch.setattr(desktop_app.sys, "platform", "darwin")
    monkeypatch.setattr(desktop_app.sys, "executable", "/local venv/bin/python")
    monkeypatch.setattr(desktop_app.sys, "argv", [entrypoint, "--custom-option"])
    monkeypatch.setattr(desktop_app.sys, "frozen", False, raising=False)

    desktop_app.relaunch_application()

    release.assert_called_once()
    execute.assert_called_once_with(
        "/local venv/bin/python", ["/local venv/bin/python", "-m", "arenamcp.desktop", "--custom-option"]
    )


@pytest.mark.parametrize("frozen", [False, True])
def test_windows_restart_relaunches_once_without_duplicating_executable(monkeypatch, frozen):
    import subprocess

    launch = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(desktop_app, "_release_single_instance_lock", MagicMock())
    monkeypatch.setattr(desktop_app, "_write_log", lambda message: None)
    monkeypatch.setattr(desktop_app.sys, "platform", "win32")
    monkeypatch.setattr(desktop_app.sys, "executable", "coach.exe")
    monkeypatch.setattr(desktop_app.sys, "argv", ["coach.exe", "--custom-option"])
    monkeypatch.setattr(desktop_app.sys, "frozen", frozen, raising=False)

    with pytest.raises(SystemExit) as result:
        desktop_app.relaunch_application()

    assert result.value.code == 0
    module_args = [] if frozen else ["-m", "arenamcp.desktop"]
    launch.assert_called_once_with(["coach.exe", *module_args, "--custom-option"])


def test_restore_existing_instance_window_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(desktop_app.os, "name", "posix")
    assert desktop_app._restore_existing_instance_window() is False


@pytest.mark.skipif(os.name != "nt", reason="Windows ctypes test")
def test_restore_existing_instance_window_finds_by_title(monkeypatch) -> None:
    mock_user32 = MagicMock()
    mock_user32.FindWindowW.side_effect = lambda class_name, title: 12345 if title == f"mtgacoach v{__version__}" else 0

    monkeypatch.setattr("ctypes.windll.user32", mock_user32)

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
