"""Verify launch arguments without starting Coach, Arena, or audio processes."""

from unittest.mock import Mock

from arenamcp.desktop import coach_process, runtime


def test_worktree_launch_pins_engine_source_and_keeps_shared_instance_location(qapp, monkeypatch, tmp_path):
    worktree = tmp_path / "feature"
    (worktree / "src" / "arenamcp").mkdir(parents=True)
    (worktree / "src" / "arenamcp" / "__init__.py").write_text("")
    (worktree / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    monkeypatch.setenv("MTGACOACH_APP_ROOT", str(worktree))
    monkeypatch.setenv("PYTHONPATH", "/regular-coach/src")
    monkeypatch.setenv("ARENAMCP_ENGINE_RELOAD", "1")
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    monkeypatch.setattr(coach_process, "find_python_executable", lambda: ("/shared/venv/python", "fixture"))
    monkeypatch.setattr(coach_process, "get_runtime_root", lambda: "/shared/coach-runtime")
    process = Mock()
    process.waitForStarted.return_value = True
    monkeypatch.setattr(coach_process, "QProcess", Mock(return_value=process))
    owner = coach_process.CoachProcess()
    owner.start()
    process.setWorkingDirectory.assert_called_once_with(str(worktree))
    process.setProgram.assert_called_once_with("/shared/venv/python")
    process.setArguments.assert_called_once_with(["-u", "-m", "arenamcp.standalone", "--pipe"])
    env = process.setProcessEnvironment.call_args.args[0]
    assert env.value("PYTHONPATH") == str(worktree / "src")
    assert env.value("MTGACOACH_RUNTIME_ROOT") == "/shared/coach-runtime"
    assert not env.contains("ARENAMCP_ENGINE_RELOAD")
    process.start.assert_called_once()  # Mock only; no child process exists.
