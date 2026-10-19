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
# Signups are rate-limited per caller network, so the test client needs a
# real address (Starlette's default peer "testclient" is not an IP).
CLIENT_ADDR = ("203.0.113.7", 50000)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    proxy_app, proxy_db, patreon = load_website_app(tmp_path, monkeypatch)
    fake = FakeLiteLLM(key_prefix="sk-fake-trial-")
    monkeypatch.setattr(patreon, "_http", lambda: fake)
    with TestClient(proxy_app.app, client=CLIENT_ADDR) as client:
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
    for name, value in patreon.TRIAL_LIMITS.items():
        assert payload[name] == value
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



# ── Signup abuse limits (website/abuse_protection.py) ──


def _machine(n: int) -> str:
    return f"{n:064x}"


def test_network_limit_is_three_new_trials_per_hour(env):
    _, _, _, fake, client = env

    for n in range(3):
        assert _post_trial(client, machine_id=_machine(n + 1)).status_code == 200

    r = _post_trial(client, machine_id=_machine(99))

    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert len(fake.minted) == 3


def test_failed_mint_blocks_an_immediate_retry_but_records_the_attempt(env, monkeypatch):
    _, proxy_db, patreon, fake, client = env

    async def broken(machine_id):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(patreon, "mint_trial_key", broken)
    assert _post_trial(client).status_code == 503

    retry = _post_trial(client)

    assert retry.status_code == 429  # one attempt per machine per 5 minutes
    with proxy_db.get_db() as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM trial_signup_reservations")]
    assert statuses == ["failed"]
    assert proxy_db.get_trial(MACHINE_ID) is None


def test_issued_trial_is_marked_and_existing_key_still_returned(env):
    _, proxy_db, _, fake, client = env
    first = _post_trial(client).json()

    again = _post_trial(client)

    assert again.status_code == 200 and again.json()["key"] == first["key"]
    with proxy_db.get_db() as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM trial_signup_reservations")]
    assert statuses == ["issued"]
    assert len(fake.minted) == 1


def test_tunnel_peer_is_identified_by_cf_connecting_ip(env, monkeypatch):
    proxy_app, _, _, fake, _ = env
    abuse = __import__("sys").modules["abuse_protection"]
    monkeypatch.setattr(abuse, "TRUSTED_TUNNEL_PEERS", {"10.0.0.2"})
    with TestClient(proxy_app.app, client=("10.0.0.2", 40000)) as tunnel:
        missing = tunnel.post("/api/trial", json={"machine_id": _machine(7)})
        assert missing.status_code == 400  # no header from the tunnel: fail closed
        for n, ip in enumerate(["198.51.100.1", "198.51.100.1", "198.51.100.1", "198.51.100.2"]):
            r = tunnel.post(
                "/api/trial", json={"machine_id": _machine(n + 10)}, headers={"CF-Connecting-IP": ip}
            )
            assert r.status_code == 200, (n, r.text)  # distinct client networks, not the tunnel's
        r = tunnel.post(
            "/api/trial", json={"machine_id": _machine(20)}, headers={"CF-Connecting-IP": "198.51.100.1"}
        )
        assert r.status_code == 429
    assert len(fake.minted) == 4


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
