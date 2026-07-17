"""Unit tests for the macOS (darwin) branches of the desktop runtime plumbing.

All tests monkeypatch ``sys.platform`` / subprocess so they run on any host.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from arenamcp.desktop import app as desktop_app
from arenamcp.desktop import runtime


@pytest.fixture(autouse=True)
def _clean_runtime_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MTGACOACH_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("MTGA_DIR", raising=False)
    runtime._invalidate_mtga_running_cache()
    yield
    runtime._invalidate_mtga_running_cache()


def _patch_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(runtime.Path, "home", staticmethod(lambda: home))


# ---------------------------------------------------------------------------
# get_runtime_root
# ---------------------------------------------------------------------------


def test_runtime_root_darwin_uses_application_support(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    _patch_home(monkeypatch, tmp_path)

    root = runtime.get_runtime_root()

    assert root == str(tmp_path / "Library" / "Application Support" / "mtgacoach")


def test_runtime_root_darwin_keeps_populated_legacy_xdg_path(monkeypatch, tmp_path: Path) -> None:
    legacy = tmp_path / ".local" / "share" / "mtgacoach"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    _patch_home(monkeypatch, tmp_path)

    assert runtime.get_runtime_root() == str(legacy)


def test_runtime_root_darwin_ignores_empty_legacy_xdg_path(monkeypatch, tmp_path: Path) -> None:
    legacy = tmp_path / ".local" / "share" / "mtgacoach"
    legacy.mkdir(parents=True)  # exists but has no content

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    _patch_home(monkeypatch, tmp_path)

    assert runtime.get_runtime_root() == str(
        tmp_path / "Library" / "Application Support" / "mtgacoach"
    )


def test_runtime_root_linux_unchanged(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    _patch_home(monkeypatch, tmp_path)

    assert runtime.get_runtime_root() == str(tmp_path / ".local" / "share" / "mtgacoach")


def test_runtime_root_env_override_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setenv("MTGACOACH_RUNTIME_ROOT", str(tmp_path / "custom"))

    assert runtime.get_runtime_root() == str(tmp_path / "custom")


# ---------------------------------------------------------------------------
# open_path
# ---------------------------------------------------------------------------


def test_open_path_darwin_uses_usr_bin_open(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "somefile.txt"
    target.write_text("x", encoding="utf-8")

    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **_: calls.append(args))
    monkeypatch.setattr(
        runtime.shutil, "which", lambda name: pytest.fail(f"unexpected which({name!r})")
    )

    runtime.open_path(str(target))

    assert calls == [["/usr/bin/open", str(target)]]


def test_open_path_linux_still_uses_xdg_open(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "somefile.txt"
    target.write_text("x", encoding="utf-8")

    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **_: calls.append(args))

    runtime.open_path(str(target))

    assert calls == [["/usr/bin/xdg-open", str(target)]]


# ---------------------------------------------------------------------------
# is_mtga_running
# ---------------------------------------------------------------------------


def _fake_pgrep(matching_patterns: set[str], calls: list[list[str]]):
    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        calls.append(list(args))
        assert args[0] == "pgrep" and args[1] == "-f"
        if args[2] in matching_patterns:
            return SimpleNamespace(returncode=0, stdout="1234\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    return fake_run


def test_mtga_process_patterns_darwin_covers_native_and_wine(monkeypatch) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    patterns = runtime._mtga_process_patterns()
    assert patterns == [r"MTGA\.app/Contents/MacOS/MTGA", r"MTGA\.exe"]

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    assert runtime._mtga_process_patterns() == [r"MTGA\.exe"]


def test_is_mtga_running_darwin_detects_native_process(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        _fake_pgrep({r"MTGA\.app/Contents/MacOS/MTGA"}, calls),
    )

    runtime._invalidate_mtga_running_cache()
    assert runtime.is_mtga_running() is True
    # Native pattern matched first; no need to try the Wine pattern.
    assert calls == [["pgrep", "-f", r"MTGA\.app/Contents/MacOS/MTGA"]]


def test_is_mtga_running_darwin_detects_wine_process(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.subprocess, "run", _fake_pgrep({r"MTGA\.exe"}, calls))

    runtime._invalidate_mtga_running_cache()
    assert runtime.is_mtga_running() is True
    assert calls == [
        ["pgrep", "-f", r"MTGA\.app/Contents/MacOS/MTGA"],
        ["pgrep", "-f", r"MTGA\.exe"],
    ]


def test_is_mtga_running_darwin_no_match(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.subprocess, "run", _fake_pgrep(set(), calls))

    runtime._invalidate_mtga_running_cache()
    assert runtime.is_mtga_running() is False
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# close_mtga
# ---------------------------------------------------------------------------


def test_close_mtga_darwin_pkills_both_patterns(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        calls.append(list(args))
        return SimpleNamespace(returncode=1, stdout="", stderr="")  # no match is fine

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: True)
    monkeypatch.setattr(runtime, "_wait_for_mtga_exit", lambda *a, **k: None)
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    assert runtime.close_mtga() is True
    assert calls == [
        ["pkill", "-f", r"MTGA\.app/Contents/MacOS/MTGA"],
        ["pkill", "-f", r"MTGA\.exe"],
    ]


def test_close_mtga_darwin_raises_on_pkill_error(monkeypatch) -> None:
    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: True)
    monkeypatch.setattr(runtime, "_wait_for_mtga_exit", lambda *a, **k: None)
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="boom"):
        runtime.close_mtga()


def test_close_mtga_windows_taskkill_is_reachable(monkeypatch) -> None:
    """Regression: `return True` used to sit above the taskkill block, making
    it dead code — Windows never actually killed MTGA."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: True)
    monkeypatch.setattr(runtime, "_wait_for_mtga_exit", lambda *a, **k: None)
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    assert runtime.close_mtga() is True
    assert calls == [["taskkill", "/IM", "MTGA.exe", "/T", "/F"]]


