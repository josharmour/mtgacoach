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


def test_unclassified_non_equip_source_keeps_a_consistent_pick():
    # "Tap target creature." is classified harmful since 2026-10-06; phasing is
    # not. Nothing in "phases out" can hurt our own Hobbit: the pick stands.
    planner = _planner('{"option_ids": ["tgt:583"], "reasoning": "phase it out"}')
    state = _target_state(source_oracle="Target creature phases out.", parent_oracle="")
    assert planner.plan_decision_options(_target_decision(583, 591), state) == ["tgt:583"]


def test_unclassified_source_aimed_at_their_creature_still_needs_intent():
    # Phasing out could also protect their creature: without stated intent, decline.
    planner = _planner('{"option_ids": ["tgt:700"], "reasoning": "phase out their blocker"}')
    state = _target_state(source_oracle="Target creature phases out.", parent_oracle="")
    state["battlefield"].append(
        {"instance_id": 700, "name": "Grizzly Bears", "type_line": "Creature — Bear", "controller_seat_id": 2}
    )
    assert planner.plan_decision_options(_target_decision(583, 700), state) == [DECLINE_DECISION]


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

    # The equip was rolled back, so it is not counted as a use...
    assert not engine._activation_exhausted(state, 454, "Lightning Greaves")
    # ...but re-offering what the autopilot itself just cancelled is the
    # activate/cancel loop (bug_20261006_185803): withheld for this turn only.
    bridge.get_pending_actions.return_value = _actions_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    assert bridge.submit_action_by_index.call_count == 1
    assert engine._try_typed_decision_path({**state, "turn": {"turn_number": 11}}, "decision_required")
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


def _relay_state():
    return {
        "match_id": "greaves-relay",
        "local_seat_id": LOCAL,
        "players": [{"seat_id": LOCAL, "is_local": True}],
        "turn": {"turn_number": 9, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1"},
        "battlefield": _board(wearer=583),
        "hand": [{"instance_id": 900, "name": "Large spell", "mana_cost": "{9}"}],
        "stack": [],
    }


def _relay_poll(wearer=583):
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": 230,
        "msg_id": 308,
        "can_pass": True,
        "actions": [
            {"actionType": "ActionType_Activate_Mana", "instanceId": 447},
            {"actionType": "ActionType_Activate_Mana", "instanceId": wearer, "abilityGrpId": 123},
            {"actionType": "ActionType_Activate", "instanceId": 454, "abilityGrpId": 2152},
            {"actionType": "ActionType_Pass"},
        ],
    }


def test_relay_banks_current_wearer_then_moves_haste_to_next_copy():
    from arenamcp.mana import haste_relay_mana_sources

    state = _relay_state()
    assert haste_relay_mana_sources(state) == {583}
    state["battlefield"][1]["is_tapped"] = True
    assert haste_relay_mana_sources(state) == set()  # must equip another first
    state["battlefield"][0]["attached_to_id"] = 590
    assert haste_relay_mana_sources(state) == {590}
    state["battlefield"][2]["is_tapped"] = True
    state["battlefield"][0]["attached_to_id"] = 591
    assert haste_relay_mana_sources(state) == set()  # last wearer can auto-pay the spell
    hint = haste_equipment_mana_hint(state["battlefield"], LOCAL, 9)
    assert "+3 mana" in hint  # already tapped Hobbits are not counted again
    assert "Notary produces colorless, not green" in hint
    assert "activate its tap-for-mana ability" in hint
    assert "same main phase" in hint


def test_relay_requires_main_phase_free_haste_gear_and_a_use():
    import copy

    from arenamcp.mana import haste_relay_mana_sources

    base = _relay_state()
    variants = []
    for phase in ("Phase_Combat", "Phase_Ending"):
        state = copy.deepcopy(base)
        state["turn"]["phase"] = phase
        variants.append(state)
    state = copy.deepcopy(base)
    state["turn"]["active_player"] = 2
    variants.append(state)
    state = copy.deepcopy(base)
    state["stack"] = [{"name": "spell"}]
    variants.append(state)
    for cost in ("", "{G}{G}"):
        state = copy.deepcopy(base)
        state["hand"][0]["mana_cost"] = cost
        variants.append(state)
    for text in (GREAVES.replace("{0}", "{1}"), "Equip {0}"):
        state = copy.deepcopy(base)
        state["battlefield"][0]["oracle_text"] = text
        variants.append(state)
    for key, value in (("attached_to_id", 447), ("controller_seat_id", 2)):
        state = copy.deepcopy(base)
        state["battlefield"][0][key] = value
        variants.append(state)
    state = copy.deepcopy(base)
    state["turn"]["turn_number"] = 10  # no longer needs haste
    variants.append(state)
    for state in variants:
        assert not haste_relay_mana_sources(state)


def test_relay_menu_exposes_only_arena_offered_wearer_and_keeps_indices():
    from arenamcp.decisions import build_pending_decision, submit_option
    from arenamcp.mana import haste_relay_mana_sources

    state = _relay_state()
    decision = build_pending_decision(_relay_poll(), mana_source_ids=haste_relay_mana_sources(state))
    assert decision.find("idx:0") is None  # Forest stays on auto-pay
    assert decision.find("idx:1").meta["hasteRelayMana"]
    assert "Bank mana" in decision.find("idx:1").label
    bridge = MagicMock()
    bridge.submit_action_by_index.return_value = True
    assert submit_option(bridge, decision, ["idx:1"])
    assert bridge.submit_action_by_index.call_args.args == (1,)
    assert bridge.submit_action_by_index.call_args.kwargs["expected"]["instanceId"] == 583
    # An unoffered creature never gains a synthetic action.
    unavailable = build_pending_decision(_relay_poll(wearer=590), mana_source_ids=frozenset({583}))
    assert unavailable.find("idx:1") is None


def test_typed_relay_survives_request_watcher_and_revalidation(monkeypatch):
    import threading

    from tests.test_llm_down_fallback import _Bridge, _engine

    from arenamcp.decisions import build_pending_decision
    from arenamcp.mana import haste_relay_mana_sources
    from arenamcp.request_tracker import decision_fingerprint

    state = _relay_state()
    poll = _relay_poll()
    bridge = _Bridge(poll)
    bridge.submit_action_by_index = MagicMock(return_value=True)
    planner = MagicMock()
    planner.plan_decision_options.return_value = ["idx:1"]
    engine = _engine(monkeypatch, planner, bridge, state)
    engine._await_log_catch_up = lambda *_: state
    assert engine._try_typed_decision_path(state, "decision_required")
    bridge.submit_action_by_index.assert_called_once()
    assert bridge.submit_action_by_index.call_args.args == (1,)
    decision = build_pending_decision(poll, mana_source_ids=haste_relay_mana_sources(state))
    listeners = []
    engine._watch_requests = lambda listener: listeners.append(listener)
    cancel = threading.Event()
    engine._cancel_on_request_change(decision, decision_fingerprint(decision), cancel)
    listeners[0](poll)
    assert not cancel.is_set()
    listeners[0]({**poll, "game_state_id": 231})
    assert cancel.is_set()
    # Each tap changes the equipment guard's board key, allowing the next equip.
    for card in state["battlefield"][1:4]:
        assert not engine._activation_exhausted(state, 454, "Lightning Greaves")
        engine._note_activation(state, 454, "Lightning Greaves")
        assert engine._activation_exhausted(state, 454, "Lightning Greaves")
        card["is_tapped"] = True
