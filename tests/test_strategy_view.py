"""Forecast trust boundaries and native UI behavior for the in-game companion."""

from copy import deepcopy

import pytest

from arenamcp.forecast_identity import forecast_key
from arenamcp.plan_card import card_payload

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, QVariantAnimation
from tests.test_compact_coach import make_snapshot

from arenamcp.desktop import theme
from arenamcp.desktop.compact_coach import CompactCoachPanel
from arenamcp.desktop.strategy_model import decision_heading, step_text
from arenamcp.desktop.strategy_view import StrategyView


def forecast(state):
    turn = state["turn"]["turn_number"]
    return {
        "snapshot_key": forecast_key(state),
        "plan_status": {"state": "ready", "turn_formed": 1, "age_turns": turn - 1},
        "turn_plan": [{"turn": 1, "land": "Forest"}],
        "facts": {
            "role": "aggressor",
            "search": {},
            "lines": [
                {
                    "outcome": "alive",
                    "steps": [
                        {"turn": turn, "attack": ["Grizzly Bears"], "life_after": 20, "opp_life_after": 12},
                        {"turn": turn + 2, "casts": ["Hill Giant"], "life_after": 18, "opp_life_after": 10},
                        {"turn": turn + 4, "attack": ["Hill Giant"], "life_after": 16, "opp_life_after": 7},
                    ],
                },
                {"outcome": "dead", "steps": [{"turn": turn, "held": ["Grizzly Bears"], "life_after": 0}]},
            ],
        },
        "outlook": {"pct": 65, "label": "ahead", "tone": "good", "why": ["board +2"]},
    }


@pytest.fixture
def view(qapp):
    widget = StrategyView()
    widget.resize(400, 800)
    widget.show()
    yield widget
    widget.close()
    widget.deleteLater()


def test_forecast_identity_ignores_clock_ticks_but_tracks_decision_changes():
    state = make_snapshot()
    before = forecast_key(state)
    state.update(timestamp=999, heartbeat=123)
    state["turn"]["timer_remaining"] = 15
    assert forecast_key(state) == before
    state["battlefield"][0]["power"] = 7
    assert forecast_key(state) != before
    state = make_snapshot()
    state["turn"]["priority_player"] = 1
    assert forecast_key(state) != before
    state = make_snapshot(game_number=2)
    assert forecast_key(state) != before


def test_publisher_tags_the_exact_assessed_snapshot():
    class Manager:
        def ui_payload(self, state):
            return {"facts": {"our_life": state["players"][1]["life_total"]}}

    state = make_snapshot()
    payload = card_payload(Manager(), state, source="coach")
    assert payload["snapshot_key"] == forecast_key(state)
    assert payload["facts"]["our_life"] == 20


def test_board_change_withdraws_glow_and_outlook_before_replacement(view):
    state = make_snapshot()
    plan = forecast(state)
    view.set_data(plan, state)
    assert view.leading and view.scale.value == 65
    changed = deepcopy(state)
    changed["battlefield"][0]["power"] = 9
    view.set_data(plan, changed)
    assert not view.leading and view.scale.value is None
    assert "Reassessing" in view.verdict.text()
    assert not view.path_button.leading
    # A delayed old result cannot re-enable the green leader.
    view.set_data(deepcopy(plan), changed)
    assert not view.leading
    fresh = forecast(changed)
    view.set_data(fresh, changed)
    assert view.leading


def test_game_transition_and_untagged_engine_payload_are_not_current(view):
    state = make_snapshot()
    plan = forecast(state)
    view.set_data(plan, make_snapshot(match_id="next-match"))
    assert not view.leading
    plan.pop("snapshot_key")
    view.set_data(plan, state)
    assert not view.leading and view.scale.value is None
    view.set_data(forecast(state), {})
    assert not view.leading


@pytest.mark.parametrize("terminal", [{"game_over": True}, {"result": "win"}])
def test_terminal_position_never_shows_a_current_recommendation(view, terminal):
    state = make_snapshot(**terminal)
    view.set_data(forecast(state), state)
    assert not view.leading
    assert view.scale.value is None


@pytest.mark.parametrize("condition", ["truncated", "unknown", "unsupported"])
def test_incomplete_or_unsupported_search_never_claims_a_supported_leader(view, condition):
    state = make_snapshot()
    plan = forecast(state)
    if condition == "truncated":
        plan["facts"]["search"]["truncated"] = True
    elif condition == "unknown":
        plan["facts"]["lines"][0]["outcome"] = "unknown"
    else:
        plan["facts"]["lines"][0]["unsupported"] = ["unmodeled trigger"]
    view.set_data(plan, state)
    assert not view.leading


