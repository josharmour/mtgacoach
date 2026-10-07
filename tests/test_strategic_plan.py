"""Grounded strategic plan: validation, prompts, role guard and UI payload.

Evidence (2026-10-06 standalone.log, FRA game 1): GamePlan lines such as
"Empty-library Fblthp combat-damage trigger win" with 26 cards in the library
and "Ramp/filter via Murmuring Volume" while dead in two turns; the typed
decision path then cast the rock and landcycled the only castable blocker.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.strategic_states import (
    DECK_CATALOG,
    G1_T12,
    G1_T12_AFTER_VOLUME,
    G1_T12_MENU,
    G1_T15_FROM_OPPONENT,
    actions_decision,
    after_land_drop,
    card,
    cast,
)

from arenamcp import board_assessment as ba
from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import ROLE_CONTROL, ROLE_DEFENDER, assess
from arenamcp.game_plan import GamePlan, GamePlanManager, compose_strategy_block, validate_plan


@pytest.fixture(autouse=True)
def _offline_deck_lookup(monkeypatch):
    """Resolve the real deck list from the fixture catalog, never the card DB."""
    monkeypatch.setattr(
        "arenamcp.match_context._local_card",
        lambda grp_id, epoch: dict(DECK_CATALOG.get(grp_id) or {"name": f"Unknown({grp_id})"}),
    )
    ba._CACHE.clear()


def _with_catalog(state):
    result = deepcopy(state)
    result["deck_catalog"] = deepcopy(DECK_CATALOG)
    return result


# The model's actual T3/T6 plan text from the field report, plus a turn plan
# that over-spends T and lists a library card.
BAD_PLAN = {
    "role": "aggressor",
    "role_reason": "",
    "turns": [
        {"turn": "T", "land": "Island", "cast": ["Murmuring Volume", "Undulating Witness"], "attack": "none"},
        {"turn": "T+1", "cast": ["Archive Arbiter"], "attack": "Witness"},
        {"turn": "T+2", "cast": ["Fblthp, Impossibly Lost"], "attack": "everything"},
    ],
    "win_conditions": [
        "Empty-library Fblthp combat-damage trigger win",
        "Archive Arbiter / Beast midrange beats",
    ],
    "path": "Develop lands + Geist/Tetsuko/Fblthp T3-T5, deplete library via flashbacks/mill, lethal-trigger ~T8",
    "threat": "opponent flyers",
    "develop_next": "Murmuring Volume",
}


def _parsed(plan: dict, turn: int = 12) -> GamePlan:
    parsed = GamePlanManager._parse(json.dumps(plan), turn)
    assert parsed is not None
    return parsed


# --- validation -------------------------------------------------------------------


def test_empty_library_fblthp_win_is_rejected_with_a_26_card_library():
    state = _with_catalog(G1_T12)
    plan = validate_plan(_parsed(BAD_PLAN), assess(state), state)
    assert plan.win_conditions == ["Archive Arbiter / Beast midrange beats"]
    assert any("Empty-library Fblthp" in issue and "26 cards" in issue for issue in plan.issues)
    assert plan.path == ""  # "deplete library" path rejected too


def test_win_needing_a_library_card_is_rejected_unless_labelled_as_a_draw():
    state = _with_catalog(G1_T12)
    plan = _parsed(
        {**BAD_PLAN, "win_conditions": ["Fblthp unblockable chip damage", "Fblthp beats if drawn"]}
    )
    validate_plan(plan, assess(state), state)
    assert plan.win_conditions == ["Fblthp beats if drawn"]
    assert any("still in the library" in issue for issue in plan.issues)


def test_all_unrealistic_wins_are_replaced_from_board_facts():
    state = _with_catalog(G1_T12)
    plan = _parsed({**BAD_PLAN, "win_conditions": ["Fblthp mill-out via unblockable combat damage"]})
    validate_plan(plan, assess(state), state)
    assert plan.win_conditions == ["Stabilize behind Undulating Witness, then win with our biggest creatures"]


def test_aggressor_role_contradicting_a_two_turn_clock_is_replaced():
    for state, expected in ((G1_T12, ROLE_DEFENDER), (G1_T12_AFTER_VOLUME, ROLE_CONTROL)):
        state = _with_catalog(state)
        plan = _parsed({**BAD_PLAN, "role_reason": "we can race them with flyers soon"})
        validate_plan(plan, assess(state), state)
        assert plan.role == expected
        assert any(issue.startswith("role aggressor rejected") for issue in plan.issues)


def test_role_disagreement_needs_a_concrete_reason():
    state = _with_catalog(G1_T12_AFTER_VOLUME)
    plan = validate_plan(_parsed({**BAD_PLAN, "role": "defender"}), assess(state), state)
    assert plan.role == ROLE_CONTROL
    plan = validate_plan(_parsed({**BAD_PLAN, "role": "control/stabilize"}), assess(state), state)
    assert plan.role == ROLE_CONTROL and plan.role_reason  # agrees: reason filled from the facts


def test_turn_plan_is_trimmed_to_mana_legal_casts_from_hand():
    state = _with_catalog(G1_T12)
    plan = validate_plan(_parsed(BAD_PLAN), assess(state), state)
    t, t1, t2 = plan.turn_plan
    # T: 5 mana after the Island; defending, so the rock goes before the blocker.
    assert (t["turn"], t["land"], t["cast"], t["mana"]) == (12, "Island", ["Undulating Witness"], 5)
    assert any("dropped Murmuring Volume — not mana-legal" in issue for issue in plan.issues)
    # T+1: Arbiter needs 6 with five lands and no rock: dropped, and the
    # emptied turn takes the board-math deployment (the rock that pays for it).
    assert (t1["turn"], t1["cast"], t1["mana"]) == (14, ["Murmuring Volume"], 5)
    assert any("dropped Archive Arbiter" in issue for issue in plan.issues)
    assert "board-math deployment" in t1["hold"]
    # T+2: Fblthp is in the library: only "if drawn".
    assert t2["turn"] == 16 and t2["cast"] == [] and "if drawn: Fblthp, Impossibly Lost" in t2["hold"]


def test_a_rock_cast_earlier_in_the_plan_pays_for_a_later_turn():
    state = _with_catalog(G1_T12)
    plan = _parsed(
        {
            **BAD_PLAN,
            "role": "defender",
            "turns": [
                {"turn": "T", "land": "Island", "cast": ["Undulating Witness"]},
                {"turn": "T+1", "cast": ["Murmuring Volume"]},
                {"turn": "T+2", "cast": ["Archive Arbiter"]},
            ],
        }
    )
    validate_plan(plan, assess(state), state)
    assert [step["cast"] for step in plan.turn_plan] == [
        ["Undulating Witness"],
        ["Murmuring Volume"],
        ["Archive Arbiter"],
    ]
    assert [step["mana"] for step in plan.turn_plan] == [5, 5, 6]


def test_the_same_card_is_not_cast_twice_and_unknown_cards_are_dropped():
    state = _with_catalog(G1_T12)
    plan = _parsed(
        {
            **BAD_PLAN,
            "role": "defender",
            "turns": [
                {"turn": "T", "land": "Island", "cast": ["Undulating Witness"]},
                {"turn": "T+1", "cast": ["Undulating Witness", "Lightning Bolt"]},
            ],
        }
    )
    validate_plan(plan, assess(state), state)
    assert plan.turn_plan[1]["cast"] == []
    assert any("already cast earlier" in issue for issue in plan.issues)
    assert any("dropped Lightning Bolt" in issue for issue in plan.issues)


# --- manager: prompt, budget, cadence ---------------------------------------------


class _Backend:
    def __init__(self, reply: dict | str):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.calls: list[tuple[str, str, tuple, dict]] = []

    def complete(self, system, user, *args, **kwargs):
        self.calls.append((system, user, args, kwargs))
        return self.reply


def test_reform_feeds_board_facts_and_uses_the_larger_background_budget():
    backend = _Backend(
        {
            **BAD_PLAN,
            "role": "defender",
            "turns": [{"turn": "T", "land": "Island", "cast": ["Undulating Witness"], "attack": "none"}],
        }
    )
    manager = GamePlanManager(backend)
    plan = manager.maybe_reform(deepcopy(G1_T12))
    system, user, args, kwargs = backend.calls[0]
    assert "WHO IS THE BEATDOWN" in system and "Removal and tutoring are conditional" in system
    assert "BOARD FACTS (deterministic" in user
    assert "ASSESSED ROLE: defender" in user
    assert "T = turn 12 (this turn); T+1 = turn 14; T+2 = turn 16" in user
    assert "MANA BUDGET BY TURN" in user and "T+1 = turn 14: 5 mana" in user
    assert args == (3000,)
    assert kwargs["reasoning_effort"] == "low" and kwargs["background"] is True
    assert kwargs["request_timeout_s"] == 75.0 and kwargs["temperature"] == 0.0
    assert plan.role == ROLE_DEFENDER
    assert plan.turn_plan[0]["cast"] == ["Undulating Witness"]
    assert plan.facts["their_clock"] == 2


def test_reform_validation_issues_are_logged(caplog):
    manager = GamePlanManager(_Backend(BAD_PLAN))
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        manager.maybe_reform(deepcopy(G1_T12))
    text = caplog.text
    assert "GamePlan validation (turn 12): win condition 'Empty-library Fblthp" in text
    assert "GamePlan (turn 12): role=defender" in text


def test_backends_without_the_new_keywords_still_work():
    class Legacy:
        def complete(self, system, user, max_tokens, temperature=0.3, request_timeout_s=None):
            return json.dumps({**BAD_PLAN, "role": "defender"})

    plan = GamePlanManager(Legacy()).maybe_reform(deepcopy(G1_T12))
    assert plan is not None and plan.role == ROLE_DEFENDER


# --- the grounded block in decision prompts ------------------------------------


class _DecisionBackend:
    def __init__(self, reply: dict):
        self.reply = json.dumps(reply)
        self.prompts: list[str] = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.reply


def _planner(reply: dict) -> tuple[ActionPlanner, _DecisionBackend]:
    backend = _DecisionBackend(reply)
    return ActionPlanner(backend), backend


def test_decision_prompt_leads_the_strategy_with_role_this_turn_and_facts():
    planner, backend = _planner({"option_ids": ["idx:5"], "reasoning": "Island first."})
    assert planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12)) == ["idx:5"]
    prompt = backend.prompts[-1]
    strategy = prompt[prompt.index("STRATEGIC ROLE") :]
    lines = strategy.splitlines()
    assert "DEFENDER" in lines[0]
    assert lines[1].startswith("  THIS TURN (T12, now): play Island; cast Undulating Witness")
    assert lines[2].startswith("  FACTS: they kill us in 2 attack(s)")


def test_decision_prompt_uses_the_validated_plan_step_from_the_manager():
    manager = GamePlanManager(
        _Backend(
            {
                **BAD_PLAN,
                "role": "defender",
                "turns": [{"turn": "T", "land": "Island", "cast": ["Undulating Witness"]}],
            }
        )
    )
    manager.maybe_reform(deepcopy(G1_T12))
    planner, backend = _planner({"option_ids": ["idx:5"], "reasoning": "Island first."})
    planner.set_game_plan(manager.plan_text())
    planner.set_game_plan_source(manager.strategy_block)
    planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12))
    prompt = backend.prompts[-1]
    assert "THIS TURN (T12, now): play Island; cast Undulating Witness [game plan T12]" in prompt
    assert prompt.index("STRATEGIC ROLE") < prompt.index("GAME PLAN (formed turn 12")
    assert "Win condition(s): Archive Arbiter / Beast midrange beats" in prompt
    assert "Empty-library" not in prompt


def test_strategy_block_flags_a_stale_plan_role():
    plan = GamePlan(role="aggressor", turn_formed=10, win_conditions=["race"], path="attack")
    block = compose_strategy_block(assess(deepcopy(G1_T12)), plan)
    assert block.startswith("STRATEGIC ROLE (deterministic board math, recomputed now): DEFENDER")
    assert "game plan (turn 10) said AGGRESSOR" in block


# --- role guard in the typed decision path ---------------------------------------


def test_typed_path_guard_replaces_the_mana_rock_and_logs_why(caplog):
    planner, _ = _planner({"option_ids": ["idx:1"], "reasoning": "Murmuring Volume ramps toward Arbiter."})
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        chosen = planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12))
    assert chosen == ["idx:5"]
    assert "Role guard: defender" in caplog.text and "Murmuring Volume" in caplog.text
    reasoning = planner.get_decision_reasoning(chosen)
    assert "Undulating Witness" in reasoning
    assert planner.get_last_decision_trace()["role_guard"]["replaced"] == "idx:1"


def test_typed_path_guard_then_casts_the_blocker():
    state = after_land_drop(G1_T12, 284)
    decision = actions_decision(
        [
            ("idx:0", "Cast Undulating Witness", cast(229, 106272), True),
            ("idx:1", "Cast Murmuring Volume", cast(217, 106419), True),
            ("pass", "Pass", None, None),
        ]
    )
    planner, _ = _planner({"option_ids": ["idx:1"], "reasoning": "Ramp."})
    assert planner.plan_decision_options(decision, state) == ["idx:0"]


def test_typed_path_guard_leaves_lethal_alone():
    state = deepcopy(G1_T15_FROM_OPPONENT)
    state["hand"] = [card(801, "Twinned Vision", 2), card(802, "Theorix Metamage", 2)]
    decision = actions_decision(
        [
            ("idx:0", "Cast Twinned Vision", cast(801), True),
            ("idx:1", "Cast Theorix Metamage", cast(802), True),
        ]
    )
    planner, _ = _planner({"option_ids": ["idx:0"], "reasoning": "Draw first."})
    assert planner.plan_decision_options(decision, state) == ["idx:0"]


# --- UI payload and coach path ------------------------------------------------------


def test_ui_payload_carries_role_clocks_and_the_three_turn_plan():
    manager = GamePlanManager(
        _Backend(
            {
                **BAD_PLAN,
                "role": "defender",
                "turns": [{"turn": "T", "land": "Island", "cast": ["Undulating Witness"]}],
            }
        )
    )
    assert manager.ui_payload(deepcopy(G1_T12))["facts"]["role"] == ROLE_DEFENDER  # before any plan
    manager.maybe_reform(deepcopy(G1_T12))
    payload = manager.ui_payload(deepcopy(G1_T12))
    json.dumps(payload)
    assert payload["role"] == ROLE_DEFENDER
    assert payload["facts"]["their_clock"] == 2 and payload["facts"]["race"] == "behind"
    assert payload["turn_plan"][0] == {
        "turn": 12,
        "label": "T",
        "land": "Island",
        "cast": ["Undulating Witness"],
        "attack": "",
        "hold": "",
        "mana": 5,
    }
    assert "facts" not in manager.ui_payload(None)


def test_autopilot_announces_plan_with_current_facts():
    from arenamcp.autopilot import AutopilotEngine

    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._game_plan_mgr = GamePlanManager(_Backend("{}"))
    engine._ui_game_plan_fn = Mock()
    engine._last_emitted_game_plan = None
    engine._announce_game_plan(deepcopy(G1_T12_AFTER_VOLUME))
    payload = engine._ui_game_plan_fn.call_args.args[0]
    assert payload["source"] == "autopilot"
    assert payload["role"] == ROLE_CONTROL
    assert "DEAD IN 2 TURNS UNLESS WE STABILIZE" in payload["facts"]["flags"]


def test_coach_advice_uses_the_same_grounded_block():
    from arenamcp.coach import CoachEngine

    manager = GamePlanManager(_Backend("{}"))
    text = CoachEngine._grounded_plan_text(manager, deepcopy(G1_T12))
    assert text.startswith("STRATEGIC ROLE") and "they kill us in 2" in text
    legacy = SimpleNamespace(plan_text=lambda: "GAME PLAN: legacy")
    assert CoachEngine._grounded_plan_text(legacy, deepcopy(G1_T12)) == "GAME PLAN: legacy"


def test_desktop_plan_card_renders_role_clocks_and_next_turns(qapp):
    pytest.importorskip("PySide6")
    from arenamcp.desktop.compact_coach import CompactCoachPanel

    manager = GamePlanManager(
        _Backend(
            {
                **BAD_PLAN,
                "role": "defender",
                "turns": [{"turn": "T", "land": "Island", "cast": ["Undulating Witness"]}],
            }
        )
    )
    manager.maybe_reform(deepcopy(G1_T12))
    panel = CompactCoachPanel()
    try:
        panel._on_game_plan_changed(dict(manager.ui_payload(deepcopy(G1_T12)), source="autopilot"))
        html = panel.game_plan_label.text()
        assert not panel.game_plan_label.isHidden()
        assert "DEFENDER" in html
        assert "they kill you in 2" in html and "race behind" in html
        assert "T12 Island + Undulating Witness" in html
        assert "Archive Arbiter / Beast midrange beats" in html
        panel._on_game_plan_changed({})
        assert panel.game_plan_label.isHidden()
    finally:
        panel.close()