def test_close_mtga_returns_false_when_not_running(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: False)
    monkeypatch.setattr(
        runtime.subprocess, "run", lambda *a, **k: pytest.fail("should not shell out")
    )

    assert runtime.close_mtga() is False


# ---------------------------------------------------------------------------
# launch_mtga
# ---------------------------------------------------------------------------


def test_launch_mtga_darwin_opens_app_bundle(monkeypatch, tmp_path: Path) -> None:
    bundle = tmp_path / "MTGA.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)

    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **_: calls.append(args))

    result = runtime.launch_mtga(str(tmp_path))

    assert calls == [["/usr/bin/open", str(bundle)]]
    assert result == str(bundle)


def test_launch_mtga_darwin_accepts_bundle_path_itself(monkeypatch, tmp_path: Path) -> None:
    bundle = tmp_path / "MTGA.app"
    bundle.mkdir()

    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **_: calls.append(args))

    result = runtime.launch_mtga(str(bundle))

    assert calls == [["/usr/bin/open", str(bundle)]]
    assert result == str(bundle)


def test_launch_mtga_darwin_falls_back_to_steam_url(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **_: calls.append(args))

    result = runtime.launch_mtga(str(tmp_path))

    assert calls == [["/usr/bin/open", "steam://rungameid/2141910"]]
    assert result == str(tmp_path)


def test_launch_mtga_darwin_steam_fallback_reports_wine_exe(monkeypatch, tmp_path: Path) -> None:
    exe = tmp_path / "MTGA.exe"
    exe.write_text("", encoding="utf-8")

    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **_: calls.append(args))

    result = runtime.launch_mtga(str(tmp_path))

    assert calls == [["/usr/bin/open", "steam://rungameid/2141910"]]
    assert result == str(exe)


def test_launch_mtga_linux_still_requires_exe(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("should not launch")
    )

    with pytest.raises(FileNotFoundError):
        runtime.launch_mtga(str(tmp_path))


# ---------------------------------------------------------------------------
# find_mtga_install_dir (darwin Steam path candidate)
# ---------------------------------------------------------------------------


def test_find_mtga_install_dir_darwin_steam_path(monkeypatch, tmp_path: Path) -> None:
    steam_dir = tmp_path / "Library/Application Support/Steam/steamapps/common/MTGA"
    steam_dir.mkdir(parents=True)

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "get_saved_mtga_dir", lambda: None)

    found, source = runtime.find_mtga_install_dir()

    assert found == str(steam_dir)
    assert source == "mac_steam_path"


