"""Desktop plan card x line search (multi-turn planning spec WP10).

The plan card gains a Line row (the searched best line's outcome, our life
after each of their attacks and the attack/hold posture), a muted Alt row (the
runner-up line) and a muted Opp row (the opponent-trick estimate), and the
tactical pill says 'Best line' when its branches come from the line search.
Payloads without lines (search off, failed or truncated, or an older backend)
render exactly as before. Boards are the real recorded states in
tests/strategic_states.py; nothing reads ~/.arenamcp.
"""

from __future__ import annotations

import html as _html
import re
import time
from copy import deepcopy
from typing import Any

import pytest

pytest.importorskip("PySide6")

from tests import strategic_states as S

from arenamcp import board_assessment as ba
from arenamcp import game_plan as gp
from arenamcp import opponent_tricks as ot
from arenamcp.desktop.compact_coach import _ROLE_TONES, CompactCoachPanel, _plan_step_text
from arenamcp.desktop.theme import block, span
from arenamcp.game_plan import GamePlanManager
from arenamcp.mcts_evaluator import MCTSEvaluator

FIXTURES = ["G1_T8", "G1_T12", "G1_T14", "G1_T15_FROM_OPPONENT", "G3_T10_BLOCKS"]
NEW_LABELS = ("Line", "Alt", "Opp")


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
    ba._CACHE.clear()
    gp._TRICK_CACHE.clear()


@pytest.fixture
def panel(qapp):
    p = CompactCoachPanel()
    p.show()
    yield p
    p.close()


def _label(text: str) -> str:
    return span(text, "muted", weight=700) + "&nbsp;&nbsp;"


def _legacy_card(plan: dict[str, Any]) -> str:
    """The plan card as ``_render_game_plan`` drew it before the line search (reference)."""
    facts = plan.get("facts") if isinstance(plan.get("facts"), dict) else {}
    role = str(facts.get("role") or plan.get("role") or "")
    steps = [s for s in plan.get("turn_plan") or [] if isinstance(s, dict)]
    lines = []
    if role:
        head = _label("Role") + span(role.upper(), _ROLE_TONES.get(role, "accent"), weight=700)
        reason = str(facts.get("role_reason") or plan.get("role_reason") or "")
        if reason:
            head += "&nbsp;" + span(f"— {reason}", "muted")
        lines.append(block(head, size="caption"))
    if facts:
        theirs, ours = facts.get("their_clock"), facts.get("our_clock")
        bits = [
            f"they kill you in {theirs}" if theirs else "no clock on you",
            f"you kill them in {ours}" if ours else "no clock on them",
        ]
        if facts.get("race"):
            bits.append(f"race {facts['race']}")
        lines.append(block(_label("Clocks") + span(" · ".join(bits)), size="caption"))
        flags = [str(f) for f in facts.get("flags") or [] if f]
        if flags:
            lines.append(block(span(" · ".join(flags[:2]), "bad", weight=600), size="caption"))
    if steps:
        turns = [f"T{s.get('turn')} {_plan_step_text(s)}" for s in steps[:3]]
    else:
        turns = [
            f"T{s.get('turn')} {' + '.join(s.get('casts') or []) or '—'}"
            for s in facts.get("lookahead") or []
            if isinstance(s, dict)
        ][:3]
    if turns:
        lines.append(block(_label("Next 3") + span(" → ".join(turns)), size="caption"))
    wins = [str(w) for w in plan.get("win_conditions") or [] if w]
    if wins:
        lines.append(block(_label("Win") + span(wins[0], "muted"), size="caption"))
    return "".join(lines)


def _blocks(html: str) -> list[str]:
    return [b + "</div>" for b in html.split("</div>") if b]


def _plain(html: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " "))


def _row(html: str, name: str) -> str | None:
    """The plain text of the card row labelled ``name`` (None when absent)."""
    rows = [_plain(b) for b in _blocks(html) if b.startswith("<div") and _label(name) in b]
    assert len(rows) <= 1
    return rows[0] if rows else None


