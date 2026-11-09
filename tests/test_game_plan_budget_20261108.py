"""Mac: hidden reasoning exhausted compact caps; queueing exhausted retry time."""

import time

import pytest
from tests.proxy_fakes import chunk, make_backend
from tests.test_game_plan import FakeBackend, _plan_json, _state

from arenamcp.game_plan import LIVE_PLAN_PROMPT, GamePlanManager


@pytest.mark.parametrize("answer", ['{"role":"aggressor","path":"attack"}', ""])
def test_truncated_game_plan_is_not_accepted_or_salvaged(answer):
    backend = make_backend([[chunk(answer, reasoning=_plan_json(), finish="length")]], model="current")
    result = backend.complete("JSON", "plan", 2048, background=True, call_class="background.game_plan")
    assert result == ""


def test_completed_game_plan_with_reasoning_is_still_accepted():
    answer = _plan_json()
    backend = make_backend([[chunk(answer, reasoning="think", finish="stop")]], model="current")
    assert (
        backend.complete("JSON", "plan", 2048, background=True, call_class="background.game_plan") == answer
    )


def test_compact_plan_keeps_answer_capacity_and_short_queue_wait():
    class Capture(FakeBackend):
        model = "current"

        def complete(self, system, user, max_tokens, **kwargs):
            assert max_tokens == 2048
            assert kwargs["lane_wait_s"] == 0.25
            assert kwargs["reasoning_effort"] == "none"
            assert kwargs["request_timeout_s"] <= 20
            return super().complete(system, user, max_tokens, **kwargs)

    manager = GamePlanManager(Capture([_plan_json()]))
    assert manager.maybe_reform(_state(turn=4)) is not None


def test_too_little_remaining_time_does_not_start_json_retry():
    backend = FakeBackend([_plan_json()])
    manager = GamePlanManager(backend)
    assert manager._retry_for_json("plan", 4, cancel=None, deadline=time.monotonic() + 1.6) is None
    assert backend.requests == []


def test_busy_background_lane_is_deferred_without_calling_model():
    from arenamcp.backends import proxy

    backend = make_backend(model="current")
    manager = GamePlanManager(backend)
    original = proxy._BACKGROUND_LANE.acquire
    seen = []

    def busy(handle, wait_s):
        seen.append(wait_s)
        return "dropped"

    proxy._BACKGROUND_LANE.acquire = busy
    try:
        result = manager._complete(LIVE_PLAN_PROMPT, "plan", effort="none", deadline=time.monotonic() + 20)
    finally:
        proxy._BACKGROUND_LANE.acquire = original
    assert seen == [0.25]
    assert backend._client.calls == []
    assert "dropped" in result


@pytest.mark.parametrize("finish, expected", [("length", ""), ("stop", "valid answer")])
def test_nonstream_game_plan_obeys_finish_reason(finish, expected):
    from types import SimpleNamespace as NS

    response = NS(
        model="current",
        usage=None,
        choices=[NS(message=NS(content="valid answer", reasoning_content="think"), finish_reason=finish)],
    )
    backend = make_backend([ValueError("stream unsupported"), response], model="current")
    assert (
        backend.complete("JSON", "plan", 2048, background=True, call_class="background.game_plan") == expected
    )
