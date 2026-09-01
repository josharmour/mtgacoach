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


def test_compact_coach_renders_on_game_state(panel):
    snap = make_snapshot()
    panel._on_game_state_changed(snap)
    html = panel.game_state_view.toHtml()
    assert "OPPONENT" in html
    assert "YOU" in html
    assert "Lightning Bolt" in html
    assert "Grizzly Bears" in html
    assert "Your Turn" in panel.turn_strip.text()


def test_compact_coach_debug_report_triggers(panel, monkeypatch):
    called = []
    monkeypatch.setattr(panel.session, "trigger_debug_report", lambda: called.append(True))

    panel.chat_input.setText("/report")
    panel.send_chat()
    assert len(called) == 1

    panel.chat_input.setText("/bug")
    panel.send_chat()
    assert len(called) == 2

    panel.chat_input.setText("/debug")
    panel.send_chat()
    assert len(called) == 3


def test_compact_coach_toolbar_buttons(panel):
    assert panel.ap_btn is not None
    assert panel.brain_stream_btn is not None
    assert panel.bug_report_btn is not None
    assert panel.voice_btn is not None
    assert panel.style_btn is not None
    assert panel.mute_btn is not None
