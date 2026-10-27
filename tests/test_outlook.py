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
        assert "Outlook" in html and "ahead" in html and "improving since T5" in html and "64%" not in html
        assert "▮▮▮▮▮▮" in html and "▯▯▯▯" in html
        assert "So far" in html and "No damage dealt on our turns T3, T5" in html
        assert "AGGRESSOR" in html
    finally:
        panel.close()
    plain = _outlook_html({"pct": 20, "label": "losing", "tone": "bad", "trend": [(3, 22)]})
    assert "losing" in plain and "20%" not in plain and "since" not in plain
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


def test_clock_urgency_and_lethal_precedence():
    scores = [outlook(facts(their_clock=n))["pct"] for n in (3, 7, 20)]
    assert scores[0] < scores[1] < scores[2]
    scores = [outlook(facts(our_clock=n))["pct"] for n in (3, 7, 20)]
    assert scores[0] > scores[1] > scores[2]
    result = outlook(facts(lethal_now=True, opp_lethal_on_board=True, dead_in=1))
    assert result["pct"] >= 95 and result["label"] == "lethal available"
    assert "lethal available" in result["why"][0]


def test_future_or_failed_line_is_not_immediate_lethal():
    for our_turn, win_at in ((False, 1), (True, 2)):
        result = outlook(facts(our_turn=our_turn, lines=[SimpleNamespace(outcome="win", win_at=win_at)]))
        assert result["pct"] == 80 and result["label"] != "lethal available"
    result = outlook(facts(lines=[SimpleNamespace(outcome="alive", win_at=None)], dead_in=1))
    assert result["pct"] <= 12


def burn_state():
    return {
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "players": [{"seat_id": 1, "life_total": 20}, {"seat_id": 2, "life_total": 2}],
        "turn": {"turn_number": 11, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "battlefield": [
            {
                "instance_id": 10,
                "grp_id": 10,
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "oracle_text": "{T}: Add {R}.",
                "controller_seat_id": 1,
                "is_tapped": False,
            }
        ],
        "hand": [
            {
                "instance_id": 11,
                "grp_id": 11,
                "name": "Boltwave",
                "type_line": "Sorcery",
                "oracle_text": "Boltwave deals 3 damage to each opponent.",
                "mana_cost": "{R}",
                "cmc": 1,
                "controller_seat_id": 1,
            }
        ],
    }


def test_castable_burn_lethal_drives_both_role_and_outlook(unlimited_search_deadline):
    from arenamcp.board_assessment import assess

    assessment = assess(burn_state())
    assert not assessment.lethal_now  # Creature-only clock has no attack.
    assert assessment.role == "aggressor"
    assert any("LETHAL LINE" in flag for flag in assessment.flags)
    result = outlook(assessment)
    assert result["pct"] >= 95 and result["label"] == "lethal available"


def test_empty_board_missing_deck_does_not_assert_defender(unlimited_search_deadline):
    from arenamcp.board_assessment import assess

    state = burn_state()
    state["hand"] = []
    assessment = assess(state)
    assert assessment.role == "undetermined"
    assert "deck curve unavailable" in assessment.role_reason
    assert not assessment.survival_mode
    state["deck_cards"] = [11]
    state["deck_catalog"] = {11: {"type_line": "Sorcery", "cmc": 1}}
    assert assess(state).role == "aggressor"
