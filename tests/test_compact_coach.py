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


def test_experimental_policy_display_does_not_invent_outcome_score(panel):
    panel._on_mcts_updated({
        'eval_source': 'MageZero UWTempo v2 — experimental, uncalibrated',
        'best_action': 'Pass', 'branches': [{
            'action': 'Pass', 'score_provenance': 'prior_only',
            'normalized_score': .9, 'prior_probability': .7,
            'value_delta': 0.0,
        }],
    })
    rendered = panel.mcts_pill_label.text()
    assert 'Policy weight 70%' in rendered
    assert 'outcome not evaluated' in rendered
    assert 'Experimental' in rendered
    assert '90%' not in rendered


def test_controls_reflow_at_sidebar_width(panel, qapp):
    from PySide6.QtWidgets import QPushButton

    panel._on_game_state_changed(make_snapshot())
    panel.voice_btn.setText("Voice: Shimmer")
    panel.style_btn.setText("Concise")
    panel._refresh_status_dots()
    panel.resize(240, 900)
    qapp.processEvents()

    assert panel.width() == 240
    buttons = panel.findChildren(QPushButton)
    for button in buttons:
        assert panel.rect().contains(button.geometry())
        assert button.width() >= button.sizeHint().width()
    for index, button in enumerate(buttons):
        for other in buttons[index + 1 :]:
            assert not button.geometry().intersects(other.geometry())
    assert panel.mute_btn.y() > panel.voice_btn.y()

    panel.resize(800, 900)
    qapp.processEvents()
    assert panel.mute_btn.y() == panel.voice_btn.y()


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


def test_compact_coach_bug_report_button_click(panel, monkeypatch):
    called = []
    monkeypatch.setattr(panel.session, "trigger_debug_report", lambda: called.append(True))
    panel.bug_report_btn.click()
    assert len(called) == 1


def test_compact_coach_bug_report_saved_updates_clipboard_and_ui(panel, tmp_path, qapp):
    report_file = tmp_path / "bug_20260901_120000.json"
    report_file.write_text("{}", encoding="utf-8")

    panel.session.bugReportSaved.emit(str(report_file), "")

    clipboard_text = qapp.clipboard().text()
    assert str(report_file) in clipboard_text or report_file.as_uri() in clipboard_text
    assert panel.bug_report_btn.text() == "🐞 Copied!"
    log_text = panel.log_view.toPlainText()
    assert "Bug report saved" in log_text


def test_compact_coach_voice_status_updates(panel):
    panel.session.statusChanged.emit("VOICE", "Sky")
    assert panel.voice_btn.text() == "Voice: Sky"

    panel.session.statusChanged.emit("VOICE_ID", "Bella")
    assert panel.voice_btn.text() == "Voice: Bella"

    panel.session.statusChanged.emit("VOICE", "Changed to: Alloy (saved)")
    assert panel.voice_btn.text() == "Voice: Alloy"

    panel.session.statusChanged.emit("VOICE", "TTS Voice: Nova")
    assert panel.voice_btn.text() == "Voice: Nova"


def test_compact_coach_log_view_newest_on_top(panel):
    panel.append_log("First advice: Cast Lightning Bolt", role="spoken")
    panel.append_log("Second advice: Attack with Grizzly Bears", role="spoken")
    panel.append_log("Third advice: Pass the turn", role="spoken")

    plain_text = panel.log_view.toPlainText().strip()
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]

    assert lines[0] == "Third advice: Pass the turn"
    assert lines[1] == "Second advice: Attack with Grizzly Bears"
    assert lines[2] == "First advice: Cast Lightning Bolt"
    assert panel.log_view.verticalScrollBar().value() == 0
