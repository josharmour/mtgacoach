"""The native submission and the new packet share the exact public assignment IDs."""

import json
from unittest.mock import Mock

import pytest
from tests.mac_bridge_fakes import FakeGame, adapter_for, enum, listing

from arenamcp import match_packets
from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.combat_receipt import bind_combat_confirmation, bind_combat_state, record_native_combat
from arenamcp.decision_snapshot import game_state_from_snapshot
from arenamcp.gre_bridge import GREBridge
from arenamcp.session_review import _choice

ACTOR, OTHER, WALKER = 7201, 7202, 8103
PLAYER = {"$h": 12, "type_": enum("Player", 1), "playerSystemSeatId_": 2}
TARGET = {"$h": 13, "type_": enum("Planeswalker", 2), "planeswalkerInstanceId_": WALKER}


def attack_request(selected=None):
    attacker = {
        "$h": 32,
        "attackerInstanceId_": ACTOR,
        "legalDamageRecipients_": listing(PLAYER, TARGET),
        "selectedDamageRecipient_": None,
    }
    return {
        "$c": "GreClient.Rules.DeclareAttackerRequest",
        "$h": 101 if selected is None else 102,
        "QualifiedAttackers": listing(attacker),
        "Attackers": listing({**attacker, "$h": 35, "selectedDamageRecipient_": selected}),
        "debugLabel": "PRIVATE-CANARY",
    }


def block_request(assigned=False):
    return {
        "$c": "GreClient.Rules.DeclareBlockersRequest",
        "$h": 103 if assigned else 104,
        "AllBlockers": listing(
            {
                "$h": 35,
                "blockerInstanceId_": ACTOR,
                "selectedAttackerInstanceIds_": listing(OTHER) if assigned else listing(),
                "attackerInstanceIds_": listing() if assigned else listing(OTHER, WALKER),
            }
        ),
        "debugLabel": "PRIVATE-CANARY",
    }


@pytest.mark.parametrize("attack", [True, False])
def test_native_execution_through_bridge_handler_and_packet(tmp_path, monkeypatch, attack):
    initial = attack_request() if attack else block_request()
    final = attack_request(PLAYER) if attack else block_request(True)
    game = FakeGame(initial, {"CanSubmit": True}, gsid=54, msg=101)
    adapter = adapter_for(game)
    bridge = GREBridge()
    bridge._connected = True

    def dispatch(cmd, timeout=None):
        response = adapter.handle(cmd, timeout)
        if response.get("needs_finalize"):
            game.requests = [final]
            game.gsid, game.msg = 55, 104
        return response

    monkeypatch.setattr(bridge, "_send_command", dispatch)
    monkeypatch.setattr(bridge, "connect", lambda: True)
    packet = match_packets.MatchPacket("synth-round")
    monkeypatch.setattr(match_packets, "PACKETS_DIR", tmp_path)
    packet.start_journal()
    monkeypatch.setattr(match_packets, "_current_packet", packet)
    state = {
        "turn": {"turn_number": 9, "phase": "Combat", "active_player": 1 if attack else 2},
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "battlefield": [
            {"instance_id": ACTOR, "name": "Copper Courier", "controller_seat_id": 1},
            {"instance_id": OTHER, "name": "Reed Visitor", "controller_seat_id": 2},
        ],
    }
    pilot = _BridgeSubmitMixin()
    pilot._gre_bridge = bridge
    pilot._get_game_state = lambda: state
    pilot._find_instance_id = lambda name, battlefield, local: ACTOR
    pilot._attack_override = Mock(return_value=None)
    pilot._log_execution_path = Mock()
    pilot._gre_bridge_failed_methods = set()
    poll = bridge.get_pending_actions()
    if attack:
        response = {"action_type": "declare_attackers", "attacker_names": ["Copper Courier"]}
        state["decision_context"] = {"type": "declare_attackers", "raw_attackers": poll["attackers"]}
        state["legal_actions"] = ["Attack with: Copper Courier", "Done (confirm attackers)"]
        options = [f"attack:{ACTOR}:player:2", f"attack:{ACTOR}:planeswalker:{WALKER}"]
    else:
        response = {
            "action_type": "declare_blockers",
            "blocker_assignments": {"Copper Courier": "Reed Visitor"},
        }
        state["decision_context"] = {"type": "declare_blockers", "raw_blockers": poll["blockers"]}
        state["legal_actions"] = ["Block with: Copper Courier", "Done (confirm blockers)"]
        options = [f"block:{ACTOR}:attacker:{OTHER}", f"block:{ACTOR}:attacker:{WALKER}"]

    def parse(board):
        planner = ActionPlanner.__new__(ActionPlanner)
        planner._last_menu = board["legal_actions"]
        plan = planner._parse_response(
            json.dumps(response), planner._last_menu, board["decision_context"], game_state=board
        )
        [action] = plan.actions
        return action

    action = parse(state)
    if attack:
        assert action.attacker_instance_ids == [ACTOR]
        result = pilot._try_bridge_declare_attackers(action)
    else:
        assert action.blocker_instance_assignments == {ACTOR: OTHER}
        result = pilot._try_gre_bridge_blockers(action)
    assert result.success
    row = packet.decisions[-1]
    pending = row["pending_decision"]
    assert pending["request_id"] == [54, 101] and row["submitted_request_id"] == [55, 104]
    assert [v["option_id"] for v in pending["options"]] == options
    assert row["chosen_options"] == [options[0]]
    assert row["display_receipt"]["chosen"] == packet.recent_decisions()[0]["chosen"]
    assert "Copper Courier" in _choice(row)
    assert parse(game_state_from_snapshot(row["state_snapshot"])) == action
    assert "PRIVATE-CANARY" not in json.dumps(row)
    assert json.loads(packet.journal.read_text().splitlines()[-1])["entry"]["chosen_options"] == [options[0]]


