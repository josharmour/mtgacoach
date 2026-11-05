"""Forecast detail contracts on recorded boards; no live files or network services."""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

import pytest

pytest.importorskip("PySide6")

from tests import strategic_states as S

from arenamcp import board_assessment as ba
from arenamcp import game_plan as gp
from arenamcp import opponent_tricks as ot
from arenamcp.desktop import theme
from arenamcp.desktop.compact_coach import CompactCoachPanel
from arenamcp.forecast_identity import forecast_key
from arenamcp.game_plan import GamePlanManager
from arenamcp.mcts_evaluator import MCTSEvaluator
from arenamcp.plan_card import card_payload

FIXTURES = ["G1_T8", "G1_T12", "G1_T14", "G1_T15_FROM_OPPONENT", "G3_T10_BLOCKS"]

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


def _offline_service(cache_dir: Any) -> ot.TrickTableService:
    """A trick table service that never reads the disk cache, the card database or 17Lands."""
    return ot.TrickTableService(
        primer_fn=lambda code: None,
        ratings_fn=lambda code: [],
        color_ratings_fn=lambda code: None,
        card_lookup=lambda grp: None,
        cache_dir=cache_dir,
    )


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """Deck lookups from the fixture catalog; no real trick tables; fresh caches."""
    monkeypatch.setattr(
        "arenamcp.match_context._local_card",
        lambda grp_id, epoch: dict(S.DECK_CATALOG.get(grp_id) or {"name": f"Unknown({grp_id})"}),
    )
    monkeypatch.setattr(ot.TrickTableService, "_shared", _offline_service(tmp_path / "tricks"))
    monkeypatch.delenv("ARENAMCP_LINE_SEARCH", raising=False)
    MCTSEvaluator.reset_cache()
    ba._CACHE.clear()
    gp._TRICK_CACHE.clear()


@pytest.fixture
def panel(qapp):
    p = CompactCoachPanel()
    p.resize(420, 720)
    p.show()
    yield p
    p.close()


def _facts(name):
    return ba.assess(deepcopy(getattr(S, name))).as_payload()


def _legacy_facts(name):
    facts = _facts(name)
    for key in ("lines", "posture", "search"):
        facts.pop(key)
    return facts


def _plan(facts, **extra):
    return {"facts": facts, "role": facts["role"], "role_reason": facts["role_reason"], **extra}


def _with_turns(facts):
    return _plan(
        facts,
        turn_plan=[{"turn": 12, "land": "Island", "cast": ["Undulating Witness"]}],
        win_conditions=["Archive Arbiter / Beast midrange beats"],
    )


def _open(panel, kind="paths", index=0):
    if kind == "paths":
        panel.strategy_view.inspect(index)
    else:
        panel._show_detail(kind)
    assert panel.workspace.currentWidget() is panel.detail_pager
    assert panel.detail_pager.isVisible()
    return panel.detail_pager.browser.toPlainText()


def _render(panel, plan, state=None):
    state = deepcopy(state or S.G1_T12)
    panel._on_game_state_changed(state)
    panel._on_game_plan_changed(dict(plan, snapshot_key=forecast_key(state)))
    assert panel.strategy_view.current
    return _open(panel)


def _step(turn, life, **extra):
    return {
        "turn": turn,
        "land": "",
        "casts": [],
        "modes": {},
        "cycles": [],
        "attack": [],
        "held": [],
        "life_after": life,
        "opp_life_after": None,
        **extra,
    }


def _line(outcome, *steps, now_life=None):
    return {"outcome": outcome, "now_life": now_life, "steps": list(steps)}


def _synthetic(lines, posture="", **search):
    facts = _legacy_facts("G1_T12")
    facts.update(lines=lines, posture=posture, search={"truncated": False, **search})
    return _plan(facts)


