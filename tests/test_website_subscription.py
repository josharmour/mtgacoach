"""mtgacoach.com subscription routes: the signup lockdown and /v1/subscription/check.

Customers hold LiteLLM ``sk-`` keys minted by the Patreon flow; the desktop
app's access badge asks /v1/subscription/check about them (200 while active,
402 once expired or revoked).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from tests.website_app import load_website_app

KEY = "sk-patron-test-key"


@pytest.fixture()
def site(tmp_path: Path, monkeypatch):
    app, db, patreon = load_website_app(tmp_path, monkeypatch)
    # The Patreon webhook's own write path for a newly minted gateway key.
    patreon._record_key("patron@example.com", "Patron", KEY, "Patreon test")
    with TestClient(app.app) as client:
        yield client, db


def _check(client, key=KEY):
    return client.post(
        "/v1/subscription/check",
        headers={"Authorization": f"Bearer {key}"},
        json={"license_key": key},
    )


def test_public_subscribe_request_no_longer_mints_or_leaks_keys(site) -> None:
    client, db = site
    response = client.post("/subscribe/request", json={"email": "patron@example.com", "name": "Patron"})

    assert response.status_code == 403
    assert "Self-service key issuance is disabled" in response.json()["detail"]
    assert KEY not in response.text
    subscribers = db.list_subscribers()
    assert [s["license_key"] for s in subscribers] == [KEY]


def test_active_patreon_key_passes_the_subscription_check(site) -> None:
    client, _ = site
    response = _check(client)

    assert response.status_code == 200
    assert response.json()["status"] == "active"


@pytest.mark.parametrize(
    ("lapse", "status", "message"),
    [
        ("expired", "expired", "Subscription expired"),
        ("revoked", "revoked", "Subscription revoked"),
    ],
)
def test_lapsed_key_gets_402_with_a_reason(site, lapse, status, message) -> None:
    client, db = site
    if lapse == "expired":
        db.update_subscriber(KEY, expires_at=time.time() - 60)
    else:
        db.revoke_subscriber(KEY)

    response = _check(client)

    assert response.status_code == 402
    body = response.json()
    assert body["status"] == status
    assert message in body["message"]


def test_unknown_key_is_rejected(site) -> None:
    client, _ = site
    response = _check(client, key="sk-not-a-subscriber")

    assert response.status_code == 401
    assert response.json()["status"] == "invalid"
