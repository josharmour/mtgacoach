"""Match-bundle intake (website/match_bundles.py).

POST /api/match-bundle stores one gzip JSON bundle per match on the data
volume; the admin listing/download/stats endpoints feed the offline
reviewer and the dashboard.  Loads the website app against a temp SQLite
DB, mirroring tests/test_trial_endpoint.py, with LiteLLM's /key/info
monkeypatched (no network).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.util
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

WEBSITE_DIR = Path(__file__).resolve().parents[1] / "website"

INSTALL_ID = "inst_" + "a" * 32
OTHER_INSTALL_ID = "inst_" + "b" * 32
MATCH_ID = "3f4b9d8e-1111-2222-3333-444455556666"
ADMIN = {"X-Admin-Key": "test-admin"}


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

    for name in ("db", "providers", "patreon", "match_bundles"):
        sys.modules.pop(name, None)

    module_name = "proxy_app_match_bundle_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, WEBSITE_DIR / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    return module, sys.modules["db"], sys.modules["patreon"], sys.modules["match_bundles"]


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
        # The customer's own key authenticates the call; it is never a query
        # parameter (it would land in the gateway's access log) and the master
        # key is never sent on a customer's behalf.
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
    proxy_app, proxy_db, patreon, mb = _load_proxy_app(tmp_path, monkeypatch)
    fake = FakeKeyInfo()
    monkeypatch.setattr(patreon, "_http", lambda: fake)
    mb._key_info_cache.clear()
    # The per-owner upload interval is covered by its own test; everything
    # else uploads several bundles from one key within a second.
    monkeypatch.setattr(mb, "MIN_UPLOAD_INTERVAL_S", 0.0)
    with TestClient(proxy_app.app) as client:
        yield proxy_app, proxy_db, patreon, mb, fake, client, tmp_path


def _bundle(install_id=INSTALL_ID, match_id=MATCH_ID, **extra) -> dict:
    payload = {
        "schema": 1,
        "install_id": install_id,
        "match_id": match_id,
        "created_at": time.time(),
        "app": {"version": "2.3.4", "platform": "Darwin", "python": "3.11"},
        "settings_subset": {"model": "glm-5.3-flash", "autopilot_enabled": True},
        "packet": {"match_id": match_id, "result": "loss", "decisions": []},
        "coach_log": {"lines": ["2026-10-08 12:00:00 | INFO | x | hello"], "truncated": 0},
        "player_log": {"events": [], "truncated": 0},
    }
    payload.update(extra)
    return payload


def _gz(payload) -> bytes:
    return gzip.compress(json.dumps(payload).encode("utf-8"))


def _headers(key: str, install_id: str = INSTALL_ID) -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/gzip",
        "User-Agent": "mtgacoach/2.3.4 (pyside)",
        "X-MTGACoach-Version": "2.3.4",
        "X-MTGACoach-Frontend": "pyside",
        "X-MTGACoach-Install-ID": install_id,
    }


def _post(client, key, payload=None, body=None, install_id=INSTALL_ID):
    data = body if body is not None else _gz(payload if payload is not None else _bundle())
    return client.post("/api/match-bundle", content=data, headers=_headers(key, install_id))


def _subscriber(proxy_db, key="sk-sub-active", status="active"):
    with proxy_db.get_db() as conn:
        conn.execute(
            "INSERT INTO subscribers (license_key, email, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, "p@example.com", "Patron", status, time.time()),
        )
    return key


def _owner(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _trial(proxy_db, key="sk-trial-key", days=3):
    now = datetime.now(timezone.utc)
    proxy_db.create_trial("c" * 64, key, _iso(now), _iso(now + timedelta(days=days)))
    return key


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_upload_requires_bearer_key(env):
    *_, client, _tmp = env
    resp = client.post(
        "/api/match-bundle", content=_gz(_bundle()), headers={"X-MTGACoach-Install-ID": INSTALL_ID}
    )
    assert resp.status_code == 401


def test_unknown_key_is_rejected_after_gateway_says_no(env):
    _, _db, _p, _mb, fake, client, _tmp = env
    resp = _post(client, "sk-nobody")
    assert resp.status_code == 401
    assert fake.calls == ["sk-nobody"]


def test_expired_subscriber_gets_402(env):
    _, proxy_db, _p, _mb, fake, client, _tmp = env
    key = _subscriber(proxy_db, "sk-sub-expired", status="expired")
    resp = _post(client, key)
    assert resp.status_code == 402
    assert fake.calls == []  # never falls through to the gateway


def test_trial_key_from_trials_table_is_accepted(env):
    _, proxy_db, _p, _mb, fake, client, _tmp = env
    key = _trial(proxy_db)
    resp = _post(client, key)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "stored"
    assert fake.calls == []


def test_expired_trial_key_gets_402(env):
    _, proxy_db, _p, _mb, fake, client, _tmp = env
    key = _trial(proxy_db, "sk-trial-old", days=-1)
    assert _post(client, key).status_code == 402


def test_litellm_key_info_path_accepts_and_caches(env):
    _, _db, _p, _mb, fake, client, _tmp = env
    fake.live["sk-litellm-live"] = {"expires": None, "blocked": False}
    r1 = _post(client, "sk-litellm-live", payload=_bundle(match_id="m-1"))
    r2 = _post(client, "sk-litellm-live", payload=_bundle(match_id="m-2"))
    assert r1.status_code == 200 and r2.status_code == 200
    assert fake.calls == ["sk-litellm-live"]  # second upload served from the cache


def test_litellm_expired_or_blocked_keys_are_rejected(env):
    _, _db, _p, mb, fake, client, _tmp = env
    fake.live["sk-expired"] = {"expires": _iso(datetime.now(timezone.utc) - timedelta(days=1))}
    fake.live["sk-blocked"] = {"expires": None, "blocked": True}
    assert _post(client, "sk-expired").status_code == 401
    assert _post(client, "sk-blocked").status_code == 401


def test_gateway_outage_is_a_401_not_a_500(env):
    _, _db, _p, _mb, fake, client, _tmp = env
    fake.fail = True
    assert _post(client, "sk-whatever").status_code == 401


def test_no_master_key_means_no_gateway_lookup(env, monkeypatch):
    _, _db, patreon, _mb, fake, client, _tmp = env
    monkeypatch.delenv("LITELLM_MASTER_KEY")
    monkeypatch.setattr(patreon, "LITELLM_MASTER_KEY", "")
    assert _post(client, "sk-nobody").status_code == 401
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_store_dedupe_list_download_and_stats(env):
    _, proxy_db, _p, mb, _fake, client, tmp_path = env
    key = _subscriber(proxy_db)
    body = _gz(_bundle())

    resp = _post(client, key, body=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "stored"
    assert out["size"] == len(body)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert out["path"] == f"{day}/{_owner(key)}/{INSTALL_ID}/{MATCH_ID}.json.gz"

    stored = tmp_path / "data" / "match_bundles" / day / _owner(key) / INSTALL_ID / f"{MATCH_ID}.json.gz"
    assert stored.read_bytes() == body
    assert not list(stored.parent.glob("*.part"))

    # Same match again -> duplicate, nothing rewritten.
    dup = _post(client, key, payload=_bundle(created_at=0))
    assert dup.status_code == 200
    assert dup.json()["status"] == "duplicate"
    assert stored.read_bytes() == body

    # Telemetry row recorded like the other authenticated routes.
    with proxy_db.get_db() as conn:
        installs = conn.execute(
            "SELECT license_key, install_id, client_version FROM client_installs"
        ).fetchall()
    assert [tuple(r) for r in installs] == [(key, INSTALL_ID, "2.3.4")]

    # Admin listing never exposes the license key; index carries the summary.
    listing = client.get("/api/match-bundles", headers=ADMIN).json()
    assert listing["count"] == 1
    row = listing["bundles"][0]
    assert row["install_id"] == INSTALL_ID and row["match_id"] == MATCH_ID
    assert row["app_version"] == "2.3.4" and row["result"] == "loss" and row["platform"] == "Darwin"
    assert row["auth_source"] == "subscriber" and row["schema_version"] == 1
    assert row["owner"] == _owner(key)
    assert "license_key" not in row
    assert "sk-" not in json.dumps(listing)

    # since= filters on received_at (epoch or ISO).
    assert (
        client.get("/api/match-bundles", params={"since": time.time() + 60}, headers=ADMIN).json()["count"]
        == 0
    )
    future_iso = _iso(datetime.now(timezone.utc) + timedelta(minutes=1))
    assert client.get("/api/match-bundles", params={"since": future_iso}, headers=ADMIN).json()["count"] == 0
    assert client.get("/api/match-bundles", params={"since": "yesterday"}, headers=ADMIN).status_code == 422

    # Download returns the exact bytes.
    dl = client.get(f"/api/match-bundle/{INSTALL_ID}/{MATCH_ID}", headers=ADMIN)
    assert dl.status_code == 200
    assert dl.content == body
    assert dl.headers["content-type"].startswith("application/gzip")
    assert client.get(f"/api/match-bundle/{INSTALL_ID}/nope", headers=ADMIN).status_code == 404

    stats = client.get("/admin/api/match-bundles/stats", headers=ADMIN).json()
    assert stats == {
        "total": 1,
        "bytes_total": len(body),
        "last_24h": 1,
        "last_7d": 1,
        "installs_7d": 1,
        "installs_total": 1,
    }


def test_admin_endpoints_require_admin(env):
    _, proxy_db, _p, _mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    assert _post(client, key).status_code == 200
    assert client.get("/api/match-bundles").status_code == 403
    assert client.get(f"/api/match-bundle/{INSTALL_ID}/{MATCH_ID}").status_code == 403
    assert client.get("/admin/api/match-bundles/stats").status_code == 403
    # A customer key is not an admin key either.
    assert client.get("/api/match-bundles", headers={"Authorization": f"Bearer {key}"}).status_code == 403


def test_same_match_id_from_two_installs_are_separate(env):
    _, proxy_db, _p, _mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    assert _post(client, key).json()["status"] == "stored"
    other = _post(client, key, payload=_bundle(install_id=OTHER_INSTALL_ID), install_id=OTHER_INSTALL_ID)
    assert other.json()["status"] == "stored"
    assert client.get("/admin/api/match-bundles/stats", headers=ADMIN).json()["installs_total"] == 2


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_install_id_header_must_match_bundle(env):
    _, proxy_db, _p, _mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    resp = _post(client, key, install_id=OTHER_INSTALL_ID)
    assert resp.status_code == 403
    assert client.get("/admin/api/match-bundles/stats", headers=ADMIN).json()["total"] == 0


@pytest.mark.parametrize(
    "install_id,match_id",
    [
        ("../etc", MATCH_ID),
        ("inst with space", MATCH_ID),
        (INSTALL_ID, "../../x"),
        (INSTALL_ID, "a/b"),
        (INSTALL_ID, ""),
        ("", MATCH_ID),
        (INSTALL_ID, "x" * 81),
    ],
)
def test_unsafe_ids_are_rejected(env, install_id, match_id):
    _, proxy_db, _p, _mb, _fake, client, tmp_path = env
    key = _subscriber(proxy_db)
    resp = client.post(
        "/api/match-bundle",
        content=_gz(_bundle(install_id=install_id, match_id=match_id)),
        headers=_headers(key, install_id),
    )
    assert resp.status_code == 422
    assert not (tmp_path / "data" / "match_bundles").exists()


def test_non_gzip_body_is_415(env):
    _, proxy_db, _p, _mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    resp = _post(client, key, body=json.dumps(_bundle()).encode())
    assert resp.status_code == 415


def test_corrupt_gzip_and_non_object_json_are_400(env):
    _, proxy_db, _p, _mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    assert _post(client, key, body=b"\x1f\x8bgarbage").status_code == 400
    assert _post(client, key, body=gzip.compress(b"[1, 2]")).status_code == 400
    assert _post(client, key, body=gzip.compress(b"not json")).status_code == 400
    assert _post(client, key, body=b"").status_code == 400


def test_oversized_upload_is_413(env, monkeypatch):
    _, proxy_db, _p, mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    monkeypatch.setattr(mb, "MAX_BODY_BYTES", 1024)
    big = _gz(_bundle(coach_log={"lines": ["x" * 50 + str(i) for i in range(2000)]}))
    assert len(big) > 1024
    assert _post(client, key, body=big).status_code == 413


def test_decompression_bomb_is_413(env, monkeypatch):
    _, proxy_db, _p, mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    monkeypatch.setattr(mb, "MAX_JSON_BYTES", 4096)
    bomb = gzip.compress(json.dumps(_bundle(filler="z" * 100_000)).encode())
    assert len(bomb) < mb.MAX_BODY_BYTES
    assert _post(client, key, body=bomb).status_code == 413


def test_download_id_validation(env):
    *_, client, _tmp = env
    assert client.get("/api/match-bundle/inst%20x/" + MATCH_ID, headers=ADMIN).status_code == 422


# ---------------------------------------------------------------------------
# Abuse resistance (review findings 2026-10-08)
# ---------------------------------------------------------------------------


def test_uploads_are_namespaced_per_key(env):
    """Another key cannot pre-empt, overwrite or be served as an install's bundle."""
    _, proxy_db, _p, mb, _fake, client, _tmp = env
    victim_key = _subscriber(proxy_db, "sk-victim")
    attacker_key = _trial(proxy_db, "sk-attacker")
    junk = _gz(_bundle(junk=True))
    real = _gz(_bundle(packet={"match_id": MATCH_ID, "result": "win", "decisions": []}))

    # The attacker uploads first, under the victim's install id.
    assert _post(client, attacker_key, body=junk).json()["status"] == "stored"
    # The victim's genuine upload is stored, not answered "duplicate".
    out = _post(client, victim_key, body=real).json()
    assert out["status"] == "stored"
    assert out["path"].split("/")[1] == _owner(victim_key)
    # ...and only the victim's own re-upload is a duplicate.
    assert _post(client, victim_key, body=real).json()["status"] == "duplicate"

    rows = client.get("/api/match-bundles", headers=ADMIN).json()["bundles"]
    assert len(rows) == 2 and {r["owner"] for r in rows} == {_owner(victim_key), _owner(attacker_key)}
    assert all(r["install_id"] == INSTALL_ID and r["match_id"] == MATCH_ID for r in rows)
    assert mb.find_bundle(INSTALL_ID, MATCH_ID, _owner(victim_key))["license_key"] == victim_key

    # The download is addressed by owner; the two files never collide on disk.
    dl = client.get(
        f"/api/match-bundle/{INSTALL_ID}/{MATCH_ID}", params={"owner": _owner(victim_key)}, headers=ADMIN
    )
    assert dl.status_code == 200 and dl.content == real
    dl = client.get(
        f"/api/match-bundle/{INSTALL_ID}/{MATCH_ID}", params={"owner": _owner(attacker_key)}, headers=ADMIN
    )
    assert dl.status_code == 200 and dl.content == junk
    assert (
        client.get(
            f"/api/match-bundle/{INSTALL_ID}/{MATCH_ID}", params={"owner": "zz"}, headers=ADMIN
        ).status_code
        == 422
    )
    assert client.get("/admin/api/match-bundles/stats", headers=ADMIN).json()["installs_total"] == 1


