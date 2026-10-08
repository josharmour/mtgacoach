"""Regression tests for the 2026-10-07 Historic Brawl Kami of Bamboo Groves loop.

Turn 7, our main phase, every land tapped (Icetill Explorer + Elvish Mystic
just cast). Player.log, ActionsAvailableReq msgId 194 (gameStateId 149):
Kami of Bamboo Groves' channel ability ({2}{G}, discard: conjure two Forests)
is offered with NO autoTapSolution, every mana ability sits in
inactiveActions, and the coach tagged it
"Activate Ability: Kami of Bamboo Groves [NEED:{2}{G}]".

The typed decision built from the Mac bridge poll still marked the
activation payable=None (only autotap solutions set True; nothing ever set
False), so it survived the payability filter that drops "Cast X (cannot
auto-pay)" — leaving the model Activate Kami / Pass / PlayMdfc. It picked the
channel at 18:03:46 and again at 18:03:51 ("worth the {2}{G} Arena confirms
payable"). Each time the GRE answered with PayCostsReq msgId 197/201:
"paymentActions": {} and a paymentSelection with no mana ids — nothing could
pay it. The autopilot called submit_auto_tap twice ("Pending is
PayCostsRequest, no AutoTapActionsRequest available"), then cancelled; the
cancel never armed the self-cancel guard, so the next window re-picked it.
"""

import json
import time
from unittest.mock import Mock

import pytest

from arenamcp import decisions
from arenamcp.decisions import build_pending_decision
from arenamcp.gamestate import GameObject, GameObjectKind, GameState, Player, Zone, ZoneType
from arenamcp.gamestate_decisions import _handle_actions_available, _handle_pay_costs

SEAT = 2
TURN = 7

# Player.log 18:03:44, GREMessageType_ActionsAvailableReq msgId 194, gameStateId 149.
ACTIONS_AVAILABLE_194 = json.loads(
    """{"type": "GREMessageType_ActionsAvailableReq", "msgId": 194, "gameStateId": 149,
 "prompt": {"promptId": 2}, "actionsAvailableReq": {"actions": [
  {"actionType": "ActionType_Cast", "grpId": 103511, "instanceId": 249, "facetId": 249, "abilityGrpId": 115,
   "sourceId": 249, "manaCost": [{"color": ["ManaColor_Generic"], "count": 3, "abilityGrpId": 115},
   {"color": ["ManaColor_Green"], "count": 2, "abilityGrpId": 115}], "shouldStop": true},
  {"actionType": "ActionType_Cast", "grpId": 29589, "instanceId": 349, "facetId": 349,
   "manaCost": [{"color": ["ManaColor_Generic"], "count": 5}, {"color": ["ManaColor_Green"], "count": 3}],
   "shouldStop": true},
  {"actionType": "ActionType_Cast", "grpId": 81103, "instanceId": 353, "facetId": 353,
   "manaCost": [{"color": ["ManaColor_Green"], "count": 1}], "shouldStop": true},
  {"actionType": "ActionType_Cast", "grpId": 95516, "instanceId": 355, "facetId": 355,
   "manaCost": [{"color": ["ManaColor_Generic"], "count": 7}], "shouldStop": true},
  {"actionType": "ActionType_Cast", "grpId": 90826, "instanceId": 457, "facetId": 457,
   "manaCost": [{"color": ["ManaColor_Generic"], "count": 3}, {"color": ["ManaColor_Green"], "count": 3}],
   "shouldStop": true},
  {"actionType": "ActionType_Activate", "grpId": 81103, "instanceId": 353, "facetId": 353,
   "abilityGrpId": 149684, "manaCost": [{"color": ["ManaColor_Generic"], "count": 2, "abilityGrpId": 149684},
   {"color": ["ManaColor_Green"], "count": 1, "abilityGrpId": 149684}], "shouldStop": true,
   "uniqueAbilityId": 297},
  {"actionType": "ActionType_Pass"},
  {"actionType": "ActionType_PlayMDFC", "grpId": 90827, "instanceId": 457, "facetId": 458, "shouldStop": true}],
 "inactiveActions": [
  {"actionType": "ActionType_Activate", "grpId": 60275, "instanceId": 448, "facetId": 448, "abilityGrpId": 5707,
   "disqualifyingSourceId": 448, "uniqueAbilityId": 379},
  {"actionType": "ActionType_Activate_Mana", "grpId": 70387, "instanceId": 459, "facetId": 459,
   "abilityGrpId": 1005, "uniqueAbilityId": 393},
  {"actionType": "ActionType_Activate_Mana", "grpId": 54163, "instanceId": 501, "facetId": 501,
   "abilityGrpId": 1005, "uniqueAbilityId": 432}]}}"""
)

