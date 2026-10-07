"""Shared circuit breaker, fast-fail sentinels, retry policy and first-token timeouts.

bug_20261006_185403: BackendHealth knew the model server was DOWN at
18:49:46, but nothing read it before calling, so every trigger kept paying
~5 s per gateway 500 and 12-30 s per saturated call (22 failed calls / 109 s,
then 4 / 84 s; coaching-loop stalls of 16-37 s). The startup probe was
GET /models, which LiteLLM answered "OK (208ms)" with vLLM dead.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace as NS

import pytest

from arenamcp.backend_health import BackendHealth, HealthState, check_gateway_health, is_backend_error_text
from arenamcp.backends import health, proxy
from arenamcp.backends.health import CircuitBreaker, add_circuit_listener, remove_circuit_listener
from arenamcp.backends.proxy import BackendError, BackendUnavailable, ProxyBackend

URL = "http://test.invalid/v1"
MODEL = "glm-5.3-flash"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in ("ARENAMCP_LLM_PRIORITY", "ARENAMCP_LLM_CIRCUIT", "ARENAMCP_LLM_BACKGROUND_LANE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ProxyBackend, "_local_warmup", lambda self: None)
    monkeypatch.setattr(proxy, "_BACKGROUND_LANE", proxy._BackgroundLane())
    health.reset_circuit_breakers()
    proxy.reset_priority_state()
    BackendHealth.reset_instance()
    yield
    health.reset_circuit_breakers()
    proxy.reset_priority_state()
    BackendHealth.reset_instance()


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeAPIError(Exception):
    def __init__(self, msg, status_code=None, body=None):
        super().__init__(msg)
        self.status_code = status_code
        self.body = body


class APIConnectionError(Exception):
    """Name matches the SDK's connection error, which the proxy keys on."""


class APITimeoutError(Exception):
    """Name matches the SDK's timeout error (a socket read timeout)."""


def chunk(text=None, *, reasoning=None, finish=None):
    has_choice = text is not None or reasoning is not None or finish is not None
    return NS(
        model=MODEL,
        usage=None,
        choices=[NS(delta=NS(content=text, reasoning_content=reasoning), finish_reason=finish)]
        if has_choice
        else [],
    )


def ok_stream(text="ok"):
    return [chunk(text, finish="stop")]


PROBE_OK = NS(model=MODEL, usage=None, choices=[NS(message=NS(content=""), finish_reason="length")])
LITELLM_500 = "Error code: 500 - OpenAIException - Connection error. No fallback model group found. LiteLLM Retried: 1 times"


def litellm_500():
    return FakeAPIError(LITELLM_500, status_code=500)