def test_leading_losing_line_still_says_projected_loss(view):
    state = make_snapshot()
    plan = forecast(state)
    plan["facts"]["lines"][0]["outcome"] = "dead"
    view.set_data(plan, state)
    assert view.leading
    assert "Projected loss" in view.detail_html()
    assert "win not established" not in view.detail_html()


def test_inspection_preserves_recommendation_and_displays_both_life_totals(view):
    state = make_snapshot()
    view.set_data(forecast(state), state)
    turn = state["turn"]["turn_number"]
    assert f"T{turn} · Opponent life after your attack: 12" in view.detail_html()
    assert f"T{turn + 1} · Your life after their attack: 20" in view.detail_html()
    view.inspect(1)
    assert view.selected == 1
    assert view.path_button.leading
    assert "Projected loss" in view.detail_html()
    assert f"T{turn + 1} · Your life after their attack: 0" in view.detail_html()


def test_elapsed_first_action_is_not_rebranded_as_a_future_line(view):
    state = make_snapshot()
    plan = forecast(state)
    plan["facts"]["lines"][0]["steps"][0]["turn"] = 1
    view.set_data(plan, state)
    assert view.lines == []
    assert not view.leading


def test_priority_is_distinct_from_active_turn():
    state = make_snapshot()
    state["turn"].update(active_player=1, priority_player=2)
    state["pending_decision"] = "declare_blockers"
    heading, detail = decision_heading(state, forecast(state), "ON")
    assert heading == "Choose blockers"
    assert detail == "Your response · Autoplay is playing"
    state["turn"]["priority_player"] = 1
    heading, detail = decision_heading(state, forecast(state))
    assert heading == "Waiting on opponent"
    assert "Opponent has priority" in detail
    state["turn"].pop("priority_player")
    heading, detail = decision_heading(state, forecast(state))
    assert "Priority unknown" in detail


def test_searched_spell_target_is_visible_in_the_recommendation():
    assert (
        step_text(
            {
                "casts": ["Lightning Bolt"],
                "targets": {"Lightning Bolt": "Opponent"},
            }
        )
        == "Cast Lightning Bolt → Opponent"
    )


def test_motion_can_be_disabled_and_stale_balance_resets_immediately(view, qapp):
    state = make_snapshot()
    view.set_data(forecast(state), state)
    view.scale.set_motion(False)
    view.scale.set_value(20)
    assert view.scale.angle < 0
    assert view.scale.animation.state() == QVariantAnimation.Stopped
    view.scale.set_motion(True)
    view.scale.set_value(None)
    assert view.scale.angle == 0
    assert view.scale.animation.state() == QVariantAnimation.Stopped
    theme.apply_theme(qapp, theme.THEME_HIGH_CONTRAST)
    view.scale.set_value(80)
    assert view.scale.animation.state() == QVariantAnimation.Stopped


def test_completed_execution_does_not_replace_next_decision(qapp):
    panel = CompactCoachPanel()
    state = make_snapshot()
    panel._on_game_state_changed(state)
    panel._on_game_plan_changed(forecast(state))
    before = panel.now_text.text()
    panel._on_spoken_line("Plan complete (1 actions)")
    panel._on_log_emitted("[1/1] declare_attackers | attackers: Grizzly Bears", "status")
    assert panel.now_text.text() == before
    assert "Plan complete" not in panel.log_view.toPlainText()
    assert "declare_attackers" not in panel.log_view.toPlainText()
    assert "T1 Forest" not in panel.game_plan_label.text()
    panel.close()


def test_late_old_result_cannot_displace_a_current_assessment(qapp):
    panel = CompactCoachPanel()
    state = make_snapshot()
    old = forecast(state)
    panel._on_game_state_changed(state)
    state = deepcopy(state)
    state["players"][0]["life_total"] = 6
    panel._on_game_state_changed(state)
    current = forecast(state)
    panel._on_game_plan_changed(current)
    panel._on_game_plan_changed(old)
    assert panel._game_plan == current
    assert panel.strategy_view.current
    panel.close()


def test_recorded_board_assessment_reaches_native_view(view):
    from tests.strategic_states import G1_T8

    from arenamcp.board_assessment import assess
    from arenamcp.outlook import outlook

    state = deepcopy(G1_T8)
    assessment = assess(state)
    view.set_data(
        {
            "snapshot_key": forecast_key(state),
            "facts": assessment.as_payload(),
            "outlook": outlook(assessment),
        },
        state,
    )
    assert view.current
    assert view.scale.value == outlook(assessment)["pct"]
    assert len(view.lines) == len(assessment.lines[:3])