def test_upload_interval_is_rate_limited(env, monkeypatch):
    _, proxy_db, _p, mb, _fake, client, _tmp = env
    monkeypatch.setattr(mb, "MIN_UPLOAD_INTERVAL_S", 10.0)
    key = _subscriber(proxy_db)
    assert _post(client, key, payload=_bundle(match_id="m-1")).status_code == 200
    resp = _post(client, key, payload=_bundle(match_id="m-2"))
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= 10
    # A duplicate of an already-stored match is answered without counting.
    assert _post(client, key, payload=_bundle(match_id="m-1")).json()["status"] == "duplicate"
    # The same install under a different key is throttled too (per-install scope).
    other = _trial(proxy_db, "sk-other")
    assert _post(client, other, payload=_bundle(match_id="m-3")).status_code == 429
    assert client.get("/admin/api/match-bundles/stats", headers=ADMIN).json()["total"] == 1


def test_daily_count_and_byte_quotas(env, monkeypatch, tmp_path):
    _, proxy_db, _p, mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    monkeypatch.setattr(mb, "MAX_BUNDLES_PER_DAY", 2)
    assert _post(client, key, payload=_bundle(match_id="m-1")).status_code == 200
    assert _post(client, key, payload=_bundle(match_id="m-2")).status_code == 200
    third = _post(client, key, payload=_bundle(match_id="m-3"))
    assert third.status_code == 429 and "quota" in third.text
    # Another install under the same key shares the owner quota...
    assert (
        _post(
            client,
            key,
            payload=_bundle(install_id=OTHER_INSTALL_ID, match_id="m-4"),
            install_id=OTHER_INSTALL_ID,
        ).status_code
        == 429
    )
    # ...while a different key on a different install is not affected by the count.
    other = _trial(proxy_db, "sk-other")
    monkeypatch.setattr(mb, "MAX_BYTES_PER_DAY", len(_gz(_bundle())) + 10)
    assert (
        _post(
            client,
            other,
            payload=_bundle(install_id=OTHER_INSTALL_ID, match_id="m-5"),
            install_id=OTHER_INSTALL_ID,
        ).status_code
        == 200
    )
    over = _post(
        client,
        other,
        payload=_bundle(install_id=OTHER_INSTALL_ID, match_id="m-6"),
        install_id=OTHER_INSTALL_ID,
    )
    assert over.status_code == 429 and "quota" in over.text
    files = list((tmp_path / "data" / "match_bundles").rglob("*.json.gz"))
    assert len(files) == 3 and not list((tmp_path / "data" / "match_bundles").rglob("*.part"))


