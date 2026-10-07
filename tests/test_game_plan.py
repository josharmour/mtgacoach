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


def test_plans_our_next_turn_during_the_opponents_turn_once():
    """The plan call takes ~30 s on a busy gateway, so it runs while they play."""
    be = FakeBackend([_plan_json(), _plan_json("plan B")])
    mgr = GamePlanManager(be)
    mgr.maybe_reform(_state(turn=1))
    assert be.calls == 1
    # The opponent's turn re-plans for our coming turn, even on a static board.
    opponent_turn = _state(turn=2)
    opponent_turn["turn"]["active_player"] = 2
    mgr.maybe_reform(opponent_turn)
    assert be.calls == 2 and mgr.current.path == "plan B"
    # ...once, not on every decision of that turn.
    mgr.maybe_reform(opponent_turn)
    assert be.calls == 2
    # Our own turn on an unchanged board keeps the plan made for it.
    mgr.maybe_reform(_state(turn=3))
    assert be.calls == 2


def test_board_churn_on_our_turn_needs_a_role_or_lethal_flip_to_reform(monkeypatch):
    """2026-10-06: 139 plan calls in 1.8 h, because card identities, creature
    counts and life changed after nearly every action. Mid-turn, only a flip
    in the board-math role or lethal flags re-forms the plan."""
    be = FakeBackend([_plan_json(), _plan_json("plan B")])
    mgr = GamePlanManager(be)
    facts = {"now": ("aggressor", False, False, False)}
    monkeypatch.setattr(mgr, "_strategic_key", lambda state, sig=None: (facts["now"], (), ()))
    mgr.maybe_reform(_state(turn=1, my_creatures=0))
    assert be.calls == 1
    # Creatures entered and life moved, but the role and lethal flags held.
    mgr.maybe_reform(_state(turn=1, my_creatures=2, my_power=4, opp_life=15))
    assert be.calls == 1
    # The board math now says the opponent has lethal on board: re-form.
    facts["now"] = ("defender", False, True, True)
    mgr.maybe_reform(_state(turn=1, my_creatures=2, my_power=4))
    assert be.calls == 2
    assert mgr.current.path == "plan B"
    # ...once: the new facts are the baseline for the next comparison.
    mgr.maybe_reform(_state(turn=1, my_creatures=3, my_power=5))
    assert be.calls == 2


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


def test_card_identity_churn_alone_does_not_reform_but_the_next_opponent_turn_does():
    be = FakeBackend([_plan_json(), _plan_json("answer the new engine")])
    mgr = GamePlanManager(be)
    state = _state(turn=4)
    state["battlefield"] = [{"instance_id": 1, "name": "Old Engine", "type_line": "Enchantment"}]
    state["hand"] = [{"instance_id": 2, "name": "Tutor"}]
    mgr.maybe_reform(state)
    state["battlefield"] = [{"instance_id": 3, "name": "New Engine", "type_line": "Enchantment"}]
    state["hand"] = [{"instance_id": 4, "name": "Tutored Answer"}]
    state["graveyard"] = [{"instance_id": 2, "name": "Tutor"}]
    mgr.maybe_reform(state)
    assert be.calls == 1
    # The opponent's turn plans our next one with the new engine in view.
    opponent_turn = dict(state, turn={"turn_number": 5, "active_player": 2, "phase": "Main1"})
    mgr.maybe_reform(opponent_turn)
    assert be.calls == 2
    assert mgr.current.path == "answer the new engine"
    assert "New Engine" in be.requests[1][1]


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
    opponent_turn = _state(turn=3, opp_creatures=1)
    opponent_turn["turn"]["active_player"] = 2
    mgr.maybe_reform(opponent_turn)
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
    from arenamcp.game_plan import STRATEGIC_LANE

    # One background strategy job at a time, process-wide: wait out a plan
    # refresh another test's get_advice() may have left running.
    assert STRATEGIC_LANE.wait_idle(5)
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
    state = _state(turn=4)
    state["turn"]["active_player"] = 2
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
    # Formed during the opponent's turn 3 for our turn 4, arriving on turn 4.
    old_snapshot = dict(_state(turn=3), match_id="same-game")
    old_snapshot["turn"] = dict(old_snapshot["turn"], active_player=2)
    mgr.observe(old_snapshot)
    generation = mgr._generation
    mgr.observe(dict(_state(turn=4), match_id="same-game"))
    mgr.maybe_reform(old_snapshot, _expected_generation=generation)
    assert mgr._generation == generation
    assert mgr._observed_turn == 4
    assert mgr.current is not None


