from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QLabel, QTextEdit

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


def _confirmation_request() -> dict[str, Any]:
    return {
        "type": "bug_report_confirmation_request",
        "request_id": "req-1",
        "destination": "https://github.com/josharmour/mtgacoach/issues",
        "title": "Bridge fallback on Cast",
        "body": "full report body",
        "report_path": "/tmp/bug_test.json",
    }


def test_bug_report_confirmation_dialog_shows_contents_and_confirms(panel, monkeypatch):
    confirmed: list[str] = []
    cancelled: list[str] = []
    monkeypatch.setattr(panel._session, "confirm_bug_report", confirmed.append)
    monkeypatch.setattr(panel._session, "cancel_bug_report", cancelled.append)

    seen: list[tuple[list[str], list[str]]] = []

    def fake_exec(dialog):
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        bodies = [view.toPlainText() for view in dialog.findChildren(QTextEdit)]
        seen.append((labels, bodies))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    panel._on_bug_report_confirmation_requested(_confirmation_request())

    assert confirmed == ["req-1"]
    assert cancelled == []
    labels, bodies = seen[0]
    assert any("https://github.com/josharmour/mtgacoach/issues" in text for text in labels)
    assert any("Bridge fallback on Cast" in text for text in labels)
    assert bodies == ["full report body"]


def test_bug_report_confirmation_dialog_cancel_keeps_local_report(panel, monkeypatch):
    confirmed: list[str] = []
    cancelled: list[str] = []
    monkeypatch.setattr(panel._session, "confirm_bug_report", confirmed.append)
    monkeypatch.setattr(panel._session, "cancel_bug_report", cancelled.append)
    monkeypatch.setattr(QDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)

    panel._on_bug_report_confirmation_requested(_confirmation_request())

    assert confirmed == []
    assert cancelled == ["req-1"]
    assert any("cancelled" in text.lower() for _, text, _ in panel._activity_history)


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
    panel.resize(320, 900)
    qapp.processEvents()

    assert panel.width() == 320
    buttons = [b for b in panel.findChildren(QPushButton) if b.isVisible()]
    assert panel.ap_btn in buttons and panel.more_btn in buttons
    assert panel.detail_buttons["settings"] in buttons
    assert panel.autopilot_bug_btn in buttons and panel.restart_btn in buttons
    rects = [QRect(b.mapTo(panel, b.rect().topLeft()), b.size()) for b in buttons]
    for button, rect in zip(buttons, rects, strict=True):
        assert panel.rect().contains(rect)
        if button in (panel.bug_report_btn, panel.reload_engine_btn, panel.restart_btn):
            assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 6
    for index, rect in enumerate(rects):
        for other in rects[index + 1 :]:
            assert not rect.intersects(other)
    # The two primary controls remain on the same fixed row.
    assert panel.more_btn.y() == panel.ap_btn.y()

    panel.resize(800, 900)
    qapp.processEvents()
    assert panel.more_btn.y() == panel.ap_btn.y()


def test_voice_style_settings_live_in_popover(panel, qapp):
    for button in (panel.voice_btn, panel.speed_btn, panel.style_btn, panel.mute_btn):
        assert button.parent() is panel.voice_style_popover
        assert not button.isVisible()
    panel.more_btn.click()
    qapp.processEvents()
    assert panel._speech_dialog.isVisible()
    assert panel._speech_dialog.windowTitle() == "Speech & voices"
    panel._speech_dialog.close()


def test_compact_coach_debug_report_triggers(panel, monkeypatch):
    called = []
    monkeypatch.setattr(panel.session, "trigger_debug_report", lambda: called.append(True))

    panel.bug_report_btn.click()
    assert called == [True]


@pytest.mark.parametrize("theme_name", ["dark", "light", "high-contrast"])
def test_recovery_buttons_always_accessible(panel, qapp, theme_name):
    from PySide6.QtCore import QRect

    from arenamcp.desktop import theme

    theme.apply_theme(qapp, theme_name)
    panel._on_game_state_changed(make_snapshot())
    panel._on_status_changed("AUTOPILOT", "PAUSED")
    assert panel.autopilot_bug_btn.isVisible()
    panel.resize(320, 1300)
    qapp.processEvents()

    assert not panel.voice_style_popover.isVisible()
    assert panel.size().width() == 320
    for button in (
        panel.autopilot_bug_btn,
        panel.bug_report_btn,
        panel.support_btn,
        panel.reload_engine_btn,
        panel.restart_btn,
    ):
        assert button.parent() is panel.primary_controls
        assert button.isVisible()
        assert button.isEnabled()
        rect = QRect(button.mapTo(panel, button.rect().topLeft()), button.size())
        assert panel.rect().contains(rect)
        if button in (panel.bug_report_btn, panel.reload_engine_btn, panel.restart_btn):
            assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 6


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


