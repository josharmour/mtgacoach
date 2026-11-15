import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp import dev_challenge_pair as gate
from arenamcp.mac_bridge_adapter import BEPINEX_RUNTIME, MacBridgeAdapter
from arenamcp.pipe_adapter import PipeAdapter


def seq(rows):
    return {"$n": len(rows), "$items": rows}


def mapping(rows):
    return {"count": len(rows), "entries": seq([{"key": k, "value": v} for k, v in rows])}


def guid(i):
    return dict(zip("abcdefghijk", [0] * 10 + [i], strict=True))


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(gate, "source_enabled", lambda: True)
    values = {
        "ROLE": "acceptor",
        "LOCAL_FULL_NAME": "Fixture A#0001",
        "OPPONENT_FULL_NAME": "Fixture B#0002",
        "DECK_ID": str(uuid.UUID(int=3)),
    }
    for k, v in values.items():
        monkeypatch.setenv("MTGACOACH_DEV_DC_" + k, v)
    p = gate.configured_pair()
    own = {"PlayerId": "a", "FullDisplayName": p.local}
    peer = {"PlayerId": "b", "FullDisplayName": p.peer}
    room = {
        "ChallengeId": guid(5),
        "Status": {"e": "Setup", "v": 1},
        "LocalPlayerId": "a",
        "LocalPlayerDisplayName": p.local,
        "ChallengeOwnerId": "b",
        "ChallengePlayers": mapping([("b", peer)]),
        "Invites": mapping([("a", {"Sender": peer, "Recipient": own, "Status": {"e": "Sent", "v": 1}})]),
    }
    return p, seq([{"FullName": p.peer, "PlayerId": "b"}]), room


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "local",
        "friend",
        "ambiguous",
        "seat",
        "other_sender",
        "other_recipient",
        "no_owner",
        "unreadable",
        "foreign_lobby",
        "extra_invite",
        "wrong_role",
        "missing_deck",
    ],
)
def test_acceptor_checks_exact_pair_without_leaking_handles(setup, fault):
    p, f, r = setup
    local = p.local
    inc = seq([r])
    if fault == "local":
        local = "different local"
    if fault == "friend":
        f["$items"][0]["PlayerId"] = "a"
    if fault == "ambiguous":
        f = seq(f["$items"] * 2)
    if fault == "seat":
        r["LocalPlayerId"] = "unexpected"
    if fault == "other_sender":
        r["Invites"]["entries"]["$items"][0]["value"]["Sender"] = {
            "PlayerId": "other",
            "FullDisplayName": p.peer,
        }
    if fault == "other_recipient":
        r["Invites"]["entries"]["$items"][0]["value"]["Recipient"] = {
            "PlayerId": "a",
            "FullDisplayName": "different",
        }
    if fault == "no_owner":
        r["ChallengePlayers"] = mapping([])
    if fault == "unreadable":
        inc["$n"] = 2
    if fault == "foreign_lobby":
        r["ChallengeOwnerId"] = "outside"
    if fault == "extra_invite":
        inc = seq([r, r])
    if fault == "wrong_role":
        p = gate.Pair("issuer", p.local, p.peer, p.deck)
    if fault is None:
        assert "incoming invitation agree" in gate.classify(p, local, "a", f, None, inc, True)
        return
    with pytest.raises(gate.PairUnavailable) as e:
        gate.classify(p, local, "a", f, None, inc, fault != "missing_deck")
    assert p.local not in str(e.value) and p.peer not in str(e.value) and p.deck not in str(e.value)


def test_unpaired_existing_lobby_cannot_be_adopted(setup):
    p, f, r = setup
    r["ChallengePlayers"] = mapping(
        [
            ("a", {"PlayerId": "a", "FullDisplayName": p.local, "DeckId": guid(0)}),
            ("b", {"PlayerId": "b", "FullDisplayName": p.peer}),
        ]
    )
    assert "active lobby matches" in gate.classify(p, p.local, "a", f, r, seq([]), True)
    with pytest.raises(gate.PairUnavailable):
        gate.classify(gate.Pair("issuer", p.local, p.peer, p.deck), p.local, "a", f, r, seq([]), True)
    r["ChallengePlayers"] = mapping(
        [
            ("a", {"PlayerId": "a", "FullDisplayName": p.local, "DeckId": guid(0)}),
            ("c", {"PlayerId": "c", "FullDisplayName": "another"}),
        ]
    )
    with pytest.raises(gate.PairUnavailable):
        gate.classify(p, p.local, "a", f, r, seq([]), True)