def test_backend_error_text_is_logged_not_silently_dropped(caplog):
    """A timed-out plan call returns an error sentinel; it must show in the log."""
    be = FakeBackend(["[BACKEND ERROR] LLM streaming time budget exhausted"])
    mgr = GamePlanManager(be)
    with caplog.at_level("WARNING", logger="arenamcp.game_plan"):
        assert mgr.maybe_reform(_state(turn=1)) is None or mgr.current is None or mgr.current.is_empty()
    assert "game-plan LLM call failed" in caplog.text


# ----- cadence across turn sides (2026-10-07 review) ------------------------


def _fixture_board(name):
    import copy
    from pathlib import Path

    raw = json.loads((Path(__file__).parent / "fixtures" / name).read_text())
    state = raw.get("game_state", raw) if "turn" not in raw else raw
    return copy.deepcopy(state)


@pytest.mark.parametrize(
    "fixture", ["bug_20261006_174855_game_state.json", "bug_20261006_185403_game_state.json"]
)
def test_an_unchanged_board_reforms_once_per_opponent_turn_whatever_the_phase(monkeypatch, fixture):
    """The strategic key used to read the board math on the raw snapshot, so
    turn side and phase flipped the role/lethal flags: a reform at the start
    of each own turn or at the End step (6-8 calls over 6 unchanged turns)."""
    import copy

    board = _fixture_board(fixture)
    local, opp = board["local_seat_id"], board["opponent_seat_id"]
    first = int(board["turn"]["turn_number"])
    calls = []

    def fake_reform(self, game_state, turn_num, cancel=None):
        side = "me" if game_state["turn"]["active_player"] == local else "op"
        calls.append(f"T{turn_num}{side}")
        return GamePlan(win_conditions=["x"], path="p", turn_formed=turn_num)

    monkeypatch.setattr(GamePlanManager, "_reform", fake_reform)
    mgr = GamePlanManager(FakeBackend([]))
    for k in range(6):
        turn, active = first - 2 + k, (local if k % 2 == 0 else opp)
        for phase in ("Phase_Main1", "Phase_Combat", "Phase_Main2", "Phase_Ending"):
            state = copy.deepcopy(board)
            state["turn"] = dict(
                board["turn"], turn_number=turn, active_player=active, priority_player=active
            )
            state["turn"].update(phase=phase, step="")
            state.update(stack=[], pending_decision=None, match_id="m1")
            state.pop("decision_context", None)
            mgr.maybe_reform(state)
    t0 = first - 2
    assert calls == [f"T{t0}me", f"T{t0 + 1}op", f"T{t0 + 3}op", f"T{t0 + 5}op"]


def test_a_race_defender_toggle_alone_does_not_reform(monkeypatch):
    be = FakeBackend([_plan_json(), _plan_json("plan B")])
    mgr = GamePlanManager(be)
    facts = {"now": ("race", False, False, False)}
    monkeypatch.setattr(mgr, "_strategic_key", lambda state, sig=None: (facts["now"], (), (), frozenset()))
    mgr.maybe_reform(_state(turn=1))
    for role in ("defender", "race", "defender"):
        facts["now"] = (role, False, False, False)
        mgr.maybe_reform(_state(turn=1))
    assert be.calls == 1
    facts["now"] = ("aggressor", False, False, False)
    mgr.maybe_reform(_state(turn=1))
    assert be.calls == 2


def test_a_new_opposing_engine_reforms_the_same_turn():
    be = FakeBackend([_plan_json(), _plan_json("answer the new engine")])
    mgr = GamePlanManager(be)
    state = _state(turn=4)
    state["battlefield"] = [
        {"instance_id": 1, "name": "Old Engine", "type_line": "Enchantment", "controller_seat_id": 2}
    ]
    mgr.maybe_reform(state)
    # Tokens, creatures, lands and our own permanents are churn, not engines.
    state["battlefield"] = state["battlefield"] + [
        {"instance_id": 5, "name": "Treasure", "type_line": "Token Artifact", "controller_seat_id": 2,
         "object_kind": "GameObjectType_Token"},
        {"instance_id": 6, "name": "Bear", "type_line": "Creature - Bear", "controller_seat_id": 2},
        {"instance_id": 7, "name": "Island", "type_line": "Basic Land - Island", "controller_seat_id": 2},
        {"instance_id": 8, "name": "Our Relic", "type_line": "Artifact", "controller_seat_id": 1},
    ]  # fmt: skip
    mgr.maybe_reform(state)
    assert be.calls == 1
    state["battlefield"] = state["battlefield"] + [
        {"instance_id": 3, "name": "New Engine", "type_line": "Enchantment", "controller_seat_id": 2}
    ]
    mgr.maybe_reform(state)
    assert be.calls == 2
    assert mgr.current.path == "answer the new engine"
    assert "New Engine" in be.requests[1][1]
    # Removing an engine is not a reason by itself.
    state["battlefield"] = [card for card in state["battlefield"] if card["instance_id"] != 1]
    mgr.maybe_reform(state)
    assert be.calls == 2