def test_full_data_volume_is_507(env, monkeypatch, tmp_path):
    _, proxy_db, _p, mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    usage = types.SimpleNamespace(total=10, used=10, free=mb.MIN_FREE_BYTES - 1)
    monkeypatch.setattr(mb.shutil, "disk_usage", lambda path: usage)
    resp = _post(client, key)
    assert resp.status_code == 507
    assert not (tmp_path / "data" / "match_bundles").exists()
    assert client.get("/admin/api/match-bundles/stats", headers=ADMIN).json()["total"] == 0


def test_body_cap_matches_the_client_cap(env):
    *_, mb, _fake, _client, _tmp = env
    assert mb.MAX_BODY_BYTES == 8 * 1024 * 1024
    assert mb.MAX_JSON_BYTES <= 32 * 1024 * 1024


def test_corrupt_deflate_and_deep_nesting_are_400_not_500(env):
    _, proxy_db, _p, _mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    good = _gz(_bundle(coach_log={"lines": ["x" * 2000]}))
    corrupt = bytearray(good)
    for i in range(20, len(corrupt) - 12, 7):  # valid gzip header, mangled deflate stream
        corrupt[i] ^= 0x5A
    resp = _post(client, key, body=bytes(corrupt))
    assert resp.status_code == 400 and "gzip" in resp.text
    deep = gzip.compress(b"[" * 200_000 + b"]" * 200_000)
    resp = _post(client, key, body=deep)
    assert resp.status_code == 400 and "JSON" in resp.text
    deep_obj = gzip.compress(b'{"a":' * 100_000 + b"1" + b"}" * 100_000)
    assert _post(client, key, body=deep_obj).status_code == 400


