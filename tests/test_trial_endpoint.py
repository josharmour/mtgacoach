"""POST /api/trial — free-trial key auto-provisioning (website/routers/proxy.py).

Loads the website app against a temp SQLite DB with the LiteLLM admin API
monkeypatched (no network).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from tests.fakes import FakeLiteLLM
from tests.website_app import load_website_app

MACHINE_ID = "a" * 64
OTHER_MACHINE_ID = "b" * 64


@pytest.fixture()
def env(tmp_path, monkeypatch):
    proxy_app, proxy_db, patreon = load_website_app(tmp_path, monkeypatch)
    fake = FakeLiteLLM(key_prefix="sk-fake-trial-")
    monkeypatch.setattr(patreon, "_http", lambda: fake)
    with TestClient(proxy_app.app) as client:
        yield proxy_app, proxy_db, patreon, fake, client


def _post_trial(client, machine_id=MACHINE_ID, app_version="2.0.1"):
    return client.post("/api/trial", json={"machine_id": machine_id, "app_version": app_version})


def test_first_issue_creates_trial_key(env):
    _, proxy_db, _, fake, client = env

    r = _post_trial(client)

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "created"
    assert data["key"] == "sk-fake-trial-0"
    assert len(fake.minted) == 1

    # expires_at is ISO8601 UTC, ~7 days out
    expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    delta = expires - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)

    # persisted in the trials table
    row = proxy_db.get_trial(MACHINE_ID)
    assert row and row["litellm_key"] == "sk-fake-trial-0"
    assert row["expires_at"] == data["expires_at"]


def test_repeat_call_returns_same_key_as_existing(env):
    _, _, _, fake, client = env

    first = _post_trial(client)
    second = _post_trial(client)

    assert second.status_code == 200
    data = second.json()
    assert data["status"] == "existing"
    assert data["key"] == first.json()["key"]
    assert data["expires_at"] == first.json()["expires_at"]
    assert len(fake.minted) == 1  # no second mint


def test_expired_trial_returns_403(env):
    _, proxy_db, _, fake, client = env
    past = (
        (datetime.now(timezone.utc) - timedelta(days=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    created = (
        (datetime.now(timezone.utc) - timedelta(days=8))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    proxy_db.create_trial(MACHINE_ID, "sk-fake-old", created, past)

    r = _post_trial(client)

    assert r.status_code == 403
    assert r.json() == {"detail": "trial_expired"}
    assert len(fake.minted) == 0  # expired machines never re-mint


@pytest.mark.parametrize(
    "machine_id",
    [
        "",  # missing
        "abc123",  # too short
        "z" * 64,  # not hex
        "a" * 63,  # wrong length
        "a" * 65,  # wrong length
    ],
)
def test_malformed_machine_id_rejected(env, machine_id):
    _, _, _, fake, client = env

    r = _post_trial(client, machine_id=machine_id)

    assert r.status_code in (400, 422)
    assert len(fake.minted) == 0


def test_litellm_payload_has_duration_and_metadata(env):
    _, _, patreon, fake, client = env

    r = _post_trial(client)
    assert r.status_code == 200

    payload = fake.minted[0]["payload"]
    assert payload["duration"] == "7d"
    assert payload["metadata"]["trial"] is True
    assert payload["metadata"]["machine_id"] == MACHINE_ID
    assert payload["key_alias"] == f"trial-{MACHINE_ID[:12]}"
    assert payload["max_budget"] == patreon.TRIAL_KEY_BUDGET
    assert payload["max_budget"] <= patreon.PATRON_KEY_BUDGET * 0.25
    assert payload["models"] == patreon.PATRON_KEY_MODELS
    # minted with the gateway admin key
    assert fake.minted[0]["headers"]["Authorization"] == "Bearer sk-master-test"


def test_trials_are_per_machine(env):
    _, _, _, fake, client = env

    r1 = _post_trial(client, machine_id=MACHINE_ID)
    r2 = _post_trial(client, machine_id=OTHER_MACHINE_ID)

    assert r1.json()["status"] == "created"
    assert r2.json()["status"] == "created"
    assert r1.json()["key"] != r2.json()["key"]
    assert len(fake.minted) == 2
