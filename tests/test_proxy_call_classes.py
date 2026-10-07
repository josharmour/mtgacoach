"""Call-class labels, metrics fields, GLM effort normalisation and the background lane.

2026-10-06 load study: 933 "[PROXY] Request metrics" lines had to be matched
to call sites through neighbouring log lines, and about 48% of model-slot
time went to background calls (game plan, deck playbook, win-in-N) that
overlapped decisions 57% of the time. Every request now carries a call_class
label, and background calls run one at a time across all backends.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest

from arenamcp.backend_health import BackendHealth, is_backend_error_text
from arenamcp.backends import health, proxy
from arenamcp.backends.proxy import BackendCallCancelled, ProxyBackend


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


def chunk(text=None, *, reasoning=None, finish=None):
    has_choice = text is not None or reasoning is not None or finish is not None
    return NS(
        model="glm-5.3-flash",
        usage=None,
        choices=[NS(delta=NS(content=text, reasoning_content=reasoning), finish_reason=finish)]
        if has_choice
        else [],
    )


class FakeClient:
    """Scriptable OpenAI client: one behavior per create() call."""

    def __init__(self, behaviors=()):
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **params):
        with self.lock:
            self.calls.append(params)
            behavior = self.behaviors.pop(0) if self.behaviors else [chunk("ok", finish="stop")]
        if isinstance(behavior, Exception):
            raise behavior
        if isinstance(behavior, list):
            return iter(behavior)
        return behavior


class BlockingStream:
    """A stream that holds its slot until released, or fails once closed."""

    def __init__(self, text="done"):
        self.text = text
        self.release = threading.Event()
        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        self.started.set()
        deadline = time.monotonic() + 10
        while not self.release.is_set():
            if self.closed.is_set():
                raise RuntimeError("stream closed by client")
            if time.monotonic() > deadline:
                raise RuntimeError("test stream never released")
            time.sleep(0.005)
        yield chunk(self.text, finish="stop")

    def close(self):
        self.closed.set()


def make_backend(behaviors=(), model="glm-5.3-flash", url="http://test.invalid/v1") -> ProxyBackend:
    be = ProxyBackend(model=model, base_url=url)
    be._client = FakeClient(behaviors)
    return be


def ctk(params) -> dict:
    return params["extra_body"]["chat_template_kwargs"]


# ── labels and metrics ────────────────────────────────────────────────


def test_explicit_call_class_is_logged_in_request_metrics(caplog):
    be = make_backend()
    with caplog.at_level(logging.INFO, logger=proxy.__name__):
        assert be.complete("s", "u", call_class="decision.typed", request_timeout_s=12) == "ok"
    metrics = be.last_request_metrics
    assert metrics["call_class"] == "decision.typed"
    assert metrics["status"] == "ok"
    assert metrics["max_tokens"] == 4096
    assert metrics["budget_s"] == 12.0
    assert metrics["reasoning_effort"] == "low"
    assert metrics["priority"] is None
    assert "'call_class': 'decision.typed'" in caplog.text


def test_missing_call_class_is_labelled_with_the_calling_function():
    be = make_backend()
    be.complete("s", "u")
    label = be.last_request_metrics["call_class"]
    assert label.startswith("auto.")
    assert label.endswith(".test_missing_call_class_is_labelled_with_the_calling_function")


def _site_that_does_not_label(be):
    return be.complete("s", "u")


def test_auto_label_names_the_first_frame_outside_the_proxy():
    be = make_backend()
    _site_that_does_not_label(be)
    assert be.last_request_metrics["call_class"].endswith("._site_that_does_not_label")


def test_executor_submitted_call_without_label_is_auto_unknown():
    be = make_backend()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(be.complete, "s", "u").result(timeout=5)
    assert be.last_request_metrics["call_class"] == "auto.unknown"


def test_blank_call_class_falls_back_to_auto_label():
    be = make_backend()
    be.complete("s", "u", call_class="   ")
    assert be.last_request_metrics["call_class"].startswith("auto.")


def test_thinking_without_effort_is_reported_as_template_default():
    be = make_backend()
    be.complete("s", "u", enable_thinking=True, call_class="coach.deck_brief")
    assert be.last_request_metrics["reasoning_effort"] == "default"
    # chat_template_kwargs semantics are unchanged: no effort is invented.
    assert ctk(be._client.calls[0]) == {"thinking": True}


def test_documented_classes_cover_every_call_site_family():
    for name in (
        "decision.typed",
        "decision.plan",
        "decision.block_repair",
        "decision.pay",
        "coach.advice",
        "coach.deck_brief",
        "background.game_plan",
        "background.deck_playbook",
        "background.win_plan",
        "background.set_primer",
        "background.postmatch",
        "vision",
        "probe",
    ):
        assert name in proxy.CALL_CLASSES


def test_vision_calls_now_record_metrics_without_counting_the_image():
    be = make_backend(
        [NS(model="glm-5.3-flash", usage=None, choices=[NS(message=NS(content="{}"), finish_reason="stop")])]
    )
    assert be.complete_with_image("system", "user", b"\x89PNG" * 1000, call_class="vision") == "{}"
    metrics = be.last_request_metrics
    assert metrics["call_class"] == "vision"
    assert metrics["status"] == "ok"
    assert metrics["streamed"] is False
    assert metrics["input_chars"] == len("system") + len("user")
    assert metrics["max_tokens"] == 600


def test_vision_failure_records_error_metrics_and_never_counts_toward_the_breaker():
    class APIConnectionError(Exception):
        pass

    be = make_backend([APIConnectionError("tunnel reset") for _ in range(6)])
    for _ in range(3):
        out = be.complete_with_image("s", "u", b"img")
        assert is_backend_error_text(out)
    assert be.last_request_metrics["status"] == "error"
    assert be.circuit_snapshot()["consecutive_failures"] == 0


# ── GLM effort normalisation ──────────────────────────────────────────


def test_medium_effort_is_sent_as_high_on_glm():
    """GLM-5.3 has low/high/max only; 'medium' silently runs as max."""
    be = make_backend()
    be.complete("s", "u", reasoning_effort="medium")
    assert ctk(be._client.calls[0]) == {"thinking": True, "reasoning_effort": "high"}
    assert be.last_request_metrics["reasoning_effort"] == "high"


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_supported_glm_efforts_pass_through(effort):
    be = make_backend()
    be.complete("s", "u", reasoning_effort=effort)
    assert ctk(be._client.calls[0])["reasoning_effort"] == effort


def test_medium_effort_untouched_for_non_glm_models():
    be = make_backend(model="qwen-local")
    be.complete("s", "u", reasoning_effort="medium")
    assert ctk(be._client.calls[0])["reasoning_effort"] == "medium"


def test_reasoning_effort_docstring_no_longer_advertises_medium():
    doc = " ".join(ProxyBackend.complete.__doc__.split())
    assert 'GLM effort for this request: "low", "high" or "max"' in doc
    assert '("low", "medium", "high")' not in doc


# ── background lane ───────────────────────────────────────────────────


def _start(fn, *args, **kwargs):
    result: dict = {}

    def run():
        try:
            result["value"] = fn(*args, **kwargs)
        except Exception as e:  # surfaced to the test through result
            result["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def test_background_lane_is_shared_across_instances_and_drops_when_asked():
    stream = BlockingStream("plan A")
    a = make_backend([stream])
    b = make_backend()
    ta, ra = _start(a.complete, "s", "u", call_class="background.game_plan", request_timeout_s=10)
    assert stream.started.wait(5)

    out = b.complete("s", "u", call_class="background.deck_playbook", lane_wait_s=0)
    assert proxy.is_call_cancelled(out)
    assert out == "[BACKEND ERROR] request cancelled (dropped)"
    assert b._client.calls == []
    assert b.last_request_metrics["status"] == "dropped"

    stream.release.set()
    ta.join(5)
    assert ra["value"] == "plan A"
    snap = BackendHealth.instance().snapshot()
    assert snap["total_failures"] == 0
    assert a.circuit_snapshot()["consecutive_failures"] == 0


def test_background_call_of_another_class_waits_for_the_lane():
    stream = BlockingStream("plan A")
    a = make_backend([stream])
    b = make_backend([[chunk("deck B", finish="stop")]])
    ta, ra = _start(a.complete, "s", "u", call_class="background.game_plan", request_timeout_s=10)
    assert stream.started.wait(5)
    tb, rb = _start(
        b.complete, "s", "u", call_class="background.deck_playbook", request_timeout_s=10, lane_wait_s=5
    )
    time.sleep(0.3)
    assert b._client.calls == [], "B must wait while A holds the lane"
    stream.release.set()
    ta.join(5)
    tb.join(5)
    assert ra["value"] == "plan A"
    assert rb["value"] == "deck B"


def test_newer_call_of_the_same_class_supersedes_and_closes_the_old_stream():
    stream = BlockingStream("stale plan")
    a = make_backend([stream])
    b = make_backend([[chunk("fresh plan", finish="stop")]])
    ta, ra = _start(
        a.complete, "s", "u", call_class="background.game_plan", request_timeout_s=10, raise_on_error=True
    )
    assert stream.started.wait(5)

    assert b.complete("s", "u", call_class="background.game_plan", request_timeout_s=10) == "fresh plan"
    ta.join(5)
    assert stream.closed.is_set(), "the superseded stream must be closed so the server can abort it"
    err = ra["error"]
    assert isinstance(err, BackendCallCancelled)
    assert err.reason == "superseded"
    assert proxy.is_call_cancelled(err)
    assert a.last_request_metrics["status"] == "cancelled"
    # A cancellation is not a backend failure.
    assert BackendHealth.instance().snapshot()["total_failures"] == 0
    assert a.circuit_snapshot()["consecutive_failures"] == 0


def test_background_true_without_label_still_uses_the_lane():
    stream = BlockingStream()
    a = make_backend([stream])
    b = make_backend()
    ta, _ = _start(a.complete, "s", "u", call_class="background.win_plan", request_timeout_s=10)
    assert stream.started.wait(5)
    out = b.complete("s", "u", background=True, lane_wait_s=0)
    assert proxy.is_call_cancelled(out)
    stream.release.set()
    ta.join(5)


def test_foreground_calls_never_wait_for_the_background_lane():
    stream = BlockingStream()
    a = make_backend([stream])
    b = make_backend()
    ta, _ = _start(a.complete, "s", "u", call_class="background.game_plan", request_timeout_s=10)
    assert stream.started.wait(5)
    assert b.complete("s", "u", call_class="decision.typed", request_timeout_s=12) == "ok"
    stream.release.set()
    ta.join(5)


def test_lane_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ARENAMCP_LLM_BACKGROUND_LANE", "0")
    stream = BlockingStream()
    a = make_backend([stream])
    b = make_backend()
    ta, _ = _start(a.complete, "s", "u", call_class="background.game_plan", request_timeout_s=10)
    assert stream.started.wait(5)
    assert b.complete("s", "u", call_class="background.deck_playbook", lane_wait_s=0) == "ok"
    stream.release.set()
    ta.join(5)


def test_cancel_event_abandons_the_call_between_chunks():
    cancel = threading.Event()

    def stream():
        yield chunk(reasoning="thinking")
        cancel.set()
        yield chunk(reasoning="more thinking")
        yield chunk("never returned", finish="stop")

    be = make_backend([stream()])
    out = be.complete("s", "u", call_class="background.game_plan", cancel_event=cancel)
    assert out == "[BACKEND ERROR] request cancelled (cancelled)"
    assert be.last_request_metrics["status"] == "cancelled"
    assert be.last_request_metrics["ttft_ms"] is not None
    assert BackendHealth.instance().snapshot()["total_failures"] == 0


def test_cancel_event_set_before_the_call_sends_nothing():
    cancel = threading.Event()
    cancel.set()
    be = make_backend()
    with pytest.raises(BackendCallCancelled):
        be.complete("s", "u", cancel_event=cancel, raise_on_error=True)
    assert be._client.calls == []
