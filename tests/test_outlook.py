"""The deterministic outlook number and its sidebar rendering."""

from __future__ import annotations

from types import SimpleNamespace

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


def test_sidebar_uses_one_scale_with_explanation_and_ledger(qapp):
    from copy import deepcopy

    from tests.strategic_states import G1_T8

    from arenamcp.desktop.compact_coach import CompactCoachPanel
    from arenamcp.forecast_identity import forecast_key

    state = deepcopy(G1_T8)
    panel = CompactCoachPanel()
    panel.show()
    try:
        panel._on_game_state_changed(state)
        panel._on_game_plan_changed(
            {
                "snapshot_key": forecast_key(state),
                "facts": {"role": "aggressor"},
                "outlook": {"pct": 64, "label": "ahead", "tone": "good", "trend": [(5, 50), (7, 64)]},
                "ledger": {
                    "lessons": ["No damage dealt on our turns T3, T5: the pressure line is not working"]
                },
            }
        )
        assert panel.strategy_view.scale.value == 64
        assert "Ahead" in panel.strategy_view.verdict.text()
        assert "64%" not in panel.strategy_view.verdict.text()
        assert "improving since T5" in panel.strategy_view.verdict.toolTip()
        panel._show_detail("advice")
        text = panel.detail_pager.browser.toPlainText()
        assert "So far: No damage dealt on our turns T3, T5" in text
        assert "AGGRESSOR" in text
        assert "Outlook" not in text and "▮" not in text and "▯" not in text
        panel._on_game_state_changed(dict(state, game_over=True))
        assert panel.strategy_view.scale.value is None
        assert "No damage dealt" not in panel.detail_pager.browser.toPlainText()
    finally:
        panel.close()


def test_plan_status_is_available_in_current_explanation_without_changing_shell(qapp):
    from copy import deepcopy

    from tests.strategic_states import G1_T8

    from arenamcp.desktop.compact_coach import CompactCoachPanel, _plan_status_html
    from arenamcp.forecast_identity import forecast_key

    state = deepcopy(G1_T8)
    panel = CompactCoachPanel()
    panel.resize(420, 720)
    panel.show()
    try:
        panel._on_game_state_changed(state)
        panel._on_game_plan_changed(
            {
                "snapshot_key": forecast_key(state),
                "facts": {"role": "aggressor"},
                "plan_status": {
                    "state": "none",
                    "attempts": 3,
                    "cancels": 3,
                    "last_outcome": "yielded to a decision",
                },
            }
        )
        panel._show_detail("advice")
        text = panel.detail_pager.browser.toPlainText()
        assert "NO GAME PLAN" in text and "3 attempts, 3 cancelled, last: yielded to a decision" in text
        assert text.index("Game plan:") < text.index("AGGRESSOR")
        before = panel.size()
        panel._on_game_plan_changed(
            {
                "snapshot_key": forecast_key(state),
                "plan_status": {
                    "state": "forming",
                    "inflight": True,
                    "inflight_s": 6,
                    "inflight_for_turn": 8,
                    "attempts": 1,
                },
            }
        )
        assert "forming… 6s for T8" in panel.detail_pager.browser.toPlainText()
        assert panel.size() == before
        changed = deepcopy(state)
        changed["turn"]["turn_number"] += 1
        panel._on_game_state_changed(changed)
        assert "forming… 6s" not in panel.detail_pager.browser.toPlainText()
    finally:
        panel.close()
    assert "formed T8" in _plan_status_html({"state": "ready", "turn_formed": 8, "age_turns": 1})
    assert "6 turns old" in _plan_status_html({"state": "ready", "turn_formed": 2, "age_turns": 6})


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