@pytest.mark.parametrize("width,height", [(320, 900), (320, 650), (420, 720), (690, 900)])
@pytest.mark.parametrize("palette", ["dark", "light", "high-contrast"])
def test_companion_fits_without_scroll_or_overlapping_controls(qapp, width, height, palette):
    from PySide6.QtWidgets import QPushButton, QScrollArea

    theme.apply_theme(qapp, palette)
    panel = CompactCoachPanel()
    panel.resize(width, height)
    panel.show()
    state = make_snapshot()
    panel._on_game_state_changed(state)
    panel._on_game_plan_changed(forecast(state))
    qapp.processEvents()
    try:
        assert panel.size().width() == width
        assert panel.size().height() == height
        assert not panel.findChildren(QScrollArea)
        assert panel.strategy_view.scale.height() <= 36
        assert panel.strategy_view.height() <= 85
        assert panel.rank_strip.isVisible()
        controls = [w for w in panel.findChildren(QPushButton) if w.isVisible() and w.window() == panel]
        rects = [w.rect().translated(w.mapTo(panel, QPoint())) for w in controls]
        for control, rect in zip(controls, rects, strict=True):
            assert panel.rect().contains(rect), control.text()
        for i, rect in enumerate(rects):
            assert not any(rect.intersects(other) for other in rects[i + 1 :])
        for button in (panel.bug_report_btn, panel.reload_engine_btn, panel.restart_btn):
            assert button.isVisible()
            assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 6
    finally:
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize("height", [650, 720, 900])
def test_real_window_does_not_jump_when_assessments_or_status_change(qapp, monkeypatch, height):
    from types import SimpleNamespace

    from arenamcp.desktop import main_window

    monkeypatch.setattr(main_window.MainWindow, "_start_session", lambda self: None)
    monkeypatch.setattr(main_window, "WatchdogPingBridge", None)
    screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1920, 1040))
    monkeypatch.setattr(main_window.QGuiApplication, "screenAt", lambda point: screen)
    monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", "/test/dev")
    monkeypatch.setenv("MTGACOACH_DEV_COMMIT", "a" * 40)
    window = main_window.MainWindow()
    panel = window.coach_panel
    window.resize(420, height)
    window.show()
    state = make_snapshot()
    panel._on_game_state_changed(state)
    panel._on_game_plan_changed(forecast(state))
    qapp.processEvents()
    fixed = (panel.rank_strip, panel.now_card, panel.strategy_view, panel.workspace, panel.primary_controls)
    before = [w.geometry() for w in fixed]
    try:
        for iteration in range(8):
            changed = deepcopy(state)
            changed["battlefield"][0]["power"] += iteration
            panel._on_game_state_changed(changed)
            panel._on_commentary_status({"state": "speaking" if iteration % 2 else "idle"})
            panel._on_startup_status(
                {"ready": iteration % 2 == 0, "phase": "starting", "message": "Long startup message " * 40}
            )
            qapp.processEvents()
            assert window.size().width() == 420 and window.size().height() == height
            assert [w.geometry() for w in fixed] == before
            plan = forecast(changed)
            plan["facts"]["role_reason"] = "A long explanation " * 80
            plan["facts"]["lines"] = plan["facts"]["lines"][:1] if iteration % 2 else []
            panel._on_game_plan_changed(plan)
            qapp.processEvents()
            assert [w.geometry() for w in fixed] == before
        for name in ("paths", "board", "activity", "settings", "history:Constructed"):
            panel._show_detail(name)
            qapp.processEvents()
            assert [w.geometry() for w in fixed] == before
        terminal = dict(state, game_over=True, result="win")
        panel._on_game_state_changed(terminal)
        panel._on_game_plan_changed(forecast(terminal))
        qapp.processEvents()
        assert [w.geometry() for w in fixed] == before
        assert "Match complete" in panel.turn_strip.text()
        assert not panel.strategy_view.path_button.leading
        for control in (
            panel.ap_btn,
            panel.stop_speech_btn,
            panel.bug_report_btn,
            panel.reload_engine_btn,
            panel.restart_btn,
            window.dev_commit_button,
        ):
            rect = control.rect().translated(control.mapTo(window, QPoint()))
            assert window.rect().contains(rect)
            assert window.childAt(rect.center()) is control
    finally:
        window.close()
        window.deleteLater()


