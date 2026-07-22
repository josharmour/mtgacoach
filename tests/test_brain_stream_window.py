from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.brain_stream_window import BrainStreamWindow


def make_snapshot(**overrides: Any) -> dict[str, Any]:
    """Build a snapshot shaped like server.get_game_state() output.

    players is a LIST of Player.to_dict() entries plus is_local, battlefield
    objects carry controller_seat_id / is_tapped / instance_id, and phase is a
    raw GRE name like "Phase_Main1".
    """
    state: dict[str, Any] = {
        "match_id": "match-1",
        "local_seat_id": 2,
        "opponent_seat_id": 1,
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
                "life_total": 18,
                "lands_played": 3,
                "mana_pool": {},
                "team_id": 1,
                "status": "",
                "is_local": False,
            },
            {
                "seat_id": 2,
                "life_total": 20,
                "lands_played": 3,
                "mana_pool": {"G": 1},
                "team_id": 2,
                "status": "",
                "is_local": True,
            },
        ],
        "battlefield": [
            {
                "instance_id": 101,
                "grp_id": 11,
                "name": "Grizzly Bears",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "power": 2,
                "toughness": 2,
                "is_tapped": False,
            },
            {
                "instance_id": 102,
                "grp_id": 12,
                "name": "Serra Angel",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "power": 4,
                "toughness": 4,
                "is_tapped": True,
            },
        ],
        "hand": [
            {
                "instance_id": 201,
                "grp_id": 13,
                "name": "Lightning Bolt",
                "mana_cost": "{R}",
            },
        ],
        "raw_gre_event_count": 5,
    }
    state.update(overrides)
    return state


@pytest.fixture
def window(qapp):
    win = BrainStreamWindow()
    win.show()
    yield win
    win.close()


def test_brain_stream_window_import():
    assert BrainStreamWindow is not None


def test_players_list_renders_life_and_mana(window):
    window.update_game_state(make_snapshot())
    text = window.battlefield_view.toPlainText()
    assert "YOU: Life=20" in text
    assert "OPP: Life=18" in text
    assert "'G': 1" in text


def test_battlefield_ownership_and_tapped_state(window):
    window.update_game_state(make_snapshot())
    text = window.battlefield_view.toPlainText()
    assert "[You] Grizzly Bears [2/2]" in text
    assert "[Opp] Serra Angel [4/4] (Tapped)" in text
    assert "(Tapped)" not in text.split("\n")[3]  # Grizzly Bears line is untapped


def test_hand_pane_renders_cards(window):
    window.update_game_state(make_snapshot())
    text = window.hand_view.toPlainText()
    assert "Lightning Bolt" in text
    assert "{R}" in text


def test_turn_label_uses_local_seat_not_hardcoded_seat_1(window):
    snap = make_snapshot()
    window.update_game_state(snap)
    text = window.turn_history_view.toPlainText()
    assert "HERO's Turn" in text
    assert "Turn 3 (Main1)" in text

    snap4 = make_snapshot()
    snap4["turn"] = dict(snap4["turn"], turn_number=4, active_player=1)
    window.update_game_state(snap4)
    text = window.turn_history_view.toPlainText()
    assert "Turn 4 (Main1) — OPPONENT's Turn" in text


def test_new_match_id_resets_turn_history(window):
    window.update_game_state(make_snapshot())
    snap4 = make_snapshot()
    snap4["turn"] = dict(snap4["turn"], turn_number=4)
    window.update_game_state(snap4)
    assert "Turn 3" in window.turn_history_view.toPlainText()

    snap_new = make_snapshot(match_id="match-2")
    snap_new["turn"] = dict(snap_new["turn"], turn_number=1, phase="Phase_Beginning")
    window.update_game_state(snap_new)
    text = window.turn_history_view.toPlainText()
    assert "Turn 3" not in text
    assert "Turn 4" not in text
    assert "Turn 1 (Beginning)" in text


def test_turn_number_drop_resets_turn_history(window):
    snap = make_snapshot(match_id=None)
    snap["turn"] = dict(snap["turn"], turn_number=7)
    window.update_game_state(snap)
    assert "Turn 7" in window.turn_history_view.toPlainText()

    snap1 = make_snapshot(match_id=None)
    snap1["turn"] = dict(snap1["turn"], turn_number=1, phase="Phase_Beginning")
    window.update_game_state(snap1)
    text = window.turn_history_view.toPlainText()
    assert "Turn 7" not in text
    assert "Turn 1" in text


