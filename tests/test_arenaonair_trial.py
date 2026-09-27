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

    gateway = SimpleNamespace(
        LITELLM_URL="http://gateway",
        _admin_headers=lambda: {"Authorization": "Bearer server-only"},
        _http=lambda: SimpleNamespace(post=post),
    )
    monkeypatch.setitem(sys.modules, "patreon", gateway)
    service = load("arena_trial_router_test", "arenaonair.py")
    app = FastAPI()
    app.include_router(service.router)
    with TestClient(app) as client:
        yield client, service, gateway, calls, database


TOKEN = "aoa_" + "t" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


def signup(client, device="d" * 64, headers=HEADERS):
    return client.post("/api/arenaonair/trial", json={"device_id": device}, headers=headers)


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