def execute(initial, final=None, *, refresh=(55, 104), source=(54, 101), ack=(55, 104), update=True):
    game = FakeGame(initial, {"CanSubmit": True}, gsid=54, msg=101)
    native = adapter_for(game)
    with record_native_combat() as record:
        bind_combat_state({}, {"game_state_id": source[0], "msg_id": source[1]})
        if update:
            native.handle(
                {
                    "action": "submit_blockers",
                    "assignments": [{"blockerInstanceId": ACTOR, "attackerInstanceIds": [OTHER]}],
                }
            )
            game.requests = [final]
            game.gsid, game.msg = refresh
            bind_combat_confirmation(ack)
        native.handle({"action": "submit_blockers", "assignments": []})
        return record.receipt


@pytest.mark.parametrize(
    "fault",
    [
        "truncated",
        "missing",
        "duplicate",
        "invalid_id",
        "old_final",
        "wrong_final",
        "selection_changed",
        "unseen_initial",
        "changed_offer",
    ],
)
def test_partial_or_unrelated_request_has_no_numeric_receipt(fault):
    first = block_request()
    final = block_request(True)
    if fault == "truncated":
        first["AllBlockers"]["$n"] = 2
    if fault == "missing":
        del first["AllBlockers"]["$items"][0]["selectedAttackerInstanceIds_"]
    if fault == "duplicate":
        first["AllBlockers"]["$items"][0]["attackerInstanceIds_"] = listing(OTHER, OTHER)
    if fault == "invalid_id":
        first["AllBlockers"]["$items"][0]["attackerInstanceIds_"] = listing(OTHER, True)
    if fault == "selection_changed":
        final["AllBlockers"]["$items"][0]["selectedAttackerInstanceIds_"] = listing(WALKER)
    if fault == "changed_offer":
        final["AllBlockers"]["$items"][0]["attackerInstanceIds_"] = listing(8901)
    kwargs = {
        "old_final": {"refresh": (54, 101)},
        "wrong_final": {"ack": (55, 103)},
        "unseen_initial": {"source": (52, 90)},
    }
    assert execute(first, final, **kwargs.get(fault, {})) is None


def test_no_blocks_has_empty_selection_and_two_offered_edges():
    result = execute(block_request(), update=False)
    assert result.chosen == () and len(result.options) == 2


def test_empty_native_confirm_from_done_button_keeps_typed_offer(monkeypatch):
    from arenamcp.gre_bridge import BridgeDecisionPoller

    game = FakeGame(block_request(), {"CanSubmit": True})
    adapter = adapter_for(game)
    bridge = GREBridge()
    bridge._connected = True
    monkeypatch.setattr(bridge, "_send_command", adapter.handle)
    monkeypatch.setattr(bridge, "connect", lambda: True)
    packet = match_packets.MatchPacket("synth-confirm")
    monkeypatch.setattr(match_packets, "_current_packet", packet)
    pilot = _BridgeSubmitMixin()
    pilot._gre_bridge = bridge
    pilot._gre_bridge_failed_methods = set()
    pilot._log_execution_path = Mock()
    trigger = BridgeDecisionPoller(bridge).poll()
    assert (trigger["game_state_id"], trigger["msg_id"]) == (game.gsid, game.msg)
    state = {
        "_bridge_trigger": trigger,
        "_bridge_request_class": "DeclareBlockersRequest",
        "_bridge_has_pending": True,
        "_bridge_connected": True,
        "turn": {"turn_number": 8},
    }
    pilot._get_game_state = lambda: state
    result = pilot._try_gre_bridge(GameAction(action_type=ActionType.CLICK_BUTTON, card_name="done"), state)
    assert result.success
    row = packet.decisions[-1]
    assert len(row["pending_decision"]["options"]) == 2 and row["chosen_options"] == []
    assert packet.recent_decisions()[0]["chosen"] == ["No blocks"]