def _facts(name: str) -> dict[str, Any]:
    assessment = ba.assess(deepcopy(getattr(S, name)))
    assert assessment is not None
    return assessment.as_payload()


def _legacy_facts(name: str) -> dict[str, Any]:
    facts = _facts(name)
    for key in ("lines", "posture", "search"):
        facts.pop(key)
    return facts


def _plan(facts: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"facts": facts, "role": facts["role"], "role_reason": facts["role_reason"], **extra}


def _with_turns(facts: dict[str, Any]) -> dict[str, Any]:
    return _plan(
        facts,
        turn_plan=[
            {"turn": 12, "label": "T", "land": "Island", "cast": ["Undulating Witness"], "attack": "none"},
            {"turn": 14, "label": "T+1", "land": "", "cast": ["Murmuring Volume"], "attack": "Witness"},
        ],
        win_conditions=["Archive Arbiter / Beast midrange beats"],
    )


def _render(panel: CompactCoachPanel, plan: dict[str, Any]) -> str:
    panel._on_game_plan_changed(plan)
    assert not panel.game_plan_label.isHidden()
    return panel.game_plan_label.text()


def _step(turn: int, life: int | None, **extra: Any) -> dict[str, Any]:
    step = {"turn": turn, "label": "T", "land": "", "casts": [], "modes": {}, "cycles": [], "attack": []}
    step.update({"held": [], "life_after": life, "opp_life_after": None, **extra})
    return step


def _line(outcome: str, *steps: dict[str, Any], now_life: int | None = None) -> dict[str, Any]:
    return {"summary": "", "outcome": outcome, "now_life": now_life, "value": 0.0, "steps": list(steps)}


def _synthetic(lines: list[dict[str, Any]], posture: str = "", **search: Any) -> dict[str, Any]:
    facts = _legacy_facts("G1_T12")
    facts.update(
        lines=lines, posture=posture, search={"nodes": 9, "bounded": False, "truncated": False, **search}
    )
    return _plan(facts)


# --- payloads without lines: exactly as before ------------------------------------


@pytest.mark.parametrize("name", FIXTURES)
@pytest.mark.parametrize("shape", [_plan, _with_turns])
def test_payloads_without_lines_render_exactly_as_before(panel, name, shape):
    plan = shape(_legacy_facts(name))
    assert _render(panel, plan) == _legacy_card(plan)
    # An unknown trick estimate adds nothing either.
    unknown = dict(plan, opp_interaction={"known": False, "ui_line": ""})
    assert _render(panel, unknown) == _legacy_card(plan)


def test_kill_switch_payloads_render_exactly_as_before(panel, monkeypatch):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    facts = _facts("G1_T12")
    assert facts["lines"] == [] and facts["posture"] == ""
    plan = _with_turns(facts)
    assert _render(panel, plan) == _legacy_card(plan)


@pytest.mark.parametrize("name", FIXTURES)
def test_line_rows_only_add_rows_between_the_flags_and_next_3(panel, name):
    facts = _facts(name)
    plan = _with_turns(facts)
    html = _render(
        panel, dict(plan, opp_interaction={"known": True, "ui_line": "Opp tricks ~9%: 2 open (U)"})
    )
    kept = [b for b in _blocks(html) if not any(_label(n) in b for n in NEW_LABELS)]
    assert "".join(kept) == _legacy_card(plan)
    blocks = _blocks(html)
    order = [
        i for i, b in enumerate(blocks) if any(_label(n) in b for n in ("Clocks", *NEW_LABELS, "Next 3"))
    ]
    labels = [next(n for n in ("Clocks", *NEW_LABELS, "Next 3") if _label(n) in blocks[i]) for i in order]
    expected = (
        ["Clocks"] + (["Line"] if facts["lines"] else []) + (["Alt"] if len(facts["lines"]) > 1 else [])
    )
    assert labels == expected + ["Opp", "Next 3"]


# --- the Line row ---------------------------------------------------------------