@pytest.mark.parametrize("name", FIXTURES)
@pytest.mark.parametrize("shape", [_plan, _with_turns])
def test_payloads_without_search_keep_explanation_without_inventing_a_leading_path(panel, name, shape):
    plan = shape(_legacy_facts(name))
    text = _render(panel, plan, getattr(S, name))
    assert "No supported searched paths" in text
    assert not panel.strategy_view.path_button.leading
    detail = _open(panel, "advice")
    assert plan["facts"]["role_reason"] in detail
    if plan.get("turn_plan"):
        assert "T12: Island + Undulating Witness" in detail
        assert "Plan objective: Archive Arbiter / Beast midrange beats" in detail
    elif plan["facts"].get("lookahead"):
        assert "Unsearched board-math projection" in detail
        for step in plan["facts"]["lookahead"][:3]:
            assert f"T{step['turn']}:" in detail
            for card in step.get("casts") or []:
                assert card in detail
    assert "▮" not in detail and "Outlook" not in detail
    _render(
        panel, dict(plan, opp_interaction={"known": False, "ui_line": "HIDDEN ESTIMATE"}), getattr(S, name)
    )
    assert "HIDDEN ESTIMATE" not in _open(panel)


def test_search_kill_switch_keeps_plan_context_without_claiming_a_searched_leader(panel, monkeypatch):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    facts = _facts("G1_T12")
    assert facts["lines"] == [] and facts["posture"] == ""
    assert "No supported searched paths" in _render(panel, _with_turns(facts))
    assert not panel.strategy_view.path_button.leading
    assert "Island + Undulating Witness" in _open(panel, "advice")


@pytest.mark.parametrize("name", FIXTURES)
def test_recorded_paths_and_alternatives_are_inspectable_without_growing_panel(panel, name, qapp):
    state = getattr(S, name)
    facts = _facts(name)
    text = _render(
        panel, _plan(facts, opp_interaction={"known": True, "ui_line": "Opp tricks ~9%: 2 open (U)"}), state
    )
    assert "Opp tricks ~9%: 2 open (U)" in text
    qapp.processEvents()
    before = panel.size()
    for index, line in enumerate(facts["lines"][:3]):
        text = _open(panel, index=index)
        expected = {"dead": "Projected loss", "win": "Projected win", "alive": "Survives modeled attacks"}
        assert expected[line["outcome"]] in text
        for step in line["steps"]:
            for card in step["casts"]:
                assert card in text
        assert panel.size() == before
    assert facts["role_reason"] in _open(panel, "advice")


def test_path_shows_lives_after_opponents_attacks_and_posture(panel):
    text = _render(panel, _synthetic([_line("alive", _step(12, 7), _step(14, 5))], posture="hold"))
    assert "Survives modeled attacks · win not established" in text
    assert "T13 · Your life after their attack: 7" in text
    assert "T15 · Your life after their attack: 5" in text
    assert "Posture: hold" in text
    assert "path:1" not in panel.strategy_view.detail_html()


@pytest.mark.parametrize("posture", ["attack", "hold", "either", ""])
def test_only_meaningful_attack_or_hold_posture_is_shown(panel, posture):
    text = _render(panel, _synthetic([_line("alive", _step(12, 17))], posture=posture))
    assert ("Posture:" in text) == (posture in ("attack", "hold"))
    if posture in ("attack", "hold"):
        assert "Posture: " + posture in text


def test_dead_line_has_loss_timing_bad_tone_and_life_floored_at_zero(panel):
    text = _render(panel, _synthetic([_line("dead", _step(14, 2), _step(16, -3))]))
    assert "Projected loss · T17" in text
    assert "T15 · Your life after their attack: 2" in text
    assert "T17 · Your life after their attack: 0" in text
    assert "-3" not in text
    assert theme.color("bad") in panel.strategy_view.detail_html()


def test_combat_already_under_way_precedes_future_life_projections(panel):
    state = deepcopy(S.G1_T12)
    state["turn"]["turn_number"] = 10
    text = _render(panel, _synthetic([_line("alive", _step(11, 16), _step(13, 12), now_life=20)]), state)
    assert text.index("After current combat: your projected life 20") < text.index(
        "T12 · Your life after their attack: 16"
    )
    assert "T14 · Your life after their attack: 12" in text


