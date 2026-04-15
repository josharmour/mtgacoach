from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from arenamcp.desktop import runtime


def _make_state(**overrides: object) -> runtime.RuntimeState:
    data: dict[str, object] = {
        "repo_dir": "/tmp/mtgacoach",
        "repo_checkout": False,
        "runtime_root": "/tmp/runtime",
        "runtime_venv_dir": "/tmp/runtime/venv",
        "runtime_venv_exists": False,
        "python_exe": r"C:\Python312\python.exe",
        "python_source": "current",
        "python_ready": True,
        "python_ready_detail": "PySide6 import ok",
        "mtga_dir": r"C:\Program Files\Wizards of the Coast\MTGA",
        "mtga_dir_source": "settings",
        "mtga_exe_path": r"C:\Program Files\Wizards of the Coast\MTGA\MTGA.exe",
        "mtga_running": False,
        "player_log": r"C:\Users\test\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log",
        "bepinex_log": None,
        "bepinex_dir": r"C:\Program Files\Wizards of the Coast\MTGA\BepInEx",
        "bepinex_installed": True,
        "plugin_install_path": r"C:\Program Files\Wizards of the Coast\MTGA\BepInEx\plugins\MtgaCoachBridge.dll",
        "plugin_installed": True,
        "plugin_build_path": None,
        "plugin_built": False,
        "bepinex_bundle": None,
        "restart_mtga_required": False,
        "issues": [],
    }
    data.update(overrides)
    return runtime.RuntimeState(**data)


def test_find_python_on_path_prefers_py_launcher(monkeypatch, tmp_path: Path) -> None:
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        calls.append(args)
        if args[:2] == ["py", "-3"]:
            return SimpleNamespace(returncode=0, stdout=f"{python_exe}\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    found, source = runtime._find_python_on_path()

    assert found == str(python_exe)
    assert source == "py_launcher"
    assert calls == [["py", "-3", "-c", "import sys; print(sys.executable)"]]


def test_managed_runtime_required_for_installed_builds() -> None:
    state = _make_state(repo_checkout=False, runtime_venv_exists=False, python_source="current")

    assert state.has_ready_python_runtime is False
    assert state.is_fully_provisioned is False


def test_repo_checkout_allows_current_python_for_dev_runs() -> None:
    state = _make_state(repo_checkout=True, runtime_venv_exists=False, python_source="current")

    assert state.has_ready_python_runtime is True
    assert state.is_fully_provisioned is True


def test_restart_mtga_required_when_plugin_is_newer_than_log(tmp_path: Path) -> None:
    player_log = tmp_path / "Player.log"
    player_log.write_text("old", encoding="utf-8")

    bepinex_dir = tmp_path / "BepInEx"
    core_dir = bepinex_dir / "core"
    plugins_dir = bepinex_dir / "plugins"
    core_dir.mkdir(parents=True)
    plugins_dir.mkdir(parents=True)
    core_dll = core_dir / "BepInEx.dll"
    plugin_dll = plugins_dir / "MtgaCoachBridge.dll"
    core_dll.write_text("core", encoding="utf-8")
    plugin_dll.write_text("plugin", encoding="utf-8")

    old_time = 1000
    new_time = 2000
    player_log.touch()
    core_dll.touch()
    plugin_dll.touch()
    import os

    os.utime(player_log, (old_time, old_time))
    os.utime(core_dll, (new_time, new_time))
    os.utime(plugin_dll, (new_time, new_time))

    assert runtime._restart_mtga_required(
        player_log=player_log,
        bepinex_dir=bepinex_dir,
        plugin_install_path=plugin_dll,
    )
