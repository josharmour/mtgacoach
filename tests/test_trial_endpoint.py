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

    # SMTP is faked end to end: the endpoint's send call just records the code.
    SENT_CODES.clear()
    monkeypatch.setattr(patreon, "SMTP_HOST", "smtp.test")

    def fake_send(to_addr, code):
        SENT_CODES.append(code)

    monkeypatch.setattr(patreon, "send_trial_code_email", fake_send)
    with TestClient(proxy_app.app, client=CLIENT_ADDR) as client:
        yield proxy_app, proxy_db, patreon, fake, client


SENT_CODES: list[str] = []
EMAIL = "Tripper@Example.COM"  # mixed case on purpose: storage must lowercase it


def _request_code(client, machine_id=MACHINE_ID, email=EMAIL, headers=None):
    return client.post(
        "/api/trial/request-code",
        json={"machine_id": machine_id, "email": email},
        headers=headers or {},
    )


def _post_trial(client, machine_id=MACHINE_ID, app_version="2.0.1", email=EMAIL, code=None, headers=None):
    """Full signup dance: request a code by email, then mint with it."""
    rc = _request_code(client, machine_id=machine_id, email=email, headers=headers)
    assert rc.status_code == 200, rc.text
    body = {
        "machine_id": machine_id,
        "app_version": app_version,
        "email": email.lower(),
        "code": code if code is not None else SENT_CODES[-1],
    }
    return client.post("/api/trial", json=body, headers=headers or {})


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

    r = client.post("/api/trial", json={"machine_id": machine_id})

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
            r = _post_trial(tunnel, machine_id=_machine(n + 10), headers={"CF-Connecting-IP": ip})
            assert r.status_code == 200, (n, r.text)  # distinct client networks, not the tunnel's
        r = _post_trial(tunnel, machine_id=_machine(20), headers={"CF-Connecting-IP": "198.51.100.1"})
        assert r.status_code == 429  # third signup on that client network this hour
    assert len(fake.minted) == 4


# -- /v1/subscription/check answers trial keys (the desktop access badge) --


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


# ── Trial telemetry: every /api/trial outcome and app check-in is recorded ──


def _events(proxy_db):
    with proxy_db.get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM trial_events ORDER BY id")]


CLIENT_HEADERS = {
    "X-MTGACoach-Install-ID": "inst_test",
    "X-MTGACoach-Version": "3.6.2",
    "X-MTGACoach-Frontend": "winui",
    "CF-IPCountry": "tr",
}


def test_trial_outcomes_are_recorded_as_events(env):
    _, proxy_db, _, _, client = env

    issued = _post_trial(client, app_version="3.6.2", headers=CLIENT_HEADERS).json()
    _post_trial(client, app_version="3.6.2", headers=CLIENT_HEADERS)
    client.post("/api/trial", json={"machine_id": "nope"})
    _check(client, issued["key"])

    events = _events(proxy_db)
    assert [e["event"] for e in events] == [
        "code_requested",
        "created",
        "code_requested",
        "existing",
        "invalid",
        "check",
    ]
    created = events[1]
    assert created["machine_id"] == MACHINE_ID
    assert (created["app_version"], created["install_id"], created["frontend"]) == (
        "3.6.2",
        "inst_test",
        "winui",
    )
    assert created["country"] == "TR"
    assert created["peer"] == CLIENT_ADDR[0]
    assert len(created["network_hash"]) == 64
    assert events[5]["machine_id"] == MACHINE_ID  # check-in resolves the key to its machine


def test_lapsed_trial_check_in_is_recorded(env):
    _, proxy_db, _, _, client = env
    past = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    proxy_db.create_trial(MACHINE_ID, "***", past, past)

    _check(client, "***")
    _post_trial(client)

    assert [(e["event"], e["machine_id"]) for e in _events(proxy_db)] == [
        ("check_expired", MACHINE_ID),
        ("code_requested", MACHINE_ID),
        ("expired", MACHINE_ID),
    ]


