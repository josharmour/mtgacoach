"""Follow-ups to the 2026-10-06 outage/load work (commits 72f7d2d, 50145f1).

- Win-in-N runs as background work at low effort, never at full thinking and
  priority 0 (31 automatic win-in-N calls in 1.8 h, none ever read).
- Every remaining model call site carries a proxy call class.
- Native-Mac autoplay plays the board-math line when the planner's model is
  down, and an outage shows "model offline" instead of pausing autoplay.
- Breaker / priority / background-lane state never leaks between tests.
- The deck playbook is cleared only when the deck changes.
"""

from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from PIL import Image

import arenamcp.standalone_hotkeys as hotkeys_module
from arenamcp.action_planner import FALLBACK_LLM_UNAVAILABLE, ActionPlan, ActionType, GameAction
from arenamcp.autopilot_models import AutopilotConfig, AutopilotState
from arenamcp.backends import health, proxy
from arenamcp.backends.proxy import ProxyBackend
from arenamcp.coach import CoachEngine
from arenamcp.native_mac_autopilot import MODEL_OFFLINE_NOTICE, NativeMacAutopilot
from arenamcp.native_mac_input import DesktopFrame, GameWindow
from arenamcp.standalone import StandaloneCoach

SKIPPED = "[BACKEND ERROR] model server unavailable (backend down; background call skipped)"
CIRCUIT_OPEN = "[BACKEND ERROR] model server unavailable (circuit open; retry in 5s)"


class Recorder:
    """A backend whose complete() takes every keyword and records them."""

    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, system, user, *args, **kwargs):
        self.calls.append(kwargs)
        return self.reply(system, user) if callable(self.reply) else self.reply


class PlainBackend:
    """An older backend: no call_class / reasoning_effort keywords."""

    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls = 0

    def complete(self, system_prompt, user_message, max_tokens=1000, temperature=0.3, request_timeout_s=None):
        self.calls += 1
        return self.reply


def _coach(backend) -> CoachEngine:
    coach = CoachEngine(backend=backend)
    coach._build_context = lambda state: "Current board: our turn."
    coach._ensure_game_plan_mgr = lambda: None
    return coach


# ---------------------------------------------------------------------------
# 1. Win-in-N: background class, low effort, background lane
# ---------------------------------------------------------------------------


def _chunk(text=None, finish=None):
    return NS(
        model="glm-5.3-flash",
        usage=None,
        choices=[NS(delta=NS(content=text, reasoning_content=None), finish_reason=finish)],
    )


class FakeOpenAI:
    """Just enough of the OpenAI client for one streamed ProxyBackend call."""

    def __init__(self, text="VIABLE: NO"):
        self.text = text
        self.calls: list[dict] = []
        self.chat = NS(completions=NS(create=self._create))
        self.models = NS(list=lambda: NS(data=[]))

    def with_options(self, **options):
        return self

    def _create(self, **params):
        self.calls.append(params)
        return iter([_chunk(self.text, finish="stop")])


class RecordingLane(proxy._BackgroundLane):
    def __init__(self):
        super().__init__()
        self.acquired: list[str] = []

    def acquire(self, handle, wait_s):
        self.acquired.append(handle.call_class)
        return super().acquire(handle, wait_s)