# Player.log 18:03:46, GREMessageType_PayCostsReq msgId 197, gameStateId 150.
PAY_COSTS_197 = json.loads(
    """{"type": "GREMessageType_PayCostsReq", "msgId": 197, "gameStateId": 150,
 "prompt": {"promptId": 11, "parameters": [{"parameterName": "Cost",
  "type": "ParameterType_NonLocalizedString", "stringValue": "o2oG"}]},
 "payCostsReq": {"manaCost": [
  {"color": ["ManaColor_Generic"], "count": 2, "objectId": 503, "abilityGrpId": 149684},
  {"color": ["ManaColor_Green"], "count": 1, "objectId": 503, "abilityGrpId": 149684}],
  "paymentActions": {},
  "paymentSelection": {"context": "SelectionContext_ManaPool", "optionContext": "OptionContext_Payment",
   "listType": "SelectionListType_Dynamic", "idType": "IdType_ManaId",
   "validationType": "SelectionValidationType_NonRepeatable",
   "minWeight": -2147483648, "maxWeight": 2147483647}},
 "allowCancel": "AllowCancel_Abort", "allowUndo": true}"""
)


def _mac(action_type, grp_id=0, instance_id=0, cost=None, **extra):
    """An action as the Mac bridge poll serialized it (coach log submit_action lines)."""
    action = {
        "actionType": action_type,
        "grpId": grp_id,
        "instanceId": instance_id,
        "facetId": instance_id,
        "assumeCanBePaidFor": False,
        "shouldStop": True,
    }
    if cost:
        action["manaCost"] = [{"color": f'[ "{color}" ]', "count": count} for color, count in cost]
    action.update(extra)
    return action


# The Mac bridge poll for the same request (packet decision [149, 194]).
BRIDGE_POLL_194 = {
    "ok": True,
    "has_pending": True,
    "request_type": "ActionsAvailable",
    "request_class": "ActionsAvailableRequest",
    "can_pass": True,
    "can_cancel": False,
    "game_state_id": 149,
    "msg_id": 194,
    "actions": [
        _mac("Cast", 103511, 249, [("Generic", 3), ("Green", 2)], abilityGrpId=115, sourceId=249),
        _mac("Cast", 29589, 349, [("Generic", 5), ("Green", 3)]),
        _mac("Cast", 81103, 353, [("Green", 1)]),
        _mac("Cast", 95516, 355, [("Generic", 7)]),
        _mac("Cast", 90826, 457, [("Generic", 3), ("Green", 3)]),
        _mac(
            "Activate", 81103, 353, [("Generic", 2), ("Green", 1)], abilityGrpId=149684, uniqueAbilityId=297
        ),
        {"actionType": "Pass"},
        _mac("PlayMdfc", 90827, 457),
    ],
}

NAMES = {
    103511: "The Notary Hobbits",
    29589: "Woodfall Primus",
    81103: "Kami of Bamboo Groves",
    95516: "Ugin, Eye of the Storms",
    90826: "Disciple of Freyalise",
    90827: "Garden of Freyalise",
}

