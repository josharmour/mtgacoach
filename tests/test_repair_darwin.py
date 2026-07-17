"""Repair engine on macOS + the Detailed Logs onboarding check.

Covers docs/PLATFORM_PARITY.md item A10 / section C:
- the Detailed Logs (Plugin Support) check fires on EVERY platform when the
  Player.log marker says DISABLED (the coach is blind without it),
- a native darwin install is accepted as found, with bridge checks reporting
  "not applicable" (IL2CPP client has no CLR to inject into),
- a darwin Wine/CrossOver bottle runs the bridge checks like Linux/Proton,
- Windows behavior is unchanged.
"""

from pathlib import Path

import pytest

from arenamcp.platform_integration import MtgaInstall
from arenamcp.repair_engine import RepairEngine


DISABLED_LINE = b"[UnityCrossThreadLogger]DETAILED LOGS: DISABLED\n"
ENABLED_LINE = b"[UnityCrossThreadLogger]DETAILED LOGS: ENABLED\n"


def _engine_with_log(tmp_path: Path, log_bytes, platform: str = "windows"):
    """Engine wired to a temp install + Player.log (None = no log file)."""
    mtga = tmp_path / "MTGA"
    mtga.mkdir(exist_ok=True)
    log = tmp_path / "Player.log"
    if log_bytes is not None:
        log.write_bytes(log_bytes)
    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=mtga, player_log=log, platform=platform
    )
    return eng


# ---------------------------------------------------------------------------
# _check_detailed_logs — every platform
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["windows", "linux-steam", "darwin", "darwin-wine"])
def test_detailed_logs_disabled_flags_action(tmp_path, platform):
    eng = _engine_with_log(
        tmp_path, b"boot noise\n" + DISABLED_LINE + b"more lines\n", platform
    )
    r = eng._check_detailed_logs()
    assert r is not None
    assert r.status == "action_needed"
    assert "Detailed Logs (Plugin Support)" in r.action_hint
    assert "restart MTGA" in r.action_hint


@pytest.mark.parametrize("platform", ["windows", "darwin"])
def test_detailed_logs_enabled_passes(tmp_path, platform):
    eng = _engine_with_log(
        tmp_path, b"boot noise\n" + ENABLED_LINE + b"gre data...\n", platform
    )
    r = eng._check_detailed_logs()
    assert r.status == "ok"


def test_detailed_logs_missing_log_skips(tmp_path):
    # _check_player_log already reports the missing log with the same hint;
    # this check must not duplicate the row.
    eng = _engine_with_log(tmp_path, None, "darwin")
    assert eng._check_detailed_logs() is None


def test_detailed_logs_skips_when_install_missing():
    eng = RepairEngine()
    eng._install = None
    assert eng._check_detailed_logs() is None


def test_detailed_logs_newest_marker_wins(tmp_path):
    # User toggled it on: a later ENABLED must override an earlier DISABLED.
    eng = _engine_with_log(tmp_path, DISABLED_LINE + b"x" * 100 + ENABLED_LINE)
    assert eng._check_detailed_logs().status == "ok"
    # And vice versa.
    eng2 = _engine_with_log(tmp_path, ENABLED_LINE + b"x" * 100 + DISABLED_LINE)
    assert eng2._check_detailed_logs().status == "action_needed"


def test_detailed_logs_marker_found_in_big_log_head(tmp_path):
    # The marker sits at startup (head); a long session must not push it
    # out of reach of the scan.
    body = DISABLED_LINE + b"log line\n" * 100_000  # ~900 KB tail of noise
    eng = _engine_with_log(tmp_path, body)
    assert eng._check_detailed_logs().status == "action_needed"


def test_detailed_logs_no_marker_is_soft_pass(tmp_path):
    eng = _engine_with_log(tmp_path, b"just some lines\nwithout markers\n")
    r = eng._check_detailed_logs()
    assert r.status == "ok"
    assert "could not verify" in r.detail.lower()


def test_detailed_logs_registered_after_player_log(monkeypatch):
    """The check must be part of the run() pipeline, next to player_log."""
    eng = RepairEngine()
    called: list[str] = []

    def _stub(name):
        def _f():
            called.append(name)
            return None
        _f.__name__ = name
        return _f

    for name in (
        "_check_python_runtime", "_check_settings", "_check_license",
        "_check_mtga_install", "_check_player_log", "_check_detailed_logs",
        "_check_bepinex", "_check_plugin", "_check_launch_options",
        "_check_bridge_signal",
    ):
        setattr(eng, name, _stub(name))
    eng.run()
    assert "_check_detailed_logs" in called
    assert (
        called.index("_check_detailed_logs")
        == called.index("_check_player_log") + 1
    )


# ---------------------------------------------------------------------------
# _check_mtga_install — darwin is a found install, not a failure
# ---------------------------------------------------------------------------

def test_mtga_install_darwin_found(monkeypatch, tmp_path):
    install = MtgaInstall(
        install_dir=tmp_path / "MTGA.app",
        player_log=tmp_path / "Player.log",
        platform="darwin",
    )
    monkeypatch.setattr(
        "arenamcp.platform_integration.find_mtga", lambda: install
    )

    class _S:
        store: dict = {}

        def get(self, k, d=None):
            return self.store.get(k, d)

        def set(self, k, v):
            self.store[k] = v

        def save(self):
            pass

    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: _S())
    r = RepairEngine()._check_mtga_install()
    assert r.status in ("ok", "fixed")
    assert "Could not find" not in r.detail
    assert "darwin" in r.detail


