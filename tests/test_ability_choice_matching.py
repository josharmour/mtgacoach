"""A plan that names one ability of a permanent must submit that ability.

bug_20261006_140351: Jace (token planeswalker, instance 443) offered two
Activate actions — abilityGrpId 208424 "-1: Surveil 1." and 208425 "-3: Draw a
card." Both were labelled "Activate: Jace"; the model said it was using the draw
ability, picked idx:1, and the surveil was submitted.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

import arenamcp.gre_action_matcher as matcher
from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.decisions import build_pending_decision
from arenamcp.gre_action_matcher import GREActionRef, match_action_to_gre, resolve_activation
from arenamcp.gre_bridge import GREBridge
from arenamcp.mac_bridge_adapter import MacBridgeAdapter

SURVEIL, DRAW = 208424, 208425
ABILITIES = {SURVEIL: ("-1", "Surveil 1."), DRAW: ("-3", "Draw a card.")}

# The bridge actions at gameStateId 493 / msgId 699, as the mac adapter serialized them.
WITNESS = {"actionType": "Activate", "grpId": 106272, "instanceId": 371, "abilityGrpId": 153236}
JACE_SURVEIL = {"actionType": "Activate", "grpId": 106555, "instanceId": 443, "abilityGrpId": SURVEIL}
JACE_DRAW = {"actionType": "Activate", "grpId": 106555, "instanceId": 443, "abilityGrpId": DRAW}
BRIDGE_ACTIONS = [
    WITNESS,
    JACE_SURVEIL,
    JACE_DRAW,
    {"actionType": "Activate_Mana", "grpId": 106529, "instanceId": 245, "abilityGrpId": 1002},
    {"actionType": "Pass"},
    {"actionType": "FloatMana"},
]
POLL = {
    "has_pending": True,
    "request_type": "ActionsAvailable",
    "request_class": "ActionsAvailableRequest",
    "can_pass": True,
    "game_state_id": 493,
    "msg_id": 699,
    "actions": BRIDGE_ACTIONS,
}
JACE_CARD = {
    "instance_id": 443,
    "grp_id": 106555,
    "name": "Jace",
    "oracle_text": "Surveil 1.\nDraw a card.",
    "type_line": "Token Planeswalker — Jace",
    "card_types": ["Planeswalker"],
    "controller_seat_id": 2,
    "owner_seat_id": 2,
    "counters": {"Loyalty": 4},
    "turn_entered_battlefield": 20,
}
WITNESS_CARD = {
    "instance_id": 371,
    "grp_id": 106272,
    "name": "Undulating Witness",
    "oracle_text": "Flying\n{o2}: This creature gets +1/-1 until end of turn.",
    "card_types": ["Creature"],
    "controller_seat_id": 2,
    "owner_seat_id": 2,
    "power": 4,
    "toughness": 6,
    "turn_entered_battlefield": 12,
}
GAME_OBJECTS = {443: JACE_CARD, 371: WITNESS_CARD}


@pytest.fixture(autouse=True)
def _known_abilities(monkeypatch):
    """Stand in for the local MTGA database (Abilities.LoyaltyCost + text)."""
    monkeypatch.setattr(matcher, "describe_ability", lambda aid: ABILITIES.get(aid, ("", "")))


def _activate(card_name: str, **extra) -> GameAction:
    action = GameAction(action_type=ActionType.ACTIVATE_ABILITY, card_name=card_name)
    for key, value in extra.items():
        setattr(action, key, value)
    return action


# --- matcher -------------------------------------------------------------


@pytest.mark.parametrize("hint", ["Jace: Draw a card", "Jace -3", "Jace −3", "Jace (draw a card.)"])
def test_plan_naming_the_draw_ability_matches_the_draw_action(hint):
    ref = match_action_to_gre(_activate(hint), BRIDGE_ACTIONS, GAME_OBJECTS)
    assert ref is not None and ref.ability_grp_id == DRAW and ref.instance_id == 443


def test_plan_naming_the_surveil_ability_matches_the_surveil_action():
    ref = match_action_to_gre(_activate("Jace -1: Surveil 1"), BRIDGE_ACTIONS, GAME_OBJECTS)
    assert ref is not None and ref.ability_grp_id == SURVEIL


def test_explicit_ability_id_wins():
    ref = match_action_to_gre(_activate("Jace", ability_grp_id=DRAW), BRIDGE_ACTIONS, GAME_OBJECTS)
    assert ref is not None and ref.ability_grp_id == DRAW


def test_ability_id_no_longer_offered_is_refused_not_substituted():
    ref = match_action_to_gre(_activate("Jace", ability_grp_id=DRAW), [WITNESS, JACE_SURVEIL], GAME_OBJECTS)
    assert ref is None


def test_bare_source_name_with_two_abilities_is_refused(caplog):
    with caplog.at_level(logging.WARNING, logger="arenamcp.gre_action_matcher"):
        ref = match_action_to_gre(_activate("Jace"), BRIDGE_ACTIONS, GAME_OBJECTS)
    assert ref is None
    assert "Refusing ACTIVATE_ABILITY 'Jace'" in caplog.text


def test_hint_naming_both_abilities_is_ambiguous():
    assert resolve_activation([JACE_SURVEIL, JACE_DRAW], hint="Jace -1 or -3") is None


def test_single_ability_source_still_matches_by_name():
    ref = match_action_to_gre(_activate("Undulating Witness"), BRIDGE_ACTIONS, GAME_OBJECTS)
    assert ref is not None and ref.ability_grp_id == 153236


def test_loyalty_numbers_inside_pt_changes_are_not_costs():
    # "+1/-1" is a P/T change, not a loyalty cost.
    assert resolve_activation([JACE_SURVEIL, JACE_DRAW], hint="Jace gets +1/-1") is None


# --- typed-decision path (the incident path) ----------------------------


class _CapturingBackend:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.response


def _typed_engine(response: str, poll: dict = POLL):
    backend = _CapturingBackend(response)
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = poll
    bridge.submit_action_by_index.return_value = True
    bridge.submit_pass.return_value = True
    engine = AutopilotEngine(
        planner=ActionPlanner(backend=backend),
        mapper=MagicMock(),
        controller=MagicMock(),
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=False),
    )
    engine._gre_bridge = bridge
    engine._request_tracker = MagicMock()
    engine._request_tracker.may_submit.return_value = True
    engine._request_tracker.exhausted.return_value = False
    return engine, bridge, backend


def _typed_state() -> dict:
    return {
        "turn": {"turn_number": 20, "phase": "Phase_Main1", "active_player": 2, "priority_player": 2},
        "players": [
            {"seat_id": 1, "life_total": 15},
            {"seat_id": 2, "life_total": 13, "is_local": True},
        ],
        "local_seat_id": 2,
        "battlefield": [JACE_CARD, WITNESS_CARD],
        "hand": [],
        "stack": [],
        "_bridge_connected": True,
    }


def _decision_prompt(backend: _CapturingBackend) -> str:
    return next(prompt for prompt in backend.prompts if "PENDING DECISION: ActionsAvailable" in prompt)


def test_sibling_loyalty_abilities_reach_the_model_with_their_text():
    engine, bridge, backend = _typed_engine(
        '{"option_ids": ["idx:2"], "reasoning": "Empty hand; Jace -3 draws a card."}'
    )
    assert engine._try_typed_decision_path(_typed_state(), "decision_required") is True
    prompt = _decision_prompt(backend)
    assert "- idx:1: Activate: Jace [-1: Surveil 1.]" in prompt
    assert "- idx:2: Activate: Jace [-3: Draw a card.]" in prompt
    # A single-ability source keeps its plain label.
    assert "- idx:0: Activate: Undulating Witness " in prompt
    bridge.submit_action_by_index.assert_called_once()
    assert bridge.submit_action_by_index.call_args.args[0] == 2


def test_indistinguishable_sibling_activations_are_withheld(monkeypatch, caplog):
    # Ability text unavailable: the model cannot know which index is which.
    monkeypatch.setattr(matcher, "describe_ability", lambda aid: ("", ""))
    engine, bridge, backend = _typed_engine('{"option_ids": ["idx:1"], "reasoning": "Draw with Jace."}')
    with caplog.at_level(logging.WARNING, logger="arenamcp.autopilot"):
        assert engine._try_typed_decision_path(_typed_state(), "decision_required") is True
    prompt = _decision_prompt(backend)
    assert "idx:1" not in prompt and "idx:2" not in prompt
    assert "withholding" in caplog.text
    submitted = [call.args[0] for call in bridge.submit_action_by_index.call_args_list]
    assert 1 not in submitted and 2 not in submitted


def test_label_helper_keeps_known_siblings_and_drops_duplicates():
    decision = build_pending_decision(POLL, resolve_name=lambda grp: {106555: "Jace"}.get(grp, "Card"))
    labelled = AutopilotEngine._label_sibling_activations(
        decision, describe=lambda aid: ("", "Draw a card.") if aid in ABILITIES else ("", "")
    )
    # Same text for two different abilities cannot be told apart: both withheld.
    assert {o.option_id for o in labelled.options} == {"idx:0", "pass"}
    labelled = AutopilotEngine._label_sibling_activations(decision, describe=ABILITIES.get)
    assert [o.label for o in labelled.options if o.option_id in {"idx:1", "idx:2"}] == [
        "Activate: Jace [-1: Surveil 1.]",
        "Activate: Jace [-3: Draw a card.]",
    ]
    assert AutopilotEngine._activation_source_name(labelled.find("idx:2").label) == "Jace"


# --- legacy plan execution guard ------------------------------------------


def _legacy_engine() -> AutopilotEngine:
    engine = AutopilotEngine(
        planner=MagicMock(),
        mapper=MagicMock(),
        controller=MagicMock(),
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=False),
    )
    engine._bridge_preloaded_actions = BRIDGE_ACTIONS
    return engine


def test_legacy_activation_without_an_ability_is_refused():
    engine = _legacy_engine()
    action = _activate("Jace")
    assert engine._pin_activation_ability(action, _typed_state()) is False
    assert action.gre_action_ref is None


def test_legacy_activation_naming_the_draw_is_pinned_to_it():
    engine = _legacy_engine()
    action = _activate("Jace: Draw a card")
    assert engine._pin_activation_ability(action, _typed_state()) is True
    assert action.gre_action_ref.ability_grp_id == DRAW


def test_legacy_activation_with_a_resolved_ref_is_kept():
    engine = _legacy_engine()
    action = _activate("Jace", gre_action_ref=GREActionRef.from_raw(JACE_DRAW))
    assert engine._pin_activation_ability(action, _typed_state()) is True
    assert action.gre_action_ref.ability_grp_id == DRAW


def test_legacy_single_ability_activation_is_untouched():
    engine = _legacy_engine()
    action = _activate("Undulating Witness")
    assert engine._pin_activation_ability(action, _typed_state()) is True
    assert action.gre_action_ref is None


# --- bridge matching and identity guard ------------------------------------


def test_bridge_match_requires_the_requested_ability():
    find = GREBridge._find_matching_action
    assert find(BRIDGE_ACTIONS, "Activate", 106555, 443, DRAW) == 2
    assert find(BRIDGE_ACTIONS, "Activate", 106555, 443, SURVEIL) == 1
    # Draw no longer offered: never fall back to the surveil.
    assert find([WITNESS, JACE_SURVEIL], "Activate", 106555, 443, DRAW) is None
    # No ability requested and two Jace abilities tie: ambiguous.
    assert find(BRIDGE_ACTIONS, "Activate", 106555, 443, 0) is None
    # Unrelated single-ability sources are unaffected.
    assert find(BRIDGE_ACTIONS, "Activate", 106272, 371, 0) == 0


def test_submit_by_index_carries_the_ability_identity():
    bridge = GREBridge()
    commands: list[dict] = []
    bridge._send_safe = lambda cmd, timeout=None: commands.append(cmd) or {"ok": True}  # type: ignore[method-assign]
    assert bridge.submit_action_by_index(2, expected=JACE_DRAW)
    assert commands[0]["expected_ability_grp_id"] == DRAW
    assert commands[0]["expected_instance_id"] == 443


def test_mac_adapter_refuses_a_stale_index_pointing_at_another_ability():
    from tests.test_mac_bridge_adapter import FakeGame, action, actions_request

    game = FakeGame(
        actions_request(
            action(11, "Activate", 106555, 443, abilityGrpId_=SURVEIL),
            action(12, "Activate", 106555, 443, abilityGrpId_=DRAW),
        )
    )
    adapter = MacBridgeAdapter(game.send)
    stale = adapter.handle(
        {
            "action": "submit_action",
            "action_index": 0,
            "expected_instance_id": 443,
            "expected_grp_id": 106555,
            "expected_ability_grp_id": DRAW,
        }
    )
    assert stale["ok"] is False and "identity mismatch" in stale["error"]
    assert game.submits() == []
    ok = adapter.handle({"action": "submit_action", "action_index": 1, "expected_ability_grp_id": DRAW})
    assert ok["ok"] is True
