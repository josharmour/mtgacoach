"""Recorded ranked losses: unaffordable plot and spending pending ward mana."""

from unittest.mock import Mock

from arenamcp.autopilot import AutopilotEngine
from arenamcp.decisions import build_pending_decision
from arenamcp.play_safety import unsafe_play_reason


def board(mana=1):
    return {
        "local_seat_id": 2,
        "turn": {"turn_number": 5},
        "battlefield": [
            {
                "instance_id": i + 1,
                "name": "Mountain",
                "type_line": "Land",
                "oracle_text": "{T}: Add {R}.",
                "controller_seat_id": 2,
            }
            for i in range(mana)
        ],
        "hand": [{"instance_id": 40, "name": "Slickshot Show-Off", "mana_cost": "{1}{R}"}],
    }


def test_plot_cost_and_identity_are_preserved():
    meta = {
        "actionType": "Special",
        "instanceId": 40,
        "abilityGrpId": 328,
        "manaCost": [{"color": '["Generic"]', "count": 1}, {"color": '["Red"]', "count": 1}],
    }
    decision = build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "actions": [meta]},
        resolve_instance=lambda iid: "Slickshot Show-Off",
    )
    option = decision.options[0]
    assert "Slickshot" in option.label and "land" not in option.label
    assert "only 1 mana" in unsafe_play_reason(board(), {}, "Special", option.meta)
    assert not unsafe_play_reason(board(2), {}, "Special", option.meta)
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._report_oops = Mock()
    engine._note_typed_play(board(), option)
    engine._withhold_after_self_cancel(board(), "PayCosts cancelled")
    assert engine._self_cancel_withheld(board(), "ActionType_Special", 40, 328)
    assert not engine._self_cancel_withheld(board(), "ActionType_Cast", 40)


def ward_board(mana=2):
    s = board(mana)
    s["battlefield"].append(
        {
            "instance_id": 298,
            "name": "Hortimancer",
            "controller_seat_id": 1,
            "oracle_text": "Ward {1}",
            "type_line": "Creature",
        }
    )
    s["stack"] = [
        {"instance_id": 308, "controller_seat_id": 2, "object_kind": "ABILITY", "parent_instance_id": 40},
        {
            "instance_id": 306,
            "controller_seat_id": 1,
            "object_kind": "ABILITY",
            "parent_instance_id": 298,
            "oracle_text": "Ward {1}",
        },
        {"instance_id": 314, "controller_seat_id": 2, "name": "Burst Lightning", "targeting": [298]},
    ]
    return s


def test_keep_mana_for_ward_below_our_cast_trigger():
    strike = {
        "name": "Lightning Strike",
        "mana_cost": "{1}{R}",
        "oracle_text": "Lightning Strike deals 3 damage to any target.",
    }
    assert "pending ward" in unsafe_play_reason(ward_board(), strike, "Cast")
    assert not unsafe_play_reason(ward_board(3), strike, "Cast")
    assert not unsafe_play_reason(ward_board(), {}, "Play")
    lethal = ward_board()
    lethal["players"] = [{"seat_id": 1, "life_total": 3}, {"seat_id": 2, "life_total": 10}]
    assert not unsafe_play_reason(lethal, strike, "Cast")
    s = ward_board()
    s["stack"] = s["stack"][:2]  # no threatened spell: no reserve
    assert not unsafe_play_reason(s, strike, "Cast")


def test_survival_baseline_cannot_pin_redundant_burn_before_sweep():
    from tests.race_boards_20261019 import card

    from arenamcp.board_model import build_board_model
    from arenamcp.line_search import search_lines

    s = board()
    s["turn"].update(active_player=2, phase="Phase_Main1", turn_number=12)
    s["players"] = [
        {"seat_id": 2, "is_local": True, "life_total": 13, "lands_played": 0},
        {"seat_id": 1, "life_total": 18},
    ]
    s["battlefield"] += [
        {
            "instance_id": i,
            "name": "Monk",
            "type_line": "Creature — Monk",
            "power": 1,
            "toughness": 1,
            "controller_seat_id": 1,
        }
        for i in (481, 482)
    ]
    s["hand"] = [
        card(i, name, 2)
        for i, name in enumerate(
            ["Shock", "Tectonic Hazard", "Tectonic Hazard", "Mountain", "Mountain"], start=20
        )
    ]
    result = search_lines(build_board_model(s), survival=True, lethal_now=False, hard_ms=1e9)
    assert result.baseline.steps[0].targets == (("Shock", "Monk"),)
    assert not any(("Shock", "Monk") in step.targets for step in result.best.steps)
    assert result.best.lives() == result.baseline.lives()
    assert result.best._leaf.opp_life < result.baseline._leaf.opp_life


def test_second_look_does_not_add_high_effort_to_background_plan(monkeypatch):
    from tests.jace_states import jace_engine, jace_state
    from tests.test_critical_decision_budget import LOW, _Legs

    engine, _, _ = jace_engine(LOW)
    engine._game_plan_mgr = None
    backend = _Legs()
    engine._planner._backend = backend
    engine._planner.set_background_yield(lambda: False)
    monkeypatch.setattr(engine._planner, "_second_look_concerns", lambda *args: [("soft", "Recheck value.")])
    assert engine._try_typed_decision_path(jace_state(), "decision_required")
    assert backend.calls == [None, None]