@pytest.fixture
def glm_proxy(monkeypatch):
    for var in ("ARENAMCP_LLM_PRIORITY", "ARENAMCP_LLM_CIRCUIT", "ARENAMCP_LLM_BACKGROUND_LANE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ProxyBackend, "_local_warmup", lambda self: None)
    lane = RecordingLane()
    monkeypatch.setattr(proxy, "_BACKGROUND_LANE", lane)
    backend = ProxyBackend(model="glm-5.3-flash", base_url="http://winplan.invalid/v1")
    backend._client = FakeOpenAI()
    return backend, lane


def test_automatic_win_plan_is_low_effort_background_work_in_the_lane(glm_proxy, caplog):
    backend, lane = glm_proxy
    backend.enable_thinking = True  # the worker's thinking backend
    coach = _coach(Mock())
    with caplog.at_level("INFO", logger="arenamcp.backends.proxy"):
        plan = coach.get_win_plan({"turn": {"turn_number": 5}}, 2, backend=backend)
    assert plan == "VIABLE: NO"
    sent = backend._client.calls[0]
    assert sent["extra_body"]["priority"] == proxy.BACKGROUND_PRIORITY
    assert sent["extra_body"]["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "low"}
    assert lane.acquired == ["background.win_plan"]
    metrics = [r.getMessage() for r in caplog.records if "Request metrics" in r.getMessage()]
    assert metrics and "background.win_plan" in metrics[-1]


def test_win_plan_is_skipped_not_sent_while_the_server_is_down(glm_proxy):
    backend, _ = glm_proxy
    breaker = health.get_circuit("http://winplan.invalid/v1", "glm-5.3-flash")
    for _ in range(5):
        breaker.record_failure("Connection error")
    coach = _coach(Mock())
    plan = coach.get_win_plan({}, 2, backend=backend)
    assert backend._client.calls == []
    assert plan.startswith("[BACKEND ERROR]")  # sentinel returned untouched


def test_win_plan_labels_reach_keyword_backends_and_skip_older_ones():
    labelled = Recorder("VIABLE: NO")
    _coach(labelled).get_win_plan({}, 3)
    assert labelled.calls[0]["call_class"] == "background.win_plan"
    assert labelled.calls[0]["reasoning_effort"] == "low"
    plain = PlainBackend("VIABLE: NO")
    assert _coach(plain).get_win_plan({}, 3) == "VIABLE: NO"
    assert plain.calls == 1


def test_win_plan_error_sentinel_is_not_narrated_into_a_plan():
    coach = _coach(Recorder(SKIPPED))
    coach.narration_mode = "autopilot"
    assert coach.get_win_plan({}, 2) == SKIPPED


def _worker_coach(monkeypatch, replies):
    monkeypatch.setattr("arenamcp.coach.create_backend", lambda *a, **k: NS(enable_thinking=False))
    monkeypatch.setattr(hotkeys_module.time, "sleep", lambda s: None)
    asked = []

    class _Coach:
        def get_win_plan(self, game_state, turns, library_summary, backend=None):
            asked.append(turns)
            return replies.pop(0)

    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._thinking_model = "glm-5.3-flash"
    coach._backend_name = "online"
    coach._coach = _Coach()
    coach._compute_library_summary = lambda state: ""
    return coach, asked


def test_worker_stops_after_a_skipped_call_instead_of_asking_again(monkeypatch):
    coach, asked = _worker_coach(monkeypatch, [SKIPPED, "VIABLE: NO"])
    coach._win_plan_worker({"turn": {"turn_number": 5}})
    assert asked == [2]


def test_worker_still_tries_three_turns_after_a_non_viable_two(monkeypatch):
    coach, asked = _worker_coach(monkeypatch, ["VIABLE: NO", "VIABLE: NO"])
    coach._win_plan_worker({"turn": {"turn_number": 5}})
    assert asked == [2, 3]


class _SyncThread:
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


def test_manual_win_plan_is_a_labelled_foreground_call(monkeypatch):
    monkeypatch.setattr(hotkeys_module, "threading", NS(Thread=_SyncThread))
    seen = {}

    class _Coach:
        def get_win_plan(self, game_state, turns, library_summary, **kwargs):
            seen.update(kwargs)
            return CIRCUIT_OPEN

    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._coach = _Coach()
    coach._mcp = Mock()
    coach._mcp.get_game_state.return_value = {"turn": {"turn_number": 4}}
    coach._compute_library_summary = lambda state: ""
    coach.ui = Mock()
    coach.speak_advice = Mock()
    coach._record_advice = Mock()
    coach._on_win_plan_hotkey(3)
    assert seen == {"call_class": "coach.win_plan"}
    # An error sentinel is never shown or spoken as a plan.
    coach.ui.advice.assert_not_called()
    coach.speak_advice.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Call-class labels on the remaining call sites
# ---------------------------------------------------------------------------


def test_coach_analysis_calls_are_labelled():
    backend = Recorder("WIN: 40%\nEven board.")
    coach = _coach(backend)
    coach.generate_win_probability({})
    coach.generate_post_match_analysis([], "loss", 12)
    coach.recommend_sideboard(["Mountain"], ["Shock"], ["Sol Ring"])
    assert [call.get("call_class") for call in backend.calls] == [
        "coach.win_prob",
        "coach.postmatch",
        "coach.sideboard",
    ]


def test_coach_analysis_still_works_on_backends_without_labels():
    backend = PlainBackend("WIN: 40%\nEven board.")
    coach = _coach(backend)
    assert coach.generate_win_probability({}).startswith("WIN: 40%")
    assert coach.recommend_sideboard(["Mountain"], ["Shock"], ["Sol Ring"])
    assert backend.calls == 2


def _postmatch_runtime(backend):
    from arenamcp.standalone_postmatch import _PostMatchMixin

    runtime = _PostMatchMixin()
    runtime._coach = NS(_backend=backend)
    return runtime


def test_automatic_postmatch_score_and_rating_are_background_calls():
    backend = Recorder('{"rating": 7, "reason": "Sound."}')
    runtime = _postmatch_runtime(backend)
    history = [{"game_snapshot": {"turn_number": 3, "phase": "Main1"}, "advice": "Cast Shock."}]
    runtime._score_match_advice("m-1", "win", history)
    assert runtime._rate_match_with_llm("win", "Good game.") == 7
    assert [call["call_class"] for call in backend.calls] == ["background.postmatch"] * 2


@pytest.mark.parametrize(("reason", "priority"), [("match_end/auto", 10), ("manual", None)])
def test_the_automatic_post_match_analysis_runs_at_background_priority(monkeypatch, reason, priority):
    # Review 2026-10-07: the automatic 4096-token analysis ran at priority 0 next to the
    # next match's mulligan and first decisions. It stays 'coach.postmatch' (the lane
    # would drop it after 30 s or let the advice score supersede it); manual is unchanged.
    seen = []
    runtime = _postmatch_runtime(Recorder())
    runtime._coach = NS(
        _backend=Recorder(),
        generate_post_match_analysis=lambda **kwargs: seen.append(kwargs) or "",
    )
    runtime._post_match_analysis_running = False
    runtime._staged_analyses = {
        "m-1": {
            "advice_history": [{"game_snapshot": {"turn_number": 3}, "advice": "Cast Shock."}],
            "result": "loss",
            "final_state": {},
            "replay_path": "",
            "missed_decisions": [],
        }
    }
    runtime.ui = Mock()
    runtime._run_match_review = Mock()
    runtime._extract_replay_context = Mock(return_value="")
    done = threading.Event()
    original = runtime._post_match_analysis_worker

    def worker(*args, **kwargs):
        try:
            original(*args, **kwargs)
        finally:
            done.set()

    runtime._post_match_analysis_worker = worker
    assert runtime._start_post_match_analysis_worker(reason=reason)
    assert done.wait(5)
    (kwargs,) = seen
    assert kwargs.get("priority") == priority
    assert "call_class" not in kwargs  # the foreground 'coach.postmatch' default
    # The coach passes it on to the backend.
    backend = Recorder("Analysis.")
    _coach(backend).generate_post_match_analysis([], "loss", 12, priority=priority)
    assert (
        backend.calls[-1].get("priority") == priority and backend.calls[-1]["call_class"] == "coach.postmatch"
    )


def test_postmatch_rating_survives_a_backend_without_labels():
    runtime = _postmatch_runtime(PlainBackend('{"rating": 6}'))
    assert runtime._rate_match_with_llm("loss", "Close game.") == 6


def test_draft_pick_and_build_calls_are_labelled():
    from arenamcp.draft_advisor import DraftAdvisor

    backend = Mock()
    backend.complete.return_value = "not json"
    advisor = DraftAdvisor(backend)
    card = {
        "grp_id": 1,
        "name": "Seer",
        "mana_cost": "{1}{U}",
        "oracle_text": "Scry 2.",
        "type_line": "Creature",
    }
    advisor.recommend({"event_name": "X", "cards": [card], "picked_cards": []}, {"evaluations": []})
    assert backend.complete.call_args.kwargs["call_class"] == "draft.pick"
    fallback = {"pool_cards": [card] * 3}
    assert advisor.recommend_deck(fallback) == fallback
    assert backend.complete.call_args.kwargs["call_class"] == "draft.build"
    # JSON mode and the budget are kept alongside the label.
    assert backend.complete.call_args.kwargs["response_format"] == {"type": "json_object"}
    assert backend.complete.call_args.kwargs["request_timeout_s"] == advisor._timeout


def _small_primer():
    from arenamcp.set_primer import data_primer

    def rating(grp_id, name, color, gih):
        return {
            "mtga_id": grp_id,
            "name": name,
            "color": color,
            "rarity": "common",
            "types": ["Creature"],
            "ever_drawn_win_rate": gih,
            "drawn_improvement_win_rate": gih - 0.55,
            "avg_seen": 5.0,
            "avg_pick": 4.0,
            "opening_hand_win_rate": gih,
            "ever_drawn_game_count": 1000,
        }

    ratings = [rating(i, f"Card {i}", "WUBRG"[i % 5], 0.50 + (i % 7) * 0.01) for i in range(25)]
    return data_primer("TST", ratings, {"UR": NS(win_rate=0.58, games=9000)})


class ConcurrencyBackend:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.classes: list[str | None] = []

    def complete(self, system, message, max_tokens, **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.classes.append(kwargs.get("call_class"))
        time.sleep(0.01)
        with self.lock:
            self.active -= 1
        return "not json"


def test_set_primer_calls_are_background_and_one_at_a_time_in_the_lane(monkeypatch):
    from arenamcp.set_primer import synthesize

    monkeypatch.delenv("ARENAMCP_LLM_BACKGROUND_LANE", raising=False)
    backend = ConcurrencyBackend()
    synthesize(_small_primer(), backend)
    assert backend.classes and set(backend.classes) == {"background.set_primer"}
    # Same-class calls supersede each other in the proxy's lane: never parallel.
    assert backend.peak == 1


def test_set_primer_stays_parallel_without_the_lane(monkeypatch):
    from arenamcp import set_primer

    labelled = ConcurrencyBackend()
    monkeypatch.setenv("ARENAMCP_LLM_BACKGROUND_LANE", "0")
    assert not set_primer._runs_in_background_lane(labelled)
    monkeypatch.delenv("ARENAMCP_LLM_BACKGROUND_LANE")
    assert set_primer._runs_in_background_lane(labelled)
    assert not set_primer._runs_in_background_lane(PlainBackend())


class SkippingBackend:
    """Every call skipped by the proxy (server down / lane dropped), or answered with ``reply``."""

    def __init__(self, reply=None):
        self.reply = reply
        self.calls = 0

    def complete(self, system, message, max_tokens, **kwargs):
        self.calls += 1
        return (
            self.reply or "[BACKEND ERROR] model server unavailable (backend down; background call skipped)"
        )


def _primer_service(tmp_path, backend):
    from arenamcp import set_primer

    primer = _small_primer()
    return set_primer.SetPrimerService(
        backend_fn=lambda: backend,
        ratings_fn=lambda code: [],
        color_stats_fn=lambda code: {},
        cache_dir=tmp_path,
    ), primer


@pytest.mark.parametrize(
    "sentinel",
    [
        "[BACKEND ERROR] model server unavailable (backend down; background call skipped)",
        "[BACKEND ERROR] request cancelled (dropped)",
    ],
)
def test_a_primer_built_while_the_model_is_skipped_is_not_persisted(monkeypatch, tmp_path, sentinel):
    # Review 2026-10-07: the 11 background primer calls were skipped while the breaker
    # reported background_blocked, and the data-only primer was written to disk and
    # served as complete for 7 days.
    from arenamcp import set_primer

    backend = SkippingBackend(sentinel)
    service, primer = _primer_service(tmp_path, backend)
    monkeypatch.setattr(set_primer, "data_primer", lambda *args, **kwargs: deepcopy(primer))
    report = {}
    set_primer.synthesize(deepcopy(primer), backend, workers=1, report=report)
    assert report == {"skipped": 11} and backend.calls == 11  # skipped calls are not retried
    service._build("TST")
    assert not (tmp_path / "TST.json").exists()
    assert service.get("TST") is not None  # this session still has the data primer
    started = []
    monkeypatch.setattr(service, "_build", lambda key: started.append(key))
    service.ensure("TST")
    assert started == []  # not before PARTIAL_RETRY_S
    service._partial["TST"] = time.monotonic() - 1
    service.ensure("TST")
    for _ in range(100):
        if started:
            break
        time.sleep(0.01)
    assert started == ["TST"]


def test_a_primer_whose_answers_were_invalid_is_still_persisted(monkeypatch, tmp_path):
    from arenamcp import set_primer

    backend = SkippingBackend("not json")
    service, primer = _primer_service(tmp_path, backend)
    monkeypatch.setattr(set_primer, "data_primer", lambda *args, **kwargs: deepcopy(primer))
    service._build("TST")
    assert (tmp_path / "TST.json").exists() and "TST" not in service._partial


# ---------------------------------------------------------------------------
# 3. Native-Mac autoplay: board-math play and "model offline" during an outage
# ---------------------------------------------------------------------------


def _frame():
    return DesktopFrame(
        GameWindow(42, 123, (100, 50, 720, 450)), Image.new("RGB", (1440, 900), "green"), time.monotonic()
    )


def _mac_state(legal, **extra):
    return {
        "match_id": "m",
        "legal_actions": list(legal),
        "turn": {"turn_number": 3, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        "local_seat_id": 1,
        "stack": [],
        "battlefield": [],
        "hand": [
            {"name": "Plains", "type_line": "Basic Land — Plains", "instance_id": 10, "grp_id": 100},
            {
                "name": "Optimistic Scavenger",
                "type_line": "Creature — Human Scout",
                "mana_cost": "{W}",
                "instance_id": 11,
                "grp_id": 101,
            },
        ],
        **extra,
    }


def _mac_engine(state, plan, *, vision_reply=None, vision_backend=None):
    controller = Mock()
    controller.capture.side_effect = lambda: _frame()
    controller.execute.return_value = True
    backend = vision_backend or Mock()
    backend.complete_with_image.return_value = vision_reply or json.dumps(
        {"kind": "double_click", "point": [0.4, 0.8], "reason": "Play Plains", "confidence": 0.95}
    )
    planner = Mock()
    planner.plan_actions.return_value = plan
    advice = []
    engine = NativeMacAutopilot(
        backend=backend,
        controller=controller,
        get_game_state=lambda: state,
        config=AutopilotConfig(),
        planner=planner,
        ui_advice_fn=lambda text, label: advice.append(text),
    )
    engine._ground_action = Mock(side_effect=lambda frame, action: action)
    return engine, controller, backend, planner, advice


def _unavailable_plan(detail: str = "llm_unavailable: circuit open"):
    return ActionPlan(actions=[], fallback_reason=FALLBACK_LLM_UNAVAILABLE, fallback_detail=detail)


def test_model_down_commits_the_board_math_land_drop():
    state = _mac_state(["Play Land: Plains", "Cast Optimistic Scavenger [OK]", "Pass"])
    engine, controller, backend, _, _ = _mac_engine(state, _unavailable_plan())
    assert engine.process_trigger(state, "desktop_poll")
    prompt = json.loads(backend.complete_with_image.call_args.args[1])
    assert "Plains" in prompt["committed_play"]
    assert "deck_reference" not in prompt  # a committed play, not an open-ended vision decision
    controller.execute.assert_called_once()


def test_model_down_board_math_pass_presses_space_without_vision():
    state = _mac_state(["Pass"], hand=[])
    engine, controller, backend, _, _ = _mac_engine(state, _unavailable_plan())
    assert engine.process_trigger(state, "desktop_poll")
    backend.complete_with_image.assert_not_called()
    action = controller.execute.call_args.args[1]
    assert action.kind == "key" and action.key == "space"


def test_model_down_with_nothing_to_commit_says_offline_and_asks_the_planner_again():
    state = _mac_state(["Target Grizzly Bears"], decision_context={"type": "select_targets"})
    engine, controller, backend, planner, advice = _mac_engine(state, _unavailable_plan())
    for _ in range(2):
        engine._next_poll = 0
        assert not engine.process_trigger(state, "desktop_poll")
    backend.complete_with_image.assert_not_called()
    controller.execute.assert_not_called()
    assert advice == [MODEL_OFFLINE_NOTICE]
    assert engine.state is not AutopilotState.PAUSED and not engine._paused_reason
    assert planner.plan_actions.call_count == 2  # not cached: retried until the model is back


def test_open_vision_breaker_skips_the_open_ended_vision_call():
    state = _mac_state(["Target Grizzly Bears"], decision_context={"type": "select_targets"})
    vision = Mock()
    vision.available.return_value = False
    engine, _, backend, _, advice = _mac_engine(state, ActionPlan(actions=[]), vision_backend=vision)
    assert not engine.process_trigger(state, "desktop_poll")
    backend.complete_with_image.assert_not_called()
    assert advice == [MODEL_OFFLINE_NOTICE]
    assert engine._vision_failures == 0


def test_vision_failures_during_an_outage_never_pause_autoplay():
    state = _mac_state(["Play Land: Plains", "Pass"])
    engine, controller, backend, _, advice = _mac_engine(
        state, _unavailable_plan(), vision_reply=CIRCUIT_OPEN
    )
    for _ in range(4):
        engine._next_poll = 0
        assert not engine.process_trigger(state, "desktop_poll")
    assert backend.complete_with_image.call_count == 4
    assert engine._vision_failures == 0
    assert not engine._paused_reason and engine.state is not AutopilotState.PAUSED
    assert advice[-1] == MODEL_OFFLINE_NOTICE
    controller.execute.assert_not_called()


def test_vision_failures_with_the_model_up_still_count():
    state = _mac_state(["Play Land: Plains", "Pass"])
    play = GameAction(ActionType.PLAY_LAND, card_name="Plains")
    engine, _, _, _, _ = _mac_engine(
        state, ActionPlan(actions=[play]), vision_reply="[BACKEND ERROR] vision analysis failed: 500"
    )
    assert not engine.process_trigger(state, "desktop_poll")
    assert engine._vision_failures == 1


def test_model_up_keeps_the_open_ended_vision_decision():
    state = _mac_state(["Target Grizzly Bears"], decision_context={"type": "select_targets"})
    engine, _, backend, _, _ = _mac_engine(state, ActionPlan(actions=[]))
    engine.process_trigger(state, "desktop_poll")
    backend.complete_with_image.assert_called_once()


@pytest.mark.parametrize(
    "detail", ["timeout", "llm_error: Request timed out.", "llm_error_sentinel: [BACKEND ERROR] HTTP 500"]
)
def test_a_slow_planner_is_not_an_offline_model(detail):
    # Review 2026-10-07: every FALLBACK_LLM_UNAVAILABLE read as 'model offline', so a
    # planner timeout in a targets window re-ran the planner (for its full timeout)
    # every 3 s and never let the healthy vision model decide.
    state = _mac_state(["Target Grizzly Bears"], decision_context={"type": "select_targets"})
    engine, _, backend, planner, advice = _mac_engine(state, _unavailable_plan(detail))
    planner._backend = Mock(available=Mock(return_value=True))
    for _ in range(2):
        engine._next_poll = 0
        engine.process_trigger(state, "desktop_poll")
    assert MODEL_OFFLINE_NOTICE not in advice
    assert backend.complete_with_image.call_count == 2  # vision decides, as before wave B
    assert planner.plan_actions.call_count == 1  # cached for the window


@pytest.mark.parametrize(
    ("detail", "breaker_open", "expected"),
    [
        ("llm_unavailable: circuit open", False, True),
        (
            "llm_error_sentinel: [BACKEND ERROR] model server unavailable (circuit open; retry in 9s)",
            False,
            True,
        ),
        ("llm_error: Connection error.", False, True),
        ("timeout", True, True),  # the breaker opened meanwhile
        ("timeout", False, False),
        ("llm_error: Request timed out.", False, False),
    ],
)
def test_model_unreachable_reads_the_failure(detail, breaker_open, expected):
    from arenamcp.action_planner import model_unreachable

    backend = Mock(available=Mock(return_value=not breaker_open))
    assert model_unreachable(_unavailable_plan(detail), backend) is expected
    assert model_unreachable(ActionPlan(actions=[], fallback_detail=detail), backend) is False


# ---------------------------------------------------------------------------
# 4. conftest: breaker / priority / lane state never leaks between tests
# ---------------------------------------------------------------------------

_LEAK_URL = "http://leak-check.invalid/v1"


def test_leak_check_part_1_trips_breaker_and_rejects_priority():
    breaker = health.get_circuit(_LEAK_URL, "glm-5.3-flash")
    for _ in range(5):
        breaker.record_failure("Connection error")
    assert not breaker.available()
    proxy._disable_priority(RuntimeError("priority rejected"))
    assert not proxy.priority_enabled()
    proxy._BACKGROUND_LANE._holder = proxy._CallHandle("background.leak")


def test_leak_check_part_2_starts_clean():
    assert health.get_circuit(_LEAK_URL, "glm-5.3-flash").available()
    assert proxy.priority_enabled()
    assert proxy._BACKGROUND_LANE.holder_class() is None


def test_the_trick_table_service_never_reads_or_writes_the_real_home(monkeypatch):
    # Review 2026-10-07: GamePlanManager.observe() starts TrickTableService.shared(); the
    # real singleton built FRA.json from the user's primer/17Lands caches and the card
    # database and wrote it to ~/.arenamcp/cache/trick_tables during a test run.
    from pathlib import Path

    from tests import strategic_states as S

    from arenamcp import opponent_tricks as ot
    from arenamcp.game_plan import GamePlanManager

    real = Path.home() / ".arenamcp"
    service = ot.TrickTableService.shared()
    assert real not in service._dir.parents and real not in Path(ot.TRICK_DIR).parents
    assert service._primer_fn("FRA") is None and service._ratings_fn("FRA") == []
    assert service._card_lookup(106272) is None  # never the MTGA card database
    built = []
    monkeypatch.setattr(service, "_load_or_build", lambda key: built.append(key))
    state = deepcopy(S.G1_T12)
    state["event_id"] = "PremierDraft_FRA_20260929"
    GamePlanManager(Recorder("{}")).observe(state)
    for _ in range(100):
        if built:
            break
        time.sleep(0.01)
    assert built == ["FRA"]  # the offline service, sandboxed, is the one asked


# ---------------------------------------------------------------------------
# 5. The deck playbook is cleared only when the deck changes
# ---------------------------------------------------------------------------


class _DeckCoach:
    def __init__(self, identity):
        self._deck_analysis_identity = identity
        self._deck_playbook = NS(identity=identity, data={"archetype": "Tempo", "spoken_summary": "Go wide."})
        self._deck_strategy = "playbook"
        self._deck_strategy_pending = False
        self.cleared = 0

    def clear_deck_strategy(self):
        self.cleared += 1
        self._deck_analysis_identity = ""
        self._deck_playbook = None


def _deck_runtime(monkeypatch, identity="deck-A"):
    from arenamcp.standalone_deck import _DeckAnalysisMixin

    monkeypatch.setattr("arenamcp.deck_strategy.deck_identity", lambda state: state["deck"])
    runtime = _DeckAnalysisMixin()
    runtime._coach = _DeckCoach(identity)
    runtime._auto_deck_strategy = True
    return runtime


def test_new_match_with_the_same_deck_keeps_the_playbook(monkeypatch):
    runtime = _deck_runtime(monkeypatch)
    assert not runtime._maybe_analyze_deck({"match_id": "m-1", "deck": "deck-A"})
    assert not runtime._maybe_analyze_deck({"match_id": "m-2", "deck": "deck-A"})
    assert runtime._coach.cleared == 0
    assert runtime._deck_analyzed


def test_a_different_deck_clears_the_playbook(monkeypatch):
    runtime = _deck_runtime(monkeypatch)
    runtime._maybe_analyze_deck({"match_id": "m-1", "deck": "deck-A"})
    runtime._maybe_analyze_deck({"match_id": "m-2", "deck": "deck-B"})
    assert runtime._coach.cleared == 1
    assert not runtime._deck_analyzed


def test_cached_republish_of_the_same_deck_is_announced_once(monkeypatch):
    from arenamcp.standalone_deck import _DeckAnalysisMixin

    workers = []
    monkeypatch.setattr(
        "arenamcp.standalone_deck.threading.Thread",
        lambda **kw: NS(start=lambda: workers.append(kw["target"])),
    )
    monkeypatch.setattr("arenamcp.deck_strategy.deck_identity", lambda state: state["deck"])
    monkeypatch.setattr("arenamcp.coach.create_backend", lambda *a, **k: None)

    class _Coach(_DeckCoach):
        def __init__(self):
            super().__init__("")
            self._deck_playbook = None
            self._deck_analysis_generation = 0
            self._deck_analysis_lock = threading.Lock()

        def begin_deck_analysis(self, identity):
            self._deck_analysis_identity = identity
            self._deck_analysis_generation += 1
            return self._deck_analysis_generation

        def analyze_deck(self, state, backend=None, analysis_generation=None):
            self._deck_playbook = NS(
                identity=state["deck"], data={"archetype": "Tempo", "spoken_summary": "Go."}
            )
            return "playbook"

    runtime = _DeckAnalysisMixin()
    runtime._coach = _Coach()
    runtime._auto_deck_strategy = True
    runtime._backend_name = "online"
    runtime.model_name = "m"
    runtime.ui = Mock()
    runtime.speak_advice = Mock()
    state = {"match_id": "m-1", "deck": "deck-A", "deck_cards": list(range(40))}
    for match_id in ("m-1", "m-2"):
        # standalone.py clears per-match strategy at every match boundary.
        runtime._coach.clear_deck_strategy()
        assert runtime._maybe_analyze_deck({**deepcopy(state), "match_id": match_id})
        workers.pop()()
    runtime.speak_advice.assert_called_once()
    assert runtime.ui.status.call_count == 2
