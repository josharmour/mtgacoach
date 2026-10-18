"""POST /api/trial — free-trial key auto-provisioning (website/app.py).

Loads the website app against a temp SQLite DB with the LiteLLM admin API
monkeypatched (no network), mirroring tests/test_proxy_server_signup.py.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

WEBSITE_DIR = Path(__file__).resolve().parents[1] / "website"

MACHINE_ID = "a" * 64
OTHER_MACHINE_ID = "b" * 64


from tests.fakes import FakeLiteLLM


def _load_proxy_app(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    (tmp_path / "static").mkdir()
    (tmp_path / "templates").mkdir()
    config_path.write_text(
        """
server:
  host: "127.0.0.1"
  port: 8443
providers: []
default_model: "gpt-5.4"
admin:
  username: "admin"
  password: "test-admin"
database:
  path: "./data/mtgacoach.db"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test")
    monkeypatch.syspath_prepend(str(WEBSITE_DIR))

    for name in ("db", "providers", "patreon"):
        sys.modules.pop(name, None)

    module_name = "proxy_app_trial_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, WEBSITE_DIR / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    return module, sys.modules["db"], sys.modules["patreon"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    proxy_app, proxy_db, patreon = _load_proxy_app(tmp_path, monkeypatch)
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


# ── /v1/subscription/check answers trial keys (the desktop access badge) ──


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check(client, key):
    return client.post(
        "/v1/subscription/check", headers={"Authorization": f"Bearer {key}"}, json={"license_key": key}
    )


def test_subscription_check_answers_an_active_trial_key(env):
    """Trial keys live in the trials table, not subscribers: the repo route
    answered them 401 ("Key invalid"); production answers kind="trial"."""
    _, _, _, _, client = env
    issued = _post_trial(client).json()

    r = _check(client, issued["key"])

    assert r.status_code == 200
    data = r.json()
    assert (data["status"], data["kind"], data["name"]) == ("trial", "trial", "")
    expires = datetime.fromisoformat(issued["expires_at"].replace("Z", "+00:00"))
    assert data["expires_at"] == expires.timestamp()

    # The desktop badge reads this as a live countdown, not "Key invalid".
    from arenamcp.subscription import SubscriptionStatus, describe_access

    status = SubscriptionStatus(
        status=data["status"], expires_at=data["expires_at"], kind=data["kind"], name=data["name"]
    )
    text, _tone, _tip, needs = describe_access(issued["key"], status, now=expires.timestamp() - 86400 * 3)
    assert text == "Trial · 3d 0h left" and not needs


@pytest.mark.parametrize("stored_expiry", ["past", "garbage"])
def test_subscription_check_says_402_for_a_lapsed_trial(env, stored_expiry):
    _, proxy_db, _, _, client = env
    now = datetime.now(timezone.utc)
    stored = _iso(now - timedelta(hours=1)) if stored_expiry == "past" else "not-a-date"
    proxy_db.create_trial(MACHINE_ID, "sk-fake-lapsed", _iso(now - timedelta(days=7)), stored)

    r = _check(client, "sk-fake-lapsed")

    assert r.status_code == 402
    data = r.json()
    assert (data["status"], data["kind"]) == ("expired", "trial")
    assert "trial has ended" in data["message"]
    if stored_expiry == "past":
        assert data["expires_at"] == datetime.fromisoformat(stored.replace("Z", "+00:00")).timestamp()


def test_subscription_check_marks_subscribers_and_rejects_unknown_keys(env):
    _, proxy_db, _, _, client = env
    sub = proxy_db.create_subscriber(email="p@example.com", name="Pat Ron", days=30)

    r = _check(client, sub["license_key"])
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["kind"], r.json()["name"]) == ("active", "subscriber", "Pat Ron")

    r = _check(client, "sk-nobody")
    assert r.status_code == 401 and r.json()["status"] == "invalid"
