"""Patreon signup -> LiteLLM key flow (website/patreon.py).

Exercises the webhook + helpers against a temp SQLite DB with the LiteLLM
admin API monkeypatched — no network.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

WEBSITE_DIR = Path(__file__).resolve().parents[1] / "website"

WEBHOOK_SECRET = "test-webhook-secret"


def _load_patreon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # db.py creates ./data/mtgacoach.db relative to cwd
    monkeypatch.setenv("PATREON_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test")
    monkeypatch.syspath_prepend(str(WEBSITE_DIR))

    for name in ("db", "patreon"):
        sys.modules.pop(name, None)

    import db  # noqa: F401  (fresh module bound to tmp cwd)
    db.init_db()

    spec = importlib.util.spec_from_file_location("patreon", WEBSITE_DIR / "patreon.py")
    patreon = importlib.util.module_from_spec(spec)
    sys.modules["patreon"] = patreon
    spec.loader.exec_module(patreon)
    return patreon, db


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeLiteLLM:
    """Stands in for patreon._http() — records /key/generate and /key/delete."""

    def __init__(self):
        self.minted = []
        self.deleted = []
        self.fail_alias_once = False

    async def post(self, url, json=None, headers=None, **kw):
        if url.endswith("/key/generate"):
            alias = json["key_alias"]
            if self.fail_alias_once:
                self.fail_alias_once = False
                return FakeResponse(400, {"error": "alias already exists"})
            key = f"sk-fake-{len(self.minted)}"
            self.minted.append({"alias": alias, "payload": json, "key": key})
            return FakeResponse(200, {"key": key})
        if url.endswith("/key/delete"):
            self.deleted.extend(json["keys"])
            return FakeResponse(200, {"deleted_keys": json["keys"]})
        raise AssertionError(f"unexpected POST {url}")


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
        "data": {"attributes": {"patron_status": status}},
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
    assert payload["models"] == ["deepseek-v4-flash", "gemma-4-12b-it"]
    assert payload["metadata"]["patron_id"] == "12345"
    assert payload["max_budget"] == patreon.PATRON_KEY_BUDGET

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

    raw, headers = _signed(_pledge_event())
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


def test_membership_gate():
    sys.path.insert(0, str(WEBSITE_DIR))
    sys.modules.pop("patreon", None)
    import patreon

    active = {"included": [{"type": "member", "attributes": {"patron_status": "active_patron"}}]}
    former = {"included": [{"type": "member", "attributes": {"patron_status": "former_patron"}}]}
    empty = {"included": []}

    assert patreon._membership_allows_key(active)[0] is True
    assert patreon._membership_allows_key(former)[0] is False
    # Ambiguous (no member objects) fails open so key recovery keeps working.
    assert patreon._membership_allows_key(empty)[0] is True