def test_winning_line_names_conditional_lethal_turn_and_opponent_life(panel):
    text = _render(panel, _synthetic([_line("win", _step(15, None, attack=["Cadet"], opp_life_after=-2))]))
    assert "Projected win · T15 · conditional" in text
    assert "Attack with Cadet" in text
    assert "T15 · Opponent life after your attack: 0" in text
    assert "-2" not in text


@pytest.mark.parametrize(
    "lines,search",
    [([], {}), ([_line("win", _step(12, 7, opp_life_after=0))], {"truncated": True}), (["not a line"], {})],
)
def test_unusable_or_truncated_search_does_not_claim_a_supported_win(panel, lines, search):
    text = _render(panel, _synthetic(lines, **search))
    assert not panel.strategy_view.path_button.leading
    assert "Projected win" not in text
    if search.get("truncated"):
        assert "Search truncated · survival and wins uncertain" in text
    else:
        assert "No supported searched paths" in text


def test_real_g1_t14_alternative_dies_before_best_line(panel):
    facts = _facts("G1_T14")
    best, alt = facts["lines"][:2]
    dead_turn = next(
        s["turn"] + 1 for s in best["steps"] if s["life_after"] is not None and s["life_after"] <= 0
    )
    alt_turn = next(
        s["turn"] + 1 for s in alt["steps"] if s["life_after"] is not None and s["life_after"] <= 0
    )
    assert alt_turn < dead_turn
    text = _render(panel, _plan(facts), S.G1_T14)
    assert f"Projected loss · T{dead_turn}" in text
    alternative = _open(panel, index=1)
    assert "Archive Arbiter (destroy" in alternative
    assert f"Projected loss · T{alt_turn}" in alternative


def test_real_g1_t12_preserves_life_chain_and_alternative_cast(panel):
    facts = _facts("G1_T12")
    best = facts["lines"][0]
    text = _render(panel, _plan(facts))
    for step in best["steps"]:
        if step["life_after"] is not None:
            assert f"T{step['turn'] + 1} · Your life after their attack: {max(0, step['life_after'])}" in text
    assert "Play Island" in text and "Cast Undulating Witness" in text
    assert "Cast Murmuring Volume" in _open(panel, index=1)


def test_alternative_preserves_modes_targets_and_each_turn(panel):
    alt = _line(
        "alive",
        _step(
            12,
            4,
            land="Island",
            casts=["Murmuring Volume", "Archive Arbiter"],
            modes={"Archive Arbiter": "gain 4 life"},
            targets={"Archive Arbiter": "You"},
        ),
        _step(14, 4, casts=["Undulating Witness"]),
    )
    _render(panel, _synthetic([_line("alive", _step(12, 7)), alt]))
    text = _open(panel, index=1)
    assert "Path 2" in text and "Leading path" not in text.splitlines()[0]
    assert "Cast Archive Arbiter (gain 4 life) → You" in text
    assert "T14: Cast Undulating Witness" in text
    assert "T15 · Your life after their attack: 4" in text


def test_alternative_preserves_outcome_and_escapes_card_names(panel):
    best = _line("alive", _step(12, 7))
    alt = _line("dead", _step(12, -4, casts=["<b>Arbiter & Co</b>"]))
    _render(panel, _synthetic([best, alt]))
    text = _open(panel, index=1)
    assert "<b>Arbiter & Co</b>" in text and "Projected loss · T13" in text
    assert "&lt;b&gt;Arbiter &amp; Co&lt;/b&gt;" in panel.strategy_view.detail_html()
    win = _line("win", _step(15, 16, attack=["Cadet"], opp_life_after=1), _step(17, None, opp_life_after=-5))
    _render(panel, _synthetic([best, win]))
    assert "Projected win · T17 · conditional" in _open(panel, index=1)


