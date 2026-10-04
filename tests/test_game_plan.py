"""Tests for the persistent GamePlan strategic layer (game_plan.py)."""

import json
import threading

import pytest

from arenamcp.game_plan import GamePlan, GamePlanManager


class FakeBackend:
    """Records calls and returns a scripted response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.requests = []

    def complete(self, system, user, max_tokens, **kwargs):
        self.calls += 1
        self.requests.append((system, user, kwargs))
        if self._responses:
            return self._responses.pop(0)
        return self._last

    # keep a stable tail response
    @property
    def _last(self):
        return getattr(self, "_tail", "{}")


def _plan_json(path="race for lethal ~T6", win="aggro beatdown"):
    return json.dumps(
        {
            "win_conditions": [win, "grind with card advantage"],
            "path": path,
            "threat": "opponent flyers",
            "develop_next": "deploy a 2-drop",
        }
    )


def _state(turn=1, my_life=20, opp_life=20, my_creatures=0, opp_creatures=0, my_power=0):
    bf = []
    for _ in range(my_creatures):
        bf.append(
            {"type_line": "Creature", "controller_seat_id": 1, "power": my_power // max(my_creatures, 1)}
        )
    for _ in range(opp_creatures):
        bf.append({"type_line": "Creature", "controller_seat_id": 2, "power": 1})
    return {
        "players": [
            {"is_local": True, "seat_id": 1, "life_total": my_life, "hand_size": 5},
            {"is_local": False, "seat_id": 2, "life_total": opp_life, "hand_size": 5},
        ],
        "turn": {"turn_number": turn, "active_player": 1, "phase": "Main1"},
        "battlefield": bf,
    }


def test_parse_strict_json():
    plan = GamePlanManager._parse(_plan_json(), turn_num=3)
    assert plan is not None
    assert plan.win_conditions[0] == "aggro beatdown"
    assert len(plan.win_conditions) == 2  # capped at 2
    assert plan.path == "race for lethal ~T6"
    assert plan.turn_formed == 3
    assert not plan.is_empty()


def test_parse_markdown_fenced_json():
    fenced = "```json\n" + _plan_json() + "\n```"
    plan = GamePlanManager._parse(fenced, turn_num=1)
    assert plan is not None
    assert plan.path == "race for lethal ~T6"


def test_parse_garbage_returns_none():
    assert GamePlanManager._parse("Error: timed out", 1) is None
    assert GamePlanManager._parse("no json here", 1) is None
    assert GamePlanManager._parse("", 1) is None


def test_planner_block_and_intro_render():
    plan = GamePlan(
        win_conditions=["aggro beatdown"],
        path="race for lethal ~T6",
        threat="flyers",
        develop_next="2-drop",
        turn_formed=2,
    )
    block = plan.as_planner_block()
    assert "GAME PLAN" in block
    assert "race for lethal" in block
    assert "do NOT just react" in block.lower() or "develop toward" in block.lower()
    intro = plan.as_coach_intro()
    assert intro.startswith("Plan:")


def test_first_call_seeds_then_no_reform_same_turn():
    be = FakeBackend([_plan_json()])
    mgr = GamePlanManager(be)
    # First call on turn 1 forms the plan (one LLM call).
    p1 = mgr.maybe_reform(_state(turn=1))
    assert p1 is not None
    assert be.calls == 1
    # Second call same turn, identical board -> no new LLM call.
    p2 = mgr.maybe_reform(_state(turn=1))
    assert be.calls == 1
    assert p2 is p1


def test_static_board_new_turn_does_not_reform():
    be = FakeBackend([_plan_json(), _plan_json("plan B")])
    mgr = GamePlanManager(be)
    mgr.maybe_reform(_state(turn=1))
    assert be.calls == 1
    # Turn advances but nothing material changed -> still 1 call.
    mgr.maybe_reform(_state(turn=2))
    assert be.calls == 1


def test_material_change_triggers_reform():
    be = FakeBackend([_plan_json(), _plan_json("plan B")])
    mgr = GamePlanManager(be)
    mgr.maybe_reform(_state(turn=1, my_creatures=0))
    assert be.calls == 1
    # New turn AND a creature entered -> material change -> reform.
    mgr.maybe_reform(_state(turn=2, my_creatures=2, my_power=4))
    assert be.calls == 2
    assert mgr.current.path == "plan B"


def test_new_game_resets_plan():
    be = FakeBackend([_plan_json(), _plan_json("game 2 plan")])
    mgr = GamePlanManager(be)
    mgr.maybe_reform(_state(turn=5))
    assert be.calls == 1
    # Turn counter goes backwards -> new match -> reset + reform.
    mgr.maybe_reform(_state(turn=1))
    assert be.calls == 2
    assert mgr.current.path == "game 2 plan"


def test_repeated_stalls_force_reform_with_hint():
    be = FakeBackend([_plan_json("Cast Rush of Dread"), _plan_json("go wide with tokens")])
    mgr = GamePlanManager(be)
    mgr.maybe_reform(_state(turn=3))
    assert be.calls == 1
    # Same turn, static board => normally no reform...
    mgr.maybe_reform(_state(turn=3))
    assert be.calls == 1
    # ...but three stalls on the plan-advancing play force a reform even though
    # nothing material changed, and tell the model the line was unexecutable.
    for _ in range(3):
        mgr.note_stall("SelectTargets (Rush of Dread)")
    mgr.maybe_reform(_state(turn=3))
    assert be.calls == 2
    assert mgr.current.path == "go wide with tokens"
    # Stall feedback is cleared after the reform.
    assert mgr._stall_count == 0


def test_note_stall_below_threshold_does_not_reform():
    be = FakeBackend([_plan_json(), _plan_json("plan B")])
    mgr = GamePlanManager(be)
    mgr.maybe_reform(_state(turn=2))
    assert be.calls == 1
    mgr.note_stall("x")
    mgr.note_stall("x")  # only 2 < threshold(3)
    mgr.maybe_reform(_state(turn=2))
    assert be.calls == 1


def test_llm_failure_keeps_prior_plan():
    class BoomBackend:
        calls = 0

        def complete(self, *a, **k):
            BoomBackend.calls += 1
            raise RuntimeError("boom")

    mgr = GamePlanManager(BoomBackend())
    out = mgr.maybe_reform(_state(turn=1))
    assert out is None  # nothing formed, but no exception escaped
    assert mgr.current is None


def test_replacement_threat_and_tutored_card_reform_same_turn():
    be = FakeBackend([_plan_json(), _plan_json("answer the new engine"), _plan_json("cast tutored answer")])
    mgr = GamePlanManager(be)
    state = _state(turn=4)
    state["battlefield"] = [{"instance_id": 1, "name": "Old Engine", "type_line": "Enchantment"}]
    state["hand"] = [{"instance_id": 2, "name": "Tutor"}]
    mgr.maybe_reform(state)
    state["battlefield"] = [{"instance_id": 3, "name": "New Engine", "type_line": "Enchantment"}]
    mgr.maybe_reform(state)
    assert mgr.current.path == "answer the new engine"
    state["hand"] = [{"instance_id": 4, "name": "Tutored Answer"}]
    mgr.maybe_reform(state)
    assert be.calls == 3
    assert mgr.current.path == "cast tutored answer"


def test_paying_mana_and_priority_churn_do_not_reform():
    be = FakeBackend([_plan_json()])
    mgr = GamePlanManager(be)
    state = _state(turn=3)
    state["battlefield"] = [{"instance_id": 1, "name": "Forest", "type_line": "Land", "is_tapped": False}]
    mgr.maybe_reform(state)
    state["battlefield"][0]["is_tapped"] = True
    state["_bridge_game_state_id"] = 99
    mgr.maybe_reform(state)
    assert be.calls == 1


def test_seed_and_prior_plan_are_available_for_adaptation():
    be = FakeBackend([_plan_json("develop commander engine"), _plan_json("protect the engine")])
    mgr = GamePlanManager(be)
    mgr.seed("Commander copies produce mana for large threats.")
    state = _state(turn=2)
    state["deck_reference"] = "DECK REFERENCE\n1x Example Commander | complete rules"
    mgr.maybe_reform(state)
    assert be.requests[0][1].startswith(state["deck_reference"])
    assert "Commander copies produce mana" in be.requests[0][1]
    mgr.maybe_reform(_state(turn=3, opp_creatures=1))
    assert "develop commander engine" in be.requests[1][1]
    assert "Removal and tutoring are conditional" in be.requests[1][0]
    assert "Holding mana or passing is correct" in mgr.plan_text()


def test_new_match_id_clears_plan_even_when_turn_number_is_unchanged():
    mgr = GamePlanManager(FakeBackend([_plan_json()]))
    state = dict(_state(turn=1), match_id="first")
    mgr.seed("Old deck")
    mgr.maybe_reform(state)
    mgr.observe(dict(state, match_id="second"))
    assert mgr.current is None
    assert mgr.plan_text() == ""
    assert mgr._seed is None


@pytest.fixture
def refresh_threads(monkeypatch):
    real_thread = threading.Thread
    started = []

    def create_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        started.append(thread)
        return thread

    monkeypatch.setattr("arenamcp.game_plan.threading.Thread", create_thread)
    yield started
    for thread in started:
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_background_refresh_returns_while_model_is_blocked_and_uses_snapshot(refresh_threads):
    entered, release, updated = threading.Event(), threading.Event(), threading.Event()
    be = FakeBackend([_plan_json()])
    original = be.complete

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    be.complete = blocked
    mgr = GamePlanManager(be)
    state = _state(turn=3)
    state["hand"] = [{"name": "Known Card"}]
    try:
        assert mgr.request_reform(state, on_updated=updated.set)
        assert entered.wait(timeout=2)
        assert mgr.current is None
        state["hand"][0]["name"] = "Changed Later"
        assert not mgr.request_reform(state)
    finally:
        release.set()
    assert updated.wait(timeout=2)
    assert be.calls == 1
    assert "Known Card" in be.requests[0][1]
    assert "Changed Later" not in be.requests[0][1]


def test_old_match_inflight_result_is_discarded(refresh_threads):
    entered, release = threading.Event(), threading.Event()

    class DelayedBackend:
        def complete(self, *args, **kwargs):
            entered.set()
            assert release.wait(timeout=2)
            return _plan_json("old game line")

    mgr = GamePlanManager(DelayedBackend())
    try:
        assert mgr.request_reform(dict(_state(turn=5), match_id="old"))
        assert entered.wait(timeout=2)
        mgr.observe(dict(_state(turn=1), match_id="new"))
        assert mgr.current is None
    finally:
        release.set()
    refresh_threads[0].join(timeout=2)
    assert mgr.current is None
    assert mgr._match_id == "new"


@pytest.mark.parametrize("response", [_plan_json(), "unusable response"])
def test_background_refresh_cooldown_applies_to_success_and_failure(monkeypatch, refresh_threads, response):
    now = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: now[0])
    be = FakeBackend([response, _plan_json("new line")])
    mgr = GamePlanManager(be)
    state = _state(turn=3)
    assert mgr.request_reform(state)
    refresh_threads[-1].join(timeout=2)
    state["hand"] = [{"name": "New Card"}]
    assert not mgr.request_reform(state)
    assert be.calls == 1
    now[0] += mgr._REFRESH_INTERVAL_S
    assert mgr.request_reform(state)
    refresh_threads[-1].join(timeout=2)
    assert be.calls == 2


def test_background_refresh_defers_to_live_stack_and_skips_empty_snapshots(refresh_threads):
    mgr = GamePlanManager(FakeBackend([_plan_json()]))
    assert not mgr.request_reform({})
    assert not mgr.request_reform(dict(_state(), stack=[{"name": "Urgent Threat"}]))
    assert refresh_threads == []


def test_background_refresh_waits_for_deck_analysis(refresh_threads):
    mgr = GamePlanManager(FakeBackend([_plan_json()]))
    mgr.background_suspended_fn = lambda: True
    assert not mgr.request_reform(_state())
    assert refresh_threads == []


def test_background_snapshot_does_not_reset_a_newer_turn():
    mgr = GamePlanManager(FakeBackend([_plan_json()]))
    old_snapshot = dict(_state(turn=3), match_id="same-game")
    mgr.observe(old_snapshot)
    generation = mgr._generation
    mgr.observe(dict(_state(turn=4), match_id="same-game"))
    mgr.maybe_reform(old_snapshot, _expected_generation=generation)
    assert mgr._generation == generation
    assert mgr._observed_turn == 4
    assert mgr.current is not None
