"""The deterministic outlook number and its sidebar rendering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arenamcp.outlook import outlook


def facts(**kw):
    base = dict(
        our_life=20,
        opp_life=20,
        board_advantage=0,
        card_advantage=0,
        our_clock=None,
        their_clock=None,
        lethal_now=False,
        lethal_next_turn=False,
        opp_lethal_on_board=False,
        dead_in=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_outlook_moves_with_life_board_clocks_and_lethal():
    assert outlook(None) == {}
    even = outlook(facts())
    assert (even["pct"], even["label"], even["tone"]) == (50, "even", "muted")
    ahead = outlook(facts(our_life=20, opp_life=12, board_advantage=2, our_clock=3))
    assert ahead["label"] in ("ahead", "winning") and ahead["pct"] > even["pct"]
    assert "we have a clock (3), they have none" in ahead["why"]
    behind = outlook(facts(our_life=9, opp_life=20, their_clock=2, board_advantage=-3))
    assert behind["label"] in ("behind", "losing") and behind["pct"] < 40
    assert outlook(facts(lethal_now=True, our_life=5, opp_life=20))["pct"] >= 95
    dying = outlook(facts(our_life=20, opp_life=5, dead_in=1))
    assert dying["pct"] <= 12 and "dead next attack" in dying["why"]
    assert outlook(facts(opp_lethal_on_board=True))["label"] == "losing"


def test_sidebar_renders_outlook_bar_and_ledger_rows():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.compact_coach import CompactCoachPanel, _outlook_html

    app = QApplication.instance() or QApplication([])
    panel = CompactCoachPanel()
    try:
        panel._on_game_plan_changed(
            {
                "role": "aggressor",
                "facts": {"role": "aggressor", "our_clock": 3, "their_clock": None, "race": "ahead"},
                "turn_plan": [{"turn": 7, "cast": ["Mindseeker Oculus"], "attack": "Void Extrapolator"}],
                "outlook": {"pct": 64, "label": "ahead", "tone": "good", "trend": [(5, 50), (7, 64)]},
                "ledger": {
                    "lessons": ["No damage dealt on our turns T3, T5: the pressure line is not working"],
                    "turns": ["T5 (our turn): did: x"],
                },
            }
        )
        html = panel.game_plan_label.text()
        assert "Outlook" in html and "64% · ahead" in html and "↑ 14 since T5" in html
        assert "▮▮▮▮▮▮" in html and "▯▯▯▯" in html
        assert "So far" in html and "No damage dealt on our turns T3, T5" in html
        assert "AGGRESSOR" in html
    finally:
        panel.close()
    plain = _outlook_html({"pct": 20, "label": "losing", "tone": "bad", "trend": [(3, 22)]})
    assert "20% · losing" in plain and "since" not in plain
    del app


def test_sidebar_shows_the_plan_status_row_first():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.compact_coach import CompactCoachPanel, _plan_status_html

    app = QApplication.instance() or QApplication([])
    panel = CompactCoachPanel()
    try:
        panel._on_game_plan_changed(
            {
                "facts": {"role": "aggressor"},
                "plan_status": {
                    "state": "none",
                    "attempts": 3,
                    "cancels": 3,
                    "last_outcome": "yielded to a decision",
                },
            }
        )
        html = panel.game_plan_label.text()
        assert "NO GAME PLAN" in html and "3 attempts, 3 cancelled, last: yielded to a decision" in html
        assert html.index("Plan") < html.index("Role")
        panel._on_game_plan_changed(
            {
                "plan_status": {
                    "state": "forming",
                    "inflight": True,
                    "inflight_s": 6,
                    "inflight_for_turn": 8,
                    "attempts": 1,
                }
            }
        )
        assert "forming… 6s for T8" in panel.game_plan_label.text()
    finally:
        panel.close()
    assert "formed T8" in _plan_status_html({"state": "ready", "turn_formed": 8, "age_turns": 1})
    assert "6 turns old" in _plan_status_html({"state": "ready", "turn_formed": 2, "age_turns": 6})
    del app