@pytest.mark.parametrize("runtime", ["il2cpp", BEPINEX_RUNTIME])
@pytest.mark.parametrize("roster_empty", [True, False])
def test_normal_bridge_and_engine_project_only_a_generic_local_result(setup, runtime, roster_empty):
    p, f, r = setup
    operations = []

    def send(command, timeout=None):
        assert command["action"] == "reflect_batch"
        ops = command["ops"]
        operations.extend(ops)
        results = []
        for op in ops:
            name = op.get("member") or op.get("method") or op.get("class")
            value = {
                "_pvpChallengeController": {"$h": 5},
                "DisplayName": p.local,
                "PersonaID": "a",
                "GetFriends": seq([]) if roster_empty else f,
                "Connected": True,
                "IsSocialEnabled": True,
                "Count": 0 if roster_empty else 1,
                "GetAllChallenges": {"count": 1, "freeCount": 0},
                "GetActiveCurrentChallengeData": None,
                "GetIncomingChallengeRequests": seq([r]),
                "GetAllCachedDecks": seq([{"$h": 8}]),
                "DeckId": guid(3),
            }.get(name, {"$h": 1})
            results.append(value)
        return {"ok": True, "results": results}

    adapter = MacBridgeAdapter(send, runtime)
    action = {"action": "dev_challenge_preflight"}
    assert adapter.handles(action)
    bridge = SimpleNamespace(_mac_adapter=adapter, _connected=True, _send_command=adapter.handle)
    pipe = PipeAdapter()
    pipe._coach = SimpleNamespace(_autopilot=SimpleNamespace(_gre_bridge=bridge))
    pipe.log = Mock()
    pipe._check_dev_challenge_pair()
    if roster_empty:
        pipe.log.assert_called_once_with(gate.PairStatus.REFRESHING.value)
        assert {op["method"] for op in operations if op["op"] == "call"} == {
            "GetFriends",
            "GetAllChallenges",
            "GetActiveCurrentChallengeData",
            "GetIncomingChallengeRequests",
            "RefreshFriendsData",
        }
        return
    assert pipe.log.call_args.args == ("The configured account, friend, deck and incoming invitation agree.",)
    assert {op["method"] for op in operations if op["op"] == "call"} == {
        "GetFriends",
        "GetAllChallenges",
        "GetActiveCurrentChallengeData",
        "GetIncomingChallengeRequests",
        "GetAllCachedDecks",
    }
    assert all(op["op"] in {"find", "get", "static", "call"} for op in operations)
    assert not any(h in str(operations) for h in (p.local, p.peer, p.deck))


def test_release_or_incomplete_profile_cannot_probe_the_game(monkeypatch, setup):
    p, _, _ = setup
    monkeypatch.setenv("MTGACOACH_DEV_DC_LOCAL_FULL_NAME", p.peer)
    adapter = MacBridgeAdapter(Mock(side_effect=AssertionError("must not dispatch")), "il2cpp")
    response = adapter.handle({"action": "dev_challenge_preflight"}, 5)
    assert response["ok"] is False and p.peer not in str(response)
    monkeypatch.setattr(gate, "source_enabled", lambda: False)
    response = adapter.handle({"action": "dev_challenge_preflight"}, 5)
    assert response["ok"] is False and response["pair_status"] == "DISABLED"


@pytest.mark.parametrize("fault", ["alias", "list", "slot", "status", "stale", "mixed", "deck", "unbound"])
def test_incomplete_or_stale_native_evidence_is_never_readiness(setup, fault):
    p, f, r = setup
    own = {"PlayerId": "a", "FullDisplayName": p.local, "DeckId": guid(3)}
    peer = {"PlayerId": "b", "FullDisplayName": p.peer}
    r["ChallengePlayers"] = mapping([("a", own), ("b", peer)])
    incoming = seq([])
    if fault == "alias":
        f = seq([*f["$items"], {"FullName": "Other", "PlayerId": "b"}])
    elif fault == "list":
        incoming = {"$n": 0}
    elif fault == "slot":
        r["Invites"]["entries"]["$items"].append({"key": "x"})
        r["Invites"]["entries"]["$n"] += 1
    elif fault == "status":
        r["Status"] = {"e": "Removed", "v": 5}
    elif fault == "stale":
        r["Invites"]["entries"]["$items"][0]["value"]["Status"] = {"e": "Cancelled", "v": 4}
    elif fault == "mixed":
        incoming = seq([{**r, "$h": 99}])
    elif fault == "deck":
        own["DeckId"] = guid(4)
    else:
        p = gate.Pair("issuer", p.local, p.peer, p.deck)
        r.update(ChallengeOwnerId="a", ChallengePlayers=mapping([("a", own)]), Invites=mapping([]))
    with pytest.raises(gate.PairUnavailable):
        gate.classify(p, p.local, "a", f, r, incoming, True)


