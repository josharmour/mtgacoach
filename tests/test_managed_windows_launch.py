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