def test_markdown_advice_and_stripped_narration_do_not_duplicate_feed(panel):
    advice = (
        "**Put both Emrakuls onto the battlefield.** Tooth and Nail is entwined, so grab "
        "Emrakul, the Aeons Torn and Emrakul, the World Anew from your hand."
    )
    panel.session.adviceReceived.emit(advice, "Visual Analysis")
    panel.session.spokenLine.emit(
        "Put both Emrakuls onto the battlefield. Tooth and Nail is entwined, so grab "
        "Emrakul, the Aeons Torn and Emrakul, the World Anew from your hand."
    )
    assert panel.log_view.toPlainText().count("Put both Emrakuls onto the battlefield.") == 1
    panel.session.spokenLine.emit("Attack with the team.")
    assert "Attack with the team." in panel.log_view.toPlainText()


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


def test_arena_connection_notice_is_separate_from_model_startup(panel):
    panel._on_startup_status({"phase": "ready", "ready": True})
    panel.session.statusChanged.emit("ARENA", "Connecting to Arena bridge…")
    assert not panel.arena_status_label.isHidden()
    assert "Connecting to Arena bridge" in panel.arena_status_label.text()
    assert panel.startup_banner.isHidden()
    panel.session.statusChanged.emit("ARENA", "")
    assert panel.arena_status_label.isHidden()


def test_transient_backend_trouble_is_a_status_line_not_the_startup_card(panel):
    """#215: a mid-match stream stall shows 'Model slow, retrying…' as a small
    line; the red 'Startup needs attention' card stays hidden."""
    panel._on_startup_status({"phase": "ready", "message": "Ready", "ready": True})
    assert panel.startup_banner.isHidden()
    panel.session._handle_process_event(
        {
            "type": "backend_health",
            "state": "degraded",
            "detail": "stream stalled: no data for 4s after the first token",
            "data": {
                "state": "degraded",
                "detail": "stream stalled: no data for 4s after the first token",
            },
        }
    )
    assert not panel.backend_status_label.isHidden()
    assert "Model slow, retrying" in panel.backend_status_label.text()
    assert panel.startup_banner.isHidden()
    # Recovery takes the line back down.
    panel.session._handle_process_event({"type": "backend_health", "state": "ok", "data": {"state": "ok"}})
    assert panel.backend_status_label.isHidden()


def test_backend_down_mid_match_never_reopens_the_startup_card(panel):
    panel._on_startup_status({"phase": "ready", "ready": True})
    panel.session._handle_process_event(
        {
            "type": "backend_health",
            "state": "down",
            "detail": "circuit open; retry in 8s",
            "data": {"state": "down", "detail": "circuit open; retry in 8s"},
        }
    )
    assert not panel.backend_status_label.isHidden()
    assert "Model unavailable" in panel.backend_status_label.text()
    assert panel.startup_banner.isHidden()


def test_backend_auth_expired_shows_expired_notice_and_marks_model_chip_bad(panel):
    panel._on_startup_status({"phase": "ready", "ready": True})
    panel._on_status_changed("MODEL", "current")
    panel.session._handle_process_event(
        {
            "type": "backend_health",
            "state": "degraded",
            "detail": "Error code: 401 - {'error': {'message': 'Authentication Error, Key is expired'}}",
            "data": {
                "state": "degraded",
                "last_status_code": 401,
                "detail": "Error code: 401 - {'error': {'message': 'Authentication Error, Key is expired'}}",
            },
        }
    )
    assert not panel.backend_status_label.isHidden()
    assert "expired" in panel.backend_status_label.text().lower()
    assert "mtgacoach.com/subscribe" in panel.backend_status_label.text()
    assert "Model slow" not in panel.backend_status_label.text()
    assert "expired" in panel.model_chip.toolTip().lower()


def test_startup_card_owns_the_sidebar_while_it_is_up(panel):
    """During startup (or a real startup failure) the card wins; no double banner."""
    panel._on_startup_status({"phase": "starting", "message": "Starting coach…", "ready": False})
    panel._on_backend_health({"state": "degraded", "detail": "slow first token"})
    assert not panel.startup_banner.isHidden()
    assert panel.backend_status_label.isHidden()
    # Once startup completes, a still-degraded backend shows the line again.
    panel._on_startup_status({"phase": "ready", "ready": True})
    assert not panel.backend_status_label.isHidden()


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
    panel.resize(320, 900)
    qapp.processEvents()
    assert panel.width() == 320
    for widget in (panel.notice_button, panel.reload_engine_btn, panel.restart_btn):
        assert widget.isVisible()
        assert panel.rect().contains(QRect(widget.mapTo(panel, widget.rect().topLeft()), widget.size()))


