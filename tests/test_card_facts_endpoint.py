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
import tests.website_app as website_app_helper
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
    # S1's file list is website/card_facts.py plus this test file, so the
    # shared helper's module list is extended here (for teardown-safe
    # isolation of card_facts) instead of by editing tests/website_app.py.
    monkeypatch.setattr(
        website_app_helper,
        "WEBSITE_MODULES",
        website_app_helper.WEBSITE_MODULES + ("card_facts",),
    )
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


def test_admin_upsert_rejects_nonfinite_numbers_and_lookup_stays_clean(env):
    """bug: Dotty verification of c8d23ed — raw records carrying NaN,
    Infinity, -Infinity or 1e400 were stored verbatim and the next customer
    lookup failed on them.  Intake now rejects each such record and lookup
    keeps serving the valid ones."""
    _, proxy_db, _, _, _, client, _tmp = env
    good = _record(_hash(1))
    raw = (
        '{"records":['
        + json.dumps(good)
        + ',{"rules_hash":"'
        + _hash(2)
        + '","v":NaN}'
        + ',{"rules_hash":"'
        + _hash(3)
        + '","v":Infinity}'
        + ',{"rules_hash":"'
        + _hash(4)
        + '","v":-Infinity}'
        + ',{"rules_hash":"'
        + _hash(5)
        + '","v":1e400}'
        + "]}"
    )
    resp = client.post("/v1/card-facts/admin/upsert", content=raw.encode(), headers=ADMIN)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["stored"] == 1
    assert [r["index"] for r in result["rejected"]] == [1, 2, 3, 4]
    assert all("strict finite JSON" in r["reason"] for r in result["rejected"])
    with proxy_db.get_db() as conn:
        stored = {r["rules_hash"] for r in conn.execute("SELECT rules_hash FROM card_facts")}
    assert stored == {_hash(1)}
    body = _lookup(client, [_hash(i) for i in range(1, 6)]).json()
    assert set(body["found"]) == {_hash(1)}
    assert body["missing"] == [_hash(i) for i in range(2, 6)]


def test_admin_upsert_rejects_out_of_range_schema_version_without_aborting_batch(env):
    """bug: Dotty verification of c8d23ed — schema_version=2**80 was bound
    straight into a SQLite INTEGER column and raised, failing the whole
    batch with a 500.  The record is now rejected at intake and the rest of
    the batch stores."""
    _, proxy_db, _, _, _, client, _tmp = env
    h_big = _hash(9)
    result = _store(
        client,
        _record(_hash(7)),
        {"rules_hash": h_big, "name": "Big Version", "schema_version": 2**80},
        _record(_hash(8), name="After Big"),
    )
    assert result["stored"] == 2
    assert [r["index"] for r in result["rejected"]] == [1]
    assert "signed 64-bit" in result["rejected"][0]["reason"]
    with proxy_db.get_db() as conn:
        stored = {r["rules_hash"] for r in conn.execute("SELECT rules_hash FROM card_facts")}
    assert stored == {_hash(7), _hash(8)}


def test_lookup_serves_legacy_nonfinite_row_as_missing_instead_of_500(env):
    """bug: Dotty verification of c8d23ed — a row whose payload carries a
    bare NaN literal (written before strict intake existed) made customer
    lookup return 500.  Serialization now serves such a row as missing."""
    _, proxy_db, _, _, _, client, _tmp = env
    h_bad, h_good = _hash(11), _hash(12)
    with proxy_db.get_db() as conn:
        conn.execute(
            "INSERT INTO card_facts (rules_hash, name, schema_version, payload, updated_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (h_bad, "Poisoned Row", 1, '{"v":NaN}', time.time(), "ok"),
        )
    _store(client, _record(h_good))
    resp = _lookup(client, [h_bad, h_good])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["found"]) == {h_good}
    assert body["missing"] == [h_bad]


