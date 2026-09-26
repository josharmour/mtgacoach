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
    html = panel.game_state_view.text()
    assert "Opponent" in html
    assert "You" in html
    assert "Lightning Bolt" in html
    assert "Grizzly Bears" in html
    assert panel.turn_strip.text() == "Your turn · T3 · Main 1"


def test_board_lists_opponent_creatures_and_groups_lands(panel):
    snap = make_snapshot()
    snap["battlefield"] += [
        {"name": "Forest", "controller_seat_id": 2, "type_line": "Basic Land — Forest"},
        {"name": "Forest", "controller_seat_id": 2, "type_line": "Basic Land — Forest"},
        {"name": "Serra Angel", "controller_seat_id": 1, "owner_seat_id": 1,
         "type_line": "Creature — Angel", "power": 4, "toughness": 4},
    ]
    panel._on_game_state_changed(snap)
    html = panel.game_state_view.text()
    assert "Forest ×2" in html
    assert "Serra Angel 4/4" in html
    assert "1 card in hand" in html


def test_tactical_pill_renders_hint_without_fake_odds(panel):
    # The score is a life/power/hand heuristic, not a win chance (2026-09-24).
    panel._on_mcts_updated({
        'eval_source': 'Tactical Heuristic Lookahead', 'root_win_probability': .62,
        'best_action': 'Play Land: Island', 'branches': [{
            'action': 'Play Land: Island', 'score_provenance': 'heuristic_lookahead',
            'normalized_score': .62, 'value_delta': .04,
        }],
    })
    rendered = panel.mcts_pill_label.text()
    assert 'Heuristic hint' in rendered
    assert 'board favorable' in rendered
    assert '%' not in rendered
    assert 'Play Land: Island' in rendered


def test_controls_reflow_at_sidebar_width(panel, qapp):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QPushButton

    from arenamcp.desktop import theme

    # Measure with the real app stylesheet, not Qt's default 80px buttons.
    theme.apply_theme(qapp, theme.THEME_DARK)
    panel._on_game_state_changed(make_snapshot())
    panel._refresh_status_dots()
    panel.resize(240, 900)
    qapp.processEvents()

    assert panel.width() == 240
    buttons = [b for b in panel.findChildren(QPushButton) if b.isVisible()]
    assert panel.ap_btn in buttons and panel.ptt_btn in buttons and panel.more_btn in buttons
    assert panel.bug_report_btn in buttons and panel.restart_btn in buttons
    rects = [QRect(b.mapTo(panel, b.rect().topLeft()), b.size()) for b in buttons]
    for button, rect in zip(buttons, rects, strict=True):
        assert panel.rect().contains(rect)
        assert button.width() >= button.sizeHint().width()
    for index, rect in enumerate(rects):
        for other in rects[index + 1 :]:
            assert not rect.intersects(other)
    # At 240px the three primary controls wrap onto a second row.
    assert panel.stop_speech_btn.y() > panel.ap_btn.y()

    panel.resize(800, 900)
    qapp.processEvents()
    assert panel.stop_speech_btn.y() == panel.ap_btn.y()


def test_voice_style_settings_live_in_popover(panel, qapp):
    for button in (panel.voice_btn, panel.speed_btn, panel.style_btn, panel.verbosity_btn,
                   panel.mute_btn):
        assert button.parent() is panel.voice_style_popover
        assert not button.isVisible()
    panel.more_btn.click()
    qapp.processEvents()
    assert panel.voice_style_popover.isVisible()
    assert panel.voice_btn.isVisible()
    panel.voice_style_popover.hide()


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
    assert not hasattr(panel, "brain_stream_btn")
    assert panel.bug_report_btn is not None
    assert panel.restart_btn is not None
    assert panel.voice_btn is not None
    assert panel.style_btn is not None
    assert panel.mute_btn is not None


def test_compact_coach_bug_report_button_click(panel, monkeypatch):
    called = []
    monkeypatch.setattr(panel.session, "trigger_debug_report", lambda: called.append(True))
    panel.bug_report_btn.click()
    assert len(called) == 1