CARD_INFO = {
    60275: {"name": "Library of Alexandria", "type_line": "Land", "oracle_text": "{oT}: Add {oC}."},
    70387: {"name": "Castle Garenbrig", "type_line": "Land", "oracle_text": "{oT}: Add {oG}."},
    79706: {"name": "Boseiju, Who Endures", "type_line": "Legendary Land", "oracle_text": "{oT}: Add {oG}."},
    75553: {"name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "({oT}: Add {oG}.)"},
    54163: {"name": "Elvish Mystic", "type_line": "Creature — Elf Druid", "oracle_text": "{oT}: Add {oG}."},
    96766: {"name": "Icetill Explorer", "type_line": "Creature — Human Scout", "oracle_text": ""},
    103511: {"name": "The Notary Hobbits", "mana_cost": "{3}{G}{G}"},
    29589: {"name": "Woodfall Primus", "mana_cost": "{5}{G}{G}{G}"},
    81103: {"name": "Kami of Bamboo Groves", "mana_cost": "{G}"},
    95516: {"name": "Ugin, Eye of the Storms", "mana_cost": "{7}"},
    90826: {"name": "Disciple of Freyalise", "mana_cost": "{3}{G}{G}{G}"},
    90827: {"name": "Garden of Freyalise", "type_line": "Land"},
    149684: {"name": "Ability"},
}


@pytest.fixture
def card_info(monkeypatch):
    from arenamcp import server

    monkeypatch.setattr(server, "get_card_info", lambda grp_id: dict(CARD_INFO.get(grp_id, {})))


def _board(tapped: bool = True) -> GameState:
    """Our side at 18:03:44: five lands tapped, Elvish Mystic cast this turn."""
    gs = GameState()
    gs.local_seat_id = SEAT
    gs.players[SEAT] = Player(seat_id=SEAT, life_total=25)
    gs.turn_info.turn_number = TURN
    gs.zones[28] = Zone(zone_id=28, zone_type=ZoneType.BATTLEFIELD)
    for instance_id, grp_id, is_tapped, entered in (
        (448, 60275, tapped, 1),
        (459, 70387, tapped, 3),
        (469, 79706, tapped, 5),
        (475, 75553, tapped, 6),
        (498, 75553, tapped, 7),
        (501, 54163, False, TURN),  # summoning sick
        (493, 96766, False, TURN),
    ):
        gs.game_objects[instance_id] = GameObject(
            instance_id=instance_id,
            grp_id=grp_id,
            zone_id=28,
            owner_seat_id=SEAT,
            controller_seat_id=SEAT,
            is_tapped=is_tapped,
            turn_entered_battlefield=entered,
        )
        gs.zones[28].object_instance_ids.append(instance_id)
    return gs


def _decision(monkeypatch, gs, poll=None):
    monkeypatch.setattr(decisions, "_live_game_state", lambda: gs)
    return build_pending_decision(
        poll or BRIDGE_POLL_194,
        resolve_name=lambda grp_id: NAMES.get(grp_id, ""),
        resolve_zone=lambda instance_id: "Hand",
    )


# --- 1. the offer -------------------------------------------------------------------


def test_log_tags_the_channel_need_exactly_as_reported(card_info):
    gs = _board()
    _handle_actions_available(gs, ACTIONS_AVAILABLE_194)
    assert gs.legal_actions == [
        "Cast The Notary Hobbits",
        "Cast Woodfall Primus",
        "Cast Kami of Bamboo Groves",
        "Cast Ugin, Eye of the Storms",
        "Cast Disciple of Freyalise",
        "Activate Ability: Kami of Bamboo Groves [NEED:{2}{G}]",
        "Pass",
        "Action: PlayMDFC",
    ]


def test_typed_decision_marks_the_unpayable_channel_unpayable(card_info, monkeypatch):
    gs = _board()
    _handle_actions_available(gs, ACTIONS_AVAILABLE_194)
    decision = _decision(monkeypatch, gs)

    channel = decision.find("idx:5")
    assert channel.meta["actionType"] == "ActionType_Activate"
    assert channel.payable is False
    assert decision.find("idx:2").payable is False  # Cast Kami, no autotap either


def test_unpayable_channel_never_reaches_the_model(card_info, monkeypatch):
    from arenamcp.play_safety import filter_play_options

    gs = _board()
    _handle_actions_available(gs, ACTIONS_AVAILABLE_194)
    decision = _decision(monkeypatch, gs)

    offered = {option.option_id for option in filter_play_options(decision, {}).options}
    assert offered == {"pass", "idx:7"}  # Pass and the MDFC land the user later played


def test_planner_rejects_the_channel_even_if_the_model_names_it(card_info, monkeypatch):
    from arenamcp.action_planner import ActionPlanner

    gs = _board()
    _handle_actions_available(gs, ACTIONS_AVAILABLE_194)
    decision = _decision(monkeypatch, gs)
    backend = Mock()
    backend.complete.return_value = json.dumps(
        {"option_ids": ["idx:5"], "reasoning": "worth the {2}{G} Arena confirms payable"}
    )

    chosen = ActionPlanner(backend=backend).plan_decision_options(decision, {"local_seat_id": SEAT})

    assert "idx:5" not in chosen
    prompt = backend.complete.call_args.args[1]
    assert "- idx:5:" not in prompt


def test_payable_channel_stays_offered(card_info, monkeypatch):
    """18:03:37, five lands untapped: Arena attached an autotap solution."""
    gs = _board(tapped=False)
    poll = json.loads(json.dumps(BRIDGE_POLL_194))
    poll["actions"][5].update(
        hasAutoTap=True, autoTapActions=[{"instanceId": 459, "manaId": 0}, {"instanceId": 469, "manaId": 0}]
    )
    assert _decision(monkeypatch, gs, poll).find("idx:5").payable is True


def test_activation_without_a_log_verdict_is_not_guessed(monkeypatch):
    """No matching log action (log behind, or a free tap ability): unknown, not unpayable."""
    gs = GameState()
    assert _decision(monkeypatch, gs).find("idx:5").payable is None


# --- 2. the PayCosts nothing can pay ------------------------------------------------


def _pay_costs_state() -> GameState:
    gs = _board()
    gs.zones[35] = Zone(zone_id=35, zone_type=ZoneType.HAND, owner_seat_id=SEAT)
    gs.game_objects[353] = GameObject(
        instance_id=353, grp_id=81103, zone_id=35, owner_seat_id=SEAT, controller_seat_id=SEAT
    )
    gs.zones[35].object_instance_ids.append(353)
    gs.zones[27] = Zone(zone_id=27, zone_type=ZoneType.STACK)
    gs.game_objects[503] = GameObject(
        instance_id=503,
        grp_id=149684,
        zone_id=27,
        owner_seat_id=SEAT,
        controller_seat_id=SEAT,
        object_kind=GameObjectKind.ABILITY,
        parent_instance_id=353,
    )
    gs.zones[27].object_instance_ids.append(503)
    return gs


def test_log_pay_costs_without_any_payment_route(card_info):
    gs = _pay_costs_state()
    _handle_pay_costs(gs, PAY_COSTS_197)
    context = gs.decision_context
    assert context["type"] == "pay_costs"
    assert context["has_autotap"] is False
    assert context["payment_action_count"] == 0
    assert context["pool_mana_count"] == 0
    assert context["no_payment_route"] is True
    assert context["source_parent_instance_id"] == 353


@pytest.mark.parametrize(
    "extra",
    [
        {"autoTapActionsReq": {"autoTapSolutions": [{"autoTapActions": [{"instanceId": 459}]}]}},
        {"paymentActions": {"actions": [{"actionType": "ActionType_Activate_Mana", "instanceId": 459}]}},
        {"paymentSelection": {"context": "SelectionContext_ManaPool", "ids": [1480]}},
    ],
)
def test_log_pay_costs_with_a_payment_route(card_info, extra):
    message = json.loads(json.dumps(PAY_COSTS_197))
    message["payCostsReq"].update(extra)
    gs = _pay_costs_state()
    _handle_pay_costs(gs, message)
    assert gs.decision_context["no_payment_route"] is False


class _PayCostsBridge:
    """The Mac bridge while PayCostsRequest (no AutoTapActions child) is pending."""

    connected = True

    def __init__(self):
        self.auto_tap_calls = 0
        self.cancel_calls = 0

    def connect(self):
        return True

    def get_pending_actions(self):
        return {
            "ok": True,
            "has_pending": True,
            "request_type": "PayCostsReq",
            "request_class": "PayCostsRequest",
            "can_cancel": True,
            "game_state_id": 150,
            "msg_id": 197,
        }

    def submit_auto_tap(self, solution_index=0):
        self.auto_tap_calls += 1
        return False  # "Pending is PayCostsRequest, no AutoTapActionsRequest available"

    def cancel_action(self):
        self.cancel_calls += 1
        return True


def _engine(monkeypatch, bridge):
    import arenamcp.autopilot as autopilot_module
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    monkeypatch.setattr(autopilot_module.time, "sleep", lambda *_: None)
    engine = AutopilotEngine(planner=Mock(), config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = bridge
    engine._pause_for_manual = Mock()
    engine._try_typed_decision_path = lambda game_state, trigger: None  # PayCosts is not a typed family
    return engine


def _channel_option():
    return decisions.DecisionOption(
        "idx:5",
        "Activate: Kami of Bamboo Groves [from hand: Channel — {2}{G}, Discard this card: "
        "Conjure two cards named Forest into your hand.]",
        meta={"actionType": "ActionType_Activate", "instanceId": 353, "abilityGrpId": 149684, "grpId": 81103},
    )


def _snapshot(gs: GameState) -> dict:
    """The trigger snapshot: the log's pay_costs context under the bridge overlay."""
    return {
        "local_seat_id": SEAT,
        "turn": {"turn_number": TURN, "active_player": SEAT},
        "pending_decision": "Pay Costs",
        "decision_context": dict(gs.decision_context),
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "PayCostsReq",
        "_bridge_request_class": "PayCostsRequest",
    }


def test_unpayable_activation_is_cancelled_without_auto_tap_and_withheld(card_info, monkeypatch):
    gs = _pay_costs_state()
    _handle_pay_costs(gs, PAY_COSTS_197)
    bridge = _PayCostsBridge()
    engine = _engine(monkeypatch, bridge)
    snapshot = _snapshot(gs)
    # The typed path just submitted the channel (18:03:46).
    engine._note_typed_play(snapshot, _channel_option())
    engine._note_activation(snapshot, 353, "kami of bamboo groves")

    assert engine.process_trigger(snapshot, "decision_required") is True

    assert bridge.auto_tap_calls == 0, "submit_auto_tap cannot pay a PayCostsReq with no payment route"
    assert bridge.cancel_calls == 1
    engine._pause_for_manual.assert_not_called()
    # Backed out for the rest of the turn, and not counted as a used activation.
    assert engine._self_cancel_withheld(snapshot, "ActionType_Activate", 353, 149684, "kami of bamboo groves")
    assert engine._activation_counts.get((TURN, "iid", 353), 0) == 0


def test_withheld_channel_is_hidden_from_the_next_priority_window(card_info, monkeypatch):
    """18:03:51: after the first cancel the same channel must not be offered again."""
    gs = _pay_costs_state()
    _handle_pay_costs(gs, PAY_COSTS_197)
    bridge = _PayCostsBridge()
    engine = _engine(monkeypatch, bridge)
    snapshot = _snapshot(gs)
    engine._note_typed_play(snapshot, _channel_option())
    engine.process_trigger(snapshot, "decision_required")

    # The next window offers it again; without a log verdict (worst case) the
    # self-cancel guard alone must hide it.
    window = GameState()
    decision = _decision(monkeypatch, window)
    assert decision.find("idx:5").payable is None
    hidden = [
        option.label
        for option in decision.options
        if option.meta.get("actionType") in engine._SELF_CANCEL_KINDS
        and engine._self_cancel_withheld(
            snapshot,
            option.meta["actionType"],
            int(option.meta.get("instanceId") or 0),
            int(option.meta.get("abilityGrpId") or 0),
            engine._play_name(option.label)[1],
        )
    ]
    assert hidden == [decision.find("idx:5").label]


def test_payable_without_auto_pay_hands_our_own_play_to_the_user(card_info, monkeypatch):
    """Mana sources exist but Arena built no Auto Pay child (#414): pay manually, don't cancel."""
    message = json.loads(json.dumps(PAY_COSTS_197))
    message["payCostsReq"]["paymentActions"] = {
        "actions": [{"actionType": "ActionType_Activate_Mana", "grpId": 75553, "instanceId": 571}]
    }
    gs = _pay_costs_state()
    _handle_pay_costs(gs, message)
    bridge = _PayCostsBridge()
    engine = _engine(monkeypatch, bridge)
    snapshot = _snapshot(gs)
    engine._note_typed_play(snapshot, _channel_option())

    assert engine.process_trigger(snapshot, "decision_required") is True

    assert bridge.auto_tap_calls == 2  # the #40 late-child retry still runs
    assert bridge.cancel_calls == 0
    engine._pause_for_manual.assert_called_once()
    assert "Pay for kami of bamboo groves manually" in engine._pause_for_manual.call_args.args[0]


def test_failed_cancel_of_an_unpayable_cost_never_asks_the_user_to_pay(card_info, monkeypatch):
    gs = _pay_costs_state()
    _handle_pay_costs(gs, PAY_COSTS_197)
    bridge = _PayCostsBridge()
    bridge.cancel_action = lambda: False
    engine = _engine(monkeypatch, bridge)
    snapshot = _snapshot(gs)
    engine._note_typed_play(snapshot, _channel_option())

    assert engine.process_trigger(snapshot, "decision_required") is True

    assert bridge.auto_tap_calls == 0
    engine._pause_for_manual.assert_called_once()
    assert engine._pause_for_manual.call_args.args[0].startswith("Cancel Kami of Bamboo Groves")


def test_stale_typed_play_is_not_blamed_for_a_later_cancel(card_info, monkeypatch):
    gs = _pay_costs_state()
    _handle_pay_costs(gs, PAY_COSTS_197)
    bridge = _PayCostsBridge()
    engine = _engine(monkeypatch, bridge)
    snapshot = _snapshot(gs)
    engine._note_typed_play(snapshot, _channel_option())
    noted = engine._last_typed_play
    engine._last_typed_play = (time.monotonic() - 60.0, *noted[1:])

    engine.process_trigger(snapshot, "decision_required")

    assert bridge.cancel_calls == 1
    assert not engine._self_cancel_withheld(
        snapshot, "ActionType_Activate", 353, 149684, "kami of bamboo groves"
    )