def test_untrusted_peer_keeps_distinct_client_hashes_for_rate_limited_callers(env, monkeypatch):
    """A proxy that is not a trusted tunnel peer collapses callers onto one
    enforced network; the unverified Cloudflare header still tells them apart."""
    proxy_app, proxy_db, _, _, _ = env
    abuse = __import__("sys").modules["abuse_protection"]
    monkeypatch.setattr(abuse, "TRUSTED_TUNNEL_PEERS", {"10.0.0.2"})
    with TestClient(proxy_app.app, client=("172.23.0.1", 40000)) as bridge:
        for n in range(4):
            r = _post_trial(
                bridge,
                machine_id=_machine(n + 1),
                headers={"CF-Connecting-IP": f"198.51.100.{n + 1}"},
            )
            assert r.status_code == (200 if n < 3 else 429), (n, r.text)

    events = _events(proxy_db)
    assert [e["event"] for e in events] == (
        ["code_requested", "created"] * 3 + ["code_requested", "rate_limited"]
    )
    assert len({e["network_hash"] for e in events}) == 1
    assert len({e["client_hash"] for e in events}) == 4
    summary = proxy_db.get_trial_event_summary(days=1)
    assert summary["networks"][0] | {"network": None} == {
        "network": None,
        "peer": "172.23.0.1",
        "machines": 4,
        "clients": 4,
        "created": 3,
        "rate_limited": 1,
    }


def test_unverifiable_tunnel_caller_and_mint_failure_are_recorded(env, monkeypatch):
    proxy_app, proxy_db, patreon, _, client = env
    abuse = __import__("sys").modules["abuse_protection"]
    monkeypatch.setattr(abuse, "TRUSTED_TUNNEL_PEERS", {"10.0.0.2"})
    with TestClient(proxy_app.app, client=("10.0.0.2", 40000)) as tunnel:
        assert tunnel.post("/api/trial", json={"machine_id": _machine(5)}).status_code == 400

    async def broken(machine_id, context=None):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(patreon, "mint_trial_key", broken)
    assert _post_trial(client).status_code == 503

    assert [e["event"] for e in _events(proxy_db)] == [
        "unverified_network",
        "code_requested",
        "mint_failed",
    ]


def test_trial_key_metadata_carries_signup_context(env):
    _, _, _, fake, client = env

    _post_trial(client, app_version="3.6.2", headers=CLIENT_HEADERS)

    metadata = fake.minted[0]["payload"]["metadata"]
    assert (metadata["app_version"], metadata["install_id"], metadata["country"]) == (
        "3.6.2",
        "inst_test",
        "TR",
    )
    assert len(metadata["network"]) == 12
    assert (metadata["trial"], metadata["source"], metadata["machine_id"]) == (True, "trial", MACHINE_ID)


def test_telemetry_failure_never_fails_trial_issue(env, monkeypatch):
    _, proxy_db, _, _, client = env

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(proxy_db, "record_trial_event", broken)

    assert _post_trial(client).status_code == 200


def test_blackwell_tunnel_bridge_is_a_trusted_peer(env):
    """cloudflared on blackwell reaches the site via the bridge gateway; its
    callers must be limited per Cloudflare client, not as one network."""
    proxy_app, proxy_db, _, fake, _ = env
    with TestClient(proxy_app.app, client=("172.23.0.1", 40000)) as bridge:
        for n in range(4):
            r = _post_trial(
                bridge,
                machine_id=_machine(n + 1),
                headers={"CF-Connecting-IP": f"198.51.100.{n + 1}"},
            )
            assert r.status_code == 200, (n, r.text)
    assert len(fake.minted) == 4
    assert len({e["network_hash"] for e in _events(proxy_db)}) == 4


# -- Email verification for new mints (/api/trial/request-code) --


def test_request_code_emails_a_six_digit_code_and_stores_only_its_hash(env):
    _, proxy_db, _, _, client = env

    r = _request_code(client)

    assert r.status_code == 200
    assert r.json() == {"status": "code_sent", "expires_in": 900}
    assert len(SENT_CODES) == 1 and len(SENT_CODES[0]) == 6 and SENT_CODES[0].isdigit()

    # only a salted hash is stored -- never the code itself
    with proxy_db.get_db() as conn:
        row = dict(
            conn.execute("SELECT * FROM trial_email_codes WHERE machine_id = ?", (MACHINE_ID,)).fetchone()
        )
    assert row["email"] == EMAIL.lower()
    assert row["code_hash"] != SENT_CODES[0] and len(row["code_hash"]) == 64
    assert row["attempts"] == 0

    minted = client.post(
        "/api/trial",
        json={
            "machine_id": MACHINE_ID,
            "app_version": "2.0.1",
            "email": EMAIL.lower(),
            "code": SENT_CODES[-1],
        },
    )
    assert minted.status_code == 200 and minted.json()["status"] == "created"
    trial = proxy_db.get_trial(MACHINE_ID)
    assert trial["email"] == EMAIL.lower() and trial["email_verified"] == 1


