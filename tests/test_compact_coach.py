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


def test_saved_autopilot_report_has_visible_local_link(panel, tmp_path):
    path = tmp_path / "bug & capture.json"
    panel._on_autopilot_bug_status({"phase": "completed", "path": str(path)})
    assert not panel.autopilot_report_link.isHidden()
    assert "Open autopilot bug report" in panel.autopilot_report_link.text()
    assert "file:///" in panel.autopilot_report_link.text()
    assert panel.autopilot_report_link.toolTip() == str(path)


def test_board_lists_opponent_creatures_and_groups_lands(panel):
    snap = make_snapshot()
    snap["battlefield"] += [
        {"name": "Forest", "controller_seat_id": 2, "type_line": "Basic Land — Forest"},
        {"name": "Forest", "controller_seat_id": 2, "type_line": "Basic Land — Forest"},
        {
            "name": "Serra Angel",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "type_line": "Creature — Angel",
            "power": 4,
            "toughness": 4,
        },
    ]
    panel._on_game_state_changed(snap)
    html = panel.game_state_view.text()
    assert "Forest ×2" in html
    assert "Serra Angel 4/4" in html
    assert "1 card in hand" in html


def test_tactical_pill_renders_hint_without_fake_odds(panel):
    # The score is a life/power/hand heuristic, not a win chance (2026-09-24).
    panel._on_mcts_updated(
        {
            "eval_source": "Tactical Heuristic Lookahead",
            "root_win_probability": 0.62,
            "best_action": "Play Land: Island",
            "branches": [
                {
                    "action": "Play Land: Island",
                    "score_provenance": "heuristic_lookahead",
                    "normalized_score": 0.62,
                    "value_delta": 0.04,
                }
            ],
        }
    )
    rendered = panel.mcts_pill_label.text()
    assert "Heuristic hint" in rendered
    assert "board favorable" in rendered
    assert "%" not in rendered
    assert "Play Land: Island" in rendered


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
    assert panel.ap_btn in buttons and panel.autopilot_bug_btn in buttons and panel.more_btn in buttons
    assert panel.bug_report_btn in buttons and panel.restart_btn in buttons
    rects = [QRect(b.mapTo(panel, b.rect().topLeft()), b.size()) for b in buttons]
    for button, rect in zip(buttons, rects, strict=True):
        assert panel.rect().contains(rect)
        assert button.width() >= button.sizeHint().width()
    for index, rect in enumerate(rects):
        for other in rects[index + 1 :]:
            assert not rect.intersects(other)
    # At 240px the voice controls wrap onto a second row.
    assert panel.more_btn.y() > panel.ap_btn.y()

    panel.resize(800, 900)
    qapp.processEvents()
    assert panel.more_btn.y() == panel.ap_btn.y()


def test_voice_style_settings_live_in_popover(panel, qapp):
    for button in (panel.voice_btn, panel.speed_btn, panel.style_btn, panel.mute_btn):
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

    panel.bug_report_btn.click()
    assert called == [True]


def test_compact_coach_toolbar_buttons(panel):
    assert panel.ap_btn is not None
    assert not hasattr(panel, "brain_stream_btn")
    assert panel.bug_report_btn is not None
    assert panel.restart_btn is not None
    assert panel.reload_engine_btn is not None
    assert panel.voice_btn is not None
    assert panel.style_btn is not None
    assert panel.mute_btn is not None


def test_compact_coach_bug_report_button_click(panel, monkeypatch):
    called = []
    monkeypatch.setattr(panel.session, "trigger_debug_report", lambda: called.append(True))
    panel.bug_report_btn.click()
    assert len(called) == 1


