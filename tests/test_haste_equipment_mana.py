"""Lightning Greaves + The Notary Hobbits, live 2026-10-04 22:50.

Greaves' equip reached SelectTargets four times and was cancelled each time:
the request's source is the stack ability whose Arena text is only
"Equip {o0}", which classified as neither harmful nor beneficial. The
cancelled equip still counted toward the repeat-activation guard, and the
planner called the haste "incidental" while three summoning-sick Hobbits
(each {T}: Add {C} for each Halfling) sat untapped.
"""

from unittest.mock import MagicMock

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.mana import haste_equipment_mana_hint
from arenamcp.target_effects import source_effect_text, target_effect_is_harmful

GREAVES = (
    "Equipped creature has haste and shroud. (It can't be the target of spells or abilities.)\nEquip {0}"
)
HOBBITS = (
    "When The Notary Hobbits enters the battlefield, if it's not a token, create two tokens that "
    "are copies of The Notary Hobbits, except the tokens aren't legendary.\n"
    "{T}: Add {C} for each Halfling you control."
)
LOCAL = 1


def test_bare_equip_ability_reads_parent_equipment_text():
    assert target_effect_is_harmful("equip {o0}") is None  # the live failure
    assert target_effect_is_harmful(source_effect_text("Equip {o0}", GREAVES)) is False
    assert target_effect_is_harmful(source_effect_text("Equip {o0}")) is False
    # A harmful equipment effect stays unclassified rather than beneficial.
    cursed = "Equipped creature gets -2/-2.\nEquip {1}"
    assert target_effect_is_harmful(source_effect_text("Equip {o1}", cursed)) is None
    # Non-keyword sources are untouched.
    assert source_effect_text("Destroy target creature.", GREAVES) == "Destroy target creature."


def _target_decision(*ids):
    return PendingDecision(
        request_id=(230, 308),
        request_type="SelectTargets",
        options=tuple(DecisionOption(f"tgt:{iid}", f"The Notary Hobbits {iid}") for iid in ids),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Lightning Greaves",
    )


def _target_state(source_oracle="Equip {o0}", parent_oracle=GREAVES):
    hobbit = {
        "name": "The Notary Hobbits",
        "type_line": "Legendary Creature — Halfling Citizen",
        "oracle_text": HOBBITS,
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
    }
    return {
        "local_seat_id": LOCAL,
        "players": [{"seat_id": LOCAL, "is_local": True}, {"seat_id": 2}],
        "battlefield": [
            {
                "instance_id": 454,
                "name": "Lightning Greaves",
                "type_line": "Legendary Artifact — Equipment",
                "oracle_text": parent_oracle,
                "controller_seat_id": LOCAL,
            },
            {**hobbit, "instance_id": 583},
            {**hobbit, "instance_id": 591},
        ],
        "stack": [
            {
                "instance_id": 594,
                "name": "Ability (ID: 2152)",
                "oracle_text": source_oracle,
                "parent_instance_id": 454,
                "controller_seat_id": LOCAL,
            }
        ],
        "_bridge_request_payload": {"sourceId": 594},
        "decision_context": {
            "type": "target_selection",
            "source_id": 594,
            "source_card": "Lightning Greaves",
            "source_oracle_text": source_oracle,
            "source_card_oracle_text": parent_oracle,
            "source_parent_instance_id": 454,
        },
    }


def _planner(reply):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._timeout = 1.0

    class _Backend:
        def complete(self, *a, **k):
            return reply

    planner._backend = _Backend()
    return planner


def test_equip_on_own_creature_is_submitted_without_controller_intent():
    planner = _planner('{"option_ids": ["tgt:583"], "reasoning": "haste lets it tap for mana"}')
    assert planner.plan_decision_options(_target_decision(583, 591), _target_state()) == ["tgt:583"]


def test_unclassified_non_equip_source_still_needs_intent():
    planner = _planner('{"option_ids": ["tgt:583"], "reasoning": "tap it"}')
    state = _target_state(source_oracle="Tap target creature.", parent_oracle="")
    assert planner.plan_decision_options(_target_decision(583, 591), state) == [DECLINE_DECISION]


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _actions_poll():
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": 229,
        "can_pass": True,
        "actions": [
            {"actionType": "ActionType_Activate", "grpId": 19737, "instanceId": 454, "abilityGrpId": 2152},
            {"actionType": "ActionType_Pass"},
        ],
    }


def _targets_poll():
    return {
        "has_pending": True,
        "request_type": "SelectTargets",
        "game_state_id": 230,
        "msg_id": 308,
        "can_cancel": True,
        "target_candidates": [{"targetInstanceId": 583, "legalAction": "SelectAction_Select"}],
    }


