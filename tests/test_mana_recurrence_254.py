"""October 24: inactive ramp must not disable X/Omen affordability guards."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp.autopilot import AutopilotEngine
from arenamcp.play_safety import affordable_x, exact_mana_total, unsafe_play_reason


def board():
    return {
        "local_seat_id": 2,
        "turn": {"turn_number": 10},
        "battlefield": [
            {
                "instance_id": 1,
                "controller_seat_id": 2,
                "type_line": "Land",
                "name": "Forest",
                "oracle_text": "{T}: Add {G}.",
                "is_tapped": True,
            },
            {
                "instance_id": 2,
                "controller_seat_id": 2,
                "type_line": "Land",
                "name": "Forest",
                "oracle_text": "{T}: Add {G}.",
            },
            {
                "instance_id": 3,
                "controller_seat_id": 2,
                "type_line": "Land",
                "name": "Nykthos",
                "oracle_text": "{T}: Add {C}. {2}, {T}: Add an amount of mana equal to devotion.",
                "is_tapped": True,
            },
            {
                "instance_id": 4,
                "controller_seat_id": 2,
                "type_line": "Creature",
                "name": "The Notary Hobbits",
                "oracle_text": "{T}: Add {C} for each Halfling you control.",
                "summoning_sickness": True,
                "turn_entered_battlefield": 10,
            },
            {
                "instance_id": 5,
                "controller_seat_id": 2,
                "type_line": "Enchantment — Aura",
                "name": "Utopia Sprawl",
                "oracle_text": "Whenever enchanted Forest is tapped for mana, its controller adds an additional one mana.",
                "attached_to_id": 1,
            },
        ],
    }


def test_inactive_ramp_does_not_allow_unpayable_hydra_x():
    state = board()
    assert exact_mana_total(state) == 1
    assert affordable_x(state, {"mana_cost": "{X}{G}"}) == 0


def test_available_variable_mana_still_keeps_budget_unknown():
    state = board()
    state["battlefield"][2]["is_tapped"] = False
    assert exact_mana_total(state) is None


def test_untapped_enchanted_land_keeps_bonus_mana_unknown():
    state = board()
    state["battlefield"][0]["is_tapped"] = False
    assert exact_mana_total(state) is None


def test_omen_checks_its_cost_not_front_face():
    meta = {"manaCost": [{"color": '["Generic"]', "count": 2}, {"color": '["Green"]', "count": 1}]}
    assert unsafe_play_reason(board(), {"mana_cost": "{0}"}, "ActionType_CastOmen", meta)
    meta.update(hasAutoTap=True, autoTapActions=[{"instanceId": 2}])
    assert not unsafe_play_reason(board(), {}, "ActionType_CastOmen", meta)


def test_omen_cancellation_survives_payment_delay_and_face_name_change():
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._report_oops = Mock()
    state = board()
    state["decision_context"] = {"source_id": 600, "type": "pay_costs"}
    state["stack"] = [{"instance_id": 600, "name": "Claim Territory", "controller_seat_id": 2}]
    option = SimpleNamespace(meta={"actionType": "ActionType_CastOmen", "instanceId": 345}, label="CastOmen")
    engine._note_typed_play(state, option)
    note = engine._last_typed_play
    engine._last_typed_play = (time.monotonic() - 30, *note[1:])
    engine._withhold_after_self_cancel(state, "PayCosts cancelled")
    assert engine._self_cancel_withheld(state, "ActionType_CastOmen", 345)
    assert not engine._self_cancel_withheld(state, "ActionType_Cast", 345)
    state["turn"]["turn_number"] = 12
    assert not engine._self_cancel_withheld(state, "ActionType_CastOmen", 345)


def test_live_sickness_flag_overrides_old_entered_turn():
    state = board()
    state["battlefield"][3]["turn_entered_battlefield"] = 8
    assert exact_mana_total(state) == 1


def test_cancelled_omen_removed_before_model_or_cached_answer(monkeypatch):
    from tests.test_self_cancel_memory_20261019 import actions_poll, board_state, make_iroh_engine

    engine, bridge, planner = make_iroh_engine(monkeypatch)
    state = board_state()
    poll = actions_poll()
    poll["actions"][0]["actionType"] = "CastOmen"
    iid = poll["actions"][0]["instanceId"]
    engine._self_cancelled_plays = {(8, "ActionType_CastOmen", iid, 0, "")}
    bridge.get_pending_actions.return_value = poll
    assert engine._try_typed_decision_path(state, "decision_required")
    offered = planner.plan_decision_options.call_args.args[0].options
    assert all(o.meta.get("actionType") != "ActionType_CastOmen" for o in offered)


def test_own_omen_is_not_mistaken_for_manual_play():
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._recent_bot_submissions = []
    engine._last_seen_own_stack = set()
    engine._own_stack_first_turn = 8
    engine._manual_play_cooldown_until = 0
    state = board()
    state["hand"] = [
        {
            "instance_id": 345,
            "grp_id": 95657,
            "name": "Bloomvine Regent",
            "related_faces": [{"grp_id": 95658, "name": "Claim Territory"}],
        }
    ]
    option = SimpleNamespace(
        meta={"actionType": "ActionType_CastOmen", "instanceId": 345, "grpId": 95658}, label="CastOmen"
    )
    engine._note_typed_play(state, option)
    state["stack"] = [
        {
            "instance_id": 600,
            "grp_id": 95658,
            "name": "Claim Territory",
            "type_line": "Sorcery — Omen",
            "controller_seat_id": 2,
        }
    ]
    assert not engine._detect_manual_play(state)
    assert engine._manual_play_cooldown_until == 0
    # If the coach restarted mid-payment, the source face can still be suppressed.
    engine._last_typed_play = None
    engine._report_oops = Mock()
    state["decision_context"] = {"source_id": 600, "type": "pay_costs"}
    engine._withhold_after_self_cancel(state, "PayCosts cancelled")
    assert engine._self_cancel_withheld(
        state, "ActionType_CastOmen", 345, name=engine._typed_play_name(state, option)
    )
