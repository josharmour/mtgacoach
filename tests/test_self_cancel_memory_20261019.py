"""The self-cancel guard fires on the first self-cancel (#71, match 115944ce).

2026-10-19 11:12:55-11:14:17: Iroh's Demonstration was cast, its "4 damage to
target creature" mode chosen, the target declined (kills nothing on a 7/7
Trostani) and the cast cancelled — eleven times on turn 8, and the guard that
withholds a self-cancelled cast for the rest of the turn never said a word.

The previous game (73ccac27) had ended on turn 9. process_trigger treats "turn
went backwards" as a new game and wipes its per-game memories, but kept the
high-water mark at 9: every trigger of turns 1-8 of the next game wiped them
again, including the cast noted at 11:12:55 before its SelectTargets cancel at
11:13:04. After the 11:14:23 restart (fresh engine) the guard fired at once.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.fakes import make_engine

from arenamcp.action_planner import DECLINE_DECISION

LOCAL, OPP = 2, 1
IROH_GRP, IROH_HAND, IROH_STACK = 97417, 219, 327


def creature(iid, name, seat, power, toughness, oracle="", tapped=False, entered=1):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature",
        "oracle_text": oracle,
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
        "turn_entered_battlefield": entered,
    }


def board_state(*, turn=8, stack=False, match_id="115944ce", source=None):
    state = {
        "match_id": match_id,
        "local_seat_id": LOCAL,
        "turn": {"turn_number": turn, "active_player": LOCAL, "phase": "Phase_Main1", "step": ""},
        "players": [
            {"seat_id": OPP, "life_total": 13, "is_local": False},
            {"seat_id": LOCAL, "life_total": 15, "is_local": True},
        ],
        "battlefield": [
            creature(299, "Trostani, Three Whispers", OPP, 7, 7, tapped=True),
            creature(292, "Slickshot Show-Off", LOCAL, 1, 2),
            creature(305, "Slickshot Show-Off", LOCAL, 1, 2),
        ],
        "hand": [{"instance_id": IROH_HAND, "grp_id": IROH_GRP, "name": "Iroh's Demonstration"}],
        "stack": [],
    }
    if stack:
        state["stack"] = [
            {
                "instance_id": IROH_STACK,
                "grp_id": IROH_GRP,
                "name": "Iroh's Demonstration",
                "type_line": "Sorcery",
                "controller_seat_id": LOCAL,
                "owner_seat_id": LOCAL,
            }
        ]
    if source:
        state["decision_context"] = {"type": "target_selection", "source_id": source}
    return state


def actions_poll(gsid=190):
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": gsid,
        "can_pass": True,
        "actions": [
            {
                "actionType": "Cast",
                "grpId": IROH_GRP,
                "instanceId": IROH_HAND,
                "facetId": IROH_HAND,
                "hasAutoTap": True,
                "autoTapActions": [{"instanceId": 304, "manaId": 0}, {"instanceId": 323, "manaId": 0}],
            },
            {"actionType": "Pass"},
        ],
    }


def modes_poll(gsid=191):
    return {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "game_state_id": gsid,
        "can_cancel": True,
        "actions": [
            {"actionType": "CastingTimeOption", "choiceKind": "modal", "label": "Mode 1", "childIndex": 0,
             "optionIndex": 0, "grpId": 87912},
            {"actionType": "CastingTimeOption", "choiceKind": "modal", "label": "Mode 2", "childIndex": 0,
             "optionIndex": 1, "grpId": 21773},
        ],
    }


def targets_poll(gsid=192):
    slot = [
        {"targetInstanceId": iid, "targetIdx": 1, "legalAction": "Select", "highlight": "Tepid"}
        for iid in (299, 292, 305)
    ]
    return {
        "has_pending": True,
        "request_type": "SelectTargets",
        "request_class": "SelectTargetsRequest",
        "game_state_id": gsid,
        "can_cancel": True,
        "source_instance_id": IROH_STACK,
        "target_selections": [
            {"targetIdx": 1, "minTargets": 1, "maxTargets": 1, "selectedTargets": 0, "targets": slot}
        ],
        "target_candidates": slot,
    }


@pytest.fixture
def iroh_engine(monkeypatch):
    """The live loop: cast Iroh's, choose mode 2, decline its target (no card text, so no mode switch)."""
    import arenamcp.card_db as card_db
    import arenamcp.decisions as decisions

    monkeypatch.setattr(
        decisions, "_default_card_resolver", lambda grp: {"name": "Iroh's Demonstration"} if grp == IROH_GRP else {}
    )
    monkeypatch.setattr(card_db, "get_card_database", lambda: SimpleNamespace(get_ability_text=lambda grp: None))
    planner = MagicMock()

    def plan(decision, state):
        if decision.request_type == "SelectTargets":
            return [DECLINE_DECISION]
        if decision.request_type == "CastingTimeOptions":
            return ["idx:1"]
        return [o.option_id for o in decision.options if o.option_id != "pass"][:1] or ["pass"]

    planner.plan_decision_options.side_effect = plan
    bridge = MagicMock()
    bridge.connected = True
    engine = make_engine(bridge, planner)
    engine._request_tracker = MagicMock()
    engine._request_tracker.may_submit.return_value = True
    engine._request_tracker.exhausted.return_value = False
    return engine, bridge, planner