def test_active_recipient_can_see_same_accepted_invite_with_native_shared_player(setup):
    p, f, r = setup
    inv = r["Invites"]["entries"]["$items"][0]["value"]
    own = {"PlayerId": "a", "FullDisplayName": p.local, "DeckId": guid(3), "$h": 21}
    peer = {"PlayerId": "b", "FullDisplayName": p.peer, "$h": 22}
    r.update(ChallengePlayers=mapping([("a", own), ("b", peer)]))
    r["$h"] = 19
    inv.update(
        Sender={"$h": 22, "$ref": True}, Recipient={"$h": 21, "$ref": True}, Status={"e": "Accepted", "v": 2}
    )
    assert gate.classify(p, p.local, "a", f, r, seq([r]), True) == gate.PairStatus.ACTIVE


@pytest.mark.parametrize("enabled", [True, False])
def test_dev_tools_menu_only_sends_generic_command(qapp, monkeypatch, enabled):
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMainWindow

    from arenamcp.desktop.main_window import MainWindow
    from arenamcp.desktop.release_updates import PENDING_ENV

    monkeypatch.setenv("MTGACOACH_DEV_DIRECT_CHALLENGE", "1")
    monkeypatch.delenv(PENDING_ENV, raising=False)
    monkeypatch.setattr(gate, "source_enabled", lambda: enabled)
    window = MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    window._session = Mock()
    window._settings = {}
    window.coach_panel = Mock()
    window._current_theme = ""
    window._theme_actions = {}
    window._build_menus()
    matches = [a for a in window.findChildren(QAction) if a.text() == "Check direct-challenge pairing…"]
    assert len(matches) == int(enabled)
    if enabled:
        matches[0].trigger()
        window._session.send_command.assert_called_once_with("dev_challenge_preflight")
    window.deleteLater()