def test_long_alternative_keeps_full_names_in_paged_detail(panel):
    names = [f"Very Long Creature Name {n}" for n in range(4)]
    _render(panel, _synthetic([_line("alive", _step(12, 7)), _line("alive", _step(12, 4, attack=names))]))
    before = panel.size()
    text = _open(panel, index=1)
    assert all(name in text for name in names)
    assert panel.size() == before


def test_opponent_estimate_is_visible_only_when_known_and_current(panel):
    ui_line = "Opp tricks ~14%: 3 open (G R), 3 in hand · Test Pump (pump) 0.31"
    plan = _plan(_legacy_facts("G3_T10_BLOCKS"))
    assert ui_line in _render(
        panel, dict(plan, opp_interaction={"known": True, "ui_line": ui_line}), S.G3_T10_BLOCKS
    )
    changed = deepcopy(S.G3_T10_BLOCKS)
    changed["turn"]["turn_number"] += 1
    panel._on_game_state_changed(changed)
    assert ui_line not in panel.detail_pager.browser.toPlainText()
    for unknown in ({"known": False, "ui_line": ui_line}, {"known": True, "ui_line": ""}, {}, "junk"):
        text = _render(panel, dict(plan, opp_interaction=unknown), S.G3_T10_BLOCKS)
        assert "Opponent interaction estimate" not in text and ui_line not in text


def test_ui_payload_end_to_end_uses_publisher_identity(panel, tmp_path):
    service = _offline_service(tmp_path / "tricks")
    pump = ot.TrickCard(grp_id=1, name="Test Pump", mana_cost="{1}{G}", mv=2, pips=("G",), kinds=("pump",))
    service.register(
        ot.TrickTable(
            set_code="FRA", prior={"RG": 1.0}, cards={1: pump}, copies={"RG": {1: 2.0}}, built_at=time.time()
        )
    )
    manager = GamePlanManager(None)
    manager.trick_service = service
    state = deepcopy(S.G3_T10_BLOCKS)
    payload = card_payload(manager, state, source="autopilot")
    assert payload["opp_interaction"]["known"] and len(payload["facts"]["lines"]) > 1
    panel._on_game_state_changed(state)
    panel._on_game_plan_changed(payload)
    assert panel.strategy_view.current
    assert "Test Pump" in _open(panel)
    assert "Path 2" in _open(panel, index=1)
    panel._on_game_state_changed(dict(state, game_over=True))
    text = panel.detail_pager.browser.toPlainText()
    assert "No current recommendation" in text and "Test Pump" not in text
    assert not panel.strategy_view.path_button.leading


# --- the tactical pill ----------------------------------------------------------


def _pill(provenance: str) -> dict[str, Any]:
    return {
        "root_win_probability": 0.3,
        "best_action": "Island + Undulating Witness",
        "branches": [
            {
                "action": "Island + Undulating Witness",
                "score_provenance": provenance,
                "sequence_steps": ["T12: Island"],
            }
        ],
    }


def test_pill_says_best_line_for_line_search_branches(panel):
    panel._on_mcts_updated(_pill("line_search"))
    rendered = panel.mcts_pill_label.text()
    assert "Best line" in rendered and "Heuristic hint" not in rendered
    assert "T12: Island" in rendered
    panel._on_mcts_updated(_pill("heuristic_lookahead"))
    rendered = panel.mcts_pill_label.text()
    assert "Heuristic hint" in rendered and "Best line" not in rendered


def test_pill_label_follows_the_real_evaluator_and_the_kill_switch(panel, monkeypatch):
    panel._on_mcts_updated(MCTSEvaluator.evaluate(deepcopy(S.G1_T12), force=True))
    assert "Best line" in panel.mcts_pill_label.text()
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    panel._on_mcts_updated(MCTSEvaluator.evaluate(deepcopy(S.G1_T12), force=True))
    assert not panel.mcts_pill_label.isHidden()
    assert "Heuristic hint" in panel.mcts_pill_label.text()