@pytest.mark.parametrize(
    "fault",
    [
        "seat_3",
        "seat_65535",
        "seat_bool",
        "recipient_id_alias",
        "oneof_alias",
        "attached_wrong_class",
        "type_alias",
        "actor_alias",
        "recipient_handle",
        "actor_handle",
        "request_handle",
        "list_handle",
        "menu_alias",
        "linked_original_handle",
        "linked_original_key",
        "original_handle",
        "original_bool_id",
        "original_key_alias",
        "original_msg_alias",
        "original_wrong_type",
        "original_wrong_family",
        "attached_ref_with_identity",
    ],
)
def test_ambiguous_native_field_or_handle_preserves_execution_without_receipt(fault):
    from copy import deepcopy

    request = deepcopy(attack_request())
    menu = request["QualifiedAttackers"]
    actor = menu["$items"][0]
    recipient = actor["legalDamageRecipients_"]["$items"][0]
    faults = {
        "seat_3": (recipient, "playerSystemSeatId_", 3),
        "seat_65535": (recipient, "playerSystemSeatId_", 65535),
        "seat_bool": (recipient, "playerSystemSeatId_", True),
        "recipient_id_alias": (recipient, "m_PlayerSystemSeatId", 1),
        "oneof_alias": (recipient, "m_Id", 12),
        "attached_wrong_class": (
            request,
            "_originalMessage",
            {"$h": 1, "$ref": True, "$c": "AnotherMessage"},
        ),
        "type_alias": (recipient, "m_Type", enum("Planeswalker", 2)),
        "actor_alias": (actor, "m_AttackerInstanceId", 9911),
        "recipient_handle": (recipient, "$h", True),
        "actor_handle": (actor, "$h", False),
        "request_handle": (request, "$h", 0),
        "list_handle": (menu, "$h", -1),
        "menu_alias": (request, "m_QualifiedAttackers", listing()),
        "linked_original_handle": (request, "_originalMessage", {"$ref": True, "$h": 1992}),
        "linked_original_key": (request, "_originalMessage", {"$h": 1, "gameStateId_": 53}),
        "attached_ref_with_identity": (request, "_originalMessage", {"$ref": True, "$h": 1, "msgId_": 77}),
    }
    if fault in faults:
        node, member, value = faults[fault]
        node[member] = value
    game = FakeGame(request, {"CanSubmit": True}, gsid=54, msg=101)

    def send(command, timeout=None):
        response = game.send(command, timeout)
        if not response.get("ok"):
            return response
        for op, obj in zip(command["ops"], response["results"], strict=True):
            if op.get("member") == "OriginalMessage":
                originals = {
                    "original_handle": ("$h", 2**32),
                    "original_bool_id": ("gameStateId_", True),
                    "original_key_alias": ("m_GameStateId", 171),
                    "original_msg_alias": ("MsgId", 13),
                    "original_wrong_type": ("$c", "UnrelatedMessage"),
                    "original_wrong_family": ("type_", enum("SelectTargetsReq", 19)),
                }
                if fault in originals:
                    member, value = originals[fault]
                    obj[member] = value
        return response

    from arenamcp.mac_bridge_adapter import MacBridgeAdapter

    with record_native_combat() as recording:
        bind_combat_state({}, {"game_state_id": 54, "msg_id": 101})
        result = MacBridgeAdapter(send).handle({"action": "submit_attackers", "attackers": []})
    assert result["ok"] is True
    assert not recording.receipt
    assert game.submits()[-1][-1]["method"] == "SubmitAttackers"


def test_conflicting_blocker_alias_withholds_the_numeric_receipt():
    initial = block_request()
    initial["AllBlockers"]["$items"][0]["m_BlockerInstanceId"] = 9999
    assert execute(initial, block_request(True)) is None