def test_decompression_bomb_stops_inflating_at_the_cap(env, monkeypatch):
    _, proxy_db, _p, mb, _fake, client, _tmp = env
    key = _subscriber(proxy_db)
    monkeypatch.setattr(mb, "MAX_JSON_BYTES", 4 * mb.DECOMPRESS_CHUNK)
    reads: list[int] = []
    real_gzipfile = mb.gzip.GzipFile

    class CountingGzip(real_gzipfile):
        def read(self, size=-1):
            data = super().read(size)
            reads.append(len(data))
            return data

    monkeypatch.setattr(mb.gzip, "GzipFile", CountingGzip)
    bomb = gzip.compress(b"0" * (64 * mb.DECOMPRESS_CHUNK))  # 64 MB of zeros, ~64 KB compressed
    assert len(bomb) < mb.MAX_BODY_BYTES
    assert _post(client, key, body=bomb).status_code == 413
    assert sum(reads) <= mb.MAX_JSON_BYTES + mb.DECOMPRESS_CHUNK


@pytest.mark.parametrize("since", ["nan", "inf", "-inf", "1e400"])
def test_since_must_be_finite(env, since):
    *_, client, _tmp = env
    resp = client.get("/api/match-bundles", params={"since": since}, headers=ADMIN)
    assert resp.status_code == 422


