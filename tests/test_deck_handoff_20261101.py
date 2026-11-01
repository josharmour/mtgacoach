"""Live 17:03 retry stole the lane after cancellation; no first plan through T8."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.coach import CoachEngine, _playbook_call_options
from arenamcp.game_plan import StrategicLane


def test_cancellation_reserves_lane_before_retry_can_overtake(monkeypatch):
    lane = StrategicLane()
    monkeypatch.setattr("arenamcp.coach.STRATEGIC_LANE", lane)
    coach = CoachEngine(backend=Mock())
    coach.begin_deck_analysis("same")
    coach._game_plan_mgr = SimpleNamespace(_plan=None)
    coach._deck_strategy_pending_since = 0
    cancel = threading.Event()
    coach._playbook_cancels["same"] = cancel
    owner = lane.try_acquire("deck_playbook")
    assert not coach._deck_analysis_blocks_plan() and cancel.is_set()
    assert lane.try_acquire("game_plan") is None  # owner must actually release
    lane.release(owner)
    coach.begin_deck_analysis("same")
    assert lane.try_acquire("deck_playbook") is None  # exact failing race
    plan = lane.try_acquire("game_plan")
    assert plan is not None
    lane.release(plan)
    retry = lane.try_acquire("deck_playbook")
    assert retry is not None
    lane.release(retry)


def test_handoff_expires_if_no_eligible_plan_window(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: clock[0])
    lane = StrategicLane()
    lane.reserve_next("game_plan", 30)
    assert lane.acquire("deck_playbook", 0) is None
    clock[0] += 31
    token = lane.acquire("deck_playbook", 0)
    assert token is not None
    lane.release(token)


@pytest.mark.parametrize("model", ["current", "deepseek-v4.1-flash-local"])
def test_hosted_discovery_and_compile_do_not_burn_budget_in_hidden_reasoning(model):
    backend = SimpleNamespace(model=model)
    discovery = _playbook_call_options(backend, True)
    compile = _playbook_call_options(backend, False)
    assert discovery == dict(
        enable_thinking=False, reasoning_effort="none", max_tokens=1536, request_timeout_s=60.0
    )
    assert compile["enable_thinking"] is False and compile["reasoning_effort"] == "none"
    assert compile["max_tokens"] == 12288  # full audited schema retained
    assert _playbook_call_options(SimpleNamespace(model="glm-5.3-flash"), True)["reasoning_effort"] == "high"


def test_lane_timeout_does_not_start_unadmitted_model_call(monkeypatch):
    from tests.deck_fakes import deck_case

    state, catalog, _ = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    lane = StrategicLane()
    owner = lane.try_acquire("game_plan")
    monkeypatch.setattr("arenamcp.coach.STRATEGIC_LANE", lane)
    coach = CoachEngine(backend=Mock())
    coach._PLAYBOOK_LANE_WAIT_S = 0
    backend = Mock()
    with pytest.raises(ValueError, match="deferred"):
        coach._build_playbook(state, backend, "deck", 1, wanted=lambda: True)
    backend.complete.assert_not_called()
    lane.release(owner)


@pytest.mark.parametrize("kind", ["replay", "preserve", "spells"])
def test_hosted_two_pass_pipeline_still_publishes_only_audited_playbook(monkeypatch, tmp_path, kind):
    import json

    from tests.deck_fakes import deck_case

    state, catalog, response = deck_case(kind)
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    backend = Mock(model="current")
    backend.complete.side_effect = [
        "Concise mechanisms grounded in the supplied card rules.",
        json.dumps(response),
    ]
    coach = CoachEngine(backend=backend)
    monkeypatch.setattr(coach, "_playbook_cache_path", lambda identity: tmp_path / (identity + ".json"))
    playbook = coach.prepare_playbook(state, backend=backend)
    assert playbook.data["mechanisms"] == response["mechanisms"]
    calls = backend.complete.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["max_tokens"] == 1536 and calls[0].kwargs["request_timeout_s"] == 60
    assert calls[1].kwargs["max_tokens"] == 12288 and "response_format" in calls[1].kwargs
    assert all(c.kwargs["reasoning_effort"] == "none" for c in calls)
    assert "Concise mechanisms grounded" in calls[1].args[1]


def test_retry_backoff_starts_after_failure_not_before_long_request(monkeypatch, tmp_path):
    from tests.deck_fakes import deck_case

    from arenamcp.standalone_deck import _DeckAnalysisMixin

    state, catalog, _ = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    clock = [100.0]
    monkeypatch.setattr("arenamcp.standalone_deck.time.monotonic", lambda: clock[0])
    workers = []
    monkeypatch.setattr(
        "arenamcp.standalone_deck.threading.Thread",
        lambda **kw: SimpleNamespace(start=lambda: workers.append(kw["target"])),
    )
    backend = Mock(model="current")

    def timed_out(*a, **kw):
        clock[0] += 120
        return "[BACKEND ERROR] LLM streaming time budget exhausted"

    backend.complete.side_effect = timed_out
    monkeypatch.setattr("arenamcp.coach.create_backend", lambda *a, **kw: backend)
    runtime = _DeckAnalysisMixin()
    runtime._coach = CoachEngine(backend=backend)
    monkeypatch.setattr(
        runtime._coach, "_playbook_cache_path", lambda identity: tmp_path / (identity + ".json")
    )
    runtime._auto_deck_strategy = True
    runtime._backend_name = "online"
    runtime.model_name = "current"
    runtime.ui = Mock()
    runtime.speak_advice = Mock()
    assert runtime._maybe_analyze_deck(state)
    workers.pop()()
    assert runtime._last_deck_analysis_attempt == 220
    assert not runtime._maybe_analyze_deck(state)
    assert backend.complete.call_count == 1
    clock[0] += 31
    assert runtime._maybe_analyze_deck(state)


def test_hosted_brief_keeps_rules_and_conditional_tradeoffs_without_exhaustive_essay():
    from arenamcp.coach import _playbook_discovery_prompt
    from arenamcp.deck_strategy import DECK_DISCOVERY_PROMPT

    prompt = _playbook_discovery_prompt(SimpleNamespace(model="current"))
    assert "at most 600 tokens" in prompt
    assert "every possible pairing" in prompt
    assert "COMMANDER ACCESS RULE" in prompt
    assert "RESOURCE CONSERVATION EXAMPLE" in prompt
    assert "surviving resources" in prompt
    assert "Keep under 4000 tokens" not in prompt
    assert _playbook_discovery_prompt(SimpleNamespace(model="glm-5.3-flash")) == DECK_DISCOVERY_PROMPT
