from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.desktop import runtime


@pytest.mark.parametrize("image", ["MTGA.exe", "MTGALauncher.exe"])
def test_shortcut_leaves_running_game_or_patcher_alone(monkeypatch, image):
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")
    monkeypatch.setattr(
        runtime.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=f'"{image}","12"'))
    )
    lookup = Mock()
    monkeypatch.setattr("arenamcp.platform_integration.find_mtga", lookup)
    assert "already open" in runtime.launch_windows_arena()
    lookup.assert_not_called()


def test_android_coaching_does_not_launch_desktop_arena(monkeypatch):
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "android")
    probe = Mock()
    monkeypatch.setattr(runtime.subprocess, "run", probe)
    assert runtime.launch_windows_arena() is None
    probe.assert_not_called()


@pytest.mark.parametrize("kind", ["wizards", "standalone", "steam"])
def test_shortcut_uses_the_detected_distribution(monkeypatch, tmp_path, kind):
    game = tmp_path / ("Steam/steamapps/common/MTGA" if kind == "steam" else "MTGA")
    game.mkdir(parents=True)
    executable = game / "MTGA.exe"
    executable.touch()
    if kind == "wizards":
        executable = game / "MTGALauncher/MTGALauncher.exe"
        executable.parent.mkdir()
        executable.touch()
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")
    monkeypatch.setattr(runtime.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="")))
    monkeypatch.setattr("arenamcp.platform_integration.find_mtga", lambda: SimpleNamespace(install_dir=game))
    spawn, shell = Mock(), Mock()
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime.os, "startfile", shell, raising=False)
    assert runtime.launch_windows_arena() == "Starting Arena."
    if kind == "steam":
        shell.assert_called_once_with("steam://rungameid/2141910")
        spawn.assert_not_called()
    else:
        spawn.assert_called_once_with([str(executable)], cwd=str(executable.parent))
        shell.assert_not_called()


@pytest.mark.parametrize("managed", [False, True])
def test_windows_startup_launches_arena_for_dev_and_managed(monkeypatch, managed):
    from arenamcp.desktop import app
    from arenamcp.repair_engine import RepairEngine

    monkeypatch.delenv("MTGACOACH_VELOPACK_PENDING", raising=False)
    if managed:
        monkeypatch.setenv("MTGACOACH_VELOPACK_PENDING", "pending.json")
    else:
        monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", "Z:/mtgacoach-dev")
    repair = Mock(return_value=[])
    launch = Mock(return_value="Starting Arena.")
    monkeypatch.setattr(RepairEngine, "managed_windows_bridge", repair)
    monkeypatch.setattr(runtime, "launch_windows_arena", launch)
    log = Mock()
    app._launch_windows_session(log)
    launch.assert_called_once_with()
    assert repair.call_count == int(managed)
    log.assert_called_once_with("Starting Arena.", "status")


def test_source_startup_reports_arena_launch_failure(monkeypatch):
    from arenamcp.desktop import app

    monkeypatch.delenv("MTGACOACH_VELOPACK_PENDING", raising=False)
    monkeypatch.setattr(
        runtime, "launch_windows_arena", Mock(side_effect=FileNotFoundError("Arena installation not found"))
    )
    log = Mock()
    app._launch_windows_session(log)
    message, kind = log.call_args.args
    assert "Arena installation not found" in message
    assert "Setup & Repair" in message
    assert kind == "error"