@pytest.mark.parametrize("theme_name", ["dark", "light", "high-contrast"])
def test_recovery_buttons_always_visible_without_opening_a_menu(panel, qapp, theme_name):
    from PySide6.QtCore import QRect

    from arenamcp.desktop import theme

    theme.apply_theme(qapp, theme_name)
    panel._on_game_state_changed(make_snapshot())
    panel._on_status_changed("AUTOPILOT", "PAUSED")
    panel.resize(240, 600)
    qapp.processEvents()

    assert not panel.voice_style_popover.isVisible()
    assert panel.size().width() == 240
    assert panel.size().height() == 600
    for button in (panel.autopilot_bug_btn, panel.bug_report_btn, panel.reload_engine_btn, panel.restart_btn):
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


def test_reload_engine_button_is_distinct_from_full_app_restart(panel):
    requested = []
    panel.reload_engine_requested.connect(lambda: requested.append("engine"))
    panel.restart_requested.connect(lambda: requested.append("app"))
    panel.reload_engine_btn.click()
    assert requested == ["engine"]


def test_autopilot_bug_requests_capture_without_claiming_pause_before_ack(panel):
    requested = []
    panel.autopilot_bug_requested.connect(lambda: requested.append(True))
    panel.autopilot_bug_btn.click()
    assert requested == [True]
    assert "Capturing" in panel.autopilot_bug_btn.text()
    assert "paused" not in panel.autopilot_bug_btn.text()
    assert not panel.autopilot_bug_btn.isEnabled()
    panel._on_autopilot_bug_status(
        {"phase": "recording", "message": "Autopilot paused; recording your manual corrections."}
    )
    assert "paused" in panel.autopilot_bug_btn.text()
    assert "recording your manual corrections" in panel.log_view.toPlainText()
    panel._on_autopilot_bug_status({"phase": "completed", "message": "Autopilot bug saved."})
    assert panel.autopilot_bug_btn.text() == "Autopilot bug"
    assert panel.autopilot_bug_btn.isEnabled()


def test_autopilot_bug_capture_remains_available_when_model_startup_fails(panel):
    panel._on_startup_status({"phase": "error", "ready": False, "message": "Connection refused"})
    assert panel.autopilot_bug_btn.isEnabled()
    panel.autopilot_bug_btn.click()
    panel._on_autopilot_bug_status({"phase": "error", "message": "Capture unavailable"})
    assert panel.autopilot_bug_btn.isEnabled()
    assert "Capture unavailable" in panel.log_view.toPlainText()


def test_autopilot_bug_recording_event_reaches_panel(panel):
    panel.session._handle_process_event(
        {"type": "autopilot_bug_status", "phase": "recording", "message": "Recording corrections."}
    )
    assert panel.autopilot_bug_btn.text() == "Recording bug · paused"
    assert not panel.autopilot_bug_btn.isEnabled()


def test_advice_is_visible_without_narration_and_narration_does_not_duplicate_feed(panel):
    panel.session.adviceReceived.emit("Hold removal for the attacker.", "COACH")
    assert panel.now_text.text() == "Hold removal for the attacker."
    assert panel.log_view.toPlainText().count("Hold removal for the attacker.") == 1
    panel.session.spokenLine.emit("Hold removal for the attacker.")
    assert panel.log_view.toPlainText().count("Hold removal for the attacker.") == 1


def test_startup_pipe_events_reach_the_panel(panel):
    panel.session._handle_process_event(
        {
            "type": "startup_status",
            "phase": "initializing_client",
            "message": "Preparing LLM connection…",
            "ready": False,
            "elapsed_s": 12,
        }
    )
    assert not panel.startup_banner.isHidden()
    assert "Preparing LLM connection" in panel.startup_label.text()
    assert "12s" in panel.startup_label.text()
    panel.session._handle_process_event(
        {"type": "startup_status", "phase": "ready", "message": "Ready", "ready": True}
    )
    assert panel.startup_banner.isHidden()