def test_history_dedup_is_scoped_per_turn(window):
    snap = make_snapshot(turn_history=["Opponent cast Shock"])
    window.update_game_state(snap)
    window.update_game_state(snap)
    text = window.turn_history_view.toPlainText()
    assert text.count("• Opponent cast Shock") == 1

    snap4 = make_snapshot(turn_history=["Opponent cast Shock"])
    snap4["turn"] = dict(snap4["turn"], turn_number=4)
    window.update_game_state(snap4)
    text = window.turn_history_view.toPlainText()
    assert text.count("• Opponent cast Shock") == 2


def test_log_views_are_capped(window):
    for view in (
        window.trigger_log_view,
        window.advice_history_view,
        window.turn_history_view,
        window.reasoning_view,
    ):
        assert view.document().maximumBlockCount() > 0

    window.trigger_log_view.document().setMaximumBlockCount(50)
    for i in range(120):
        window.log_trigger_event("GAME_STATE", f"Turn {i}")
    assert window.trigger_log_view.document().blockCount() <= 50


def test_hidden_window_skips_rendering_but_accumulates_history(qapp):
    win = BrainStreamWindow()
    try:
        assert not win.isVisible()
        win.update_game_state(make_snapshot())
        # Heavy panels are untouched while hidden...
        assert win.battlefield_view.toPlainText() == ""
        assert win.hand_view.toPlainText() == ""
        # ...but the cheap multi-turn bookkeeping still runs.
        assert "Turn 3" in win.turn_history_view.toPlainText()
    finally:
        win.close()


def test_history_accumulates_across_turns_while_hidden(qapp):
    win = BrainStreamWindow()
    try:
        win.show()
        win.update_game_state(make_snapshot())
        win.hide()

        snap4 = make_snapshot()
        snap4["turn"] = dict(snap4["turn"], turn_number=4, active_player=1)
        win.update_game_state(snap4)
        snap5 = make_snapshot()
        snap5["turn"] = dict(snap5["turn"], turn_number=5)
        win.update_game_state(snap5)
        win.append_advice_history("SEAT 2", "Attack with everything.")

        win.show()
        text = win.turn_history_view.toPlainText()
        assert "Turn 3" in text
        assert "Turn 4" in text
        assert "Turn 5" in text
        assert "Attack with everything." in win.advice_history_view.toPlainText()
    finally:
        win.close()


def test_show_event_repaints_from_last_payload(qapp):
    win = BrainStreamWindow()
    try:
        win.update_game_state(make_snapshot())
        assert win.battlefield_view.toPlainText() == ""

        win.show()
        assert "Grizzly Bears" in win.battlefield_view.toPlainText()
        assert "Lightning Bolt" in win.hand_view.toPlainText()
    finally:
        win.close()


def test_x_close_hides_but_preserves_history(qapp):
    win = BrainStreamWindow()
    try:
        closed = []
        win.window_closed.connect(lambda: closed.append(True))
        win.show()
        win.update_game_state(make_snapshot())
        win.append_advice_history("SEAT 2", "Hold up mana.")

        win.close()  # X-close: emits window_closed and hides, never destroys
        assert closed
        assert not win.isVisible()

        win.show()
        assert "Turn 3" in win.turn_history_view.toPlainText()
        assert "Hold up mana." in win.advice_history_view.toPlainText()
    finally:
        win.close()


def test_coach_tab_close_handler_keeps_window_instance(qapp):
    from arenamcp.desktop.coach_tab import CoachTab

    class Holder:
        pass

    holder = Holder()
    holder._brain_stream_window = BrainStreamWindow()
    CoachTab._on_brain_stream_closed(holder)
    assert holder._brain_stream_window is not None
    holder._brain_stream_window.deleteLater()


def test_raw_gre_tab_only_updates_when_selected(window):
    window.update_game_state(make_snapshot())
    assert window.raw_gre_view.toPlainText() == ""

    window.context_tabs.setCurrentWidget(window.raw_gre_view)
    window.update_game_state(make_snapshot())
    text = window.raw_gre_view.toPlainText()
    assert "match-1" in text
    assert "Grizzly Bears" in text


def test_latency_badge_has_no_fabricated_default(window):
    assert "129" not in window.latency_badge.text()
    assert "—" in window.latency_badge.text()

    window.update_telemetry(latency=42, backend="vLLM")
    assert window.latency_badge.text() == "⚡ 42ms vLLM"

    window.update_telemetry(bridge_connected=True)
    assert "Connected" in window.bridge_badge.text()


def test_draw_odds_placeholder_is_neutral(window):
    window.update_game_state(make_snapshot())
    assert window.draw_odds_view.toPlainText() == "—"

    window.update_game_state(make_snapshot(draw_odds={"Land": "42.5%"}))
    assert "Land: 42.5%" in window.draw_odds_view.toPlainText()
