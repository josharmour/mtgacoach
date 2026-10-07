"""The line search's lines replace the heuristic 'Suggested line' (multi-turn planning WP11).

The coach prompt and the autopilot's typed-decision prompt both render
``CoachEngine._format_game_context``, which appends mcts_evaluator's
HEURISTIC HINTS; the sidebar's tactical line is ``MCTSTreePayload.to_dict()``.
On the real G1_T12 board (2026-10-06 FRA game 1, our T12 at 11 life, no
creatures vs four) the hints suggested "Play Island, cast Countersculpt, hold
priority" while the line search's best line is Island + Undulating Witness.

When ``board_assessment.assess`` has searched lines (present, not truncated)
the prompt drops the heuristic suggestion and keeps the position, life/mana,
format and BLUNDER TRAP lines, and the sidebar shows the searched lines. With
nothing searched (ARENAMCP_LINE_SEARCH=0, a truncated search, no assessment)
today's hints stay. Fixtures are real recorded states (tests/strategic_states.py).
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from types import SimpleNamespace

import pytest
from tests.strategic_states import G1_T8, G1_T12, G1_T14, mac_phase

from arenamcp import board_assessment as ba
from arenamcp import mcts_evaluator
from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import assess
from arenamcp.coach import CoachEngine
from arenamcp.mcts_evaluator import MCTSEvaluator, searched_lines

SUGGESTION_MARKERS = ("Suggested line:", "Other candidates:", "↳ Why:")
# The heuristic's own G1_T12 pick, which the search contradicts.
HEURISTIC_T12_STEPS = ("Cast: Countersculpt [{U}{U}]", "Hold Priority with")

LIVE_SHAPES = {
    "G1_T8": G1_T8,
    "G1_T12": G1_T12,
    "G1_T14": G1_T14,
    # The Mac bridge's names ("Main1", step "None"), as live snapshots carry them.
    "G1_T12_mac": mac_phase(G1_T12),
}


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.delenv("ARENAMCP_LINE_SEARCH", raising=False)
    MCTSEvaluator.reset_cache()
    ba._CACHE.clear()
    yield
    MCTSEvaluator.reset_cache()
    ba._CACHE.clear()


def _searched(state: dict) -> dict:
    """``state`` with an untruncated search in the assessment cache.

    The search is reproducible unless its wall-clock hard cap cut it short
    (machine load, a GC pause): then it runs again, as tests/test_board_assessment.py
    does.
    """
    for _attempt in range(3):
        ba._CACHE.clear()
        if searched_lines(state):
            return state
    pytest.fail("the line search was truncated three times")


def _context(state: dict) -> str:
    return CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)


def _hints(text: str) -> str:
    return text[text.index("=== HEURISTIC HINTS") :]


def _trap_lines(text: str) -> list[str]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if "BLUNDER TRAP" in line), len(lines))
    return lines[start:]


# --- prompts --------------------------------------------------------------------


@pytest.mark.parametrize("name", list(LIVE_SHAPES))
def test_prompt_leaves_out_the_heuristic_suggestion_when_the_search_has_lines(name):
    state = _searched(deepcopy(LIVE_SHAPES[name]))
    context = _context(state)
    hints = _hints(context)
    assert not any(marker in hints for marker in SUGGESTION_MARKERS)
    # The position, life/mana and format lines stay.
    assert "• Board position:" in hints and "• HERO:" in hints and "• Format:" in hints
    # The searched lines are what the prompts carry instead (strategic block).
    assert "LINES (2-turn search" in assess(state).prompt_block()


def test_autopilot_typed_decision_context_drops_the_contradicting_suggestion():
    state = _searched(deepcopy(G1_T12))
    planner = ActionPlanner.__new__(ActionPlanner)
    context = planner._decision_game_context(state)
    assert "=== HEURISTIC HINTS" in context
    assert not any(marker in context for marker in SUGGESTION_MARKERS)
    assert not any(step in context for step in HEURISTIC_T12_STEPS)
    assert searched_lines(state)[0].steps[0].text() == "Island + Undulating Witness"


def test_blunder_traps_stay_in_the_prompt():
    state = _searched(deepcopy(G1_T14))
    payload = MCTSEvaluator.evaluate(state)
    assert payload.blunder_traps
    full = payload.format_for_llm_prompt()
    traps = _trap_lines(full)
    assert traps and "BLUNDER TRAP" in traps[0]
    assert _trap_lines(payload.format_for_llm_prompt(include_suggested_line=False)) == traps
    assert _trap_lines(_hints(_context(state))) == traps


def test_the_keyword_omits_exactly_the_suggestion_and_defaults_to_today():
    state = _searched(deepcopy(G1_T14))
    payload = MCTSEvaluator.evaluate(state)
    full = payload.format_for_llm_prompt()
    assert payload.format_for_llm_prompt(include_suggested_line=True) == full
    lines = full.splitlines()
    start = lines.index("Suggested line:")
    end = next(i for i in range(start, len(lines)) if lines[i].startswith(("⚠️ BLUNDER TRAP", "=====")))
    assert (
        payload.format_for_llm_prompt(include_suggested_line=False).splitlines()
        == lines[:start] + lines[end:]
    )


# --- nothing searched: today's hints ------------------------------------------------


def _switched_off(monkeypatch):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")


def _truncated(monkeypatch):
    """search_lines past its hard deadline from the first expansion (a real truncated result)."""
    from arenamcp import line_search

    original = line_search.search_lines

    def truncated(model, **kwargs):
        return original(model, **{**kwargs, "soft_ms": 0.0, "hard_ms": 0.0})

    monkeypatch.setattr(line_search, "search_lines", truncated)


def _no_assessment(monkeypatch):
    monkeypatch.setattr(ba, "assess", lambda _state: None)


def _raising_assessment(monkeypatch):
    def broken(_state):
        raise RuntimeError("assessment exploded")

    monkeypatch.setattr(ba, "assess", broken)


NOTHING_SEARCHED = {
    "switched-off": _switched_off,
    "truncated": _truncated,
    "no-assessment": _no_assessment,
    "raising": _raising_assessment,
}


@pytest.mark.parametrize("mode", list(NOTHING_SEARCHED))
def test_nothing_searched_keeps_todays_hints(mode, monkeypatch):
    NOTHING_SEARCHED[mode](monkeypatch)
    state = deepcopy(G1_T12)
    if mode == "truncated":
        assert assess(state).line_search.truncated
    assert searched_lines(state) == []
    payload = MCTSEvaluator.evaluate(state)
    hints = _hints(_context(state))
    # Byte-identical to the heuristic render, the contradicting suggestion included.
    assert hints == payload.format_for_llm_prompt()
    assert "Suggested line:" in hints and all(step in hints for step in HEURISTIC_T12_STEPS)
    tree = payload.to_dict()
    assert tree["eval_source"] == "Tactical Heuristic Lookahead"
    assert tree["best_action"] == payload.best_action
    assert [b["action"] for b in tree["branches"]] == [b.action for b in payload.branches]
    assert all(b["score_provenance"] == "heuristic_lookahead" for b in tree["branches"])


def test_a_truncated_search_reported_with_lines_still_counts_as_nothing(monkeypatch):
    lines = searched_lines(_searched(deepcopy(G1_T12)))
    fake = SimpleNamespace(line_search=SimpleNamespace(truncated=True), lines=lines)
    monkeypatch.setattr(ba, "assess", lambda _state: fake)
    assert searched_lines(deepcopy(G1_T12)) == []


# --- the sidebar ---------------------------------------------------------------------


def test_sidebar_shows_the_searched_lines():
    state = _searched(deepcopy(G1_T12))
    lines = searched_lines(state)
    payload = MCTSEvaluator.evaluate(state)
    tree = payload.to_dict()
    json.dumps(tree)
    branches = tree["branches"]
    assert tree["eval_source"] == f"Line search ({len(lines)} lines)"
    assert tree["best_action"] == lines[0].summary()
    assert [b["action"] for b in branches] == [line.summary() for line in lines]
    assert all(b["score_provenance"] == "line_search" for b in branches)
    best = branches[0]
    assert best["tag"] == "⭐ BEST LINE" and all(b["tag"] == "NORMAL" for b in branches[1:])
    # One step per turn of the line, with its absolute turn: the T12 pick is land + Witness.
    assert best["sequence_steps"][0] == "T12: Island + Undulating Witness"
    assert len(best["sequence_steps"]) == len(lines[0].steps)
    for branch, line in zip(branches, lines, strict=True):
        if line.outcome == "alive":
            assert re.fullmatch(r"life -?\d+ vs -?\d+ by T(\+\d)?", branch["outcome_summary"])
            assert branch["outcome_summary"].startswith(f"life {line.lives()[-1]} vs ")
    # The heuristic payload itself is unchanged: its branches still render the
    # default prompt; the traps and the position stay in the sidebar.
    assert all(b.score_provenance == "heuristic_lookahead" for b in payload.branches)
    assert tree["blunder_traps"] == [t.to_dict() for t in payload.blunder_traps]
    assert (tree["hero_life"], tree["opp_life"]) == (11, 20)


def test_sidebar_on_the_mode_menu_board_names_the_surviving_mode():
    state = _searched(deepcopy(G1_T14))
    tree = MCTSEvaluator.evaluate(state).to_dict()
    best = tree["branches"][0]
    assert "gain 4 life" in best["sequence_steps"][0]
    destroy = next(b for b in tree["branches"] if "destroy target" in b["sequence_steps"][0])
    # Lifegain survives their first attack; destroying Splinter Twin does not.
    assert best["details"]["outcome"] == "dead" and best["outcome_summary"].startswith("dead on T")
    assert destroy["details"]["dead_at"] == 1 < best["details"]["dead_at"]
    assert tree["blunder_traps"]


def test_sidebar_follows_the_kill_switch_without_a_cache_reset(monkeypatch):
    state = _searched(deepcopy(G1_T12))
    first = MCTSEvaluator.evaluate(state)
    assert first.line_branches and first.to_dict()["branches"][0]["score_provenance"] == "line_search"
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    second = MCTSEvaluator.evaluate(state)
    assert second is first  # the heuristic cache still serves the payload
    assert second.line_branches == []
    assert second.to_dict()["eval_source"] == "Tactical Heuristic Lookahead"
    monkeypatch.delenv("ARENAMCP_LINE_SEARCH")
    assert MCTSEvaluator.evaluate(_searched(state)).line_branches


def test_repeated_evaluations_reuse_the_payload_and_its_lines():
    state = _searched(deepcopy(G1_T12))
    first = MCTSEvaluator.evaluate(state)
    second = MCTSEvaluator.evaluate(deepcopy(state))
    assert second is first and second.line_branches is first.line_branches


def test_a_failing_line_branch_keeps_the_heuristic_sidebar(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("branch exploded")

    monkeypatch.setattr(mcts_evaluator, "_line_branch", broken)
    state = _searched(deepcopy(G1_T12))
    tree = MCTSEvaluator.evaluate(state).to_dict()
    assert tree["eval_source"] == "Tactical Heuristic Lookahead"
    assert all(b["score_provenance"] == "heuristic_lookahead" for b in tree["branches"])


def test_no_hints_outside_our_open_main_phase():
    state = deepcopy(G1_T12)
    state["turn"]["active_player"] = 2  # their turn: the hints never apply
    tree = MCTSEvaluator.evaluate(state).to_dict()
    assert tree["branches"] == [] and tree["blunder_traps"] == []
    assert tree["eval_source"] == "Tactical Heuristic Lookahead"
