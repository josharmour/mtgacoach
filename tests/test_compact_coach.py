from __future__ import annotations

from typing import Any
import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.compact_coach import CompactCoachPanel


def make_snapshot(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "match_id": "match-1",
        "local_seat_id": 2,
        "turn": {
            "turn_number": 3,
            "active_player": 2,
            "priority_player": 2,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {
                "seat_id": 1,
                "life_total": 14,
                "is_local": False,
            },
            {
                "seat_id": 2,
                "life_total": 20,
                "is_local": True,
            },
        ],
        "battlefield": [
            {
                "instance_id": 101,
                "name": "Grizzly Bears",
                "controller_seat_id": 2,
                "type_line": "Creature — Bear",
                "power": 2,
                "toughness": 2,
                "is_tapped": False,
            }
        ],
        "hand": [
            {
                "instance_id": 201,
                "name": "Lightning Bolt",
                "mana_cost": "{R}",
                "type_line": "Instant",
                "oracle_text": "deals 3 damage to any target",
            }
        ],
    }
    state.update(overrides)
    return state


@pytest.fixture
def panel(qapp):
    p = CompactCoachPanel()
    p.show()
    yield p
    p.close()


def test_compact_coach_mcts_tree_renders_on_game_state(panel):
    snap = make_snapshot()
    panel._update_game_state(snap)
    html = panel.mcts_view.toHtml()
    assert "WIN EXPECTANCY" in html
    assert "sims" in html
    assert "MCTS" in panel.mcts_toggle_btn.text()


def test_compact_coach_mcts_tree_event(panel):
    mcts_payload = {
        "root_win_probability": 0.72,
        "total_simulations": 1000,
        "turn_number": 4,
        "phase": "Main1",
        "best_action": "Cast: Lightning Bolt",
        "branches": [
            {
                "action": "Cast: Lightning Bolt",
                "mana_cost": "{R}",
                "win_probability": 0.75,
                "value_delta": 0.03,
                "tag": "⭐ BEST LINE",
                "outcome_summary": "Removes top opponent threat; swings board power delta",
            }
        ],
    }
    panel._handle_event({"type": "mcts_tree", "data": mcts_payload})
    html = panel.mcts_view.toHtml()
    assert "72%" in html
    assert "Cast: Lightning Bolt" in html
    assert "BEST LINE" in html
    assert "72% WIN" in panel.mcts_toggle_btn.text()


def test_compact_coach_mcts_toggle_collapse(panel):
    assert panel._mcts_expanded is True
    assert panel.mcts_view.isVisible() is True
    panel._toggle_mcts_expanded()
    assert panel._mcts_expanded is False
    assert panel.mcts_view.isVisible() is False


def test_compact_coach_debug_report_triggers(panel, monkeypatch):
    called = []
    monkeypatch.setattr(panel, "_submit_debug_report", lambda: called.append(True))

    # Test slash commands
    panel.chat_input.setText("/report")
    panel.send_chat()
    assert len(called) == 1

    panel.chat_input.setText("/bug")
    panel.send_chat()
    assert len(called) == 2

    panel.chat_input.setText("/debug")
    panel.send_chat()
    assert len(called) == 3


def test_compact_coach_overflow_has_debug_report(panel):
    more_btn = panel._build_overflow_button()
    menu = more_btn.menu()
    actions = [a.text() for a in menu.actions()]
    assert "Debug Report" in actions
