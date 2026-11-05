from unittest.mock import Mock

import pytest

from arenamcp.desktop import screen_capture_permissions as permissions


@pytest.fixture
def denied(monkeypatch):
    monkeypatch.setattr(permissions, "require_screen_capture_access", Mock(side_effect=PermissionError))
    request = Mock(return_value=False)
    monkeypatch.setattr(permissions, "request_screen_capture_access", request)
    return request


def test_permission_already_granted_does_not_prompt(monkeypatch):
    monkeypatch.setattr(permissions, "require_screen_capture_access", lambda: None)
    ask = Mock()
    monkeypatch.setattr(permissions, "_ask", ask)
    assert permissions.ensure_screen_capture_permission()
    ask.assert_not_called()


def test_decline_does_not_request_or_open_settings(denied, monkeypatch):
    monkeypatch.setattr(permissions, "_ask", Mock(return_value=False))
    settings = Mock()
    monkeypatch.setattr(permissions, "open_url", settings)
    assert not permissions.ensure_screen_capture_permission()
    denied.assert_not_called()
    settings.assert_not_called()


def test_native_grant_continues_feature_immediately(denied, monkeypatch):
    denied.return_value = True
    ask = Mock(return_value=True)
    monkeypatch.setattr(permissions, "_ask", ask)
    assert permissions.ensure_screen_capture_permission()
    denied.assert_called_once()
    assert ask.call_count == 1


def test_denial_offers_settings_and_exact_dev_app(denied, monkeypatch):
    monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", "/network/source")
    monkeypatch.setenv("MTGACOACH_DEV_APP", "/Applications/Custom Coach Dev.app")
    ask = Mock(return_value=True)
    monkeypatch.setattr(permissions, "_ask", ask)
    settings = Mock()
    monkeypatch.setattr(permissions, "open_url", settings)
    assert not permissions.ensure_screen_capture_permission()
    denied.assert_called_once()
    assert "/Applications/Custom Coach Dev.app" in ask.call_args.args[2]
    settings.assert_called_once_with(permissions.SETTINGS_URL)