# ---------------------------------------------------------------------------
# single-instance guard (app.py, POSIX flock)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(desktop_app.os.name == "nt", reason="POSIX flock guard")
def test_posix_single_instance_lock_roundtrip(monkeypatch, tmp_path: Path) -> None:
    # Lock dir intentionally does not exist yet — the guard must create it.
    lock_root = tmp_path / "runtime-root" / "nested"
    monkeypatch.setenv("MTGACOACH_RUNTIME_ROOT", str(lock_root))
    monkeypatch.setattr(desktop_app, "_INSTANCE_LOCK_FILE", None)

    assert desktop_app._acquire_single_instance_lock() is True
    assert (lock_root / "desktop.lock").exists()
    assert desktop_app._INSTANCE_LOCK_FILE is not None

    # A second acquisition (fresh fd, as another process would use) must fail.
    assert desktop_app._acquire_posix_instance_lock() is False

    desktop_app._release_single_instance_lock()
    assert desktop_app._INSTANCE_LOCK_FILE is None

    # After release the lock is acquirable again.
    assert desktop_app._acquire_single_instance_lock() is True
    desktop_app._release_single_instance_lock()


@pytest.mark.skipif(desktop_app.os.name == "nt", reason="POSIX flock guard")
def test_posix_single_instance_lock_blocks_second_holder(monkeypatch, tmp_path: Path) -> None:
    """Simulate a real second process by holding the flock on a separate fd."""
    import fcntl

    lock_root = tmp_path / "rt"
    lock_root.mkdir()
    lock_path = lock_root / "desktop.lock"
    other = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setenv("MTGACOACH_RUNTIME_ROOT", str(lock_root))
    monkeypatch.setattr(desktop_app, "_INSTANCE_LOCK_FILE", None)
    try:
        assert desktop_app._acquire_single_instance_lock() is False
        assert desktop_app._INSTANCE_LOCK_FILE is None
    finally:
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)
        other.close()


def test_release_single_instance_lock_noop_when_unheld(monkeypatch) -> None:
    monkeypatch.setattr(desktop_app, "_INSTANCE_LOCK_FILE", None)
    monkeypatch.setattr(desktop_app, "_INSTANCE_MUTEX", None)
    desktop_app._release_single_instance_lock()  # must not raise


# ── bridge_applicable / is_fully_provisioned gating ──────────────────────


def _minimal_state(**overrides):
    """Build a RuntimeState with sane defaults for gating tests."""
    from arenamcp.desktop.runtime import RuntimeState

    base = dict(
        repo_dir="/repo",
        repo_checkout=True,
        runtime_root="/rt",
        runtime_venv_dir="/rt/venv",
        runtime_venv_exists=True,
        python_exe="/rt/venv/bin/python",
        python_source="runtime_venv",
        python_ready=True,
        python_ready_detail="ok",
        mtga_dir=None,
        mtga_dir_source="none",
        mtga_exe_path=None,
        mtga_running=False,
        player_log="/tmp/Player.log",
        bepinex_log=None,
        bepinex_dir=None,
        bepinex_installed=False,
        plugin_install_path=None,
        plugin_installed=False,
        plugin_build_path=None,
        plugin_built=False,
        bepinex_bundle=None,
        restart_mtga_required=False,
    )
    base.update(overrides)
    return RuntimeState(**base)


def test_native_mac_install_is_provisioned_without_bridge(monkeypatch, tmp_path):
    """Native Mac client (no MTGA.exe) must not gate provisioning on BepInEx."""
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    (tmp_path / "MTGA.app").mkdir()
    state = _minimal_state(mtga_dir=str(tmp_path))
    assert state.bridge_applicable is False
    assert state.bridge_ready is False
    assert state.is_fully_provisioned is True


def test_crossover_bottle_still_requires_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    (tmp_path / "MTGA.exe").write_bytes(b"MZ")
    state = _minimal_state(mtga_dir=str(tmp_path))
    assert state.bridge_applicable is True
    assert state.is_fully_provisioned is False


def test_windows_still_requires_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    state = _minimal_state(mtga_dir=str(tmp_path))
    assert state.bridge_applicable is True
    assert state.is_fully_provisioned is False
    provisioned = _minimal_state(
        mtga_dir=str(tmp_path), bepinex_installed=True, plugin_installed=True
    )
    assert provisioned.is_fully_provisioned is True