def test_wrong_code_is_rejected_and_counts_an_attempt(env):
    _, proxy_db, _, _, client = env

    assert _request_code(client).status_code == 200
    wrong = f"{int(SENT_CODES[-1]) + 1:06d}"  # guaranteed different from the sent code
    for _ in range(2):
        r = client.post(
            "/api/trial",
            json={"machine_id": MACHINE_ID, "email": EMAIL.lower(), "code": wrong},
        )
        assert r.status_code == 422 and r.json() == {"detail": "invalid_code"}

    with proxy_db.get_db() as conn:
        attempts = conn.execute(
            "SELECT attempts FROM trial_email_codes WHERE machine_id = ?", (MACHINE_ID,)
        ).fetchone()[0]
    assert attempts == 2

    # the right code still works before the lock threshold
    ok = client.post(
        "/api/trial",
        json={"machine_id": MACHINE_ID, "email": EMAIL.lower(), "code": SENT_CODES[-1]},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "created"


def test_expired_code_is_rejected(env):
    _, proxy_db, _, _, client = env

    assert _request_code(client).status_code == 200
    with proxy_db.get_db() as conn:
        conn.execute(
            "UPDATE trial_email_codes SET expires_at = ? WHERE machine_id = ?",
            ("2000-01-01T00:00:00Z", MACHINE_ID),
        )
    r = client.post(
        "/api/trial",
        json={"machine_id": MACHINE_ID, "email": EMAIL.lower(), "code": SENT_CODES[-1]},
    )
    assert r.status_code == 422 and r.json() == {"detail": "invalid_code"}


def test_five_wrong_attempts_lock_the_pending_code(env):
    _, proxy_db, _, _, client = env

    assert _request_code(client).status_code == 200
    wrong = f"{int(SENT_CODES[-1]) + 1:06d}"
    for _ in range(5):
        client.post(
            "/api/trial",
            json={"machine_id": MACHINE_ID, "email": EMAIL.lower(), "code": wrong},
        )
    # even the correct code is now locked out
    r = client.post(
        "/api/trial",
        json={"machine_id": MACHINE_ID, "email": EMAIL.lower(), "code": SENT_CODES[-1]},
    )
    assert r.status_code == 422 and r.json() == {"detail": "invalid_code"}


def test_missing_email_or_code_requires_verification(env):
    _, _, _, fake, client = env

    r = client.post("/api/trial", json={"machine_id": MACHINE_ID})
    assert r.status_code == 422 and r.json() == {"detail": "email_verification_required"}
    r = client.post("/api/trial", json={"machine_id": MACHINE_ID, "email": EMAIL.lower()})
    assert r.status_code == 422 and r.json() == {"detail": "email_verification_required"}
    assert len(fake.minted) == 0


def test_existing_active_trial_still_returns_key_without_code(env):
    _, _, _, fake, client = env
    first = _post_trial(client).json()

    again = client.post("/api/trial", json={"machine_id": MACHINE_ID})

    assert again.status_code == 200
    data = again.json()
    assert data["status"] == "existing" and data["key"] == first["key"]
    assert len(fake.minted) == 1


def test_existing_trial_backfills_verified_email(env):
    _, proxy_db, _, _, client = env
    now = datetime.now(timezone.utc)
    proxy_db.create_trial(
        OTHER_MACHINE_ID,
        "sk-legacy",
        _iso(now - timedelta(days=1)),
        _iso(now + timedelta(days=6)),
    )

    r = client.post("/api/trial", json={"machine_id": OTHER_MACHINE_ID, "email": EMAIL.upper()})

    assert r.status_code == 200 and r.json()["status"] == "existing"
    row = proxy_db.get_trial(OTHER_MACHINE_ID)
    assert row["email"] == EMAIL.lower() and row["email_verified"] == 1


def test_smtp_unset_returns_503(env, monkeypatch):
    _, _, patreon, _, client = env

    monkeypatch.setattr(patreon, "SMTP_HOST", "")
    r = _request_code(client)

    assert r.status_code == 503
    assert r.json() == {"detail": "email_delivery_unavailable"}


def test_code_requests_are_rate_limited_per_machine(env):
    _, proxy_db, _, _, client = env

    for _ in range(3):
        assert _request_code(client).status_code == 200
    r = _request_code(client)

    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    events = _events(proxy_db)
    assert events[-1]["event"] == "code_rate_limited"