def test_admin_upsert_rejects_lone_surrogate_strings_and_lookup_stays_clean(env):
    """bug: Dotty verification of 55a8f20 — an escaped lone surrogate
    (\\ud800) in an ordinary payload field was stored (json.dumps re-escapes
    it into ASCII-safe payload text) and the next customer lookup failed
    with 500 when the response layer serialized it back; the same string in
    ``name`` failed the admin request itself when SQLite encoded the column.
    Intake now rejects any string that is not valid Unicode, in values and
    in object keys alike, without aborting the rest of the batch."""
    _, proxy_db, _, _, _, client, _tmp = env
    good = _record(_hash(21))
    bad_value = {"rules_hash": _hash(22), "name": "Bad Value", "schema_version": 1, "v": "\ud800"}
    bad_key = {"rules_hash": _hash(23), "name": "Bad Key", "schema_version": 1, "\ud800": 1}
    bad_name = {"rules_hash": _hash(24), "name": "\ud800", "schema_version": 1}
    raw = json.dumps({"records": [good, bad_value, bad_key, bad_name]}).encode()
    resp = client.post("/v1/card-facts/admin/upsert", content=raw, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["stored"] == 1
    assert [r["index"] for r in result["rejected"]] == [1, 2, 3]
    assert all("not valid Unicode" in r["reason"] for r in result["rejected"])
    with proxy_db.get_db() as conn:
        stored = {r["rules_hash"] for r in conn.execute("SELECT rules_hash FROM card_facts")}
    assert stored == {_hash(21)}
    body = _lookup(client, [_hash(i) for i in range(21, 25)]).json()
    assert set(body["found"]) == {_hash(21)}
    assert body["missing"] == [_hash(i) for i in range(22, 25)]


def test_lookup_serves_legacy_lone_surrogate_row_as_missing_instead_of_500(env):
    """bug: Dotty verification of 55a8f20 — a row whose payload carries an
    escaped lone surrogate (written before intake validated strings) made
    customer lookup return 500 at response serialization.  Such a row is
    now served as missing, like the non-finite legacy rows."""
    _, proxy_db, _, _, _, client, _tmp = env
    h_bad, h_good = _hash(31), _hash(32)
    with proxy_db.get_db() as conn:
        conn.execute(
            "INSERT INTO card_facts (rules_hash, name, schema_version, payload, updated_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (h_bad, "Surrogated Row", 1, '{"v":"\\ud800"}', time.time(), "ok"),
        )
    _store(client, _record(h_good))
    resp = _lookup(client, [h_bad, h_good])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["found"]) == {h_good}
    assert body["missing"] == [h_bad]


def test_admin_upsert_rejects_deeply_nested_record_without_aborting_batch(env):
    """bug: Dotty verification of a2a04bf — a valid admin batch containing
    one record with a 1000-deep nested array (~2.3 kB, far under the record
    cap) returned 500 and aborted the whole batch: the recursive strict-JSON
    walk raised RecursionError.  Deep records are now rejected per-record
    with a bounded-depth reason and their neighbours still store."""
    _, proxy_db, _, _, _, client, _tmp = env
    nested: dict = {"v": None}
    for _ in range(1000):
        nested = {"v": [nested]}
    deep = {"rules_hash": _hash(41), "v": nested}
    good = _record(_hash(42))
    result = _store(client, deep, good)
    assert result["stored"] == 1
    assert [r["index"] for r in result["rejected"]] == [0]
    assert "nests deeper than" in result["rejected"][0]["reason"]
    with proxy_db.get_db() as conn:
        stored = {r["rules_hash"] for r in conn.execute("SELECT rules_hash FROM card_facts")}
    assert stored == {_hash(42)}
    body = _lookup(client, [_hash(41), _hash(42)]).json()
    assert set(body["found"]) == {_hash(42)}
    assert body["missing"] == [_hash(41)]


def test_lookup_serves_legacy_deeply_nested_row_as_missing_instead_of_500(env):
    """bug: Dotty verification of a2a04bf — a ~2 kB legacy row whose payload
    nests 1000 deep made customer lookup return 500.  Like the non-finite
    and surrogate legacy rows, it is now served as missing."""
    _, proxy_db, _, _, _, client, _tmp = env
    h_bad, h_good = _hash(51), _hash(52)
    payload = json.dumps({"v": None}, separators=(",", ":"))
    for _ in range(1000):
        payload = json.dumps({"v": json.loads(payload)}, separators=(",", ":"))
    assert len(payload) < 10000
    with proxy_db.get_db() as conn:
        conn.execute(
            "INSERT INTO card_facts (rules_hash, name, schema_version, payload, updated_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (h_bad, "Deep Legacy Row", 1, payload, time.time(), "ok"),
        )
    _store(client, _record(h_good))
    resp = _lookup(client, [h_bad, h_good])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["found"]) == {h_good}
    assert body["missing"] == [h_bad]


def test_lookup_rejects_body_that_nests_too_deeply_to_parse(env):
    """bug: Dotty verification of a2a04bf — a ~20 kB lookup body whose
    unused field nests 10000 deep crashed json.loads with RecursionError
    and returned 500.  Parsing now answers 400 instead."""
    _, _proxy_db, _, _, _, client, _tmp = env
    raw = '{"hashes":["' + _hash(61) + '"],"unused":'
    raw += "[" * 10000 + "]" * 10000 + "}"
    assert len(raw) < 30000
    resp = client.post(
        "/v1/card-facts/lookup",
        content=raw.encode(),
        headers={"Authorization": f"Bearer {CUSTOMER_KEY}"},
    )
    assert resp.status_code == 400, resp.text
    assert "too deeply" in resp.json()["detail"]


def test_normal_unicode_round_trips_through_store_and_lookup(env):
    """Guard for the surrogate rejection: ordinary non-ASCII text (accents,
    emoji beyond the BMP, CJK) must keep storing and looking up unchanged,
    in payload values and in the name column alike."""
    _, proxy_db, _, _, _, client, _tmp = env
    h = _hash(41)
    record = _record(h, name="José's Ætherflux 🚀 渦", oracle_text="Trample — deals ⚡ damage")
    result = _store(client, record)
    assert result == {"stored": 1, "rejected": []}
    with proxy_db.get_db() as conn:
        (name,) = conn.execute("SELECT name FROM card_facts WHERE rules_hash = ?", (h,)).fetchone()
    assert name == "José's Ætherflux 🚀 渦"
    resp = _lookup(client, [h])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["missing"] == []
    assert body["found"][h] == record


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


