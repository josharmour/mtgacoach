from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from arenamcp.desktop import runtime


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


def test_current_interpreter_needs_no_subprocess(monkeypatch, tmp_path: Path) -> None:
    current = tmp_path / "python.exe"
    monkeypatch.setattr(runtime.sys, "executable", str(current))
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime, "_PYTHON_PROBE_CACHE", {str(current): False})

    def unexpected_probe(*args, **kwargs):
        raise AssertionError("already-running interpreter must not be probed")

    monkeypatch.setattr(runtime.subprocess, "run", unexpected_probe)
    assert runtime._python_responds(current)


def test_dead_preferred_runtime_falls_back_without_probing_current(monkeypatch, tmp_path: Path) -> None:
    app = tmp_path / "app"
    preferred = app / "runtime" / "Scripts" / "python.exe"
    preferred.parent.mkdir(parents=True)
    preferred.touch()
    current = tmp_path / "python.exe"
    current.touch()
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.sys, "executable", str(current))
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime, "get_app_root", lambda: str(app))
    monkeypatch.setattr(runtime, "get_runtime_root", lambda: str(tmp_path / "local"))
    monkeypatch.setattr(runtime, "_PYTHON_PROBE_CACHE", {})
    calls = []

    def failed_probe(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=9009)

    monkeypatch.setattr(runtime.subprocess, "run", failed_probe)
    assert runtime.find_python_executable() == (str(current), "current")
    assert calls == [[str(preferred), "--version"]]


def test_frozen_executable_is_not_assumed_to_be_python(monkeypatch, tmp_path: Path) -> None:
    launcher = tmp_path / "coach.exe"
    monkeypatch.setattr(runtime.sys, "executable", str(launcher))
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime, "_PYTHON_PROBE_CACHE", {})
    calls = []

    def failed_probe(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runtime.subprocess, "run", failed_probe)
    assert not runtime._python_responds(launcher)
    assert calls == [[str(launcher), "--version"]]