def test_model_or_adapter_text_does_not_reach_public_feedback(setup):
    p, _, _ = setup
    bridge = SimpleNamespace(
        _connected=True,
        _mac_adapter=True,
        _send_command=lambda *a, **k: {"ok": False, "error": p.peer, "pair_status": p.local},
    )
    pipe = PipeAdapter()
    pipe._coach = SimpleNamespace(_autopilot=SimpleNamespace(_gre_bridge=bridge))
    pipe.log = Mock()
    pipe._check_dev_challenge_pair()
    pipe.log.assert_called_once_with("The pairing check was unavailable.")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "other_local_id",
        "other_peer_id",
        "alias",
        "different_role",
        "contradictory_name",
        "empty_roster",
        "one_way",
        "duplicate",
        "invalid_token",
        "short_key",
        "same_account",
        "progress_incomplete",
        "disabled",
        "corrupt_file",
        "duplicate_key",
    ],
)
def test_observed_native_pair_remains_pinned_in_normal_lookup(setup, tmp_path, monkeypatch, fault):
    from arenamcp.dev_challenge_identity import EnrolledPair, IdentityUnavailable, enrollment

    p, f, r = setup
    key = b"x" * 32
    sign = EnrolledPair(key, "", "").token
    local = sign(p.local, "a")
    peer = sign(p.peer, "b")
    other = sign("A different account", "c")

    def receipt(own, peers):
        return {
            "own": own,
            "friends": peers,
            "social_connected": True,
            "social_enabled": True,
            "num_friends": len(peers),
            "get_count": len(peers),
        }

    issuer = receipt(peer, [local])
    acceptor = receipt(local, [peer, other])
    if fault == "one_way":
        issuer = receipt(peer, [other])
    elif fault == "duplicate":
        issuer = receipt(peer, [local, local])
    elif fault == "progress_incomplete":
        issuer["get_count"] += 1
    elif fault == "disabled":
        acceptor["social_enabled"] = False
    elif fault == "invalid_token":
        acceptor["friends"][0] = "!"
    elif fault == "same_account":
        acceptor = issuer
    elif fault == "short_key":
        key = key[:9]
    if fault in {
        "one_way",
        "duplicate",
        "progress_incomplete",
        "disabled",
        "invalid_token",
        "same_account",
        "short_key",
    }:
        with pytest.raises(IdentityUnavailable):
            enrollment(key, issuer, acceptor)
        return
    path = tmp_path / "profile.json"
    raw = enrollment(key, issuer, acceptor)
    assert other not in str(raw) and p.local not in str(raw) and p.peer not in str(raw)
    import json

    if fault == "corrupt_file":
        raw["issuer"] = raw["acceptor"]
    path.write_text(json.dumps(raw))
    if fault == "duplicate_key":
        path.write_text(path.read_text().replace('"version": 1,', '"version": 9, "version": 1,'))
    monkeypatch.setenv("MTGACOACH_DEV_DC_PAIR_PROFILE", str(path))
    monkeypatch.delenv("MTGACOACH_DEV_DC_LOCAL_FULL_NAME")
    monkeypatch.delenv("MTGACOACH_DEV_DC_OPPONENT_FULL_NAME")
    if fault == "contradictory_name":
        monkeypatch.setenv("MTGACOACH_DEV_DC_LOCAL_FULL_NAME", p.local)
        monkeypatch.setenv("MTGACOACH_DEV_DC_OPPONENT_FULL_NAME", "Wrong peer")
    if fault == "different_role":
        monkeypatch.setenv("MTGACOACH_DEV_DC_ROLE", "issuer")
    if fault in {"corrupt_file", "duplicate_key"}:
        with pytest.raises(gate.PairUnavailable):
            gate.configured_pair()
        return
    confirmed = gate.configured_pair()
    actor = "z" if fault == "other_local_id" else "a"
    if fault == "other_peer_id":
        f["$items"][0]["PlayerId"] = "five"
    if fault == "alias":
        f = seq([*f["$items"], {"FullName": "Alias", "PlayerId": "b"}])
    if fault == "empty_roster":
        f = seq([])
    if fault is None:
        assert gate.classify(confirmed, p.local, actor, f, None, seq([r]), True) is gate.PairStatus.INVITED
    else:
        with pytest.raises(gate.PairUnavailable):
            gate.classify(confirmed, p.local, actor, f, None, seq([r]), True)


def test_enrollment_cli_is_private_exclusive_and_leaves_older_profile_intact(
    setup, tmp_path, monkeypatch, capsys
):
    import json
    import os

    from arenamcp import dev_challenge_identity as identity

    p, _, _ = setup
    key = b"y" * 32
    sig = identity.EnrolledPair(key, "", "").token
    one, two, third = sig(p.local, "a"), sig(p.peer, "b"), sig("Other", "c")
    keyfile = tmp_path / "key"
    keyfile.write_bytes(key)
    paths = []
    for n, (own, friends) in enumerate([(one, [two, third]), (two, [one])]):
        f = tmp_path / f"{n}.json"
        f.write_text(
            json.dumps(
                dict(
                    own=own,
                    friends=friends,
                    num_friends=len(friends),
                    get_count=len(friends),
                    social_connected=True,
                    social_enabled=True,
                )
            )
        )
        paths.append(f)
    out = tmp_path / "output.json"
    argv = [
        "--key-file",
        str(keyfile),
        "--issuer-receipt",
        str(paths[0]),
        "--acceptor-receipt",
        str(paths[1]),
        "--output",
        str(out),
    ]
    identity.main(argv)
    previous = out.read_bytes()
    assert third not in previous.decode() and set(json.loads(previous)) == {
        "version",
        "key",
        "issuer",
        "acceptor",
    }
    if os.name == "posix":
        assert out.stat().st_mode & 0o077 == 0
    with pytest.raises(SystemExit):
        identity.main(argv)
    assert out.read_bytes() == previous
    monkeypatch.setattr(gate, "source_enabled", lambda: False)
    with pytest.raises(SystemExit):
        identity.main(argv)
    stdout, stderr = capsys.readouterr()
    assert all(x not in stdout + stderr for x in (one, two, third, p.local, p.peer, str(keyfile), str(out)))
