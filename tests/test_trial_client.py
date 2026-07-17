"""Tests for client-side free-trial provisioning (subscription + repair)."""

import io
import json
import urllib.error
from unittest import mock

from arenamcp import subscription
from arenamcp.repair_engine import RepairEngine


def _http_response(payload: dict):
    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    resp = mock.MagicMock()
    resp.read.side_effect = body.read
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _http_error(code: int):
    return urllib.error.HTTPError(
        url="https://mtgacoach.com/api/trial", code=code,
        msg="err", hdrs=None, fp=io.BytesIO(b"{}"),
    )


def test_machine_id_is_stable_sha256():
    a = subscription.get_machine_id()
    b = subscription.get_machine_id()
    assert a == b
    assert len(a) == 64
    int(a, 16)  # hex


def test_request_trial_key_created(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _http_response(
            {"key": "sk-trial-123", "expires_at": "2026-07-23T00:00:00Z",
             "status": "created"}
        )

    monkeypatch.setattr(subscription.urllib.request, "urlopen", fake_urlopen)
    result = subscription.request_trial_key()
    assert result["status"] == "created"
    assert result["key"] == "sk-trial-123"
    assert captured["url"] == "https://mtgacoach.com/api/trial"
    assert len(captured["body"]["machine_id"]) == 64
    assert captured["body"]["app_version"]


def test_request_trial_key_expired(monkeypatch):
    monkeypatch.setattr(
        subscription.urllib.request, "urlopen",
        mock.Mock(side_effect=_http_error(403)),
    )
    result = subscription.request_trial_key()
    assert result["status"] == "trial_expired"
    assert "mtgacoach.com/subscribe" in result["message"]


def test_request_trial_key_endpoint_missing_is_offline(monkeypatch):
    monkeypatch.setattr(
        subscription.urllib.request, "urlopen",
        mock.Mock(side_effect=_http_error(404)),
    )
    assert subscription.request_trial_key()["status"] == "offline"


def test_request_trial_key_network_down(monkeypatch):
    monkeypatch.setattr(
        subscription.urllib.request, "urlopen",
        mock.Mock(side_effect=OSError("no route")),
    )
    assert subscription.request_trial_key()["status"] == "offline"


class _FakeSettings:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, save=True):
        self.data[key] = value

    def save(self):
        pass


def test_ensure_license_key_persists_trial(monkeypatch):
    settings = _FakeSettings()
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        subscription, "request_trial_key",
        lambda: {"status": "created", "key": "sk-t", "expires_at": "2026-07-23", "message": ""},
    )
    result = subscription.ensure_license_key()
    assert result["status"] == "created"
    assert settings.data["license_key"] == "sk-t"
    assert settings.data["trial_expires_at"] == "2026-07-23"


def test_ensure_license_key_noop_with_existing_key(monkeypatch):
    settings = _FakeSettings({"license_key": "sk-real"})
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        subscription, "request_trial_key",
        mock.Mock(side_effect=AssertionError("must not be called")),
    )
    assert subscription.ensure_license_key()["status"] == "existing_key"


def test_check_license_trial_expired_points_at_patreon(monkeypatch):
    settings = _FakeSettings({"license_key": ""})
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "arenamcp.subscription.ensure_license_key",
        lambda: {"status": "trial_expired", "message": "over"},
    )
    result = RepairEngine()._check_license()
    assert result.status == "action_needed"
    assert "trial has ended" in result.detail
    assert "subscribe" in result.action_hint.lower()


def test_check_license_offline_trial_keeps_manual_entry_path(monkeypatch):
    settings = _FakeSettings({"license_key": ""})
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "arenamcp.subscription.ensure_license_key",
        lambda: {"status": "offline", "message": "no net"},
    )
    result = RepairEngine()._check_license()
    assert result.status == "action_needed"
    assert "trial could not be started" in result.detail
    assert "license key below" in result.action_hint


def test_check_license_trial_key_validates_against_gateway(monkeypatch):
    settings = _FakeSettings({"license_key": ""})
    monkeypatch.setattr("arenamcp.settings.get_settings", lambda: settings)

    def fake_ensure():
        settings.set("license_key", "sk-trial")
        settings.set("trial_expires_at", "2026-07-23T00:00:00Z")
        return {"status": "created", "key": "sk-trial",
                "expires_at": "2026-07-23T00:00:00Z", "message": ""}

    monkeypatch.setattr("arenamcp.subscription.ensure_license_key", fake_ensure)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=0: _models_response()
    )
    result = RepairEngine()._check_license()
    assert result.status == "ok"
    assert "free trial until 2026-07-23" in result.detail


def _models_response():
    resp = _http_response({"data": [{"id": "deepseek-v4-flash"}]})
    resp.status = 200
    return resp
