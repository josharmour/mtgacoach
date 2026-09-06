"""Backend health architecture: tracker transitions, tags, gateway probe.

Covers the 2026-07-23 backend-health build: silent LLM failures (gateway
outage 2026-07-05, license-key 401s 2026-07-16) must be announced via the
BackendHealth tracker and [BACKEND ERROR]/[LOCAL FALLBACK] tags.
"""

import urllib.error

import pytest

from arenamcp.backend_health import (
    BACKEND_ERROR_PREFIX,
    LOCAL_FALLBACK_PREFIX,
    BackendHealth,
    HealthState,
    check_gateway_health,
    is_backend_error_text,
    is_local_fallback_text,
    strip_health_tags,
)
from arenamcp.backends.proxy import ProxyBackend
from arenamcp.coach import CoachEngine, _fallback_non_action_advice


@pytest.fixture(autouse=True)
def _reset_singleton():
    BackendHealth.reset_instance()
    yield
    BackendHealth.reset_instance()


# ── Tracker state transitions ────────────────────────────────────────


def test_fresh_tracker_is_ok():
    tracker = BackendHealth()
    assert tracker.state is HealthState.OK
    assert tracker.snapshot()["consecutive_failures"] == 0


def test_failures_walk_ok_degraded_down():
    tracker = BackendHealth()
    assert tracker.record_failure(error="boom") is True  # OK → DEGRADED
    assert tracker.state is HealthState.DEGRADED
    assert tracker.record_failure(error="boom") is False  # stays DEGRADED
    assert tracker.state is HealthState.DEGRADED
    assert tracker.record_failure(error="boom") is True  # DEGRADED → DOWN
    assert tracker.state is HealthState.DOWN
    assert tracker.record_failure(error="boom") is False  # stays DOWN
    assert tracker.state is HealthState.DOWN


def test_success_resets_to_ok():
    tracker = BackendHealth()
    for _ in range(3):
        tracker.record_failure(error="boom")
    assert tracker.state is HealthState.DOWN
    assert tracker.record_success(detail="recovered") is True
    assert tracker.state is HealthState.OK
    snap = tracker.snapshot()
    assert snap["consecutive_failures"] == 0
    assert snap["total_failures"] == 3
    assert snap["total_successes"] == 1


def test_transition_listener_fires_on_change_only():
    tracker = BackendHealth()
    seen: list[dict] = []
    tracker.add_listener(seen.append)
    tracker.record_failure(error="a")  # transition → fires
    tracker.record_failure(error="b")  # no transition → silent
    assert len(seen) == 1
    assert seen[0]["state"] == HealthState.DEGRADED.value


def test_broken_listener_does_not_break_tracker():
    tracker = BackendHealth()

    def _bad_listener(snapshot):
        raise RuntimeError("listener exploded")

    tracker.add_listener(_bad_listener)
    assert tracker.record_failure(error="a") is True
    assert tracker.state is HealthState.DEGRADED


def test_snapshot_shape():
    tracker = BackendHealth()
    tracker.record_failure(error="HTTP 401 from gateway", status_code=401)
    snap = tracker.snapshot()
    for key in (
        "state",
        "consecutive_failures",
        "total_successes",
        "total_failures",
        "last_success_ts",
        "last_failure_ts",
        "last_error",
        "last_status_code",
        "detail",
        "timestamp",
    ):
        assert key in snap, f"missing snapshot key: {key}"
    assert snap["state"] == HealthState.DEGRADED.value
    assert snap["last_status_code"] == 401
    assert "401" in snap["detail"]


# ── Tag predicates ───────────────────────────────────────────────────


def test_is_backend_error_text_matches_new_and_legacy():
    assert is_backend_error_text(f"{BACKEND_ERROR_PREFIX} auth failed")
    assert is_backend_error_text("Error getting advice: auth failed")
    assert is_backend_error_text("  Error: LLM timed out after 30s")
    assert not is_backend_error_text("Attack with the 3/3 first.")
    assert not is_backend_error_text("")
    assert not is_backend_error_text(None)


