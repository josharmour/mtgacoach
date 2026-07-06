"""Repair engine: function-verifying checks, not file-existence theater."""

import arenamcp.repair_engine as re_mod
from arenamcp.repair_engine import RepairEngine, RepairReport, CheckResult


def test_report_summary_and_health():
    rep = RepairReport(results=[
        CheckResult("a", "A", "ok", "fine"),
        CheckResult("b", "B", "fixed", "repaired"),
    ])
    assert rep.healthy is True
    assert "repaired" in rep.summary()

    rep.results.append(CheckResult("c", "C", "action_needed", "broken", "do x"))
    assert rep.healthy is False
    assert rep.needs_user[0].key == "c"
    assert "1 item(s) need attention" in rep.summary()


def test_license_check_no_key(monkeypatch):
    class _S:
        def get(self, k, d=None):
            return ""

    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: _S())
    r = RepairEngine()._check_license()
    assert r.status == "action_needed"
    assert "license key" in r.action_hint.lower()


def test_license_check_rejected(monkeypatch):
    import urllib.error

    class _S:
        def get(self, k, d=None):
            return "sk-bogus"

    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: _S())

    def _boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    r = RepairEngine()._check_license()
    assert r.status == "action_needed"
    assert "rejected" in r.detail


def test_license_check_gateway_down(monkeypatch):
    class _S:
        def get(self, k, d=None):
            return "sk-something"

    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: _S())

    def _boom(req, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    r = RepairEngine()._check_license()
    assert r.status == "error"
    assert "reach" in r.detail.lower()


def test_plugin_check_detects_stale_dll(monkeypatch, tmp_path):
    from arenamcp.platform_integration import MtgaInstall

    packaged = tmp_path / "packaged.dll"
    packaged.write_bytes(b"NEW PLUGIN BYTES")
    mtga = tmp_path / "MTGA"
    plugins = mtga / "BepInEx" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "MtgaCoachBridge.dll").write_bytes(b"OLD 2.4 BYTES")

    import arenamcp.desktop.runtime as rt
    monkeypatch.setattr(rt, "find_plugin_dll", lambda: packaged)
    monkeypatch.setattr(rt, "is_mtga_running", lambda: False)

    installed = {}

    def _fake_install(mtga_dir):
        (plugins / "MtgaCoachBridge.dll").write_bytes(packaged.read_bytes())
        installed["yes"] = True
        return str(plugins / "MtgaCoachBridge.dll")

    monkeypatch.setattr(rt, "install_plugin", _fake_install)

    eng = RepairEngine()
    eng._install = MtgaInstall(install_dir=mtga, player_log=None, platform="linux-steam")
    r = eng._check_plugin()
    assert r.status == "fixed"
    assert installed.get("yes")

    # Second run: hashes now match → ok, no reinstall.
    r2 = eng._check_plugin()
    assert r2.status == "ok"


def test_plugin_check_defers_while_mtga_running(monkeypatch, tmp_path):
    from arenamcp.platform_integration import MtgaInstall
    import arenamcp.desktop.runtime as rt

    packaged = tmp_path / "packaged.dll"
    packaged.write_bytes(b"NEW")
    monkeypatch.setattr(rt, "find_plugin_dll", lambda: packaged)
    monkeypatch.setattr(rt, "is_mtga_running", lambda: True)

    eng = RepairEngine()
    eng._install = MtgaInstall(
        install_dir=tmp_path / "MTGA", player_log=None, platform="windows"
    )
    r = eng._check_plugin()
    assert r.status == "action_needed"
    assert "Close MTGA" in r.action_hint