def test_resize_reveals_context_but_live_updates_do_not(qapp):
    panel = CompactCoachPanel()
    panel.resize(320, 650)
    panel.show()
    panel._on_game_state_changed(make_snapshot())
    qapp.processEvents()
    small = [w.isVisible() for w in (panel.overview_board, panel.overview_reason, panel.overview_event)]
    panel.resize(690, 900)
    qapp.processEvents()
    qapp.processEvents()
    large = [w.isVisible() for w in (panel.overview_board, panel.overview_reason, panel.overview_event)]
    assert sum(large) > sum(small)
    panel.close()


def test_open_details_withdraw_stale_winning_claims(qapp):
    panel = CompactCoachPanel()
    state = make_snapshot()
    plan = forecast(state)
    plan["facts"]["lines"][0]["outcome"] = "win"
    plan["win_conditions"] = ["OLD LETHAL CLAIM"]
    panel._on_game_state_changed(state)
    panel._on_game_plan_changed(plan)
    panel._show_detail("paths")
    assert "Projected win" in panel.detail_pager.browser.toPlainText()
    changed = deepcopy(state)
    changed["turn"]["turn_number"] += 1
    panel._on_game_state_changed(changed)
    assert "Projected win" not in panel.detail_pager.browser.toPlainText()
    assert "No current recommendation" in panel.detail_pager.browser.toPlainText()
    panel._show_detail("advice")
    assert "OLD LETHAL CLAIM" not in panel.detail_pager.browser.toPlainText()
    assert "Outlook" not in panel.game_plan_label.text()
    assert "▮" not in panel.game_plan_label.text()
    panel.close()


def test_details_are_paged_and_do_not_change_window_size(qapp):
    panel = CompactCoachPanel()
    panel.resize(420, 720)
    panel.show()
    for i in range(120):
        panel.append_log(f"Observed event {i}: " + "long explanation " * 10)
    panel._show_detail("activity")
    qapp.processEvents()
    pager = panel.detail_pager
    assert pager.next.isEnabled()
    before = panel.geometry()
    first = pager.browser.verticalScrollBar().value()
    pager.next.click()
    assert pager.browser.verticalScrollBar().value() > first
    assert not pager.browser.verticalScrollBar().isVisible()
    while pager.next.isEnabled():
        pager.next.click()
    assert pager.browser.verticalScrollBar().value() == pager.browser.verticalScrollBar().maximum()
    assert panel.geometry() == before
    pager.close_button.click()
    assert panel.workspace.currentWidget() is panel.overview
    panel.close()


def test_countdown_and_long_update_keep_small_shell_fixed(qapp):
    panel = CompactCoachPanel()
    panel.resize(320, 650)
    panel.show()
    qapp.processEvents()
    before = panel.geometry()
    panel.update_btn.setText("Downloading 4.0.0-preview.123456789")
    panel.update_btn.show()
    panel._on_concede_countdown({"state": "armed", "id": 7, "seconds": 10})
    qapp.processEvents()
    assert panel.geometry() == before
    assert panel.concede_cancel_btn.isVisible()
    assert "Auto-concede in 10s" in panel.notice_button.text()
    assert (
        panel.notice_button.width()
        >= panel.notice_button.fontMetrics().horizontalAdvance("Auto-concede in 10s") + 28
    )
    panel._show_detail("status")
    assert "Auto-concede" in panel.detail_pager.browser.toPlainText()
    panel.close()


@pytest.mark.parametrize("palette", ["dark", "light", "high-contrast"])
def test_recovery_row_shares_space_by_label_needs_at_320(qapp, palette):
    theme.apply_theme(qapp, palette)
    panel = CompactCoachPanel()
    panel.resize(320, 650)
    panel.show()
    qapp.processEvents()
    before = (panel.size(), panel.primary_controls.geometry(), panel.workspace.geometry())
    # Exercise the same asymmetric label pressure as Linux fonts, on any host.
    # Qt rounds some equal shares up; exceed the largest rounded share, not
    # the floor, or a 99px label can still fit an equal 99/99/98px allocation.
    available = panel.primary_controls.width() - 8
    equal_share = (available + 2) // 3
    text = panel.reload_engine_btn.text()
    while panel.reload_engine_btn.fontMetrics().horizontalAdvance(text) + 6 <= equal_share:
        text += "."
    panel.reload_engine_btn.setText(text)
    qapp.processEvents()
    buttons = (panel.bug_report_btn, panel.reload_engine_btn, panel.restart_btn)
    assert (panel.size(), panel.primary_controls.geometry(), panel.workspace.geometry()) == before
    for button in buttons:
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 6
        assert panel.rect().contains(button.rect().translated(button.mapTo(panel, QPoint())))
    assert panel.reload_engine_btn.width() > panel.bug_report_btn.width()
    panel.close()