def test_is_local_fallback_text():
    assert is_local_fallback_text(f"{LOCAL_FALLBACK_PREFIX} pass priority")
    assert not is_local_fallback_text("pass priority")


def test_strip_health_tags():
    assert strip_health_tags(f"{BACKEND_ERROR_PREFIX} auth failed") == "auth failed"
    assert strip_health_tags(f"{LOCAL_FALLBACK_PREFIX} pass priority") == "pass priority"
    assert strip_health_tags("Attack first.") == "Attack first."


# ── Coach fallback generators are tagged ─────────────────────────────


def test_fallback_non_action_advice_tagged():
    out = _fallback_non_action_advice({})
    assert out.startswith(LOCAL_FALLBACK_PREFIX)
    assert "pass priority" in out


def test_fallback_non_action_advice_tagged_for_mulligan():
    out = _fallback_non_action_advice({"_bridge_request_class": "MulliganRequest"})
    assert out.startswith(LOCAL_FALLBACK_PREFIX)


class _DummyBackend:
    def __init__(self, response: str = "Pass.") -> None:
        self.response = response
        self.model = "dummy-health-model"

    def complete(self, system_prompt: str, user_message: str, *args, **kwargs) -> str:
        return self.response


def test_build_threat_fallback_tagged():
    coach = CoachEngine(backend=_DummyBackend())
    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 14},
            {"seat_id": 2, "is_local": False, "life_total": 18},
        ],
        "turn": {"turn_number": 6, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "hand": [],
        "battlefield": [],
        "graveyard": [],
        "stack": [],
    }
    threat = {
        "name": "Sheoldred, the Apocalypse",
        "warning": "Drains 2 on your draws!",
        "card": {"type_line": "Legendary Creature — Praetor"},
    }
    out = coach._build_threat_fallback(game_state, threat)
    assert out.startswith(LOCAL_FALLBACK_PREFIX)
    assert "Sheoldred" in out


# ── Proxy error returns carry the tag ────────────────────────────────


class _FakeAPIError(Exception):
    def __init__(self, msg, status_code=None, body=None):
        super().__init__(msg)
        self.status_code = status_code
        self.body = body


class _Completions:
    """Scriptable completions endpoint: pops one behavior per call."""

    def __init__(self, script):
        self.script = list(script)

    def create(self, **params):
        behavior = self.script.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _Client:
    def __init__(self, script):
        self.chat = type("chat", (), {})()
        self.chat.completions = _Completions(script)

    def with_options(self, **kw):
        return self


def _proxy_backend(script) -> ProxyBackend:
    be = ProxyBackend(model="nemotron-3-super", base_url="http://test.invalid/v1")
    be._client = _Client(script)
    return be


def test_proxy_error_return_tagged_and_detectable():
    be = _proxy_backend([_FakeAPIError("auth", status_code=401)])
    out = be.complete("sys", "user", 16)
    assert out.startswith(BACKEND_ERROR_PREFIX)
    assert is_backend_error_text(out)


def test_proxy_failure_feeds_tracker():
    be = _proxy_backend([_FakeAPIError("auth", status_code=401)])
    be.complete("sys", "user", 16)
    snap = BackendHealth.instance().snapshot()
    assert snap["total_failures"] >= 1
    assert snap["state"] == HealthState.DEGRADED.value


# ── Gateway probe ────────────────────────────────────────────────────


class _ProbeBackend:
    _base_url = "http://gateway.test/v1"
    _api_key = "test-key"
    model = "probe-model"


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"data": []}'


def test_probe_ok_on_200(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResponse())
    state, detail = check_gateway_health(_ProbeBackend())
    assert state is HealthState.OK
    assert "reachable" in detail
    assert BackendHealth.instance().state is HealthState.OK


def test_probe_degraded_on_http_error(monkeypatch):
    def _raise_http(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _raise_http)
    state, detail = check_gateway_health(_ProbeBackend())
    assert state is HealthState.DEGRADED
    assert "401" in detail


def test_probe_down_on_unreachable(monkeypatch):
    def _raise_url(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise_url)
    state, detail = check_gateway_health(_ProbeBackend())
    assert state is HealthState.DOWN
    assert "unreachable" in detail
