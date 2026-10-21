"""Access badge text (subscriber / trial countdown) and the subscription-check request target."""

import io
import json
import time
import urllib.error

import pytest

from arenamcp import subscription
from arenamcp.subscription import SubscriptionStatus, check_subscription, describe_access

NOW = 1_800_000_000.0


def test_no_key_prompts_setup():
    text, tone, _, needs = describe_access("", None, now=NOW)
    assert (text, tone, needs) == ("No key", "warn", True)


def test_subscriber_badge_includes_name_in_tooltip():
    st = SubscriptionStatus("active", kind="subscriber", name="Josh Armour")
    text, tone, tip, needs = describe_access("sk-x", st, now=NOW)
    assert text == "★ Subscriber" and tone == "good" and "Josh Armour" in tip and not needs


@pytest.mark.parametrize(
    "left,expected,tone",
    [
        (4 * 86400 + 6 * 3600 + 60, "Trial · 4d 6h left", "good"),
        (5 * 3600 + 30 * 60, "Trial · 5h 30m left", "warn"),
        (90, "Trial · 1m left", "warn"),
    ],
)
def test_trial_countdown_from_server_expiry(left, expected, tone):
    st = SubscriptionStatus("trial", kind="trial", expires_at=NOW + left)
    text, got_tone, _, needs = describe_access("sk-trial", st, now=NOW)
    assert (text, got_tone, needs) == (expected, tone, False)


def test_trial_countdown_falls_back_to_provisioned_iso_expiry():
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 2 * 86400 + 3600))
    text, _, _, _ = describe_access("sk-trial", SubscriptionStatus("unknown"), iso, now=NOW)
    assert text == "Trial · 2d 1h left"


def test_trial_ended_needs_subscription():
    st = SubscriptionStatus("expired", kind="trial", expires_at=NOW - 10)
    text, tone, _, needs = describe_access("sk-trial", st, now=NOW)
    assert (text, tone, needs) == ("Trial ended — Subscribe", "bad", True)
    # Locally lapsed before the server has been asked again.
    text, _, _, needs = describe_access("sk-trial", None, "2020-01-01T00:00:00Z", now=NOW)
    assert text == "Trial ended — Subscribe" and needs


def test_invalid_and_unknown():
    assert describe_access("sk-x", SubscriptionStatus("invalid"), now=NOW)[3] is True
    text, tone, _, needs = describe_access("sk-x", SubscriptionStatus("unknown"), now=NOW)
    assert (text, tone, needs) == ("Status unknown", "muted", False)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(subscription, "_SUB_CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(subscription, "_SETTINGS_DIR", tmp_path)


def test_check_targets_website_not_gateway(no_cache, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return _Resp(json.dumps({"status": "trial", "kind": "trial", "expires_at": NOW, "name": ""}).encode())

    monkeypatch.setattr(subscription.urllib.request, "urlopen", fake_urlopen)
    st = check_subscription("sk-trial", force=True)
    assert seen["url"] == "https://mtgacoach.com/v1/subscription/check"
    assert (st.status, st.kind, st.expires_at) == ("trial", "trial", NOW) and st.is_valid
    cached = subscription._load_cache("sk-trial")
    assert cached is not None and cached.kind == "trial"


def test_expired_trial_402_keeps_server_message(no_cache, monkeypatch):
    body = json.dumps(
        {"status": "expired", "kind": "trial", "expires_at": NOW, "message": "Your 7-day trial has ended."}
    ).encode()

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {}, io.BytesIO(body))

    monkeypatch.setattr(subscription.urllib.request, "urlopen", fake_urlopen)
    st = check_subscription("sk-trial", force=True)
    assert (st.status, st.kind, st.message) == ("expired", "trial", "Your 7-day trial has ended.")
    assert st.needs_subscription