def test_malformed_basic_auth_is_403_not_500(env):
    *_, client, _tmp = env
    for value in ("Basic notbase64!!", "Basic ", "Basic " + base64.b64encode(b"\xff\xfe").decode()):
        assert client.get("/api/match-bundles", headers={"Authorization": value}).status_code == 403
    good = base64.b64encode(b"admin:test-admin").decode()
    assert client.get("/api/match-bundles", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_legacy_index_table_is_moved_aside(env):
    """A pre-owner table cannot keep its UNIQUE(install, match); it is renamed, not migrated."""
    _, proxy_db, _p, mb, _fake, client, _tmp = env
    with proxy_db.get_db() as conn:
        conn.execute("DROP TABLE match_bundles")
        conn.execute(
            "CREATE TABLE match_bundles (id INTEGER PRIMARY KEY, install_id TEXT, match_id TEXT, "
            "received_at REAL, size INTEGER, path TEXT, UNIQUE(install_id, match_id))"
        )
        conn.execute(
            "INSERT INTO match_bundles (install_id, match_id, received_at, size, path) VALUES (?, ?, 1, 1, 'x')",
            (INSTALL_ID, MATCH_ID),
        )
    mb.init_db()
    with proxy_db.get_db() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(match_bundles)").fetchall()}
        legacy = conn.execute("SELECT COUNT(*) FROM match_bundles_v0").fetchone()[0]
    assert "owner" in cols and legacy == 1
    key = _subscriber(proxy_db)
    assert _post(client, key).json()["status"] == "stored"