# ---------------------------------------------------------------------------
# S2: admin read routes (the ingest job's view of the store)
# ---------------------------------------------------------------------------


def _seed_wanted(proxy_db, rows: list[tuple[str, int, float]]) -> None:
    """Insert card_facts_wanted rows directly (hash, wanted_count, last_requested_at)."""
    with proxy_db.get_db() as conn:
        for digest, count, last_requested in rows:
            conn.execute(
                "INSERT OR REPLACE INTO card_facts_wanted"
                " (rules_hash, wanted_count, last_requested_at) VALUES (?, ?, ?)",
                (digest, count, last_requested),
            )


def test_admin_wanted_requires_admin_credential(env):
    """The work list is operational data: same admin gate as upsert."""
    _, proxy_db, _, _, _, client, _tmp = env
    _seed_wanted(proxy_db, [(_hash(1), 3, 1000.0)])
    resp = client.get("/v1/card-facts/admin/wanted")
    assert resp.status_code == 403
    resp = client.get("/v1/card-facts/admin/wanted", headers={"X-Admin-Key": "not-the-admin-key"})
    assert resp.status_code == 403


def test_admin_wanted_ranks_by_demand_then_recency(env):
    """Most-demanded hash first; ties break on the newer request.

    Built from the real demand counter S1's lookup feeds: two lookups for
    hash 2 and one for hash 3 (test_missing_hashes_increment_the_wanted_counter
    covers the counting itself); here hash 1 is seeded with the same count as
    hash 2 but an older timestamp and must rank below it.
    """
    _, proxy_db, _, _, _, client, _tmp = env
    _seed_wanted(
        proxy_db,
        [
            (_hash(1), 2, 1000.0),  # same count as hash 2, older
            (_hash(2), 2, 2000.0),  # newer → ranks first among the twos
            (_hash(3), 5, 500.0),   # highest count → first overall
        ],
    )
    resp = client.get("/v1/card-facts/admin/wanted", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    wanted = resp.json()["wanted"]
    assert [row["rules_hash"] for row in wanted] == [_hash(3), _hash(2), _hash(1)]
    assert wanted[0]["wanted_count"] == 5
    assert set(wanted[0]) == {"rules_hash", "wanted_count", "last_requested_at"}


def test_admin_wanted_limit_is_capped_and_defaulted(env):
    """limit is bounded (5000) and defaults to 500 rows."""
    *_, client, _tmp = env
    resp = client.get("/v1/card-facts/admin/wanted?limit=5001", headers=ADMIN)
    assert resp.status_code == 422
    resp = client.get("/v1/card-facts/admin/wanted?limit=0", headers=ADMIN)
    assert resp.status_code == 422
    resp = client.get("/v1/card-facts/admin/wanted", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"wanted": []}


def test_admin_records_returns_store_statuses(env):
    """The ingest job sees ok/failed/review per hash; unknown hashes are absent."""
    _, _, _, _, _, client, _tmp = env
    ok_h, failed_h, review_h = _hash(1), _hash(2), _hash(3)
    _store(
        client,
        _record(ok_h),
        _record(failed_h, status="failed"),
        _record(review_h, status="review"),
    )
    resp = client.get(
        f"/v1/card-facts/admin/records?hashes={ok_h},{failed_h},{review_h},{_hash(4)}",
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    records = resp.json()["records"]
    assert records == {ok_h: "ok", failed_h: "failed", review_h: "review"}


def test_admin_records_requires_admin_credential(env):
    *_, client, _tmp = env
    resp = client.get(f"/v1/card-facts/admin/records?hashes={_hash(1)}")
    assert resp.status_code == 403


def test_admin_records_rejects_bad_input_like_lookup_does(env):
    """Same validation contract as the customer-facing lookup route."""
    *_, client, _tmp = env
    # malformed hash
    resp = client.get(f"/v1/card-facts/admin/records?hashes=NOTAHASH,{_hash(1)}", headers=ADMIN)
    assert resp.status_code == 422
    assert "hex" in resp.json()["detail"]
    # uppercase is malformed here too (hashes are stored lowercase)
    resp = client.get(f"/v1/card-facts/admin/records?hashes={_hash(1).upper()}", headers=ADMIN)
    assert resp.status_code == 422
    # over the per-request cap (same 300 as lookup)
    many = ",".join(_hash(i) for i in range(301))
    resp = client.get(f"/v1/card-facts/admin/records?hashes={many}", headers=ADMIN)
    assert resp.status_code == 422
    assert "300" in resp.json()["detail"]
    # duplicates are deduplicated, not an error
    h = _hash(9)
    resp = client.get(f"/v1/card-facts/admin/records?hashes={h},{h}", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"records": {}}
