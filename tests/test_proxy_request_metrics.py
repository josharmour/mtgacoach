"""Streaming usage, compatibility, and shared retry budget regressions."""

from types import SimpleNamespace as NS

import pytest

from arenamcp.backends import proxy


class FakeAPIError(Exception):
    status_code = 400


class FakeClock:
    now = 100.0

    def perf_counter(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class FakeStream:
    def __init__(self, clock, chunks):
        self.clock = clock
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        for delay, chunk in self.chunks:
            self.clock.now += delay
            yield chunk

    def close(self):
        self.closed = True


def chunk(text=None, *, reasoning=None, finish=None, usage=None):
    return NS(
        model="glm-5.3-flash-local",
        usage=usage,
        choices=[NS(delta=NS(content=text, reasoning_content=reasoning), finish_reason=finish)]
        if text is not None or reasoning is not None or finish is not None
        else [],
    )


def backend(monkeypatch, clock, behaviors):
    monkeypatch.setattr(proxy.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(proxy.time, "sleep", clock.sleep)
    calls = []
    timeouts = []

    def create(**params):
        calls.append(params)
        behavior = behaviors.pop(0)
        if callable(behavior):
            return behavior()
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    client = NS(chat=NS(completions=NS(create=create)))

    def with_options(**options):
        timeouts.append(options["timeout"])
        return client

    client.with_options = with_options
    be = proxy.ProxyBackend(model="glm-5.3-flash", base_url="http://test.invalid/v1")
    be._client = client
    return be, calls, timeouts


def test_stream_usage_only_chunk_and_reasoning_ttft(monkeypatch, caplog):
    clock = FakeClock()
    usage = NS(
        prompt_tokens=20000,
        completion_tokens=120,
        prompt_tokens_details=NS(cached_tokens=18000),
        completion_tokens_details=NS(reasoning_tokens=100),
    )
    stream = FakeStream(
        clock,
        [
            (0.1, chunk()),
            (0.2, chunk(reasoning="private deliberation")),
            (0.4, chunk("Cast removal.")),
            (0.1, chunk(finish="stop")),
            (0.1, chunk(usage=usage)),
        ],
    )
    be, calls, _ = backend(monkeypatch, clock, [stream])
    with caplog.at_level("DEBUG", logger=proxy.__name__):
        assert be.complete("private system", "private state", request_timeout_s=12) == "Cast removal."
    assert calls[0]["stream_options"] == {"include_usage": True}
    metrics = be.last_request_metrics
    assert metrics["input_tokens"] == 20000
    assert metrics["output_tokens"] == 120
    assert metrics["cached_input_tokens"] == 18000
    assert metrics["reasoning_tokens"] == 100
    assert metrics["ttft_ms"] == pytest.approx(300)
    assert metrics["first_content_ms"] == pytest.approx(700)
    assert metrics["total_ms"] == pytest.approx(900)
    assert metrics["finish_reason"] == "stop"
    assert stream.closed
    for secret in ("private system", "private state", "private deliberation"):
        assert secret not in caplog.text


def test_usage_rejection_retries_stream_without_option_and_remembers(monkeypatch):
    clock = FakeClock()
    streams = [FakeStream(clock, [(0.1, chunk("ok", finish="stop"))]) for _ in range(2)]
    be, calls, _ = backend(monkeypatch, clock, [FakeAPIError("unsupported stream_options"), *streams])
    assert be.complete("s", "u") == "ok"
    assert be.complete("s", "u") == "ok"
    assert len(calls) == 3
    assert all(call["stream"] for call in calls)
    assert "stream_options" in calls[0]
    assert all("stream_options" not in call for call in calls[1:])
    assert be.last_request_metrics["input_tokens"] is None
    assert be.last_request_metrics["cached_input_tokens"] is None


def test_unrelated_bad_request_is_not_retried_as_usage_incompatibility(monkeypatch):
    be, calls, _ = backend(monkeypatch, FakeClock(), [FakeAPIError("invalid model")])
    with pytest.raises(proxy.BackendError):
        be.complete("s", "u", raise_on_error=True)
    assert len(calls) == 1


def test_retries_share_budget_and_metrics_include_retry_delay(monkeypatch):
    clock = FakeClock()

    def first_attempt():
        clock.now += 8
        raise TimeoutError("read stalled")

    stream = FakeStream(clock, [(1, chunk("ok", finish="stop"))])
    be, _, timeouts = backend(monkeypatch, clock, [first_attempt, stream])
    assert be.complete("s", "u", request_timeout_s=12) == "ok"
    assert timeouts == pytest.approx([12, 3.5])
    assert be.last_request_metrics["attempts"] == 2
    assert be.last_request_metrics["total_ms"] == pytest.approx(9500)


def test_exhausted_budget_does_not_start_another_request(monkeypatch):
    clock = FakeClock()

    def first_attempt():
        clock.now += 12
        raise TimeoutError("read stalled")

    be, calls, _ = backend(monkeypatch, clock, [first_attempt])
    with pytest.raises(proxy.BackendError):
        be.complete("s", "u", request_timeout_s=12, raise_on_error=True)
    assert len(calls) == 1
    assert be.last_request_metrics["status"] == "error"


def test_continuously_streaming_model_stops_on_budget_and_closes(monkeypatch):
    clock = FakeClock()
    stream = FakeStream(clock, [(4, chunk(reasoning="thinking")) for _ in range(4)])
    be, calls, _ = backend(monkeypatch, clock, [stream])
    with pytest.raises(proxy.BackendError):
        be.complete("s", "u", request_timeout_s=12, raise_on_error=True)
    assert len(calls) == 1
    assert stream.closed


def test_nonstream_fallback_has_usage_but_no_fabricated_ttft(monkeypatch):
    clock = FakeClock()
    response = NS(
        model="glm-5.3-flash-local",
        usage={"prompt_tokens": 100, "completion_tokens": 8, "cache_read_input_tokens": 90},
        choices=[NS(message=NS(content="ok"), finish_reason="length")],
    )
    be, _, _ = backend(monkeypatch, clock, [ValueError("no streaming"), response])
    assert be.complete("s", "u") == "ok"
    metrics = be.last_request_metrics
    assert metrics["streamed"] is False
    assert metrics["cached_input_tokens"] == 90
    assert metrics["ttft_ms"] is None
    assert metrics["finish_reason"] == "length"
