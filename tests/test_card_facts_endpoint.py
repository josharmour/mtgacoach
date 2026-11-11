"""Card-facts store and lookup (website/card_facts.py).

POST /v1/card-facts/lookup serves the shared ``ok`` records to any valid
customer key and counts demand for missing hashes in ``card_facts_wanted``
(the S2 ingest job's work list).  POST /v1/card-facts/admin/upsert loads
records with the admin credential.  Loads the website app against a temp
SQLite DB, mirroring tests/test_match_bundle_endpoint.py, with LiteLLM's
/key/info monkeypatched (no network).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from tests.website_app import load_website_app

ADMIN = {"X-Admin-Key": "test-admin"}
CUSTOMER_KEY = "sk-cf-customer"


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeKeyInfo:
    """Stands in for the LiteLLM client: answers GET /key/info about the calling key only."""

    def __init__(self):
        self.live: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail = False

    async def get(self, url, params=None, headers=None, **kw):
        assert url.endswith("/key/info")
        assert not params
        auth = (headers or {}).get("Authorization", "")
        assert auth.startswith("Bearer ") and "master" not in auth
        key = auth[7:]
        self.calls.append(key)
        if self.fail:
            raise ConnectionError("gateway down")
        if key in self.live:
            return _FakeResp(200, {"key": key, "info": self.live[key]})
        return _FakeResp(404, {"error": "no such key"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    proxy_app, proxy_db, patreon = load_website_app(tmp_path, monkeypatch, "proxy_app_card_facts_test")
    cf = sys.modules["card_facts"]
    fake = FakeKeyInfo()
    monkeypatch.setattr(patreon, "_http", lambda: fake)
    sys.modules["match_bundles"]._key_info_cache.clear()
    # The per-key lookup interval is covered by its own test; everything
    # else looks up several times from one key within a second.
    monkeypatch.setattr(cf, "MIN_LOOKUP_INTERVAL_S", 0.0)
    # Seed the default customer key so lookup tests authenticate like the
    # match-bundle suite's (every lookup test there inserts its key first).
    with proxy_db.get_db() as conn:
        conn.execute(
            "INSERT INTO subscribers (license_key, email, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (CUSTOMER_KEY, "p@example.com", "Patron", "active", time.time()),
        )
    with TestClient(proxy_app.app) as client:
        yield proxy_app, proxy_db, patreon, cf, fake, client, tmp_path


def _hash(seed: int) -> str:
    return hashlib.sha256(str(seed).encode()).hexdigest()


def _record(rules_hash: str, name="Lightning Bolt", schema_version=1, status=None, **extra) -> dict:
    record = {
        "rules_hash": rules_hash,
        "name": name,
        "schema_version": schema_version,
        "faces": [{"oracle_text": "Deal 3 damage to any target."}],
    }
    if status is not None:
        record["status"] = status
    record.update(extra)
    return record


def _upsert(client, records) -> dict:
    resp = client.post("/v1/card-facts/admin/upsert", json={"records": records}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _store(client, *records) -> dict:
    return _upsert(client, list(records))


def _lookup(client, hashes, key=CUSTOMER_KEY):
    return client.post(
        "/v1/card-facts/lookup",
        json={"hashes": hashes},
        headers={"Authorization": f"Bearer {key}"},
    )


def _subscriber(proxy_db, key=CUSTOMER_KEY, status="active"):
    with proxy_db.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO subscribers (license_key, email, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, "p@example.com", "Patron", status, time.time()),
        )
    return key


def _trial(proxy_db, key="sk-trial-cf", days=3):
    now = datetime.now(timezone.utc)
    proxy_db.create_trial("c" * 64, key, now.isoformat(), (now + timedelta(days=days)).isoformat())
    return key


# ---------------------------------------------------------------------------
# Admin upsert auth
# ---------------------------------------------------------------------------


def test_admin_upsert_without_credential_is_403(env):
    *_, client, _tmp = env
    resp = client.post("/v1/card-facts/admin/upsert", json={"records": [_record(_hash(1))]})
    assert resp.status_code == 403


def test_admin_upsert_with_wrong_credential_is_403(env):
    *_, client, _tmp = env
    resp = client.post(
        "/v1/card-facts/admin/upsert",
        json={"records": [_record(_hash(1))]},
        headers={"X-Admin-Key": "not-the-admin-key"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Admin upsert behaviour
# ---------------------------------------------------------------------------


def test_admin_upsert_stores_records_and_columns(env):
    _, proxy_db, _, _, _, client, _tmp = env
    h1, h2 = _hash(1), _hash(2)
    result = _store(client, _record(h1), _record(h2, name="Counterspell", schema_version=2))
    assert result == {"stored": 2, "rejected": []}
    with proxy_db.get_db() as conn:
        rows = {r["rules_hash"]: r for r in conn.execute("SELECT * FROM card_facts").fetchall()}
    assert set(rows) == {h1, h2}
    assert rows[h1]["name"] == "Lightning Bolt"
    assert rows[h1]["schema_version"] == 1
    assert rows[h1]["status"] == "ok"
    assert rows[h2]["name"] == "Counterspell"
    assert rows[h2]["schema_version"] == 2
    assert abs(rows[h1]["updated_at"] - time.time()) < 60


def test_admin_upsert_replaces_existing_record(env):
    _, proxy_db, _, _, _, client, _tmp = env
    h = _hash(1)
    _store(client, _record(h))
    before = time.time()
    time.sleep(0.01)
    result = _store(client, _record(h, name="Updated Face"))
    assert result["stored"] == 1
    with proxy_db.get_db() as conn:
        row = conn.execute("SELECT * FROM card_facts WHERE rules_hash = ?", (h,)).fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM card_facts").fetchone()["n"]
    assert row["name"] == "Updated Face"
    assert row["updated_at"] >= before
    assert count == 1  # upsert replaced the row instead of duplicating it


def test_admin_upsert_rejects_malformed_records_individually(env):
    _, proxy_db, _, _, _, client, _tmp = env
    big_blob = {"rules_hash": _hash(2), "blob": "x" * (65 * 1024)}
    records = [
        _record(_hash(1)),
        "not-an-object",
        {"name": "no hash"},
        {"rules_hash": "NOTAHASH", "name": "bad hash"},
        {"rules_hash": _hash(3).upper(), "name": "uppercase hash"},
        big_blob,
        {"rules_hash": _hash(4), "status": "banana"},
        _record(_hash(5)),
    ]
    result = _upsert(client, records)
    assert result["stored"] == 2
    rejected = result["rejected"]
    assert [r["index"] for r in rejected] == [1, 2, 3, 4, 5, 6]
    reasons = [r["reason"] for r in rejected]
    assert any("JSON object" in r for r in reasons[0:1])
    assert all("rules_hash" in r for r in reasons[1:4])
    assert any("exceeds" in r for r in reasons[4:5])
    assert any("status" in r for r in reasons[5:])
    with proxy_db.get_db() as conn:
        stored = {r["rules_hash"] for r in conn.execute("SELECT rules_hash FROM card_facts")}
    assert stored == {_hash(1), _hash(5)}


def test_admin_upsert_batch_limit(env):
    _, _, _, cf, _, client, _tmp = env
    original = cf.MAX_BATCH_RECORDS
    cf.MAX_BATCH_RECORDS = 2
    try:
        resp = client.post(
            "/v1/card-facts/admin/upsert",
            json={"records": [_record(_hash(i)) for i in range(3)]},
            headers=ADMIN,
        )
        assert resp.status_code == 422
        assert "capped at 2" in resp.json()["detail"]
        ok = client.post(
            "/v1/card-facts/admin/upsert",
            json={"records": [_record(_hash(i)) for i in range(2)]},
            headers=ADMIN,
        )
        assert ok.status_code == 200 and ok.json()["stored"] == 2
    finally:
        cf.MAX_BATCH_RECORDS = original


def test_admin_upsert_body_must_be_records_list(env):
    *_, client, _tmp = env
    resp = client.post("/v1/card-facts/admin/upsert", json={"nope": []}, headers=ADMIN)
    assert resp.status_code == 422
    resp = client.post("/v1/card-facts/admin/upsert", json=[_record(_hash(1))], headers=ADMIN)
    assert resp.status_code == 400


def test_admin_upsert_invalid_json_is_400(env):
    *_, client, _tmp = env
    resp = client.post("/v1/card-facts/admin/upsert", content=b"{broken", headers=dict(ADMIN))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Lookup auth
# ---------------------------------------------------------------------------


def test_lookup_requires_bearer_key(env):
    *_, client, _tmp = env
    resp = client.post("/v1/card-facts/lookup", json={"hashes": [_hash(1)]})
    assert resp.status_code == 401


def test_unknown_key_is_rejected_after_gateway_says_no(env):
    _, _, _, _, fake, client, _tmp = env
    resp = _lookup(client, [_hash(1)], key="sk-nobody")
    assert resp.status_code == 401
    assert fake.calls == ["sk-nobody"]


def test_expired_subscriber_gets_402(env):
    _, proxy_db, _, _, fake, client, _tmp = env
    key = _subscriber(proxy_db)
    with proxy_db.get_db() as conn:
        conn.execute("UPDATE subscribers SET status='expired' WHERE license_key=?", (key,))
    resp = _lookup(client, [_hash(1)], key=key)
    assert resp.status_code == 402
    assert fake.calls == []  # never falls through to the gateway


def test_trial_key_from_trials_table_is_accepted(env):
    _, proxy_db, _, _, fake, client, _tmp = env
    key = _trial(proxy_db)
    resp = _lookup(client, [_hash(1)], key=key)
    assert resp.status_code == 200
    assert fake.calls == []


def test_gateway_outage_is_a_401_not_a_500(env):
    _, _, _, _, fake, client, _tmp = env
    fake.fail = True
    # A key known to neither subscribers nor trials must reach the gateway;
    # when the gateway is down the answer is 401, never a 500.
    resp = _lookup(client, [_hash(1)], key="sk-gateway-outage")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Lookup behaviour
# ---------------------------------------------------------------------------


def test_lookup_hit_and_miss_round_trip(env):
    _, _, _, _, _, client, _tmp = env
    hit_hash = _hash(1)
    record = _record(hit_hash)
    _store(client, record)
    resp = _lookup(client, [hit_hash])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["missing"] == []
    assert set(body["found"]) == {hit_hash}
    assert body["found"][hit_hash] == record  # payload round-trips as parsed JSON


def test_lookup_reports_missing_hashes(env):
    _, _, _, _, _, client, _tmp = env
    _store(client, _record(_hash(1)))
    resp = _lookup(client, [_hash(1), _hash(2), _hash(3)])
    body = resp.json()
    assert set(body["found"]) == {_hash(1)}
    assert body["missing"] == [_hash(2), _hash(3)]


def test_missing_hashes_increment_the_wanted_counter(env):
    _, proxy_db, _, _, _, client, _tmp = env
    _store(client, _record(_hash(1)))
    _lookup(client, [_hash(1), _hash(2)])
    _lookup(client, [_hash(2), _hash(3)])
    with proxy_db.get_db() as conn:
        rows = {r["rules_hash"]: r["wanted_count"] for r in conn.execute("SELECT * FROM card_facts_wanted")}
    assert rows == {_hash(2): 2, _hash(3): 1}  # only missing hashes are counted, once per request
    assert _hash(1) not in rows


def test_lookup_caps_at_300_hashes(env):
    *_, client, _tmp = env
    resp = _lookup(client, [_hash(i) for i in range(301)])
    assert resp.status_code == 422
    assert "300" in resp.json()["detail"]
    ok = _lookup(client, [_hash(i) for i in range(300)])
    assert ok.status_code == 200


def test_lookup_rejects_malformed_hashes(env):
    *_, client, _tmp = env
    for bad in ["NOTAHASH", _hash(1).upper(), "short", 42, None]:
        resp = _lookup(client, [bad])
        assert resp.status_code == 422, bad
        assert "hex" in resp.json()["detail"]


def test_duplicate_hashes_in_one_lookup_are_deduplicated(env):
    _, proxy_db, _, _, _, client, _tmp = env
    h = _hash(9)
    resp = _lookup(client, [h, h, h])
    body = resp.json()
    assert body["missing"] == [h]  # counted once, not three times
    with proxy_db.get_db() as conn:
        row = conn.execute("SELECT wanted_count FROM card_facts_wanted WHERE rules_hash=?", (h,)).fetchone()
    assert row["wanted_count"] == 1


def test_failed_and_review_records_are_not_served(env):
    _, proxy_db, _, _, _, client, _tmp = env
    ok_h, failed_h, review_h = _hash(1), _hash(2), _hash(3)
    _store(
        client,
        _record(ok_h),
        _record(failed_h, status="failed"),
        _record(review_h, status="review"),
    )
    resp = _lookup(client, [ok_h, failed_h, review_h])
    body = resp.json()
    assert set(body["found"]) == {ok_h}
    assert sorted(body["missing"]) == sorted([failed_h, review_h])


def test_lookup_body_must_be_a_hash_list(env):
    *_, client, _tmp = env
    key_headers = {"Authorization": f"Bearer {CUSTOMER_KEY}"}
    resp = client.post("/v1/card-facts/lookup", json={"nope": []}, headers=key_headers)
    assert resp.status_code == 422
    resp = client.post("/v1/card-facts/lookup", json=[_hash(1)], headers=key_headers)
    assert resp.status_code == 400
    resp = client.post("/v1/card-facts/lookup", content=b"", headers=key_headers)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Lookup rate limiting (mirrors the match-bundle upload limits)
# ---------------------------------------------------------------------------


def test_daily_lookup_quota_answers_429_with_retry_after(env):
    _, _, _, cf, _, client, _tmp = env
    original = cf.MAX_LOOKUPS_PER_DAY
    cf.MAX_LOOKUPS_PER_DAY = 2
    try:
        assert _lookup(client, [_hash(1)]).status_code == 200
        assert _lookup(client, [_hash(2)]).status_code == 200
        resp = _lookup(client, [_hash(3)])
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) > 0
        assert "quota" in resp.json()["detail"].lower()
    finally:
        cf.MAX_LOOKUPS_PER_DAY = original


def test_minimum_interval_between_lookups_is_enforced(env):
    _, _, _, cf, _, client, _tmp = env
    original = cf.MIN_LOOKUP_INTERVAL_S
    cf.MIN_LOOKUP_INTERVAL_S = 60.0
    try:
        assert _lookup(client, [_hash(1)]).status_code == 200
        resp = _lookup(client, [_hash(2)])
        assert resp.status_code == 429
        retry_after = int(resp.headers["Retry-After"])
        assert 0 < retry_after <= 60
        assert "retry" in resp.json()["detail"].lower()
    finally:
        cf.MIN_LOOKUP_INTERVAL_S = original


def test_rate_limited_lookups_do_not_touch_the_wanted_counter(env):
    _, proxy_db, _, cf, _, client, _tmp = env
    original = cf.MIN_LOOKUP_INTERVAL_S
    cf.MIN_LOOKUP_INTERVAL_S = 60.0
    try:
        _store(client, _record(_hash(1)))
        assert _lookup(client, [_hash(1)]).status_code == 200
        blocked = _lookup(client, [_hash(2)])
        assert blocked.status_code == 429
        with proxy_db.get_db() as conn:
            rows = conn.execute("SELECT rules_hash FROM card_facts_wanted").fetchall()
        assert [r["rules_hash"] for r in rows] == []  # the blocked request recorded nothing
    finally:
        cf.MIN_LOOKUP_INTERVAL_S = original


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------


def test_responses_never_echo_the_license_key(env):
    _, proxy_db, _, _, _, client, _tmp = env
    key = "sk-secret-cf-key-123456"
    _subscriber(proxy_db, key)
    resp = _lookup(client, [_hash(1)], key=key)
    assert resp.status_code == 200
    assert key not in resp.text


def test_lookup_does_not_leak_stored_payloads_of_other_statuses_via_found(env):
    """A 'review' record must never reach customers even if its hash is requested."""
    _, _, _, _, _, client, _tmp = env
    h = _hash(7)
    _store(client, _record(h, status="review"))
    body = _lookup(client, [h]).json()
    assert body["found"] == {} and body["missing"] == [h]
