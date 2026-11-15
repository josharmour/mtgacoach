"""Synthetic normal UI/bridge checks of the single, enrolled incoming invitation."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_dev_challenge_pair import guid, mapping, seq

from arenamcp import dev_challenge_invitation as invite
from arenamcp import dev_challenge_pair as gate
from arenamcp.dev_challenge_identity import EnrolledPair, enrollment
from arenamcp.mac_bridge_adapter import BEPINEX_RUNTIME, MacBridgeAdapter
from arenamcp.pipe_adapter import PipeAdapter


@pytest.fixture
def native(monkeypatch, tmp_path):
    key = b"Y" * 32
    token = EnrolledPair(key, "", "").token
    labels = ("Fixture sender#1001", "Fixture receiver#1002")
    actor, receiver = token(labels[0], "sender"), token(labels[1], "receiver")

    def receipt(a, b):
        return dict(
            own=a, friends=[b], get_count=1, num_friends=1, social_enabled=True, social_connected=True
        )

    profile = tmp_path / "private-pair"
    profile.write_text(json.dumps(enrollment(key, receipt(actor, receiver), receipt(receiver, actor))))
    ticket = tmp_path / "private-room"
    monkeypatch.setattr(gate, "source_enabled", lambda: True)
    for k, v in {
        "ROLE": "issuer",
        "DECK_ID": "00000000-0000-0000-0000-000000000003",
        "PAIR_PROFILE": str(profile),
        "ROOM_TICKET": str(ticket),
        "BEST_OF": "1",
        "MATCH_TYPE": "DirectGame",
    }.items():
        monkeypatch.setenv("MTGACOACH_DEV_DC_" + k, v)
    member = {"PlayerId": "sender", "FullDisplayName": labels[0], "DeckId": guid(3)}
    recipient = {"PlayerId": "receiver", "FullDisplayName": labels[1], "DeckId": guid(0)}
    invitation = dict(Sender=member, Recipient=recipient, Status={"e": "Sent", "v": 1})
    common = {
        "ChallengeId": guid(9),
        "ChallengeOwnerId": "sender",
        "ChallengePlayers": mapping([("sender", member)]),
        "Invites": mapping([("receiver", invitation)]),
        "MatchType": {"e": "DirectGame", "v": 0},
        "IsBestOf3": False,
    }
    rooms = {
        "issuer": {
            **common,
            "LocalPlayerId": "sender",
            "LocalPlayerDisplayName": labels[0],
            "Status": {"e": "Setup", "v": 1},
        },
        "acceptor": {
            **common,
            "LocalPlayerId": "receiver",
            "LocalPlayerDisplayName": labels[1],
            "Status": {"e": "None", "v": 0},
        },
    }

    def observed(adapter, timeout, pair):
        is_owner = pair.role == "issuer"
        code = "sender" if is_owner else "receiver"
        other = "receiver" if is_owner else "sender"
        friend = seq([{"FullName": labels[int(is_owner)], "PlayerId": other}])
        room = rooms[pair.role]
        status = gate.classify(
            pair,
            labels[int(not is_owner)],
            code,
            friend,
            room if is_owner else None,
            seq([] if is_owner else [room]),
            True,
        )
        bound, peer = gate._resolve_accounts(pair, labels[int(not is_owner)], code, friend)
        return gate.Observation(status, bound, code, peer, room)

    monkeypatch.setattr(gate, "observe", observed)
    return SimpleNamespace(room=rooms["acceptor"], issuer=rooms["issuer"], ticket=ticket, labels=labels)


class NativeUI:
    """Only public reads of the active social invitation tile may be dispatched."""

    def __init__(self, native, fault=None):
        self.n = native
        self.fault = fault
        self.reads = 0

    def send(self, cmd, timeout):
        self.reads += 1
        assert cmd["action"] == "reflect_batch"
        if self.fault == "transport":
            raise OSError("synthetic private error")
        obj = {
            "class": invite._TILE,
            "isActiveAndEnabled": True,
            "gameObject": {"activeInHierarchy": True},
            "ChallengeSenderFullDisplayName": self.n.labels[0],
            "ChallengeSenderPlayerId": "sender",
            "IncomingChallengeId": guid(9),
        }
        if self.fault == "inactive":
            obj["isActiveAndEnabled"] = False
        if self.fault == "hidden":
            obj["gameObject"]["activeInHierarchy"] = False
        if self.fault == "wrong_sender":
            obj["ChallengeSenderPlayerId"] = "another"
        if self.fault == "wrong_name":
            obj["ChallengeSenderFullDisplayName"] = "changed"
        if self.fault == "wrong_room":
            obj["IncomingChallengeId"] = guid(8)
        if self.fault == "wrapped_room":
            obj["IncomingChallengeId"] = dict(guid(9), k=265)
        if self.fault == "invalid_room":
            obj["IncomingChallengeId"] = {"a": 1}
        values = []
        for i, op in enumerate(cmd["ops"]):
            kind = op["op"]
            if kind == "find":
                assert op["class"] == invite._TILE
                value = obj
            elif kind == "get":
                assert op["member"] in {"gameObject", "IncomingChallengeId"}
                value = values[op["target"]["ref"]][op["member"]]
            elif kind == "expect":
                ref = values[op["target"]["ref"]]
                ok = ref.get("class") == op["class"] if "class" in op else ref[op["member"]] == op["equals"]
                if not ok:
                    return {"ok": False, "failed_op": i, "error": "private fixture error"}
                value = None
            else:
                pytest.fail("Read-only tile check attempted an unsupported operation")
            values.append(value)
        return {"ok": True, "results": values}


@pytest.mark.parametrize("runtime", ["il2cpp", BEPINEX_RUNTIME])
def test_paired_receipt_and_active_tile_use_only_public_reads(native, monkeypatch, runtime):
    ui = NativeUI(native)
    adapter = MacBridgeAdapter(ui.send, runtime)
    assert adapter.handles({"action": "dev_challenge_verify_offer"})
    pipe = PipeAdapter()
    pipe.log = Mock()
    bridge = SimpleNamespace(_connected=True, _mac_adapter=adapter, _send_command=adapter.handle)
    pipe._coach = SimpleNamespace(_autopilot=SimpleNamespace(_gre_bridge=bridge))
    pipe._check_dev_challenge_pair("dev_challenge_record_offer")
    pipe.log.assert_called_with(gate.PairStatus.RECORDED.value)
    assert len(native.ticket.read_bytes()) == 64 and ui.reads == 0
    monkeypatch.setenv("MTGACOACH_DEV_DC_ROLE", "acceptor")
    for _ in range(2):
        pipe._check_dev_challenge_pair("dev_challenge_verify_offer")
        pipe.log.assert_called_with(gate.PairStatus.VERIFIED.value)
    assert ui.reads == 2
    assert not any(word in str(pipe.log.call_args_list) for word in (*native.labels, str(native.ticket)))


@pytest.mark.parametrize(
    "fault",
    [
        "inactive",
        "hidden",
        "wrong_sender",
        "wrong_name",
        "wrong_room",
        "invalid_room",
        "wrapped_room",
        "transport",
        "wrong_ticket",
        "missing_ticket",
        "wrong_role",
        "unset_profile",
        "format",
        "best_of",
        "other_room",
    ],
)
def test_mismatched_offer_never_verifies_or_sends_input(native, monkeypatch, fault):
    ui = NativeUI(native, fault)
    adapter = MacBridgeAdapter(ui.send, BEPINEX_RUNTIME)
    assert adapter.handle({"action": "dev_challenge_record_offer"}, 1)["pair_status"] == "RECORDED"
    if fault != "wrong_role":
        monkeypatch.setenv("MTGACOACH_DEV_DC_ROLE", "acceptor")
    if fault == "unset_profile":
        monkeypatch.delenv("MTGACOACH_DEV_DC_PAIR_PROFILE")
    if fault == "wrong_ticket":
        native.ticket.write_bytes(b"0" * 64)
    if fault == "missing_ticket":
        native.ticket.unlink()
    if fault == "format":
        native.room["MatchType"] = {"e": "DirectGameBrawl", "v": 1}
    if fault == "best_of":
        native.room["IsBestOf3"] = True
    if fault == "other_room":
        native.room["ChallengeId"] = guid(8)
    result = adapter.handle({"action": "dev_challenge_verify_offer", "method": "irrelevant"}, 1)
    assert result["ok"] is False
    assert not any(x in str(result) for x in (*native.labels, "private", str(native.ticket)))


@pytest.mark.parametrize("deck", [guid(0), guid(4), None])
def test_sent_offer_requires_issuer_chosen_deck_in_actual_room(native, deck):
    member = native.issuer["ChallengePlayers"]["entries"]["$items"][0]["value"]
    member["DeckId"] = deck
    ui = NativeUI(native)
    adapter = MacBridgeAdapter(ui.send, BEPINEX_RUNTIME)
    assert adapter.handle({"action": "dev_challenge_record_offer"}, 1)["ok"] is False
    assert not native.ticket.exists() and ui.reads == 0


def test_directory_receipts_are_exclusive_and_idempotent(native, monkeypatch):
    native.ticket.mkdir()
    ui = NativeUI(native)
    adapter = MacBridgeAdapter(ui.send, BEPINEX_RUNTIME)
    for _ in range(2):
        assert adapter.handle({"action": "dev_challenge_record_offer"}, 1)["pair_status"] == "RECORDED"
    (receipt,) = native.ticket.iterdir()
    assert receipt.name == receipt.read_text() + ".ticket"
    original = receipt.read_bytes()
    receipt.write_bytes(b"different room")
    assert adapter.handle({"action": "dev_challenge_record_offer"}, 1)["pair_status"] == "TICKET"
    assert receipt.read_bytes() == b"different room"
    receipt.write_bytes(original)
    monkeypatch.setenv("MTGACOACH_DEV_DC_ROLE", "acceptor")
    assert adapter.handle({"action": "dev_challenge_verify_offer"}, 1)["pair_status"] == "VERIFIED"
    assert ui.reads == 1
