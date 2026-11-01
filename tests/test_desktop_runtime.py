from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from arenamcp.desktop import coach_process, runtime


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
    preferred = app / "runtime" / "python.exe"
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


def test_legacy_scripts_runtime_is_never_selected(monkeypatch, tmp_path: Path) -> None:
    app = tmp_path / "app"
    legacy = app / "runtime" / "Scripts" / "python.exe"
    legacy.parent.mkdir(parents=True)
    legacy.touch()
    current = tmp_path / "python.exe"
    current.touch()
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.sys, "executable", str(current))
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime, "get_app_root", lambda: str(app))
    monkeypatch.setattr(runtime, "get_runtime_root", lambda: str(tmp_path / "local"))
    monkeypatch.setattr(runtime, "_PYTHON_PROBE_CACHE", {})

    def unexpected_probe(*args, **kwargs):
        raise AssertionError("obsolete runtime/Scripts interpreter must not be probed")

    monkeypatch.setattr(runtime.subprocess, "run", unexpected_probe)
    assert runtime.find_python_executable() == (str(current), "current")


def test_packaged_runtime_requires_root_interpreter_and_manifest(monkeypatch, tmp_path: Path) -> None:
    app = tmp_path / "app"
    runtime_dir = app / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "python.exe"
    exe.touch()
    manifest = runtime_dir / "runtime-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runtime.sys, "platform", "win32")

    assert runtime.is_packaged_runtime(str(exe), app)
    assert not runtime.is_packaged_runtime(str(runtime_dir / "Scripts" / "python.exe"), app)
    manifest.unlink()
    assert not runtime.is_packaged_runtime(str(exe), app)


def test_packaged_runtime_is_windows_only(monkeypatch, tmp_path: Path) -> None:
    app = tmp_path / "app"
    runtime_dir = app / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "python.exe"
    exe.touch()
    (runtime_dir / "runtime-manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runtime.sys, "platform", "darwin")

    assert not runtime.is_packaged_runtime(str(exe), app)


def test_source_checkout_with_ambient_editable_install_stays_nonisolated(monkeypatch, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "src" / "arenamcp").mkdir(parents=True)
    (checkout / "pyproject.toml").touch()
    current = checkout / ".venv" / "Scripts" / "python.exe"
    current.parent.mkdir(parents=True)
    current.touch()
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.sys, "executable", str(current))
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(runtime, "get_app_root", lambda: str(checkout))
    monkeypatch.setattr(runtime, "get_runtime_root", lambda: str(tmp_path / "local"))
    monkeypatch.setattr(runtime, "_PYTHON_PROBE_CACHE", {str(current): True})

    found, source = runtime.find_python_executable()
    assert (found, source) == (str(current), "current")
    args, packaged = coach_process._coach_launch_args(found or "", checkout)
    assert not packaged
    assert args == ["-u", "-m", "arenamcp.standalone", "--pipe"]


def test_packaged_windows_runtime_uses_isolated_flags(monkeypatch, tmp_path: Path) -> None:
    app = tmp_path / "app"
    runtime_dir = app / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "python.exe"
    exe.touch()
    (runtime_dir / "runtime-manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runtime.sys, "platform", "win32")

    args, packaged = coach_process._coach_launch_args(str(exe), app)
    assert packaged
    assert args == ["-I", "-X", "utf8", "-u", "-m", "arenamcp.standalone", "--pipe"]


class _FakeSignal:
    def connect(self, _slot) -> None:
        pass


class _FakeProcess:
    NotRunning = 0

    def __init__(self, _parent=None) -> None:
        self.env = None
        self.args: list[str] = []
        self.program = ""
        self.readyReadStandardOutput = _FakeSignal()
        self.readyReadStandardError = _FakeSignal()
        self.finished = _FakeSignal()
        self.errorOccurred = _FakeSignal()

    def state(self) -> int:
        return self.NotRunning

    def setProcessEnvironment(self, env) -> None:
        self.env = env

    def setWorkingDirectory(self, _path: str) -> None:
        pass

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, args: list[str]) -> None:
        self.args = list(args)

    def start(self) -> None:
        pass

    def waitForStarted(self, _timeout: int) -> bool:
        return True

    def errorString(self) -> str:
        return ""

    def deleteLater(self) -> None:
        pass


def test_source_checkout_coach_process_keeps_pythonpath(monkeypatch, tmp_path: Path) -> None:
    app = tmp_path / "checkout"
    (app / "src").mkdir(parents=True)
    current = tmp_path / "python.exe"
    current.touch()
    captured: list[_FakeProcess] = []

    class CapturingProcess(_FakeProcess):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            captured.append(self)

    monkeypatch.setattr(coach_process, "QProcess", CapturingProcess)
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(coach_process, "get_app_root", lambda: str(app))
    monkeypatch.setattr(coach_process, "get_runtime_root", lambda: str(tmp_path / "local"))
    monkeypatch.setattr(coach_process, "find_python_executable", lambda: (str(current), "current"))

    coach_process.CoachProcess().start()

    assert captured[0].args == ["-u", "-m", "arenamcp.standalone", "--pipe"]
    assert captured[0].env.value("PYTHONPATH") == str(app / "src")


def test_packaged_coach_process_removes_pythonpath(monkeypatch, tmp_path: Path) -> None:
    app = tmp_path / "app"
    runtime_dir = app / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "python.exe"
    exe.touch()
    (runtime_dir / "runtime-manifest.json").write_text("{}", encoding="utf-8")
    captured: list[_FakeProcess] = []

    class CapturingProcess(_FakeProcess):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            captured.append(self)

    monkeypatch.setattr(coach_process, "QProcess", CapturingProcess)
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(coach_process, "get_app_root", lambda: str(app))
    monkeypatch.setattr(coach_process, "get_runtime_root", lambda: str(tmp_path / "local"))
    monkeypatch.setattr(coach_process, "find_python_executable", lambda: (str(exe), "app_runtime"))

    coach_process.CoachProcess().start()

    assert captured[0].args == ["-I", "-X", "utf8", "-u", "-m", "arenamcp.standalone", "--pipe"]
    assert not captured[0].env.contains("PYTHONPATH")


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