@pytest.mark.parametrize("mode", ["turn_advice", "conversation"])
@pytest.mark.parametrize("theme_name", ["dark", "light", "high-contrast"])
def test_recovery_buttons_always_visible_without_opening_a_menu(panel, qapp, mode, theme_name):
    from PySide6.QtCore import QRect

    from arenamcp.desktop import theme

    theme.apply_theme(qapp, theme_name)
    panel._on_game_state_changed(make_snapshot())
    panel._on_mode_changed(mode)
    panel._on_status_changed("AUTOPILOT", "PAUSED")
    panel.resize(240, 600)
    qapp.processEvents()

    assert not panel.voice_style_popover.isVisible()
    assert panel.size().width() == 240
    assert panel.size().height() == 600
    for button in (panel.bug_report_btn, panel.restart_btn):
        assert button.parent() is panel
        assert button.isVisible()
        assert button.isEnabled()
        rect = QRect(button.mapTo(panel, button.rect().topLeft()), button.size())
        assert panel.rect().contains(rect)
        assert button.width() >= button.sizeHint().width()


def test_restart_button_requests_full_app_restart(panel):
    requested = []
    panel.restart_requested.connect(lambda: requested.append(True))
    panel.restart_btn.click()
    assert requested == [True]


def test_compact_coach_bug_report_saved_updates_clipboard_and_ui(panel, tmp_path, qapp):
    report_file = tmp_path / "bug_20260901_120000.json"
    report_file.write_text("{}", encoding="utf-8")

    panel.session.bugReportSaved.emit(str(report_file), "")

    clipboard_text = qapp.clipboard().text()
    assert str(report_file) in clipboard_text or report_file.as_uri() in clipboard_text
    assert panel.bug_report_btn.text() == "Report saved — link copied"
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


def test_compact_coach_feed_is_chronological(panel):
    panel.append_log("First advice: Cast Lightning Bolt", role="spoken")
    panel.append_log("Second advice: Attack with Grizzly Bears", role="spoken")
    panel.append_log("Third advice: Pass the turn", role="spoken")

    plain_text = panel.log_view.toPlainText().strip()
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]

    # Same direction as the conversation transcript: newest at the bottom.
    assert lines[0].endswith("First advice: Cast Lightning Bolt")
    assert lines[1].endswith("Second advice: Attack with Grizzly Bears")
    assert lines[2].endswith("Third advice: Pass the turn")


def test_spoken_line_fills_now_card(panel):
    assert panel.now_text.property("empty") is True
    panel.session.spokenLine.emit("Bolt the attacker.")
    assert panel.now_text.text() == "Bolt the attacker."
    assert panel.now_text.property("empty") is False
    assert "Bolt the attacker." in panel.log_view.toPlainText()


def test_theme_switch_rerenders_feed_colours(panel, qapp):
    from arenamcp.desktop import theme

    panel.append_log("Something failed", role="error")
    theme.apply_theme(qapp, theme.THEME_LIGHT)
    assert theme.tokens().bad in panel.log_view.toHtml()
    theme.apply_theme(qapp, theme.THEME_DARK)
    assert theme.tokens().bad in panel.log_view.toHtml()


class _FakeSettings:
    def __init__(self, **data: Any) -> None:
        self.data = dict(data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self.data[key] = value


def test_source_chip_shows_the_android_phone_and_bridge_state(panel):
    panel.session.statusChanged.emit("DEVICE", "ANDROID:Pixel 10 Pro")
    assert "Android · Pixel 10 Pro" in panel.source_chip.text()
    assert "isn't connected" in panel.source_chip.toolTip()

    panel.session.statusChanged.emit("BRIDGE", "Connected (il2cpp-android)")
    assert "Android · Pixel 10 Pro" in panel.source_chip.text()
    assert "actions through the bridge" in panel.source_chip.toolTip()

    # The Mac client grabbed the bridge: the chip must not claim the phone has it.
    panel.session.statusChanged.emit("BRIDGE", "Connected (il2cpp-macos)")
    assert "held by MTGA on this computer" in panel.source_chip.toolTip()

    panel.session.statusChanged.emit("BRIDGE", "Disconnected")
    assert "isn't connected" in panel.source_chip.toolTip()

    panel.session.statusChanged.emit("DEVICE", "ANDROID_NONE")
    assert "no phone" in panel.source_chip.text()


def test_play_on_cycles_the_device_and_restarts_the_coach(panel):
    panel._settings = _FakeSettings(game_device="desktop")  # never touch the real settings file
    restarts: list[bool] = []
    panel.restart_requested.connect(lambda: restarts.append(True))
    panel._dot_values["DEVICE"] = "ANDROID:Old Phone"

    panel.device_btn.click()
    assert panel._settings.data["game_device"] == "android"
    assert panel.device_btn.text() == "Play on: Android phone"
    assert restarts == [True]
    assert "DEVICE" not in panel._dot_values  # the restarted coach reports afresh

    panel.device_btn.click()
    assert panel._settings.data["game_device"] == "desktop"
    assert panel.device_btn.text().startswith("Play on: This ")
    assert restarts == [True, True]
