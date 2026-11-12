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


def test_local_launcher_delegates_to_two_phase_bootstrap(monkeypatch, tmp_path):
    """The .pyw launcher never imports arenamcp itself: it scrubs stale
    interpreter/snapshot state and delegates to scripts/launch_desktop.py in a
    fresh interpreter, whose two-phase bootstrap binds imports to the resolved
    snapshot (or coherently to the checkout)."""
    module = load(Path(__file__).resolve().parents[1] / "installer/windows-dev/launcher.pyw")
    checkout = tmp_path / "Shared source"
    entry = checkout / "scripts/launch_desktop.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    popen = Mock(return_value=Mock(wait=Mock(return_value=7)))
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setenv("PYTHONHOME", "old")
    monkeypatch.setenv("MTGACOACH_VELOPACK_PENDING", "old")
    monkeypatch.setenv("PYTHONPATH", "/stale/snapshots/old/src")
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", "a" * 40)
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_ROOT", "/stale/snapshots/old")
    monkeypatch.setenv("MTGACOACH_LAUNCH_CHILD", "1")
    monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", "old")
    monkeypatch.setenv("MTGACOACH_BUILD_LABEL", "old")

    exit_code = module.launch({"checkout": str(checkout), "git_dir": str(tmp_path / "git")})

    assert exit_code == 7
    args, kwargs = popen.call_args
    assert args[0] == [sys.executable, str(entry)]
    assert kwargs["cwd"] == str(checkout)
    env = kwargs["env"]
    assert env["MTGACOACH_DEV_CHECKOUT"] == str(checkout)
    assert env["MTGACOACH_DEV_GIT_DIR"] == str(tmp_path / "git")
    assert env["PYTHONNOUSERSITE"] == "1"
    # Stale interpreter state and previous snapshot pins never survive.
    for key in (
        "PYTHONHOME",
        "MTGACOACH_VELOPACK_PENDING",
        "PYTHONPATH",
        "MTGACOACH_SNAPSHOT_SHA",
        "MTGACOACH_SNAPSHOT_ROOT",
        "MTGACOACH_LAUNCH_CHILD",
        "MTGACOACH_SNAPSHOTS_DIR",
    ):
        assert key not in env


def test_local_launcher_disables_snapshots_when_configured_off(monkeypatch, tmp_path):
    module = load(Path(__file__).resolve().parents[1] / "installer/windows-dev/launcher.pyw")
    checkout = tmp_path / "Shared source"
    entry = checkout / "scripts/launch_desktop.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    popen = Mock(return_value=Mock(wait=Mock(return_value=0)))
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.delenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", raising=False)

    module.launch({"checkout": str(checkout), "runtime_snapshots": False})

    env = popen.call_args.kwargs["env"]
    assert env["MTGACOACH_DISABLE_RUNTIME_SNAPSHOT"] == "1"


def test_local_launcher_passes_configured_snapshots_dir(monkeypatch, tmp_path):
    module = load(Path(__file__).resolve().parents[1] / "installer/windows-dev/launcher.pyw")
    checkout = tmp_path / "Shared source"
    entry = checkout / "scripts/launch_desktop.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    popen = Mock(return_value=Mock(wait=Mock(return_value=0)))
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    snapshots_dir = tmp_path / "snapshots"

    module.launch(
        {
            "checkout": str(checkout),
            "runtime_snapshots": True,
            "snapshots_dir": str(snapshots_dir),
        }
    )

    env = popen.call_args.kwargs["env"]
    assert env["MTGACOACH_SNAPSHOTS_DIR"] == str(snapshots_dir)
    assert env.get("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT") != "1"


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