def test_line_row_shows_outcome_lives_with_their_attack_turns_and_posture(panel):
    best = _line("alive", _step(12, 7), _step(14, 5))
    html = _render(panel, _synthetic([best], posture="hold"))
    assert _row(html, "Line") == "Line  survive · 7 → 5 (T13, T15) · hold"
    # Default tone: only a dead line's outcome is coloured.
    expected = block(_label("Line") + span("survive") + span(" · 7 → 5 (T13, T15) · hold"), size="caption")
    assert expected in html
    assert _row(html, "Alt") is None


@pytest.mark.parametrize(
    "posture, shown", [("attack", " · attack"), ("hold", " · hold"), ("either", ""), ("", "")]
)
def test_posture_is_shown_only_when_it_says_attack_or_hold(panel, posture, shown):
    html = _render(panel, _synthetic([_line("alive", _step(8, 17), _step(10, 17))], posture=posture))
    assert _row(html, "Line") == f"Line  survive · 17 → 17 (T9, T11){shown}"


def test_dead_line_outcome_is_bad_toned_and_lives_floor_at_zero(panel):
    best = _line("dead", _step(14, 2), _step(16, -3))
    html = _render(panel, _synthetic([best], posture="hold"))
    assert _row(html, "Line") == "Line  dead T17 · 2 → 0 (T15, T17) · hold"
    assert span("dead T17", "bad") in html


def test_the_attack_under_way_comes_first(panel):
    best = _line("alive", _step(11, 16), _step(13, 12), now_life=20)
    html = _render(panel, _synthetic([best]))
    assert _row(html, "Line") == "Line  survive · 20 → 16 → 12 (T10, T12, T14)"


def test_a_winning_line_names_its_lethal_turn_once(panel):
    win = _line("win", _step(15, None, attack=["Cadet"], opp_life_after=-2))
    html = _render(panel, _synthetic([win], posture="lethal"))
    assert _row(html, "Line") == "Line  lethal T15"
    assert span("lethal T15", "bad") not in html


@pytest.mark.parametrize(
    "lines, search",
    [
        ([], {}),
        ([_line("alive", _step(12, 7))], {"truncated": True}),
        (["not a line"], {}),
    ],
)
def test_no_line_rows_without_usable_lines(panel, lines, search):
    html = _render(panel, _synthetic(lines, posture="hold", **search))
    assert _row(html, "Line") is None and _row(html, "Alt") is None


def test_real_g1_t14_best_line_is_dead_and_the_destroy_mode_alt_dies_first(panel):
    facts = _facts("G1_T14")
    best, alt = facts["lines"][0], facts["lines"][1]
    html = _render(panel, _plan(facts))
    dead_turn = re.search(r"dead on (T\d+)", best["summary"]).group(1)
    line_row = _row(html, "Line")
    assert line_row.startswith(f"Line  dead {dead_turn} · ")
    assert span(f"dead {dead_turn}", "bad") in html
    alt_turn = re.search(r"dead on (T\d+)", alt["summary"]).group(1)
    assert int(alt_turn[1:]) < int(dead_turn[1:])
    assert _row(html, "Alt").startswith("Alt  Island + Archive Arbiter (destroy")
    assert _row(html, "Alt").endswith(f" · dead {alt_turn}")


def test_real_g1_t12_line_row_reads_the_best_lines_lives(panel):
    facts = _facts("G1_T12")
    best = facts["lines"][0]
    lives = [s["life_after"] for s in best["steps"] if s["life_after"] is not None]
    turns = [s["turn"] + 1 for s in best["steps"] if s["life_after"] is not None]
    html = _render(panel, _plan(facts))
    chain = " → ".join(str(life) for life in lives)
    assert _row(html, "Line").startswith(f"Line  survive · {chain} ({', '.join(f'T{t}' for t in turns)})")
    # The best line is Island + Witness (WP3); the runner-up casts Murmuring Volume first.
    assert best["steps"][0]["land"] == "Island" and best["steps"][0]["casts"] == ["Undulating Witness"]
    assert _row(html, "Alt").startswith("Alt  Island + Murmuring Volume: ")


# --- the Alt row ----------------------------------------------------------------