def test_cancelled_equip_does_not_trip_repeat_activation_guard(monkeypatch):
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    planner = MagicMock()
    planner.plan_decision_options.side_effect = lambda decision, gs: (
        [DECLINE_DECISION] if decision.request_type == "SelectTargets" else ["idx:0"]
    )
    engine = AutopilotEngine(
        planner=planner, get_game_state=lambda: {}, config=AutopilotConfig(dry_run=False)
    )
    bridge = MagicMock()
    bridge.connected = True
    engine._gre_bridge = bridge
    engine._request_tracker = MagicMock()
    engine._request_tracker.may_submit.return_value = True
    engine._request_tracker.exhausted.return_value = False
    state = {**_target_state(), "turn": {"turn_number": 9}}

    bridge.get_pending_actions.return_value = _actions_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    assert bridge.submit_action_by_index.call_count == 1

    bridge.get_pending_actions.return_value = _targets_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    bridge.cancel_action.assert_called_once()

    # The equip was rolled back, so the next window still offers it.
    bridge.get_pending_actions.return_value = _actions_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    assert bridge.submit_action_by_index.call_count == 2


def test_cancel_long_after_activation_is_not_attributed(monkeypatch):
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    engine = AutopilotEngine(planner=MagicMock(), get_game_state=lambda: {}, config=AutopilotConfig())
    state = {**_target_state(), "turn": {"turn_number": 9}}
    engine._note_activation(state, 454, "Lightning Greaves")
    noted_at, *rest = engine._last_activation_note
    engine._last_activation_note = (noted_at - 60, *rest)
    engine._undo_activation_after_cancel(state, "late cancel")
    assert engine._activation_exhausted(state, 454, "Lightning Greaves")


def _board(turn=9, wearer=None):
    hobbit = {
        "name": "The Notary Hobbits",
        "type_line": "Creature — Halfling Citizen",
        "subtypes": ["SubType_Halfling", "SubType_Citizen"],
        "oracle_text": HOBBITS,
        "controller_seat_id": LOCAL,
        "turn_entered_battlefield": turn,
        "is_tapped": False,
    }
    return [
        {
            "instance_id": 454,
            "name": "Lightning Greaves",
            "type_line": "Legendary Artifact — Equipment",
            "oracle_text": GREAVES,
            "controller_seat_id": LOCAL,
            "attached_to_id": wearer,
        },
        {**hobbit, "instance_id": 583, "type_line": "Legendary Creature — Halfling Citizen"},
        {**hobbit, "instance_id": 590},
        {**hobbit, "instance_id": 591},
        {
            "instance_id": 447,
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "controller_seat_id": LOCAL,
        },
    ]


def test_hint_names_equip_tap_reequip_line_for_sick_mana_creatures():
    hint = haste_equipment_mana_hint(_board(), LOCAL, 9)
    assert "Lightning Greaves grants haste (equip {0})" in hint
    assert "The Notary Hobbits x3" in hint
    assert "+9 mana" in hint
    assert "move the equipment to the next untapped one" in hint
    assert "already wears it" in haste_equipment_mana_hint(_board(wearer=583), LOCAL, 9)


def test_hint_stays_quiet_without_a_live_line():
    assert haste_equipment_mana_hint(_board(turn=8), LOCAL, 9) == ""  # nobody is summoning sick
    board = _board()
    for card in board[1:4]:
        card["is_tapped"] = True
    assert haste_equipment_mana_hint(board, LOCAL, 9) == ""
    board = _board()
    board[0] = {**board[0], "oracle_text": "Equipped creature gets +2/+2.\nEquip {2}"}
    assert haste_equipment_mana_hint(board, LOCAL, 9) == ""  # no haste grant
    board = _board()
    board[0] = {**board[0], "oracle_text": "Equipped creature has haste.\nEquip {5}"}
    assert haste_equipment_mana_hint(board, LOCAL, 9) == ""  # equip costs more than it unlocks
    board = _board()
    for card in board:
        card["controller_seat_id"] = 2
    assert haste_equipment_mana_hint(board, LOCAL, 9) == ""


def test_arena_markup_and_dorks_are_read():
    board = [
        {
            "instance_id": 1,
            "name": "Swiftfoot Boots",
            "type_line": "Artifact — Equipment",
            "oracle_text": "Equipped creature has hexproof and haste.\nEquip {o1}",
            "controller_seat_id": LOCAL,
        },
        {
            "instance_id": 2,
            "name": "Llanowar Elves",
            "type_line": "Creature — Elf Druid",
            "oracle_text": "{oT}: Add {oG}.",
            "controller_seat_id": LOCAL,
            "turn_entered_battlefield": 4,
        },
    ]
    hint = haste_equipment_mana_hint(board, LOCAL, 4)
    assert "Swiftfoot Boots grants haste (equip {1})" in hint
    assert "Llanowar Elves (up to +1 mana" in hint


def test_planner_context_carries_hint_with_live_bridge_menu():
    from arenamcp.coach import CoachEngine

    coach = CoachEngine.__new__(CoachEngine)
    state = {
        "players": [{"seat_id": LOCAL, "is_local": True, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        "turn": {"turn_number": 9, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1"},
        "battlefield": _board(),
        "hand": [],
        "stack": [],
        "_bridge_request_type": "ActionsAvailable",
        "_bridge_actions": [
            {"actionType": "Activate", "grpId": 19737, "instanceId": 454, "abilityGrpId": 2152},
        ],
        "_bridge_can_pass": True,
        "legal_actions": ["Activate Ability: Lightning Greaves", "Pass"],
    }
    context = coach._format_game_context(state, for_planner=True)
    assert "HASTE-EQUIP MANA: Lightning Greaves" in context
