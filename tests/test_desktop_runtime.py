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