def test_startup_banner_shows_elapsed_and_wait_reason_until_ready(panel, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("arenamcp.desktop.compact_coach.time.monotonic", lambda: now[0])
    panel._on_startup_status(
        {"phase": "initializing_client", "message": "Preparing LLM client…", "ready": False}
    )
    assert not panel.startup_banner.isHidden()
    assert "Preparing LLM client" in panel.startup_label.text()
    assert "Advice and autoplay are waiting" in panel.startup_label.text()
    assert "0s" in panel.startup_label.text()
    assert panel._startup_timer.isActive()

    # Model discovery/health and game-state updates cannot dismiss startup.
    panel._on_status_changed("MODEL", "GLM 5.3")
    panel._on_status_changed("ENGINE", "Connected")
    panel._on_game_state_changed(make_snapshot())
    assert not panel.startup_banner.isHidden()
    assert "not ready" in panel.model_chip.toolTip()
    now[0] = 116.0
    panel._render_startup_status()
    assert "16s" in panel.startup_label.text()
    assert "%" not in panel.startup_label.text()

    panel._on_startup_status({"phase": "ready", "message": "Ready", "ready": True})
    assert panel.startup_banner.isHidden()
    assert not panel._startup_timer.isActive()


def test_startup_phase_changes_preserve_elapsed_time_and_new_launch_resets(panel, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("arenamcp.desktop.compact_coach.time.monotonic", lambda: now[0])
    panel._on_startup_status({"phase": "starting", "ready": False})
    now[0] = 110.0
    panel._on_startup_status({"phase": "loading_cards", "ready": False})
    assert "10s" in panel.startup_label.text()
    now[0] = 112.0
    panel._on_startup_status({"phase": "checking_connection", "elapsed_s": 15.0, "ready": False})
    assert "15s" in panel.startup_label.text()
    now[0] = 114.0
    panel._render_startup_status()
    assert "17s" in panel.startup_label.text()
    panel._on_startup_status({"phase": "starting", "ready": False})
    assert "0s" in panel.startup_label.text()


def test_child_startup_clock_cannot_move_elapsed_backwards(panel, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("arenamcp.desktop.compact_coach.time.monotonic", lambda: now[0])
    panel._on_startup_status({"phase": "starting", "ready": False})
    now[0] = 117.0
    panel._on_startup_status({"phase": "starting", "elapsed_s": 0.0, "ready": False})
    assert "17s" in panel.startup_label.text()
    now[0] = 120.0
    panel._on_startup_status({"phase": "checking_connection", "elapsed_s": 3.0, "ready": False})
    assert "20s" in panel.startup_label.text()


def test_inconclusive_connection_check_keeps_coaching_available(panel):
    panel._on_startup_status(
        {
            "phase": "connection_warning",
            "ready": True,
            "message": "Gateway model listing is restricted; inference remains available.",
        }
    )
    assert not panel.startup_banner.isHidden()
    assert "Connection check incomplete" in panel.startup_label.text()
    assert "no reload required" in panel.startup_label.text()
    assert "waiting for startup" not in panel.startup_label.text()
    assert not panel._startup_timer.isActive()
    assert "next game decision" in panel.model_chip.toolTip()
    assert panel.autopilot_bug_btn.isEnabled()
    panel._on_startup_status({"phase": "ready", "ready": True})
    assert panel.startup_banner.isHidden()


def test_arena_connection_notice_is_separate_from_model_startup(panel):
    panel._on_startup_status({"phase": "ready", "ready": True})
    panel.session.statusChanged.emit("ARENA", "Connecting to Arena bridge…")
    assert not panel.arena_status_label.isHidden()
    assert "Connecting to Arena bridge" in panel.arena_status_label.text()
    assert panel.startup_banner.isHidden()
    panel.session.statusChanged.emit("ARENA", "")
    assert panel.arena_status_label.isHidden()


@pytest.mark.parametrize("phase", ["error", "stopped"])
def test_failed_startup_remains_visible_without_running_timer(panel, phase):
    panel._on_startup_status({"phase": phase, "message": "Connection unavailable <retry>", "ready": False})
    assert not panel.startup_banner.isHidden()
    assert "Connection unavailable &lt;retry&gt;" in panel.startup_label.text()
    assert "Reload engine" in panel.startup_label.text()
    assert not panel._startup_timer.isActive()


@pytest.mark.parametrize("theme_name", ["dark", "light", "high-contrast"])
def test_startup_banner_and_reload_controls_fit_sidebar(panel, qapp, theme_name):
    from PySide6.QtCore import QRect

    from arenamcp.desktop import theme

    theme.apply_theme(qapp, theme_name)
    panel._on_startup_status(
        {"phase": "resuming_match", "message": "Restoring the current match…", "ready": False}
    )
    panel.resize(240, 900)
    qapp.processEvents()
    assert panel.width() == 240
    for widget in (panel.startup_banner, panel.reload_engine_btn, panel.restart_btn):
        assert widget.isVisible()
        assert panel.rect().contains(QRect(widget.mapTo(panel, widget.rect().topLeft()), widget.size()))


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


def test_feed_follows_after_layout_and_resize(panel, qapp):
    panel.resize(450, 700)
    for i in range(70):
        panel.append_log(f"Action {i}: " + "A long wrapped strategic explanation. " * 5)
    qapp.processEvents()
    scrollbar = panel.log_view.verticalScrollBar()
    assert scrollbar.maximum() > 0
    assert scrollbar.value() == scrollbar.maximum()
    panel.resize(360, 600)
    qapp.processEvents()
    panel.append_log("Newest action: choose two Forests.")
    qapp.processEvents()
    assert scrollbar.value() == scrollbar.maximum()
    assert not panel.latest_feed_btn.isVisible()


def test_feed_preserves_manual_scroll_and_latest_resumes_following(panel, qapp):
    panel.resize(450, 700)
    for i in range(70):
        panel.append_log(f"Action {i}: a longer entry for the scrolling feed.")
    qapp.processEvents()
    scrollbar = panel.log_view.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    old_position = scrollbar.value()
    panel.append_log("A new action while reading history.")
    qapp.processEvents()
    assert scrollbar.value() == old_position
    assert panel.latest_feed_btn.isVisible()
    panel.latest_feed_btn.click()
    panel.append_log("Follow this newest action too.")
    qapp.processEvents()
    assert scrollbar.value() == scrollbar.maximum()
    assert not panel.latest_feed_btn.isVisible()


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


def test_draft_explanations_are_visible_without_debug_logging(panel):
    text = "Take Seer to trigger Saheeli. Still need: early creatures."
    panel._on_advice_received(text, "DRAFT")
    assert panel.now_text.text() == text
    assert text in panel.log_view.toPlainText()


def test_autopilot_action_reason_is_visible_without_speech(panel):
    text = "ActionsAvailable: Pass. Our five-mana creatures are not payable yet."
    panel._on_advice_received(text, "AUTOPILOT")
    assert panel.now_text.text() == text


def test_draft_speech_keeps_detailed_needs_and_alternatives_visible(panel):
    spoken = "Take Seer to trigger Saheeli."
    detailed = spoken + "\nStill need: early creatures.\nAlternative: Dragon is too expensive."
    panel._on_advice_received(detailed, "DRAFT")
    panel._on_spoken_line(spoken)
    assert panel.now_text.text() == detailed
    panel._on_spoken_line("Play your land.")
    assert panel.now_text.text() == "Play your land."


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


def test_auto_queue_button_reaches_engine_with_boolean_payload(panel, monkeypatch):
    import json
    from types import SimpleNamespace

    from arenamcp.pipe_adapter import PipeAdapter

    panel._settings = _FakeSettings(auto_queue_enabled=False)
    panel._auto_queue_enabled = False
    payloads, received = [], []
    monkeypatch.setattr(panel.session._process, "send_payload", payloads.append)
    panel.auto_queue_btn.click()
    panel.auto_queue_btn.click()
    assert payloads == [
        {"cmd": "set_auto_queue", "enabled": True},
        {"cmd": "set_auto_queue", "enabled": False},
    ]
    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._coach = SimpleNamespace(set_auto_queue=received.append)
    for payload in payloads:
        adapter._dispatch(json.loads(json.dumps(payload)))
    assert received == [True, False]
    assert panel._settings.data["auto_queue_enabled"] is False


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
