import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

from arenamcp.desktop.dev_source import source_revision


def load(path):
    loader = importlib.machinery.SourceFileLoader(path.stem, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_local_launcher_keeps_shared_source_and_restart_environment(monkeypatch, tmp_path):
    module = load(Path(__file__).resolve().parents[1] / "installer/windows-dev/launcher.pyw")
    checkout = tmp_path / "Shared source"
    entry = checkout / "scripts/launch_desktop.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    run = Mock()
    monkeypatch.setattr(module.runpy, "run_path", run)
    monkeypatch.setenv("PYTHONHOME", "old")
    monkeypatch.setenv("MTGACOACH_VELOPACK_PENDING", "old")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", ["launcher.pyw", "--version"])
    monkeypatch.chdir(tmp_path)
    # Environment changes are scoped to this test, including those made by launch().
    for key in (
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "MTGACOACH_DEV_CHECKOUT",
        "MTGACOACH_BUILD_LABEL",
        "MTGACOACH_DEV_GIT_DIR",
    ):
        monkeypatch.setenv(key, "old")
    module.launch({"checkout": str(checkout), "git_dir": str(tmp_path / "git")})
    assert module.os.environ["PYTHONPATH"] == str(checkout / "src")
    assert module.os.environ["MTGACOACH_DEV_CHECKOUT"] == str(checkout)
    assert "PYTHONHOME" not in module.os.environ
    assert "MTGACOACH_VELOPACK_PENDING" not in module.os.environ
    assert sys.argv == [str(entry), "--version"]
    run.assert_called_once_with(str(entry), run_name="__main__")


def test_installer_uses_local_python_and_never_targets_master_shortcut(monkeypatch, tmp_path):
    installer = load(Path(__file__).resolve().parents[1] / "scripts/install_windows_dev_launcher.py")
    checkout = tmp_path / "Share"
    entry = checkout / "scripts/launch_desktop.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    python = tmp_path / "Local Python/python.exe"
    python.parent.mkdir()
    python.with_name("pythonw.exe").touch()
    run = Mock(return_value=Mock(stdout="Dev shortcut"))
    monkeypatch.setattr(installer.subprocess, "run", run)
    destination = tmp_path / "Local launcher"
    result = installer.install(checkout, python, destination)
    config = json.loads((destination / "launcher.json").read_text())
    assert config["checkout"] == str(checkout)
    assert result.is_file()
    env = run.call_args.kwargs["env"]
    assert env["COACH_DEV_PYTHONW"] == str(python.with_name("pythonw.exe"))
    assert "MTGA Coach (Dev).lnk" in installer.SHORTCUT_SCRIPT
    assert "master" not in installer.SHORTCUT_SCRIPT


def test_shared_worktree_revision_without_git_executable(monkeypatch, tmp_path):
    checkout = tmp_path / "Share/checkout"
    checkout.mkdir(parents=True)
    metadata = tmp_path / "Share/repository/.git/worktrees/checkout"
    metadata.mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: /Volumes/repos/repository/.git/worktrees/checkout\n")
    (metadata / "HEAD").write_text("ref: refs/heads/dev\n")
    (metadata / "commondir").write_text("../..\n")
    common = metadata.parents[1]
    (common / "packed-refs").write_text("# pack-refs\n" + "a" * 40 + " refs/heads/dev\n")
    monkeypatch.setenv("MTGACOACH_DEV_GIT_DIR", str(metadata))
    assert source_revision(str(checkout)) == "aaaaaaa"
    branch = common / "refs/heads/dev"
    branch.parent.mkdir(parents=True)
    branch.write_text("b" * 40 + "\n")
    assert source_revision(str(checkout)) == "bbbbbbb"
    (metadata / "HEAD").write_text("c" * 40 + "\n")
    assert source_revision(str(checkout)) == "ccccccc"


def test_regular_checkout_and_missing_share(monkeypatch, tmp_path):
    monkeypatch.delenv("MTGACOACH_DEV_GIT_DIR", raising=False)
    assert source_revision(str(tmp_path)) == "unknown commit"
    metadata = tmp_path / ".git"
    metadata.mkdir()
    (metadata / "HEAD").write_text("d" * 40)
    assert source_revision(str(tmp_path)) == "ddddddd"