def test_compact_coach_bug_report_copies_exact_local_link(panel, tmp_path, qapp, monkeypatch):
    from arenamcp import support

    report_file = tmp_path / "bug report #1.json"
    original = '{"prompt": "private-game-sentinel", "local_path": "/local/debug/path"}'
    report_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        support, "contact_josh", lambda *args: pytest.fail("Debug report must not open support review")
    )
    panel.resize(320, 650)
    qapp.processEvents()
    before = panel.size()
    panel.session.bugReportSaved.emit(str(report_file), "")
    qapp.processEvents()
    assert panel.size() == before
    assert qapp.clipboard().text() == report_file.resolve().as_uri()
    assert report_file.read_text() == original
    assert panel.bug_report_btn.text() == "Saved"
    assert "Bug report saved — link copied" in panel.log_view.toPlainText()
    assert report_file.resolve().as_uri() in panel.log_view.toPlainText()


def test_failed_debug_report_preserves_clipboard(panel, qapp):
    qapp.clipboard().setText("existing-clipboard")
    panel.session.bugReportSaved.emit("", "capture failed")
    assert qapp.clipboard().text() == "existing-clipboard"
    assert "Failed to save bug report: capture failed" in panel.log_view.toPlainText()


@pytest.mark.parametrize("surface", ["compact", "tools"])
def test_debug_report_generates_file_and_copies_link(qapp, tmp_path, monkeypatch, surface):
    import json

    from arenamcp import support
    from arenamcp.desktop.main_window import MainWindow

    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path / "debug reports")
    monkeypatch.setattr(
        support, "contact_josh", lambda *args: pytest.fail("Debug report must not open support review")
    )
    win = MainWindow()
    try:
        # Avoid capturing the user's screen, while preserving screenshot references in the report.
        screenshots = {"arena": str(tmp_path / "Arena screenshot.png")}
        monkeypatch.setattr(win._session, "capture_screenshots", lambda: screenshots)
        win._session._last_game_state = {"match_id": "local-debug-match"}
        if surface == "compact":
            win.coach_panel.bug_report_btn.click()
        else:
            from PySide6.QtGui import QAction

            next(a for a in win.findChildren(QAction) if a.text().startswith("Report a Bug")).trigger()
        reports = list((tmp_path / "debug reports" / "bug_reports").glob("bug_*.json"))
        assert len(reports) == 1
        data = json.loads(reports[0].read_text())
        assert data["reason"] == "Desktop Bug Report (offline)"
        assert data["game_state"] == {"match_id": "local-debug-match"}
        assert data["screenshots"] == screenshots
        assert qapp.clipboard().text() == reports[0].resolve().as_uri()
    finally:
        win.close()


def test_compact_coach_voice_status_updates(panel):
    panel.session.statusChanged.emit("VOICE", "Sky")
    assert panel.voice_btn.text() == "Voice: Sky"

    panel.session.statusChanged.emit("VOICE_ID", "Bella")
    assert panel.voice_btn.text() == "Voice: Bella"

    panel.session.statusChanged.emit("VOICE", "Changed to: Alloy (saved)")
    assert panel.voice_btn.text() == "Voice: Alloy"

    panel.session.statusChanged.emit("VOICE", "TTS Voice: Nova")
    assert panel.voice_btn.text() == "Voice: Nova"


def test_activity_shows_newest_first_after_resize(panel, qapp):
    panel.resize(450, 700)
    for i in range(70):
        panel.append_log(f"Action {i}: " + "A long strategic explanation. " * 5)
    panel._show_detail("activity")
    qapp.processEvents()
    assert panel.detail_pager.browser.toPlainText().find(
        "Action 69:"
    ) < panel.detail_pager.browser.toPlainText().find("Action 0:")
    panel.resize(360, 650)
    panel.append_log("Newest action: choose two Forests.")
    qapp.processEvents()
    assert "Newest action" in panel.detail_pager.browser.toPlainText().splitlines()[0]
    assert panel.detail_pager.page == 0
    assert not panel.detail_pager.browser.verticalScrollBar().isVisible()


