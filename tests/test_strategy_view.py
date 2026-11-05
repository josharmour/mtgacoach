"""Forecast trust boundaries and native UI behavior for the in-game companion."""

from copy import deepcopy

import pytest

from arenamcp.forecast_identity import forecast_key
from arenamcp.plan_card import card_payload

pytest.importorskip("PySide6")

from PySide6.QtCore import QVariantAnimation
from PySide6.QtWidgets import QScrollArea
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
    assert not any(c.rail.leading for c in view.cards)
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
    assert "Projected loss" in view.cards[0].outcome.text()
    assert "win not established" not in view.cards[0].outcome.text()


def test_inspection_preserves_recommendation_and_displays_both_life_totals(view):
    state = make_snapshot()
    view.set_data(forecast(state), state)
    assert "you 20 · opponent 12" in view.cards[0].detail.text()
    view.cards[1].button.click()
    assert view.selected == 1
    assert view.cards[0].rail.leading
    assert not view.cards[1].rail.leading
    assert not view.cards[1].detail.isHidden()


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


@pytest.mark.parametrize("width", [240, 320, 440])
@pytest.mark.parametrize("palette", ["dark", "light", "high-contrast"])
def test_native_companion_fits_narrow_sidebar(qapp, width, palette):
    theme.apply_theme(qapp, palette)
    panel = CompactCoachPanel()
    state = make_snapshot()
    panel._on_game_state_changed(state)
    panel._on_game_plan_changed(forecast(state))
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(panel)
    scroll.resize(width, 850)
    scroll.show()
    qapp.processEvents()
    assert scroll.horizontalScrollBar().maximum() == 0
    assert panel.width() <= width
    assert panel.strategy_view.cards[0].summary.wordWrap()
    assert panel.strategy_view.cards[0].rail.isVisible()
    scale = panel.strategy_view.scale
    assert panel.strategy_view.reason.y() >= scale.y() + scale.height()
    for card in panel.strategy_view.cards:
        if card.rail.isVisible():
            assert card.outcome.y() >= card.rail.y() + card.rail.height()
    children = [
        panel.layout().itemAt(i).widget()
        for i in range(panel.layout().count())
        if panel.layout().itemAt(i).widget() and panel.layout().itemAt(i).widget().isVisible()
    ]
    for before, after in zip(children, children[1:], strict=False):
        assert before.geometry().bottom() < after.geometry().top()
    assumptions = panel.strategy_view.assumptions
    assert assumptions.toggle.geometry().bottom() < assumptions.height()
    scroll.close()
    scroll.deleteLater()
