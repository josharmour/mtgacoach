"""Tests for live license-key reloading and verification-code protection."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.proxy_fakes import FakeAPIError, FakeClient, ok_stream

from arenamcp import settings as settings_mod
from arenamcp.backend_health import BACKEND_ERROR_PREFIX
from arenamcp.backends.proxy import ProxyBackend
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.repair_engine import set_license_key


def _write_settings_atomic(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def test_online_backend_reloads_updated_key_from_disk(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(settings_file, {"license_key": "328390"})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)

    constructed: list[tuple[str, str, Mock]] = []

    def fake_openai(**kwargs):
        close_mock = Mock()
        client = SimpleNamespace(
            base_url=kwargs.get("base_url"),
            api_key=kwargs.get("api_key"),
            close=close_mock,
        )
        constructed.append((str(kwargs.get("base_url")), str(kwargs.get("api_key")), close_mock))
        return client

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=fake_openai))

    be = ProxyBackend.create_online(license_key="328390")
    first_client = be._get_client()
    assert first_client.api_key == "328390"
    assert len(constructed) == 1

    # Simulate UI process updating settings.json atomically with a valid Patreon key.
    _write_settings_atomic(settings_file, {"license_key": "sk-new-patreon-key"})

    second_client = be._get_client()
    assert second_client is not first_client
    assert second_client.api_key == "sk-new-patreon-key"
    assert be._api_key == "sk-new-patreon-key"
    constructed[0][2].assert_called_once()
    assert len(constructed) == 2


def test_online_backend_retries_once_on_401_when_disk_key_updated(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(settings_file, {"license_key": "328390"})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)

    be = ProxyBackend.create_online(license_key="328390")
    second_client = FakeClient([ok_stream("ok")])
    built_clients: list[FakeClient] = []

    def fail_then_update_disk():
        # Update settings.json while the first request is in flight.
        _write_settings_atomic(settings_file, {"license_key": "sk-new-patreon-key"})
        raise FakeAPIError("LiteLLM Virtual Key expected", status_code=401)

    first_client = FakeClient([fail_then_update_disk])
    be._client = first_client

    def fake_openai(**kwargs):
        assert kwargs.get("api_key") == "sk-new-patreon-key"
        built_clients.append(second_client)
        return second_client

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=fake_openai))

    out = be.complete("sys", "user", 16)
    assert out == "ok"
    assert len(first_client.calls) == 1
    assert len(second_client.calls) == 1
    assert len(built_clients) == 1
    assert be._api_key == "sk-new-patreon-key"


def test_online_backend_does_not_retry_401_when_disk_unchanged(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(settings_file, {"license_key": "sk-bad-key"})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)

    be = ProxyBackend.create_online(license_key="sk-bad-key")
    client = FakeClient([FakeAPIError("auth failed", status_code=401)])
    be._client = client

    out = be.complete("sys", "user", 16)
    assert out.startswith(BACKEND_ERROR_PREFIX)
    assert len(client.calls) == 1


def test_create_online_keeps_explicit_key_when_settings_untouched(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(settings_file, {"license_key": "sk-real-disk-key"})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)

    seen_keys: list[str] = []

    def fake_openai(**kwargs):
        seen_keys.append(str(kwargs.get("api_key")))
        return SimpleNamespace(api_key=kwargs.get("api_key"))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=fake_openai))

    be = ProxyBackend.create_online(license_key="explicit-test-key")
    client = be._get_client()
    assert client.api_key == "explicit-test-key"
    assert seen_keys == ["explicit-test-key"]


def test_set_license_key_rejects_6_digit_verification_code(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(settings_file, {"license_key": "sk-existing-trial"})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_mod, "_settings", None)

    res = set_license_key("328390")
    assert res.status == "action_needed"
    assert "verification code" in res.detail.lower()
    assert settings_mod.get_settings().get("license_key") == "sk-existing-trial"


def test_set_license_key_saves_valid_key_and_clears_trial_expiry(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(
        settings_file,
        {"license_key": "sk-old-trial", "trial_expires_at": "2026-12-01T00:00:00Z"},
    )
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_mod, "_settings", None)

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"data": [{"id": "current"}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _Resp())

    res = set_license_key("  sk-valid-key  ")
    assert res.status == "ok"
    s = settings_mod.get_settings()
    assert s.get("license_key") == "sk-valid-key"
    assert s.get("trial_expires_at") == ""


@pytest.mark.parametrize("raw_cmd", ["sk-new-key", "/key sk-new-key"])
def test_pipe_adapter_handle_set_key_updates_live_backends(monkeypatch, tmp_path, raw_cmd):
    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(settings_file, {"license_key": "328390"})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_mod, "_settings", None)

    coach_be = SimpleNamespace(set_api_key=Mock())
    autopilot_be = SimpleNamespace(set_api_key=Mock())
    adapter = PipeAdapter()
    adapter._coach = SimpleNamespace(
        _coach=SimpleNamespace(_backend=coach_be),
        _autopilot_backend=autopilot_be,
    )
    adapter.log = Mock()

    adapter._handle_set_key(raw_cmd)

    assert settings_mod.get_settings().get("license_key") == "sk-new-key"
    coach_be.set_api_key.assert_called_once_with("sk-new-key")
    autopilot_be.set_api_key.assert_called_once_with("sk-new-key")


def test_main_window_pushes_license_updated_only(qapp, monkeypatch):
    pytest.importorskip("PySide6")
    from arenamcp.desktop.main_window import MainWindow
    from arenamcp.desktop.repair_tab import RepairTab

    monkeypatch.setattr(RepairTab, "run_checks", Mock())
    win = MainWindow()
    try:
        send_cmd = Mock()
        badge_refresh = Mock()
        monkeypatch.setattr(win._session, "send_command", send_cmd)
        monkeypatch.setattr(win.access_badge, "refresh", badge_refresh)

        win.repair_tab.license_changed.emit()
        badge_refresh.assert_called_once_with(force=True)
        send_cmd.assert_not_called()

        win.repair_tab.license_updated.emit("sk-new-key")
        send_cmd.assert_called_once_with("set_license_key", "sk-new-key")
    finally:
        win.close()


def test_check_license_distinguishes_6_digit_code_from_expired_trial(monkeypatch, tmp_path):
    import urllib.error
    from io import BytesIO

    from arenamcp.repair_engine import RepairEngine

    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(
        settings_file,
        {"license_key": "521601", "trial_expires_at": "2099-12-01T00:00:00Z"},
    )
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_mod, "_settings", None)

    def _raise_401(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", _raise_401)
    res_code = RepairEngine()._check_license()
    assert res_code.status == "action_needed"
    assert "6-digit verification code" in res_code.detail
    assert "expired" not in res_code.detail.lower()

    # Control: an expired sk- trial key still reports trial expired.
    _write_settings_atomic(
        settings_file,
        {"license_key": "sk-expired-trial", "trial_expires_at": "2020-01-01T00:00:00Z"},
    )
    monkeypatch.setattr(settings_mod, "_settings", None)
    res_exp = RepairEngine()._check_license()
    assert res_exp.status == "action_needed"
    assert "trial has expired" in res_exp.detail.lower()


def test_trial_signup_dialog_shows_done_confirmation_stage(qapp, monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from arenamcp.desktop.trial_dialog import TrialSignupDialog

    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(settings_file, {})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_mod, "_settings", None)

    dlg = TrialSignupDialog()
    try:
        emitted = []
        dlg.trial_started.connect(lambda k, exp: emitted.append((k, exp)))
        dlg._on_step_result(
            {
                "stage": "code",
                "status": "created",
                "key": "sk-trial-123",
                "expires_at": "2026-12-02T00:00:00Z",
            }
        )

        assert emitted == [("sk-trial-123", "2026-12-02T00:00:00Z")]
        assert settings_mod.get_settings().get("license_key") == "sk-trial-123"
        assert dlg._stage == "done"
        assert "active" in dlg._done_label.text().lower()
        assert dlg._done_label.isHidden() is False
        assert dlg._done_btn.isHidden() is False
        assert dlg._code_edit.isHidden() is True
        assert dlg._escape_btn.isHidden() is True
    finally:
        dlg.close()


def test_repair_tab_offers_trial_recovery_when_6_digit_code_saved(qapp, monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from arenamcp.desktop.repair_tab import RepairTab
    from arenamcp.repair_engine import CheckResult, RepairReport

    settings_file = tmp_path / "settings.json"
    _write_settings_atomic(settings_file, {"license_key": "521601"})
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_mod, "_settings", None)

    monkeypatch.setattr(RepairTab, "showEvent", lambda self, ev: None)
    tab = RepairTab()
    try:
        assert tab._recover_trial_btn.text() == "Start / recover free trial…"
        assert tab._has_valid_shaped_license_key() is False

        offered = []
        monkeypatch.setattr(tab, "_offer_trial_signup", lambda: offered.append(True))
        report = RepairReport(
            results=[CheckResult("license", "License / online backend", "action_needed", "6-digit code")]
        )
        tab._render_report(report)
        qapp.processEvents()
        assert offered == [True]
    finally:
        tab.close()


def test_trial_code_email_warns_not_to_paste_into_subscriber_box(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from tests.test_patreon_litellm import _load_patreon

    monkeypatch.setenv("SMTP_USER", "relay@example.test")
    monkeypatch.setenv("SMTP_PASS", "pw")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    patreon, _db = _load_patreon(tmp_path, monkeypatch)
    smtp_factory = MagicMock()
    smtp = smtp_factory.return_value.__enter__.return_value
    monkeypatch.setattr(patreon.smtplib, "SMTP", smtp_factory)

    patreon.send_trial_code_email("user@example.test", "521601")
    body = smtp.send_message.call_args.args[0].get_content()
    assert "521601" in body
    assert "Start your free trial" in body
    assert "Subscriber key" in body
