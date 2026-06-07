"""Tests for the persistent GamePlan strategic layer (game_plan.py)."""

import json

from arenamcp.game_plan import GamePlan, GamePlanManager


class FakeBackend:
    """Records calls and returns a scripted response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, system, user, max_tokens, **kwargs):
        self.calls += 1
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
        bf.append({"type_line": "Creature", "controller_seat_id": 1, "power": my_power // max(my_creatures, 1)})
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
