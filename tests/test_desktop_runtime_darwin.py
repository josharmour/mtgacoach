"""Unit tests for the macOS (darwin) branches of the desktop runtime plumbing.

All tests monkeypatch ``sys.platform`` / subprocess so they run on any host.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arenamcp import platform_integration
from arenamcp.desktop import app as desktop_app
from arenamcp.desktop import runtime


@pytest.fixture(autouse=True)
def _clean_runtime_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MTGACOACH_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("MTGA_DIR", raising=False)
    monkeypatch.setattr(platform_integration, "mac_bridge_installed", lambda: False)
    monkeypatch.setattr(runtime, "_native_mtga_process", None)
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

    assert runtime.get_runtime_root() == str(tmp_path / "Library" / "Application Support" / "mtgacoach")


def test_runtime_root_linux_unchanged(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    _patch_home(monkeypatch, tmp_path)

    assert runtime.get_runtime_root() == str(tmp_path / ".local" / "share" / "mtgacoach")


def test_runtime_root_env_override_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setenv("MTGACOACH_RUNTIME_ROOT", str(tmp_path / "custom"))

    assert runtime.get_runtime_root() == str(tmp_path / "custom")


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


NATIVE, WINE = r"MTGA\.app/Contents/MacOS/MTGA", r"MTGA\.exe"


@pytest.mark.parametrize(
    ("matching", "running", "probed"),
    [
        # The native pattern matches first: no need to try the Wine pattern.
        ({NATIVE}, True, [NATIVE]),
        ({WINE}, True, [NATIVE, WINE]),
        (set(), False, [NATIVE, WINE]),
    ],
    ids=["native", "wine", "none"],
)
def test_is_mtga_running_darwin(monkeypatch, matching, running, probed) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.subprocess, "run", _fake_pgrep(matching, calls))

    runtime._invalidate_mtga_running_cache()
    assert runtime.is_mtga_running() is running
    assert calls == [["pgrep", "-f", pattern] for pattern in probed]


# ---------------------------------------------------------------------------
# launch_mtga
# ---------------------------------------------------------------------------


def test_mac_bridge_launch_executes_game_directly(monkeypatch, tmp_path):
    bundle = tmp_path / "MTGA.app"
    executable = bundle / "Contents" / "MacOS" / "MTGA"
    executable.parent.mkdir(parents=True)
    executable.touch()
    calls = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(platform_integration, "mac_bridge_installed", lambda: True)
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: False)
    monkeypatch.setattr(runtime, "get_runtime_root", lambda: str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)))

    assert runtime.launch_mtga(str(bundle)) == str(bundle)
    args, kwargs = calls[0]
    assert args == [str(executable)]
    assert kwargs["env"]["DYLD_INSERT_LIBRARIES"] == str(platform_integration.MAC_BRIDGE_LIBRARY)
    assert kwargs["env"]["SteamAppId"] == "2141910"
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["start_new_session"] is True


def test_mac_bridge_launch_leaves_running_match_alone(monkeypatch, tmp_path):
    bundle = tmp_path / "MTGA.app"
    bundle.mkdir()
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(platform_integration, "mac_bridge_installed", lambda: True)
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: True)
    monkeypatch.setattr(
        runtime.subprocess, "run", lambda *args, **kwargs: pytest.fail("game already running")
    )
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("duplicate game"))
    assert runtime.launch_mtga(str(bundle)) == str(bundle)


@pytest.mark.parametrize(
    "installed,running,expected",
    [(True, False, True), (True, True, False), (False, False, False), (False, True, False)],
)
def test_native_mac_session_auto_launch(monkeypatch, tmp_path, installed, running, expected):
    bundle = tmp_path / "MTGA.app"
    bundle.mkdir()
    calls = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(platform_integration, "mac_bridge_installed", lambda: installed)
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: running)
    monkeypatch.setattr(runtime, "find_mtga_install_dir", lambda: (str(tmp_path), "test"))
    monkeypatch.setattr(runtime, "launch_mtga", lambda path, **kwargs: calls.append(path) or str(bundle))
    result = runtime.launch_native_mac_session()
    assert calls == ([str(tmp_path)] if expected else [])
    assert result == (str(bundle) if expected else None)


def test_mac_auto_launch_failure_is_logged_and_shown_without_closing_the_coach(monkeypatch):
    statuses, errors, messages = [], [], []

    def fail_launch(**kwargs):
        raise OSError("Steam unavailable")

    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")  # independent of the user's settings.json
    monkeypatch.setattr(runtime, "launch_native_mac_session", fail_launch)
    monkeypatch.setattr(desktop_app, "_write_log", messages.append)
    desktop_app._launch_native_mac_session(statuses.append, errors.append)  # must not raise
    assert messages == ["MTGA automatic launch failed: Steam unavailable"]
    assert statuses == errors
    assert "Arena could not start: Steam unavailable" in statuses[0]


@pytest.mark.parametrize("installed,running", [(True, True), (False, False), (False, True)])
def test_mac_auto_launch_explains_reconnect_or_coaching_only(monkeypatch, installed, running):
    statuses = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(platform_integration, "mac_bridge_installed", lambda: installed)
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: running)
    monkeypatch.setattr(runtime, "launch_mtga", lambda *a, **k: pytest.fail("must not launch"))
    runtime.launch_native_mac_session(on_status=statuses.append)
    assert len(statuses) == 1
    assert ("already running" if running else "Native bridge not installed") in statuses[0]
    if not installed:
        # Bridge-only autoplay (2026-10-18): no screenshot mode to promise.
        assert "coaching from the log only; autoplay needs the bridge" in statuses[0]
        assert "screen" not in statuses[0]
    if running:
        assert "Start Arena normally" not in statuses[0]


def test_mac_launch_does_not_duplicate_a_still_starting_child(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    bundle = tmp_path / "MTGA.app"
    executable = bundle / "Contents" / "MacOS" / "MTGA"
    executable.parent.mkdir(parents=True)
    executable.touch()
    calls, statuses = [], []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(platform_integration, "mac_bridge_installed", lambda: True)
    # Simulate the interval before the new child appears in process discovery.
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: False)
    monkeypatch.setattr(runtime, "get_runtime_root", lambda: str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))

    def spawn(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    ready = Barrier(2)

    def launch():
        ready.wait(timeout=3)
        return runtime.launch_mtga(str(bundle), on_status=statuses.append)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(launch)
        second = pool.submit(launch)
        assert first.result(timeout=5) == second.result(timeout=5) == str(bundle)
    assert calls == [[str(executable)]]
    assert any("Starting Steam" in status for status in statuses)
    assert any("Waiting for its bridge" in status for status in statuses)
    assert any("already running" in status for status in statuses)


def test_mac_launch_keeps_game_started_during_steam_startup(monkeypatch, tmp_path):
    bundle = tmp_path / "MTGA.app"
    executable = bundle / "Contents" / "MacOS" / "MTGA"
    executable.parent.mkdir(parents=True)
    executable.touch()
    checks = iter([False, False, True])
    statuses = []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(platform_integration, "mac_bridge_installed", lambda: True)
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: next(checks))
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("duplicate game"))
    assert runtime.launch_mtga(str(bundle), on_status=statuses.append) == str(bundle)
    assert "already running" in statuses[-1]


def test_android_mode_does_not_start_mtga_on_the_mac(monkeypatch):
    # A Mac client would take the coach's single bridge slot from the phone (2026-09-24).
    messages = []
    launched = []
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "android")
    monkeypatch.setattr(runtime, "launch_native_mac_session", lambda: launched.append(True))
    monkeypatch.setattr(desktop_app, "_write_log", messages.append)
    desktop_app._launch_native_mac_session()
    assert launched == []
    assert messages == ["game_device=android: not starting MTGA on this Mac"]


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