class FakeClient:
    def __init__(self, behaviors=()):
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []
        self.timeouts: list = []
        self.lock = threading.Lock()
        self.chat = NS(completions=NS(create=self._create))
        self.models = NS(list=lambda: NS(data=[]))

    def with_options(self, **options):
        self.timeouts.append(options.get("timeout"))
        return self

    def _create(self, **params):
        with self.lock:
            self.calls.append(params)
            behavior = self.behaviors.pop(0) if self.behaviors else ok_stream()
        if isinstance(behavior, Exception):
            raise behavior
        if callable(behavior):
            return behavior()
        if isinstance(behavior, list):
            return iter(behavior)
        return behavior


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Skip the proxy's real 0.5 s retry pause (time.sleep is the module's)."""
    monkeypatch.setattr(proxy.time, "sleep", lambda s: None)


def make_backend(behaviors=(), model=MODEL, url=URL) -> ProxyBackend:
    be = ProxyBackend(model=model, base_url=url)
    be._client = FakeClient(behaviors)
    return be


def install_breaker(clock: Clock, url=URL, model=MODEL) -> CircuitBreaker:
    """A registry breaker on a fake clock with no probe thread (tests drive probes)."""
    breaker = CircuitBreaker(f"{model} @ {url}", clock=clock, probe_thread=False)
    with health._REGISTRY_LOCK:
        health._REGISTRY[(url.rstrip("/"), model)] = breaker
    return breaker


def trip(breaker: CircuitBreaker, clock: Clock) -> None:
    for _ in range(3):
        breaker.record_failure("boom")
        clock.advance(3)
    assert breaker.snapshot()["state"] == "open"


# ── trip / close rules ────────────────────────────────────────────────


def test_three_failures_spanning_five_seconds_trip_it():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    assert not breaker.record_failure("a")
    clock.advance(3)
    assert not breaker.record_failure("b")
    clock.advance(2.5)
    assert breaker.record_failure("c")
    snap = breaker.snapshot()
    assert snap["state"] == "open"
    assert snap["retry_in_s"] == 5.0
    assert not breaker.available()
    assert not breaker.allow()


def test_a_burst_of_failures_in_one_second_needs_five_to_trip():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    for _ in range(4):
        breaker.record_failure("blip")
        clock.advance(0.2)
    assert breaker.available(), "four concurrent failures on one blip must not trip it"
    breaker.record_failure("blip")
    assert not breaker.available()


def test_a_single_slow_failure_never_trips_it():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    breaker.record_failure("30 s timeout")
    clock.advance(45)
    assert breaker.available()


def test_success_resets_the_streak():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    breaker.record_failure("a")
    clock.advance(3)
    breaker.record_failure("b")
    breaker.record_success()
    clock.advance(3)
    breaker.record_failure("c")
    assert breaker.available()
    assert breaker.snapshot()["consecutive_failures"] == 1


def test_a_stale_streak_starts_over():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    breaker.record_failure("a")
    clock.advance(3)
    breaker.record_failure("b")
    clock.advance(61)
    breaker.record_failure("c")
    assert breaker.available()
    assert breaker.snapshot()["consecutive_failures"] == 1


def test_probe_success_closes_and_probation_reopens_on_two_failures():
    """2026-10-07 review: one slow first token (or one blip) right after a
    recovery re-opened the breaker for 15 s against a healthy server."""
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    trip(breaker, clock)
    assert breaker.probe_now(lambda: True)
    snap = breaker.snapshot()
    assert snap["state"] == "closed"
    assert snap["probation"] is True
    assert breaker.retry_allowed(), "the in-call retry absorbs a blip during probation"
    clock.advance(30)
    assert not breaker.record_failure("one slow first token")
    assert breaker.available()
    clock.advance(1)
    assert breaker.record_failure("again"), "two failures in a row during probation re-open it"
    assert not breaker.available()


def test_a_success_between_probation_failures_resets_the_count():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    trip(breaker, clock)
    breaker.probe_now(lambda: True)
    assert not breaker.record_failure("one")
    breaker.record_success()
    assert not breaker.record_failure("two")
    assert breaker.available()


def test_after_probation_one_failure_is_tolerated_again():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    trip(breaker, clock)
    breaker.probe_now(lambda: True)
    clock.advance(121)
    assert breaker.retry_allowed()
    breaker.record_failure("one")
    assert breaker.available()


def test_failed_probes_retry_every_five_seconds_without_backing_off():
    """2026-10-07 review: backing off to 30 s kept a recovered server
    'offline' for 20-28 s (41 typed decisions on the fallback)."""
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    trip(breaker, clock)
    assert breaker.snapshot()["probe_interval_s"] == 5.0

    def failing_probe():
        raise ConnectionError("still down")

    for _ in range(4):
        assert not breaker.probe_now(failing_probe)
        assert breaker.snapshot()["retry_in_s"] == 5.0
        clock.advance(5)
    assert breaker.snapshot()["probe_interval_s"] == 5.0
    assert not breaker.available()
    # A long outage (not a restart) is probed every 15 s.
    clock.advance(300)
    assert not breaker.probe_now(failing_probe)
    assert breaker.snapshot()["retry_in_s"] == 15.0
    assert breaker.probe_now(lambda: True)
    trip(breaker, clock)
    assert not breaker.available()
    assert breaker.snapshot()["probe_interval_s"] == 5.0, "the next outage starts at 5 s again"


def test_a_late_failure_from_before_the_open_does_not_delay_the_probe():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    old = breaker.admit()
    trip(breaker, clock)  # opens 3 s ago: the first probe is due in 2 s
    clock.advance(1)
    breaker.record_failure("a 30 s call admitted before the open ends", token=old)
    assert breaker.snapshot()["retry_in_s"] == pytest.approx(1.0)


def test_a_late_success_from_before_the_open_keeps_it_open():
    """A background stream that held a server slot finishes while new calls
    get no first token: that proves nothing about the server now."""
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    old = breaker.admit()
    trip(breaker, clock)
    assert not breaker.record_success(token=old)
    assert breaker.snapshot()["state"] == "open"
    assert breaker.snapshot()["consecutive_failures"] == 0, "the streak is still reset"
    assert breaker.probe_now(lambda: True), "a probe still closes it"


def test_a_late_failure_from_before_the_close_does_not_count_in_probation():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    trip(breaker, clock)
    in_flight = breaker.call_token()
    breaker.probe_now(lambda: True)
    for _ in range(3):
        assert not breaker.record_failure("admitted while it was open", token=in_flight)
    assert breaker.available()
    assert breaker.snapshot()["consecutive_failures"] == 0


def test_without_a_probe_thread_one_trial_call_per_interval_is_admitted():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    trip(breaker, clock)
    assert not breaker.allow()
    clock.advance(15)
    assert breaker.allow(), "half-open trial"
    assert not breaker.allow(), "only one trial per interval"
    assert not breaker.available()
    clock.advance(15)
    assert breaker.allow()


def test_background_work_stops_at_three_fresh_failures_before_the_breaker_opens():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    for _ in range(3):
        breaker.record_failure("x")
        clock.advance(0.5)
    assert breaker.available()
    assert breaker.background_blocked()
    clock.advance(61)
    assert not breaker.background_blocked(), "a stale streak no longer blocks background work"


def test_listeners_hear_open_and_close():
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    events = []

    def listener(event, snap):
        events.append((event, snap["state"]))

    add_circuit_listener(listener)
    try:
        trip(breaker, clock)
        breaker.record_success()
    finally:
        remove_circuit_listener(listener)
    assert events == [("opened", "open"), ("closed", "closed")]


def test_env_switch_keeps_it_closed(monkeypatch):
    monkeypatch.setenv("ARENAMCP_LLM_CIRCUIT", "0")
    clock = Clock()
    breaker = CircuitBreaker("k", clock=clock, probe_thread=False)
    for _ in range(6):
        breaker.record_failure("x")
        clock.advance(3)
    assert breaker.available()
    assert breaker.allow()
    assert breaker.snapshot()["state"] == "closed"


def test_probe_callable_is_held_weakly():
    breaker = CircuitBreaker("k", probe_thread=False)

    class Owner:
        def probe(self):
            return True

    owner = Owner()
    breaker.register_probe(owner.probe)
    assert breaker._probe_callable() is not None
    del owner
    assert breaker._probe_callable() is None


# ── proxy integration ─────────────────────────────────────────────────


def test_open_breaker_fails_fast_without_network_or_health_noise():
    clock = Clock()
    breaker = install_breaker(clock)
    be = make_backend([litellm_500(), litellm_500(), litellm_500()])
    for _ in range(3):
        out = be.complete("s", "u", call_class="decision.typed")
        assert is_backend_error_text(out)
        clock.advance(3)
    assert breaker.snapshot()["state"] == "open"
    assert len(be._client.calls) == 3, "LiteLLM already retried: no client retry"
    failures = BackendHealth.instance().snapshot()["total_failures"]

    started = time.perf_counter()
    with pytest.raises(BackendUnavailable) as exc:
        be.complete("s", "u", call_class="decision.typed", raise_on_error=True)
    assert time.perf_counter() - started < 0.05
    assert exc.value.circuit_open is True
    assert exc.value.retryable is False
    assert exc.value.reason == "circuit_open"
    assert exc.value.retry_in_s == pytest.approx(5.0, abs=3.1)
    assert isinstance(exc.value, BackendError)
    assert proxy.is_backend_unavailable(exc.value)

    out = be.complete("s", "u", call_class="coach.advice")
    assert out.startswith("[BACKEND ERROR] model server unavailable (circuit open; retry in ")
    assert is_backend_error_text(out)
    assert proxy.is_backend_unavailable(out)
    assert not proxy.is_backend_unavailable("[BACKEND ERROR] HTTP 500")
    assert not proxy.is_backend_unavailable("")
    assert be.last_request_metrics["status"] == "circuit_open"
    assert be.last_request_metrics["call_class"] == "coach.advice"
    assert len(be._client.calls) == 3
    assert BackendHealth.instance().snapshot()["total_failures"] == failures
    assert be.available() is False


def test_breaker_is_shared_across_backend_instances_per_endpoint():
    clock = Clock()
    install_breaker(clock)
    first = make_backend([litellm_500() for _ in range(3)])
    for _ in range(3):
        first.complete("s", "u")
        clock.advance(3)
    second = make_backend()
    assert proxy.is_backend_unavailable(second.complete("s", "u"))
    assert second._client.calls == []
    other_model = make_backend(model="gemma-4-12b-it")
    assert other_model.complete("s", "u") == "ok"


def test_background_calls_are_skipped_while_the_endpoint_is_down():
    clock = Clock()
    breaker = install_breaker(clock)
    be = make_backend([litellm_500() for _ in range(3)])
    for _ in range(3):
        be.complete("s", "u", call_class="decision.typed")
        clock.advance(0.5)
    assert breaker.available(), "three failures inside one second do not open it"
    out = be.complete("s", "u", call_class="background.game_plan")
    assert out == "[BACKEND ERROR] model server unavailable (backend down; background call skipped)"
    assert proxy.is_backend_unavailable(out)
    assert be.last_request_metrics["status"] == "backend_down"
    assert len(be._client.calls) == 3
    assert be.complete("s", "u", call_class="decision.typed") == "ok", "foreground still goes out"


def test_background_call_rechecks_the_breaker_after_waiting_for_the_lane(monkeypatch):
    clock = Clock()
    breaker = install_breaker(clock)
    lane = proxy._BACKGROUND_LANE
    real_acquire = lane.acquire

    def acquire_while_the_endpoint_dies(handle, wait_s):
        trip(breaker, clock)
        return real_acquire(handle, wait_s)

    monkeypatch.setattr(lane, "acquire", acquire_while_the_endpoint_dies)
    be = make_backend()
    out = be.complete("s", "u", call_class="background.game_plan")
    assert proxy.is_backend_unavailable(out)
    assert be._client.calls == []
    assert lane.holder_class() is None, "the lane is released after the skip"


def test_a_real_success_from_a_trial_call_closes_the_breaker():
    clock = Clock()
    breaker = install_breaker(clock)
    trip(breaker, clock)
    be = make_backend()
    assert proxy.is_backend_unavailable(be.complete("s", "u"))
    clock.advance(15)
    assert be.complete("s", "u") == "ok"
    assert breaker.snapshot()["state"] == "closed"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_and_auth_errors_never_count(status):
    clock = Clock()
    breaker = install_breaker(clock)
    be = make_backend([FakeAPIError("rejected", status_code=status) for _ in range(6)])
    for _ in range(6):
        be.complete("s", "u")
        clock.advance(3)
    assert breaker.snapshot()["consecutive_failures"] == 0
    assert breaker.available()


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 524])
def test_server_unavailable_statuses_count(status):
    clock = Clock()
    breaker = install_breaker(clock)
    be = make_backend([FakeAPIError("x LiteLLM Retried: 1 times", status_code=status)])
    be.complete("s", "u")
    assert breaker.snapshot()["consecutive_failures"] == 1


def test_connection_errors_count_and_get_one_retry_from_healthy(no_retry_sleep):
    clock = Clock()
    breaker = install_breaker(clock)
    be = make_backend([APIConnectionError("refused"), APIConnectionError("refused")])
    assert is_backend_error_text(be.complete("s", "u"))
    assert len(be._client.calls) == 2
    assert breaker.snapshot()["consecutive_failures"] == 1


def test_litellm_retried_500_is_not_retried_again_by_the_client():
    be = make_backend([litellm_500()])
    be.complete("s", "u")
    assert len(be._client.calls) == 1


def test_plain_503_still_gets_one_retry_from_a_healthy_endpoint(no_retry_sleep):
    be = make_backend([FakeAPIError("unavailable", status_code=503), ok_stream("x")])
    assert be.complete("s", "u") == "x"
    assert len(be._client.calls) == 2


def test_one_client_retry_during_probation_absorbs_a_blip(no_retry_sleep):
    clock = Clock()
    breaker = install_breaker(clock)
    trip(breaker, clock)
    breaker.record_success()
    be = make_backend([APIConnectionError("reset"), ok_stream("x")])
    assert be.complete("s", "u") == "x"
    assert len(be._client.calls) == 2
    assert breaker.available()


def test_two_failing_calls_during_probation_reopen_it(no_retry_sleep):
    clock = Clock()
    breaker = install_breaker(clock)
    trip(breaker, clock)
    breaker.record_success()
    be = make_backend([litellm_500(), litellm_500()])
    assert is_backend_error_text(be.complete("s", "u"))
    assert breaker.available(), "one failed call during probation is tolerated"
    clock.advance(1)
    assert is_backend_error_text(be.complete("s", "u"))
    assert not breaker.available()


def test_disabled_breaker_lets_every_call_through(monkeypatch, no_retry_sleep):
    monkeypatch.setenv("ARENAMCP_LLM_CIRCUIT", "off")
    be = make_backend([APIConnectionError("refused") for _ in range(12)])
    for _ in range(6):
        be.complete("s", "u")
    assert len(be._client.calls) == 12
    assert be.available()


# ── probes ────────────────────────────────────────────────────────────


def test_probe_inference_sends_one_token_and_closes_the_breaker():
    clock = Clock()
    breaker = install_breaker(clock)
    trip(breaker, clock)
    be = make_backend([PROBE_OK])
    ok, detail = be.probe_inference(timeout=4)
    assert ok is True
    assert "inference OK" in detail
    params = be._client.calls[0]
    assert params["max_tokens"] == 1
    assert "stream" not in params
    assert params["messages"] == [{"role": "user", "content": "ping"}]
    assert be._client.timeouts == [4]
    assert breaker.snapshot()["state"] == "closed"
    snap = BackendHealth.instance().snapshot()
    assert snap["state"] == HealthState.OK.value
    assert "inference probe OK" in snap["detail"]
    assert be.last_request_metrics["call_class"] == "probe"


def test_probe_inference_failure_is_reported_and_counted():
    clock = Clock()
    breaker = install_breaker(clock)
    be = make_backend([litellm_500()])
    ok, detail = be.probe_inference()
    assert ok is False
    assert "inference probe failed" in detail
    assert breaker.snapshot()["consecutive_failures"] == 1
    assert BackendHealth.instance().state is HealthState.DEGRADED


def test_model_listing_never_closes_the_breaker():
    """LiteLLM answers GET /models with vLLM dead ("OK (208ms)" at 18:50:46)."""
    clock = Clock()
    breaker = install_breaker(clock)
    trip(breaker, clock)
    be = make_backend()
    check_gateway_health(be, timeout=2)
    assert breaker.snapshot()["state"] == "open"


def test_breaker_probe_treats_an_auth_error_as_an_answer():
    be = make_backend([FakeAPIError("bad key", status_code=401)])
    assert be._circuit_probe() is True


def test_breaker_probe_raises_on_a_server_failure():
    be = make_backend([litellm_500()])
    with pytest.raises(FakeAPIError):
        be._circuit_probe()


def test_probe_thread_closes_the_breaker_once_inference_answers(monkeypatch, no_retry_sleep):
    monkeypatch.setattr(CircuitBreaker, "PROBE_INTERVAL_S", 0.05)
    be = make_backend(
        [APIConnectionError("refused") for _ in range(10)] + [APIConnectionError("still"), PROBE_OK]
    )
    for _ in range(5):
        be.complete("s", "u", request_timeout_s=5)
    assert not be.available()
    deadline = time.monotonic() + 5
    poll = threading.Event()
    while not be.available() and time.monotonic() < deadline:
        poll.wait(0.02)
    assert be.available(), "the probe thread should close it after one failed and one good probe"
    probes = [call for call in be._client.calls if call.get("max_tokens") == 1]
    assert len(probes) == 2
    assert be.circuit_snapshot()["probation"] is True


def test_registering_a_probe_restarts_probing_for_an_open_breaker(monkeypatch):
    monkeypatch.setattr(CircuitBreaker, "PROBE_INTERVAL_S", 0.05)
    breaker = CircuitBreaker("k")
    for _ in range(5):
        breaker.record_failure("down")
    assert not breaker.available()
    assert breaker._probe_thread is None, "no probe callable yet: half-open trials only"
    breaker.register_probe(lambda: True)
    deadline = time.monotonic() + 5
    poll = threading.Event()
    while not breaker.available() and time.monotonic() < deadline:
        poll.wait(0.02)
    assert breaker.available()
    breaker.stop()


# ── first-token timeout ───────────────────────────────────────────────


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def perf_counter(self):
        return self.now


def timed_stream(clock, steps):
    for delay, item in steps:
        clock.now += delay
        yield item


@pytest.fixture
def fake_time(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(proxy.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(proxy.time, "sleep", lambda s: setattr(clock, "now", clock.now + s))
    return clock


def test_no_first_token_in_time_is_its_own_status_and_is_not_retried(fake_time):
    stream = timed_stream(fake_time, [(3, chunk()), (3, chunk()), (3, chunk("late", finish="stop"))])
    be = make_backend([lambda: stream])
    with pytest.raises(BackendError) as exc:
        be.complete("s", "u", request_timeout_s=12, first_token_timeout_s=5, raise_on_error=True)
    assert exc.value.kind == "first_token_timeout"
    assert exc.value.first_token_seen is False
    assert len(be._client.calls) == 1
    assert be.last_request_metrics["status"] == "ttft_timeout"
    assert be.last_request_metrics["ttft_ms"] is None
    assert be.circuit_snapshot()["consecutive_failures"] == 1


def test_first_token_timeout_sets_a_short_socket_read_limit(fake_time):
    be = make_backend()
    be.complete("s", "u", request_timeout_s=12, first_token_timeout_s=6)
    timeout = be._client.timeouts[0]
    assert timeout.read == pytest.approx(6)
    assert timeout.connect == pytest.approx(12)
    plain = make_backend()
    plain.complete("s", "u", request_timeout_s=12)
    assert plain._client.timeouts[0] == pytest.approx(12)


def test_socket_read_timeout_before_any_token_counts_as_first_token_timeout(fake_time):
    be = make_backend([APITimeoutError("Request timed out.")])
    out = be.complete("s", "u", request_timeout_s=12, first_token_timeout_s=6)
    assert out.startswith("[BACKEND ERROR] no first token within 6.0s")
    assert len(be._client.calls) == 1
    assert be.last_request_metrics["status"] == "ttft_timeout"


def test_without_first_token_timeout_a_timeout_keeps_its_budgeted_retry(fake_time):
    be = make_backend([APITimeoutError("Request timed out."), ok_stream("x")])
    assert be.complete("s", "u", request_timeout_s=12) == "x"
    assert len(be._client.calls) == 2


def test_tokens_arriving_in_time_are_unaffected(fake_time):
    stream = timed_stream(fake_time, [(2, chunk(reasoning="r")), (5, chunk("Cast it.", finish="stop"))])
    be = make_backend([lambda: stream])
    assert be.complete("s", "u", request_timeout_s=12, first_token_timeout_s=4) == "Cast it."


def test_timeout_after_the_first_token_is_slow_not_down(fake_time):
    stream = timed_stream(fake_time, [(1, chunk(reasoning="thinking")), (20, chunk("too late"))])
    be = make_backend([lambda: stream])
    with pytest.raises(BackendError) as exc:
        be.complete("s", "u", request_timeout_s=12, raise_on_error=True)
    assert exc.value.first_token_seen is True
    assert be.circuit_snapshot()["consecutive_failures"] == 0
    assert BackendHealth.instance().snapshot()["total_failures"] == 1


def test_one_first_token_timeout_during_probation_does_not_reopen(fake_time):
    """2026-10-07 review: right after a recovery, one decision whose first
    token came at 9 s (limit 8 s) re-opened the breaker for 15 s against a
    healthy server."""
    clock = Clock()
    breaker = install_breaker(clock)
    trip(breaker, clock)
    breaker.record_success()
    stream = timed_stream(fake_time, [(9, chunk(reasoning="late")), (0.2, chunk("Cast it.", finish="stop"))])
    be = make_backend([lambda: stream])
    out = be.complete("s", "u", request_timeout_s=12, first_token_timeout_s=8)
    assert out.startswith("[BACKEND ERROR] no first token within 8.0s")
    assert breaker.available(), "one slow first token in probation is not an outage"
    assert be.complete("s", "u") == "ok"


def test_a_stream_admitted_before_the_open_cannot_close_it():
    """A background stream that already held a server slot finishes while new
    requests get no first token (2026-10-07 review: the UI flapped between
    'reachable again' and 'unavailable' and the next decision stalled 8 s)."""
    clock = Clock()
    breaker = install_breaker(clock)
    release = threading.Event()

    def slow_stream():
        assert release.wait(5)
        yield chunk("plan", finish="stop")

    be = make_backend([slow_stream])
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("out", be.complete("s", "u", call_class="background.game_plan")),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not be._client.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    for _ in range(5):
        breaker.record_failure("saturated: no first token")
    assert not breaker.available()
    release.set()
    thread.join(5)
    assert result["out"] == "plan"
    assert breaker.snapshot()["state"] == "open", "a success admitted before the open proves nothing now"
    assert breaker.probe_now(lambda: True)
