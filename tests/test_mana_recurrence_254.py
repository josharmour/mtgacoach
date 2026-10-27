"""October 24: inactive ramp must not disable X/Omen affordability guards."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.autopilot import AutopilotEngine
from arenamcp.play_safety import affordable_x, exact_mana_total, unsafe_play_reason, x_choice_reason


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


def test_fixed_multi_mana_source_restores_exact_x_limit():
    state = board()
    state["battlefield"][2] = {
        "instance_id": 3,
        "controller_seat_id": 2,
        "type_line": "Artifact",
        "name": "Sol Ring",
        "oracle_text": "{oT}: Add {oC}{oC}.",
    }
    assert exact_mana_total(state) == 3
    assert affordable_x(state, {"mana_cost": "{X}{G}"}) == 2
    assert "largest affordable X is 2" in x_choice_reason(state, {"mana_cost": "{X}{G}"}, 3)
    assert x_choice_reason(state, {"mana_cost": "{X}{G}"}, 2) == ""
    assert affordable_x(state, {"mana_cost": "{X}{X}{G}"}) == 1
    state["battlefield"][2]["is_tapped"] = True
    assert exact_mana_total(state) == 1


def test_fixed_multi_mana_does_not_override_other_unknown_sources():
    state = board()
    state["battlefield"].append(
        {
            "instance_id": 6,
            "controller_seat_id": 2,
            "type_line": "Artifact",
            "name": "Sol Ring",
            "oracle_text": "{T}: Add {C}{C}.",
        }
    )
    state["battlefield"][2]["is_tapped"] = False  # variable Nykthos
    assert exact_mana_total(state) is None


def test_conditional_or_costly_multi_mana_remains_unknown():
    for text in ("{1}, {T}: Add {G}{G}.", "{T}: Add {G}{G}. Activate only if you control a creature."):
        state = board()
        state["battlefield"].append(
            {
                "instance_id": 6,
                "controller_seat_id": 2,
                "type_line": "Artifact",
                "oracle_text": text,
            }
        )
        assert exact_mana_total(state) is None


def test_paid_single_mana_filter_does_not_create_extra_mana():
    state = board()
    state["battlefield"].append(
        {
            "instance_id": 6,
            "controller_seat_id": 2,
            "type_line": "Artifact",
            "name": "Mana filter",
            "oracle_text": "{1}, {T}: Add {G}.",
        }
    )
    assert exact_mana_total(state) == 1  # Forest's one mana only changes color
    assert affordable_x(state, {"mana_cost": "{X}{G}"}) == 0
    state["battlefield"][1]["is_tapped"] = True
    assert exact_mana_total(state) == 0  # filter cannot start itself
    state["battlefield"][-1]["is_tapped"] = True
    assert exact_mana_total(state) == 0


def test_fetch_land_does_not_pay_x_before_search_resolves():
    state = {
        "local_seat_id": 2,
        "turn": {"turn_number": 4},
        "battlefield": [
            {
                "instance_id": i,
                "controller_seat_id": 2,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "oracle_text": "{T}: Add {G}.",
            }
            for i in (1, 2)
        ],
    }
    fetch = {
        "instance_id": 3,
        "controller_seat_id": 2,
        "name": "Misty Rainforest",
        "type_line": "Land",
        "oracle_text": "{oT}, Pay 1 life, Sacrifice this land: Search your library for a Forest or Island card, put it onto the battlefield, then shuffle.",
    }
    state["battlefield"].append(fetch)
    assert exact_mana_total(state) == 2
    assert affordable_x(state, {"mana_cost": "{X}{G}{G}"}) == 0
    # A fetch utility alongside a real mana ability must retain its mana.
    fetch["oracle_text"] = (
        "{T}: Add {C}. {2}, {T}, Sacrifice this land: Search your library for a basic land card."
    )
    assert exact_mana_total(state) == 3
    # Once the fetch resolves, the new untapped Forest can pay normally.
    state["battlefield"][-1] = dict(state["battlefield"][0], instance_id=4)
    assert exact_mana_total(state) == 3


@pytest.mark.parametrize("tapped,expected", [(False, 2), (True, 1)])
def test_channel_self_discount_does_not_disable_x_ceiling(tapped, expected):
    state = {
        "local_seat_id": 2,
        "battlefield": [
            {"controller_seat_id": 2, "type_line": "Land", "name": "Forest", "oracle_text": "{T}: Add {G}."},
            {
                "controller_seat_id": 2,
                "type_line": "Legendary Land",
                "name": "Boseiju, Who Endures",
                "is_tapped": tapped,
                "oracle_text": "{oT}: Add {oG}.\n<i>Channel</i> — {o1oG}, Discard this card: Destroy target artifact. This ability costs {o1} less to activate for each legendary creature you control.",
            },
        ],
    }
    assert exact_mana_total(state) == expected
    state["battlefield"][1]["oracle_text"] += " Green spells you cast cost {1} less to cast."
    assert exact_mana_total(state) is None


def test_visible_subtype_mana_count_counts_each_ready_source_once():
    state = {
        "local_seat_id": 2,
        "turn": {"turn_number": 12},
        "battlefield": [
            {
                "instance_id": i,
                "controller_seat_id": 2,
                "type_line": "Creature — Halfling Advisor",
                "name": "The Notary Hobbits",
                "turn_entered_battlefield": 8,
                "oracle_text": "When this enters, create two tokens.\n{T}: Add {C} for each Halfling you control.",
            }
            for i in (1, 2)
        ],
    }
    assert exact_mana_total(state) == 4
    state["battlefield"][1]["is_tapped"] = True
    assert exact_mana_total(state) == 2  # tapped Halfling still contributes to the count
    state["battlefield"].append({"controller_seat_id": 1, "type_line": "Creature — Halfling"})
    assert exact_mana_total(state) == 2
    state["battlefield"].append(
        {"controller_seat_id": 2, "type_line": "Creature — Shapeshifter", "oracle_text": "Changeling"}
    )
    assert exact_mana_total(state) is None