def test_alt_row_is_muted_and_names_the_runner_up_t_step(panel):
    best = _line("alive", _step(12, 7), _step(14, 5))
    alt = _line(
        "alive",
        _step(
            12,
            4,
            land="Island",
            casts=["Murmuring Volume", "Archive Arbiter"],
            modes={"Archive Arbiter": "gain 4 life"},
        ),
        _step(14, 4, casts=["Undulating Witness"]),
    )
    html = _render(panel, _synthetic([best, alt]))
    text = "Island + Murmuring Volume + Archive Arbiter (gain 4 life): 4 → 4"
    assert _row(html, "Alt") == f"Alt  {text}"
    assert block(_label("Alt") + span(text, "muted"), size="caption") in html


def test_alt_row_shows_a_dead_or_winning_outcome_and_escapes_names(panel):
    best = _line("alive", _step(12, 7))
    alt = _line("dead", _step(12, -4, land="Island", casts=["<b>Arbiter & Co</b>"]))
    html = _render(panel, _synthetic([best, alt]))
    assert _row(html, "Alt") == "Alt  Island + <b>Arbiter & Co</b>: 0 · dead T13"
    assert "<b>Arbiter" not in html and "&lt;b&gt;Arbiter &amp; Co&lt;/b&gt;" in html
    win = _line("win", _step(15, 16, attack=["Cadet"], opp_life_after=1), _step(17, None, opp_life_after=-5))
    html = _render(panel, _synthetic([best, win]))
    assert _row(html, "Alt") == "Alt  attack with Cadet: 16 · lethal T17"


def test_alt_row_shortens_a_long_t_step(panel):
    names = [f"Very Long Creature Name {n}" for n in range(4)]
    alt = _line("alive", _step(12, 4, attack=names))
    html = _render(panel, _synthetic([_line("alive", _step(12, 7)), alt]))
    row = _row(html, "Alt")
    head = row[len("Alt  ") : row.index(": 4")]
    assert head.endswith("…") and len(head) <= 64


# --- the Opp row ----------------------------------------------------------------


def test_opp_row_shows_the_trick_estimate_only_when_known(panel):
    ui_line = "Opp tricks ~14%: 3 open (G R), 3 in hand · Test Pump (pump) 0.31"
    plan = _plan(_legacy_facts("G3_T10_BLOCKS"))
    html = _render(panel, dict(plan, opp_interaction={"known": True, "ui_line": ui_line}))
    text = "tricks ~14%: 3 open (G R), 3 in hand · Test Pump (pump) 0.31"
    assert _row(html, "Opp") == f"Opp  {text}"
    assert block(_label("Opp") + span(text, "muted"), size="caption") in html
    for unknown in ({"known": False, "ui_line": ui_line}, {"known": True, "ui_line": ""}, {}, "junk"):
        html = _render(panel, dict(plan, opp_interaction=unknown))
        assert _row(html, "Opp") is None and html == _legacy_card(plan)


def test_ui_payload_end_to_end(panel, tmp_path):
    """The game_plan event payload as GamePlanManager builds it reaches all three rows."""
    service = _offline_service(tmp_path / "tricks")
    pump = ot.TrickCard(grp_id=1, name="Test Pump", mana_cost="{1}{G}", mv=2, pips=("G",), kinds=("pump",))
    service.register(
        ot.TrickTable(
            set_code="FRA", prior={"RG": 1.0}, cards={1: pump}, copies={"RG": {1: 2.0}}, built_at=time.time()
        )
    )
    manager = GamePlanManager(None)
    manager.trick_service = service
    payload = manager.ui_payload(deepcopy(S.G3_T10_BLOCKS))
    assert payload["opp_interaction"]["known"] is True and len(payload["facts"]["lines"]) > 1
    html = _render(panel, dict(payload, source="autopilot"))
    assert _row(html, "Line").startswith("Line  survive · ")
    assert _row(html, "Alt").startswith("Alt  ")
    assert "Test Pump" in _row(html, "Opp")
    assert not _row(html, "Opp").startswith("Opp  Opp")


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
