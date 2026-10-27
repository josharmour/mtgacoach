"""Real SQLite + HTTP routes; gateway is the only fake."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def trial_service(tmp_path, monkeypatch):
    website = Path(__file__).resolve().parents[1] / "website"
    monkeypatch.syspath_prepend(str(website))

    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, website / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    database = load("arena_trial_db_test", "db.py")
    database.DB_PATH = tmp_path / "trial.sqlite"
    monkeypatch.setitem(sys.modules, "db", database)
    calls = []

    async def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": "{}"}}]})

    emails = []
    gateway = SimpleNamespace(
        SMTP_HOST="smtp.test",
        send_trial_code_email=lambda email, code, **kwargs: emails.append((email, code, kwargs)),
        LITELLM_URL="http://gateway",
        _admin_headers=lambda: {"Authorization": "Bearer server-only"},
        _http=lambda: SimpleNamespace(post=post),
    )
    monkeypatch.setitem(sys.modules, "patreon", gateway)
    monkeypatch.setenv("LITELLM_ARENAONAIR_KEY", "sk-arenaonair-test")
    service = load("arena_trial_router_test", "arenaonair.py")
    monkeypatch.setattr(service.secrets, "randbelow", lambda _: 123456)
    service.sent_emails = emails
    app = FastAPI()
    app.include_router(service.router)
    with TestClient(app, client=("198.51.100.10", 40000)) as client:
        yield client, service, gateway, calls, database


TOKEN = "aoa_" + "t" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


def signup(client, device="d" * 64, headers=HEADERS):
    body = {"device_id": device}
    if client.get("/api/arenaonair/status", headers=headers).status_code != 200:
        response = client.post(
            "/api/arenaonair/trial/request-code",
            json={**body, "email": "caster@example.com"},
            headers=headers,
        )
        if response.status_code != 200:
            return response
        body.update(email="caster@example.com", code="123456")
    return client.post("/api/arenaonair/trial", json=body, headers=headers)


def chat(client, match="match-1", **body):
    return client.post(
        "/api/arenaonair/v1/chat/completions",
        headers={**HEADERS, "X-ArenaOnAir-Match": match},
        json={"messages": [{"role": "user", "content": "hello"}], **body},
    )


def test_five_matches_reconnect_and_no_gateway_key_exposed(trial_service):
    client, _, _, calls, _ = trial_service
    assert signup(client).json()["matches_remaining"] == 5
    assert client.get("/api/arenaonair/v1/models", headers=HEADERS).json()["data"][0]["id"] == "glm-5.3-flash"
    for i in range(5):
        assert chat(client, f"match-{i}").status_code == 200
    assert chat(client, "match-5").status_code == 402
    assert chat(client, "match-0").status_code == 200  # reconnect / next game in same BO3
    again = signup(client)
    assert again.json()["matches_remaining"] == 0
    assert "key" not in again.json()
    assert len(calls) == 6


def test_identifier_without_possession_cannot_steal_trial(trial_service):
    client, *_ = trial_service
    assert signup(client).status_code == 200
    assert signup(client, headers={"Authorization": "Bearer aoa_" + "x" * 43}).status_code == 409
    assert signup(client, headers={}).status_code == 401
    assert signup(client, device="bad").status_code == 422


def test_failure_refunds_match_and_blocks_client_routing(trial_service):
    client, _, gateway, calls, _ = trial_service
    signup(client)

    async def fail(*args, **kwargs):
        raise RuntimeError("secret must not appear")

    original = gateway._http
    gateway._http = lambda: SimpleNamespace(post=fail)
    response = chat(client)
    assert response.status_code == 503 and "secret" not in response.text
    assert signup(client).json()["matches_remaining"] == 5
    gateway._http = original
    assert (
        chat(client, model="old-model", api_base="http://evil", api_key="bad", max_tokens=99999).status_code
        == 200
    )
    sent = calls[-1][1]["json"]
    assert sent["model"] == "glm-5.3-flash" and sent["max_tokens"] == 1800
    assert "api_base" not in sent and "api_key" not in sent
    # Commentary spends the dedicated ArenaOnAir gateway key, not the admin key.
    assert calls[-1][1]["headers"] == {"Authorization": "Bearer sk-arenaonair-test"}


def test_missing_gateway_key_is_503_and_refunds_match(trial_service, monkeypatch):
    client, *_ = trial_service
    signup(client)
    monkeypatch.delenv("LITELLM_ARENAONAIR_KEY")
    response = chat(client)
    assert response.status_code == 503
    assert signup(client).json()["matches_remaining"] == 5


def test_expired_match_cannot_be_reused_forever(trial_service):
    client, service, _, _, database = trial_service
    signup(client)
    chat(client)
    with database.get_db() as conn:
        conn.execute("UPDATE arenaonair_matches SET started=0")
    assert chat(client).status_code == 402
    assert signup(client).json()["matches_remaining"] == 4


def test_invalid_body_and_missing_match_do_not_spend_trial(trial_service):
    client, *_ = trial_service
    signup(client)
    assert chat(client, "").status_code == 422
    assert chat(client, messages="bad").status_code == 422
    assert signup(client).json()["matches_remaining"] == 5


def test_concurrent_starts_cannot_overdraw_last_match(trial_service):
    from concurrent.futures import ThreadPoolExecutor

    client, *_ = trial_service
    signup(client)
    for i in range(4):
        assert chat(client, f"previous-{i}").status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda match: chat(client, match).status_code, ["last-a", "last-b"]))
    assert sorted(statuses) == [200, 402]
    assert signup(client).json()["matches_used"] == 5


def test_new_trial_requires_verified_email(trial_service):
    client, service, _, _, database = trial_service
    body = {"device_id": "d" * 64}
    response = client.post("/api/arenaonair/trial", json=body, headers=HEADERS)
    assert response.status_code == 422 and response.json()["detail"] == "email_verification_required"
    assert signup(client).status_code == 200
    assert service.sent_emails == [("caster@example.com", "123456", {"product": "ArenaOnAir"})]
    with database.get_db() as conn:
        row = conn.execute("SELECT email,email_verified FROM arenaonair_trials").fetchone()
        assert tuple(row) == ("caster@example.com", 1)
        assert conn.execute("SELECT count(*) FROM arenaonair_trial_codes").fetchone()[0] == 0


def request_code(client, email="caster@example.com"):
    return client.post(
        "/api/arenaonair/trial/request-code", headers=HEADERS, json={"device_id": "d" * 64, "email": email}
    )


def verify(client, code="123456", email="caster@example.com", headers=HEADERS):
    return client.post(
        "/api/arenaonair/trial", headers=headers, json={"device_id": "d" * 64, "email": email, "code": code}
    )


def test_bad_code_attempts_commit_and_lock_out(trial_service):
    client, _, _, _, database = trial_service
    assert request_code(client).status_code == 200
    for _ in range(5):
        assert verify(client, "000000").status_code == 422
    assert verify(client).json()["detail"] == "invalid_code"
    with database.get_db() as conn:
        assert conn.execute("SELECT attempts FROM arenaonair_trial_codes").fetchone()[0] == 6
        assert conn.execute("SELECT count(*) FROM arenaonair_trials").fetchone()[0] == 0


def test_expired_or_wrong_email_or_wrong_possession_rejected(trial_service):
    client, _, _, _, database = trial_service
    request_code(client)
    assert verify(client, email="other@example.com").status_code == 422
    assert verify(client, headers={"Authorization": "Bearer aoa_" + "x" * 43}).status_code == 422
    with database.get_db() as conn:
        conn.execute("UPDATE arenaonair_trial_codes SET expires=0")
    assert verify(client).status_code == 422


def test_resend_replaces_code_and_is_rate_limited(trial_service, monkeypatch):
    client, service, *_ = trial_service
    request_code(client)
    monkeypatch.setattr(service.secrets, "randbelow", lambda _: 654321)
    assert request_code(client).status_code == 200
    assert verify(client, "123456").status_code == 422
    assert request_code(client).status_code == 200
    assert request_code(client).status_code == 429
    assert verify(client, "654321").status_code == 200


def test_mail_failure_cannot_provision_or_leak_error(trial_service):
    client, _, gateway, _, database = trial_service

    def fail(*args, **kwargs):
        raise RuntimeError("smtp password secret")

    gateway.send_trial_code_email = fail
    response = request_code(client)
    assert response.status_code == 503 and "secret" not in response.text
    assert verify(client).status_code == 422
    with database.get_db() as conn:
        assert conn.execute("SELECT count(*) FROM arenaonair_trial_codes").fetchone()[0] == 0


def test_existing_trial_migrates_without_reverification(trial_service):
    client, service, _, _, database = trial_service
    with database.get_db() as conn:
        conn.execute("DROP TABLE arenaonair_trials")
        conn.execute(
            "CREATE TABLE arenaonair_trials(device_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created REAL NOT NULL, ip_hash TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO arenaonair_trials VALUES(?,?,?,?)", ("d" * 64, service._digest(TOKEN), 1, "old")
        )
    service.init_db()
    response = client.post("/api/arenaonair/trial", headers=HEADERS, json={"device_id": "d" * 64})
    assert response.status_code == 200 and response.json()["matches_remaining"] == 5
    assert not service.sent_emails