# ---------------------------------------------------------------------------
# Bridge checks — native darwin: informational not-applicable, never failure
# ---------------------------------------------------------------------------

def _native_mac_engine(tmp_path):
    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=tmp_path / "MTGA.app",
        player_log=tmp_path / "Player.log",
        platform="darwin",
    )
    return eng


@pytest.mark.parametrize(
    "check", ["_check_bepinex", "_check_plugin", "_check_bridge_signal"]
)
@pytest.mark.parametrize(
    "platform", ["darwin", "darwin-steam", "darwin-epic"]
)
def test_bridge_checks_native_darwin_not_applicable(tmp_path, check, platform):
    eng = _native_mac_engine(tmp_path)
    eng._install.platform = platform
    r = getattr(eng, check)()
    assert r is not None
    assert r.status == "ok"  # informational — must not fail the report
    assert "Not applicable" in r.detail
    assert "Wine/CrossOver" in r.detail
    assert "PLATFORM_PARITY" in r.detail


def test_launch_options_check_skipped_on_darwin(tmp_path):
    # Steam-Proton launch options are a Linux-only concept.
    for plat in ("darwin", "darwin-wine"):
        eng = _engine_with_log(tmp_path, None, plat)
        assert eng._check_launch_options() is None


# ---------------------------------------------------------------------------
# Bridge checks — darwin-wine bottle: run like Linux/Proton
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["darwin-wine", "darwin-crossover"])
def test_bepinex_check_runs_for_darwin_wine(tmp_path, platform):
    # Bottle with BepInEx properly installed → the real check runs and passes.
    mtga = tmp_path / "bottle" / "drive_c" / "MTGA"
    (mtga / "BepInEx" / "core").mkdir(parents=True)
    (mtga / "BepInEx" / "core" / "BepInEx.dll").write_bytes(b"core")
    (mtga / "winhttp.dll").write_bytes(b"doorstop")
    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=mtga, player_log=None, platform=platform
    )
    r = eng._check_bepinex()
    assert r.status == "ok"
    assert "Not applicable" not in r.detail
    assert "doorstop" in r.detail


def test_plugin_check_runs_for_darwin_wine(monkeypatch, tmp_path):
    # Same flow as the Linux test: stale DLL in the bottle gets updated.
    import arenamcp.desktop.runtime as rt

    packaged = tmp_path / "packaged.dll"
    packaged.write_bytes(b"NEW PLUGIN BYTES")
    mtga = tmp_path / "bottle" / "drive_c" / "MTGA"
    plugins = mtga / "BepInEx" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "MtgaCoachBridge.dll").write_bytes(b"OLD BYTES")

    monkeypatch.setattr(rt, "find_plugin_dll", lambda: packaged)
    monkeypatch.setattr(rt, "is_mtga_running", lambda: False)
    monkeypatch.setattr(
        rt, "install_plugin",
        lambda d: (plugins / "MtgaCoachBridge.dll").write_bytes(
            packaged.read_bytes()
        ),
    )

    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=mtga, player_log=None, platform="darwin-wine"
    )
    assert eng._check_plugin().status == "fixed"
    assert eng._check_plugin().status == "ok"


def test_bridge_signal_darwin_wine_hint_mentions_bottle(tmp_path):
    # No LogOutput.log yet → actionable hint must speak Wine, not Steam.
    mtga = tmp_path / "bottle" / "drive_c" / "MTGA"
    mtga.mkdir(parents=True)
    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=mtga, player_log=None, platform="darwin-wine"
    )
    r = eng._check_bridge_signal()
    assert r.status == "action_needed"
    assert "winhttp=n,b" in r.action_hint
    assert "Wine/CrossOver" in r.action_hint


def test_bridge_signal_darwin_wine_detects_banner(tmp_path):
    mtga = tmp_path / "bottle" / "drive_c" / "MTGA"
    (mtga / "BepInEx").mkdir(parents=True)
    (mtga / "BepInEx" / "LogOutput.log").write_text(
        "[Info: BepInEx] Loading [MtgaCoachBridge v3.0.0]\n"
    )
    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=mtga, player_log=None, platform="darwin-wine"
    )
    assert eng._check_bridge_signal().status == "ok"


# ---------------------------------------------------------------------------
# Windows behavior unchanged
# ---------------------------------------------------------------------------

def test_windows_bridge_checks_still_run(tmp_path):
    # BepInEx present → real check, real ok (not a not-applicable stub).
    mtga = tmp_path / "MTGA"
    (mtga / "BepInEx" / "core").mkdir(parents=True)
    (mtga / "BepInEx" / "core" / "BepInEx.dll").write_bytes(b"core")
    (mtga / "winhttp.dll").write_bytes(b"doorstop")
    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=mtga, player_log=None, platform="windows"
    )
    r = eng._check_bepinex()
    assert r.status == "ok"
    assert "Not applicable" not in r.detail


def test_windows_bridge_signal_hint_unchanged(tmp_path):
    mtga = tmp_path / "MTGA"
    mtga.mkdir()
    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=mtga, player_log=None, platform="windows"
    )
    r = eng._check_bridge_signal()
    assert r.status == "action_needed"
    assert "Start MTGA once" in r.action_hint
