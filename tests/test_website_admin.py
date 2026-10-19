"""mtgacoach.com legacy admin API (website/routers/admin.py)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from tests.website_app import load_website_app

ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:test-admin").decode()}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    app, _db, _patreon = load_website_app(tmp_path, monkeypatch)
    with TestClient(app.app) as client:
        yield client


def test_providers_view_reports_the_yaml_default_model(client) -> None:
    # The routers/ split left this view reading a module-level ``config`` that
    # no longer exists there, so the Providers tab answered 500.
    r = client.get("/admin/api/providers", headers=ADMIN)

    assert r.status_code == 200
    data = r.json()
    assert data["yaml_default_model"] == "glm-5.3-flash"
    assert data["providers"] == [] and data["overridden"] is False


def test_admin_api_requires_credentials(client) -> None:
    assert client.get("/admin/api/providers").status_code == 403
    assert client.get("/admin", follow_redirects=False).status_code == 302