def trigger(engine, state):
    """What the coaching loop does before each typed decision: process_trigger's per-game bookkeeping.

    A manual-play stand-by stops it right after that bookkeeping.
    """
    engine._manual_play_cooldown_until = time.time() + 600
    engine.process_trigger(state, "decision_required")
    engine._manual_play_cooldown_until = 0.0


def test_a_game_after_a_longer_one_keeps_its_memories_between_triggers(iroh_engine):
    engine, _, _ = iroh_engine
    trigger(engine, board_state(turn=9, match_id="73ccac27"))  # the previous game's last turn
    trigger(engine, board_state(turn=1))  # the new game: memories cleared once
    engine._self_cancelled_plays = {(8, "ActionType_Cast", 0, 0, "iroh's demonstration")}
    engine._last_typed_play = (time.monotonic(), 8, "ActionType_Cast", IROH_HAND, 0, "iroh's demonstration")
    trigger(engine, board_state(turn=8))
    assert engine._last_typed_play is not None
    assert engine._self_cancelled_plays


def test_a_later_game_of_the_same_match_still_starts_clean(iroh_engine):
    engine, _, _ = iroh_engine
    trigger(engine, board_state(turn=9))
    engine._self_cancelled_plays = {(9, "ActionType_Cast", 0, 0, "shock")}
    trigger(engine, board_state(turn=1))  # game 2: the turn went back
    assert engine._self_cancelled_plays == set()
    assert engine._max_seen_turn == 1


def test_the_first_self_cancel_withholds_the_cast_after_a_longer_game(iroh_engine, caplog):
    engine, bridge, planner = iroh_engine
    trigger(engine, board_state(turn=9, match_id="73ccac27"))
    trigger(engine, board_state(turn=1))

    trigger(engine, board_state())
    bridge.get_pending_actions.return_value = actions_poll()
    assert engine._try_typed_decision_path(board_state(), "decision_required")
    assert bridge.submit_action_by_index.call_count == 1  # Cast Iroh's Demonstration

    trigger(engine, board_state(stack=True))  # 11:12:56 CastingTimeOptions trigger
    bridge.get_pending_actions.return_value = modes_poll()
    assert engine._try_typed_decision_path(board_state(stack=True), "decision_required")
    assert bridge.submit_action_by_index.call_count == 2  # Mode 2

    trigger(engine, board_state(stack=True, source=IROH_STACK))  # 11:13:01 SelectTargets trigger
    bridge.get_pending_actions.return_value = targets_poll()
    with caplog.at_level(logging.INFO, logger="arenamcp.autopilot"):
        assert engine._try_typed_decision_path(board_state(stack=True, source=IROH_STACK), "decision_required")
    bridge.cancel_action.assert_called_once()
    assert "withholding it for the rest of turn 8" in caplog.text

    # 11:13:04: the cast is offered again; only Pass reaches the planner.
    trigger(engine, board_state())
    bridge.get_pending_actions.return_value = actions_poll(193)
    assert engine._try_typed_decision_path(board_state(), "decision_required")
    offered = planner.plan_decision_options.call_args.args[0]
    assert [o.option_id for o in offered.options] == ["pass"]
    bridge.submit_pass.assert_called_once()


def test_a_cancel_with_no_record_withholds_our_spell_on_the_stack(iroh_engine, caplog):
    # A cast the previous coach process submitted (restart at 11:14:23): no
    # record here, but the cancelled request belongs to our spell on the stack.
    engine, _, _ = iroh_engine
    state = board_state(stack=True, source=IROH_STACK)
    with caplog.at_level(logging.INFO, logger="arenamcp.autopilot"):
        engine._withhold_after_self_cancel(state, "SelectTargets cancelled")
    assert "no cast or activation of ours is on record" in caplog.text
    assert engine._self_cancel_withheld(board_state(), "ActionType_Cast", IROH_HAND, 0, "iroh's demonstration")


def test_every_silent_return_says_why(iroh_engine, caplog):
    engine, _, _ = iroh_engine
    no_source = board_state()
    with caplog.at_level(logging.INFO, logger="arenamcp.autopilot"):
        engine._withhold_after_self_cancel(no_source, "SelectTargets cancelled")
        engine._last_typed_play = (time.monotonic() - 60, 8, "ActionType_Cast", IROH_HAND, 0, "iroh's demonstration")
        engine._withhold_after_self_cancel(no_source, "SelectTargets cancelled")
        engine._last_typed_play = (time.monotonic(), 7, "ActionType_Cast", IROH_HAND, 0, "iroh's demonstration")
        engine._withhold_after_self_cancel(no_source, "SelectTargets cancelled")
    messages = [r.getMessage() for r in caplog.records if "Self-cancel guard" in r.getMessage()]
    assert len(messages) == 3
    assert "no cast or activation of ours is on record" in messages[0]
    assert "is 60s old" in messages[1]
    assert "was on turn 7" in messages[2]
    assert engine._self_cancelled_plays == set()


def test_an_opponents_spell_or_an_ability_is_never_withheld_as_ours(iroh_engine):
    engine, _, _ = iroh_engine
    state = board_state(stack=True, source=IROH_STACK)
    state["stack"][0]["controller_seat_id"] = state["stack"][0]["owner_seat_id"] = OPP
    engine._withhold_after_self_cancel(state, "SelectTargets cancelled")
    state = board_state(stack=True, source=IROH_STACK)
    state["stack"][0]["object_kind"] = "ABILITY"
    engine._withhold_after_self_cancel(state, "SelectTargets cancelled")
    assert engine._self_cancelled_plays == set()