def test_activity_keeps_selected_page_during_updates(panel, qapp):
    panel.resize(450, 700)
    for i in range(70):
        panel.append_log(f"Action {i}: a longer entry for the paged feed.")
    panel._show_detail("activity")
    qapp.processEvents()
    panel.detail_pager.next.click()
    assert panel.detail_pager.page == 1
    panel.append_log("A new action while reading history.")
    qapp.processEvents()
    assert panel.detail_pager.page == 1
    panel.detail_pager.previous.click()
    assert panel.detail_pager.page == 0


def test_compact_coach_feed_is_chronological(panel):
    panel.append_log("First advice: Cast Lightning Bolt", role="spoken")
    panel.append_log("Second advice: Attack with Grizzly Bears", role="spoken")
    panel.append_log("Third advice: Pass the turn", role="spoken")

    plain_text = panel.log_view.toPlainText().strip()
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]

    # Chronological: newest at the bottom.
    assert lines[0].endswith("First advice: Cast Lightning Bolt")
    assert lines[1].endswith("Second advice: Attack with Grizzly Bears")
    assert lines[2].endswith("Third advice: Pass the turn")


def test_spoken_line_fills_now_card(panel):
    before = panel.now_text.height()
    panel.session.spokenLine.emit("Bolt the attacker.")
    assert panel.now_text.text() == "Bolt the attacker."
    assert panel.now_text.height() == before
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


def test_auto_queue_is_opt_in_and_each_click_reaches_the_engine(panel, monkeypatch):
    import json
    from types import SimpleNamespace

    from arenamcp import settings
    from arenamcp.pipe_adapter import PipeAdapter

    assert panel.auto_queue_btn.text() == "Auto-queue: Off"
    assert panel.auto_queue_status_label.isHidden()
    payloads, received = [], []
    monkeypatch.setattr(panel.session._process, "send_payload", payloads.append)

    panel.auto_queue_btn.click()
    assert panel.auto_queue_btn.text() == "Auto-queue: On"
    assert not panel.auto_queue_status_label.isHidden()
    assert settings.get_settings().get("auto_queue_enabled") is True
    panel.auto_queue_btn.click()
    assert settings.get_settings().get("auto_queue_enabled") is False
    assert payloads == [
        {"cmd": "set_auto_queue", "enabled": True},
        {"cmd": "set_auto_queue", "enabled": False},
    ]
    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._coach = SimpleNamespace(set_auto_queue=received.append)
    for payload in payloads:
        adapter._dispatch(json.loads(json.dumps(payload)))
    assert received == [True, False]


def test_saved_auto_queue_choice_is_shown_but_not_sent_at_startup(qapp, monkeypatch):
    from arenamcp import settings
    from arenamcp.desktop.coach_session import CoachSession

    settings.get_settings().set("auto_queue_enabled", True)
    sent: list[bool] = []
    monkeypatch.setattr(CoachSession, "set_auto_queue", lambda self, enabled: sent.append(enabled))
    restored = CompactCoachPanel()
    try:
        assert restored.auto_queue_btn.text() == "Auto-queue: On"
        restored.session.started.emit()
        assert sent == []
    finally:
        restored.close()


def test_auto_queue_status_detail_never_toggles_queueing_or_autoplay(panel, monkeypatch):
    sent: list = []
    monkeypatch.setattr(panel.session._process, "send_payload", sent.append)
    monkeypatch.setattr(panel.session._process, "send_command", lambda *a: sent.append(a))

    panel.session.statusChanged.emit("AUTO_QUEUE", "ON")
    panel.session.statusChanged.emit("AUTO_QUEUE_DETAIL", "Paused: turn on Autoplay to continue.")
    assert panel.auto_queue_btn.text() == "Auto-queue: On"
    assert "turn on Autoplay" in panel.auto_queue_status_label.text()
    assert panel.ap_btn.text() == "Enable autoplay"
    panel.session.statusChanged.emit("AUTO_QUEUE_DETAIL", "Continuing the result screen.")
    assert "result screen" in panel.auto_queue_status_label.text()
    panel.session.statusChanged.emit("AUTO_QUEUE", "OFF")
    assert panel.auto_queue_btn.text() == "Auto-queue: Off"
    assert panel.auto_queue_status_label.isHidden()
    assert sent == []


def test_stop_button_silences_the_desktop_voice_and_tells_the_engine(panel, monkeypatch):
    stops: list[bool] = []
    commands: list[str] = []
    monkeypatch.setattr(panel.session._tts, "stop_speech", lambda: stops.append(True))
    monkeypatch.setattr(panel.session._process, "send_command", lambda cmd, *a: commands.append(cmd))

    panel.stop_speech_btn.click()

    assert stops == [True]
    assert commands == ["stop_speech"]


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
