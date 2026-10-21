"""Patreon signup -> LiteLLM key flow (website/patreon.py).

Exercises the webhook + helpers against a temp SQLite DB with the LiteLLM
admin API monkeypatched — no network.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.fakes import FakeLiteLLM
from tests.website_app import isolate_website_modules, load_website_module

WEBHOOK_SECRET = "test-webhook-secret"


def _load_patreon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # db.py creates ./data/mtgacoach.db relative to cwd
    monkeypatch.setenv("PATREON_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test")
    isolate_website_modules(monkeypatch)

    db = load_website_module(monkeypatch, "db", "db.py")  # fresh module bound to tmp cwd
    db.init_db()
    patreon = load_website_module(monkeypatch, "patreon", "patreon.py")
    return patreon, db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    patreon, db = _load_patreon(tmp_path, monkeypatch)
    fake = FakeLiteLLM()
    monkeypatch.setattr(patreon, "_http", lambda: fake)
    app = FastAPI()
    app.include_router(patreon.router)
    return patreon, db, fake, TestClient(app)


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.md5).hexdigest()
    return raw, {"X-Patreon-Signature": sig}


def _pledge_event(email="pat@example.com", status="active_patron"):
    return {
        "data": {
            "type": "member",
            "id": "member-1",
            "attributes": {"patron_status": status},
            "relationships": {"user": {"data": {"type": "user", "id": "12345"}}},
        },
        "included": [
            {
                "type": "user",
                "id": "12345",
                "attributes": {"email": email, "full_name": "Pat Patron"},
            }
        ],
    }


def test_webhook_rejects_bad_signature(env):
    _, _, _, client = env
    raw, _ = _signed(_pledge_event())
    r = client.post(
        "/patreon/webhook",
        content=raw,
        headers={"X-Patreon-Signature": "0" * 32, "X-Patreon-Event": "members:pledge:create"},
    )
    assert r.status_code == 403


def test_pledge_create_mints_scoped_sk_key(env):
    patreon, db, fake, client = env
    raw, headers = _signed(_pledge_event())
    headers["X-Patreon-Event"] = "members:pledge:create"
    r = client.post("/patreon/webhook", content=raw, headers=headers)
    assert r.status_code == 200

    assert len(fake.minted) == 1
    payload = fake.minted[0]["payload"]
    assert payload["models"] == patreon.PATRON_KEY_MODELS
    assert payload["metadata"]["patron_id"] == "12345"
    assert payload["max_budget"] == patreon.PATRON_KEY_BUDGET
    for name, value in patreon.PATRON_LIMITS.items():
        assert payload[name] == value

    sub = db.check_license("sk-fake-0")
    assert sub and sub["email"] == "pat@example.com" and sub["status"] == "active"


def test_pledge_create_is_idempotent(env):
    _, _, fake, client = env
    raw, headers = _signed(_pledge_event())
    headers["X-Patreon-Event"] = "members:pledge:create"
    client.post("/patreon/webhook", content=raw, headers=headers)
    client.post("/patreon/webhook", content=raw, headers=headers)
    assert len(fake.minted) == 1  # second event reuses the recorded key


def test_legacy_mc_key_is_replaced_on_reactivation(env):
    patreon, db, fake, client = env
    db.create_subscriber(email="pat@example.com", name="Pat", days=0, notes="legacy")
    old = db.list_subscribers()[0]["license_key"]
    assert old.startswith("mc_")

    raw, headers = _signed(_pledge_event())
    headers["X-Patreon-Event"] = "members:pledge:update"
    client.post("/patreon/webhook", content=raw, headers=headers)

    row = [s for s in db.list_subscribers() if s["email"] == "pat@example.com"][0]
    assert row["license_key"] == "sk-fake-0"
    assert row["status"] == "active"
    assert old not in fake.deleted  # mc_ keys don't exist at the gateway


def test_pledge_delete_deletes_gateway_key(env):
    _, db, fake, client = env
    raw, headers = _signed(_pledge_event())
    headers["X-Patreon-Event"] = "members:pledge:create"
    client.post("/patreon/webhook", content=raw, headers=headers)

    raw, headers = _signed(_pledge_event(status="former_patron"))
    headers["X-Patreon-Event"] = "members:pledge:delete"
    client.post("/patreon/webhook", content=raw, headers=headers)

    assert fake.deleted == ["sk-fake-0"]
    row = [s for s in db.list_subscribers() if s["email"] == "pat@example.com"][0]
    assert row["status"] == "revoked"


def test_alias_conflict_retries_with_suffix(env):
    patreon, _, fake, _ = env
    fake.fail_alias_once = True
    import asyncio

    key = asyncio.run(patreon.mint_litellm_key("pat@example.com", "Pat", "12345"))
    assert key == "sk-fake-0"
    assert fake.minted[0]["alias"].startswith("patreon-pat@example.com-")


def test_membership_gate(env):
    patreon = env[0]
    active = {"included": [{"type": "member", "attributes": {"patron_status": "active_patron"}}]}
    former = {"included": [{"type": "member", "attributes": {"patron_status": "former_patron"}}]}
    empty = {"included": []}

    assert patreon._membership_allows_key(active)[0] is True
    assert patreon._membership_allows_key(former)[0] is False
    # Ambiguous (no member objects) fails open so key recovery keeps working.
    assert patreon._membership_allows_key(empty)[0] is True


@pytest.mark.parametrize(
    "event", ["members:create", "members:update", "members:pledge:create", "members:pledge:update"]
)
def test_v2_member_events_use_linked_user(env, event):
    _, db, fake, client = env
    body = _pledge_event()
    body["included"].append(
        {
            "type": "user",
            "id": "creator",
            "attributes": {"email": "creator@example.com", "full_name": "Creator"},
        }
    )
    raw, headers = _signed(body)
    headers["X-Patreon-Event"] = event
    assert client.post("/patreon/webhook", content=raw, headers=headers).status_code == 200
    assert fake.minted[0]["payload"]["metadata"]["patron_id"] == "12345"
    assert db.list_subscribers()[0]["email"] == "pat@example.com"


def test_member_attributes_supply_email_without_included_user(env):
    _, db, _, client = env
    body = _pledge_event()
    body["included"] = []
    body["data"]["attributes"].update(email="member@example.com", full_name="Member")
    raw, headers = _signed(body)
    headers["X-Patreon-Event"] = "members:update"
    assert client.post("/patreon/webhook", content=raw, headers=headers).status_code == 200
    assert db.list_subscribers()[0]["email"] == "member@example.com"


@pytest.mark.parametrize("event", ["members:pledge:delete", "members:update"])
def test_cancellation_preserves_active_paid_membership(env, event):
    _, db, fake, client = env
    body = _pledge_event()
    body["data"]["attributes"].update(currently_entitled_amount_cents=999, will_pay_amount_cents=0)
    raw, headers = _signed(body)
    headers["X-Patreon-Event"] = "members:create"
    client.post("/patreon/webhook", content=raw, headers=headers)
    headers["X-Patreon-Event"] = event
    assert client.post("/patreon/webhook", content=raw, headers=headers).status_code == 200
    assert not fake.deleted
    assert len(fake.minted) == 1
    assert db.list_subscribers()[0]["status"] == "active"


@pytest.mark.parametrize("status", ["declined_patron", "former_patron", None])
def test_member_update_revokes_inactive_membership(env, status):
    _, db, fake, client = env
    raw, headers = _signed(_pledge_event())
    headers["X-Patreon-Event"] = "members:create"
    client.post("/patreon/webhook", content=raw, headers=headers)
    raw, headers = _signed(_pledge_event(status=status))
    headers["X-Patreon-Event"] = "members:update"
    assert client.post("/patreon/webhook", content=raw, headers=headers).status_code == 200
    assert fake.deleted == ["sk-fake-0"]
    assert db.list_subscribers()[0]["status"] == "revoked"


@pytest.mark.parametrize("missing", ["status", "email", "relationship"])
def test_incomplete_payload_does_not_revoke(env, missing):
    _, db, fake, client = env
    raw, headers = _signed(_pledge_event())
    headers["X-Patreon-Event"] = "members:create"
    client.post("/patreon/webhook", content=raw, headers=headers)
    body = _pledge_event()
    if missing == "status":
        body["data"]["attributes"].pop("patron_status")
    elif missing == "email":
        body["included"] = []
    else:
        body["data"].pop("relationships")
    raw, headers = _signed(body)
    headers["X-Patreon-Event"] = "members:update"
    assert client.post("/patreon/webhook", content=raw, headers=headers).status_code == 400
    assert not fake.deleted
    assert db.list_subscribers()[0]["status"] == "active"


def test_webhook_requires_configured_secret(env, monkeypatch):
    patreon, _, fake, client = env
    monkeypatch.setattr(patreon, "PATREON_WEBHOOK_SECRET", "")
    raw, headers = _signed(_pledge_event())
    headers["X-Patreon-Event"] = "members:create"
    assert client.post("/patreon/webhook", content=raw, headers=headers).status_code == 503
    assert not fake.minted


def test_malformed_json_returns_400(env):
    _, _, _, client = env
    raw = b"{bad json"
    headers = {
        "X-Patreon-Event": "members:update",
        "X-Patreon-Signature": hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.md5).hexdigest(),
    }
    assert client.post("/patreon/webhook", content=raw, headers=headers).status_code == 400


@pytest.mark.parametrize("operation", ["mint", "revoke"])
def test_gateway_failure_is_retryable_without_local_state_change(env, monkeypatch, operation):
    patreon, db, _, client = env
    raw, headers = _signed(_pledge_event())
    headers["X-Patreon-Event"] = "members:create"
    if operation == "revoke":
        client.post("/patreon/webhook", content=raw, headers=headers)
        raw, headers = _signed(_pledge_event(status="former_patron"))
        headers["X-Patreon-Event"] = "members:update"

    async def fail(*args, **kwargs):
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr(patreon, "mint_litellm_key" if operation == "mint" else "delete_litellm_key", fail)
    client = TestClient(client.app, raise_server_exceptions=False)
    assert client.post("/patreon/webhook", content=raw, headers=headers).status_code == 500
    rows = db.list_subscribers()
    assert (rows[0]["status"] == "active") if operation == "revoke" else not rows
