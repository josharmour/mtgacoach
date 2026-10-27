"""Peacekeeper's manual Channel response was TitleId 21415, not GrpId 77529."""

import sqlite3
from types import SimpleNamespace

import pytest
from tests.mac_bridge_fakes import FakeGame, adapter_for, enum, listing, submitted_uint_values

from arenamcp import card_name_choice, decisions
from arenamcp.decisions import asking_card, build_pending_decision, select_n_note, submit_option
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.gre_bridge import GREBridge
from arenamcp.mtgadb import MTGADatabase

TITLE_IDS = {77529: 21415, 95516: 926667}
CARDS = {
    160: {"grp_id": 77529, "name": "Channel", "type_line": "Sorcery", "mana_cost": "{G}{G}"},
    219: {"grp_id": 95516, "name": "Ugin, Eye of the Storms", "type_line": "Planeswalker"},
    223: {"grp_id": 77529, "name": "Channel", "type_line": "Sorcery"},
}


@pytest.fixture(autouse=True)
def known_titles(monkeypatch):
    monkeypatch.setattr(card_name_choice, "card_title_id", lambda grp_id: TITLE_IDS.get(grp_id, 0))
    monkeypatch.setattr(card_name_choice, "is_card_title_id", lambda title_id: title_id in TITLE_IDS.values())
    monkeypatch.setattr(
        card_name_choice,
        "card_title_name",
        lambda title_id: {
            21415: "Channel",
            926667: "Ugin, Eye of the Storms",
        }.get(title_id, ""),
    )
    monkeypatch.setattr(decisions, "_live_game_state", lambda: None)


def request():
    return {
        "$c": "GreClient.Rules.SelectNRequest",
        "$h": 41,
        "Ids": listing(),
        "UnfilteredIds": listing(160, 219, 223),
        "IdType": enum("None", 0),
        "ListType": enum("Static", 1),
        "StaticList": enum("CardNames", 2),
        "MinSel": 1,
        "MaxSel": 1,
    }


def poll():
    return adapter_for(FakeGame(request(), {"Type": enum("SelectN", 9)}, gsid=107, msg=150)).handle(
        {"action": "get_pending_actions"}
    )


def test_revealed_names_become_distinct_title_choices_not_instance_or_printing_ids():
    decision = build_pending_decision(poll(), resolve_card=lambda iid: CARDS.get(iid, {}))
    assert decision.option_ids() == {"sel:21415", "sel:926667"}
    assert decision.find("sel:21415").label == "Channel"
    assert decision.find("sel:926667").meta["choice"] == "card_name"
    assert "CARD NAME CHOICE" in select_n_note(decision, {})
    assert not decision.selection_is_valid(["sel:77529"])
    assert not decision.selection_is_valid(["sel:219"])


def test_prompt_card_id_preserves_peacekeeper_tax_rules_despite_phantom_source_id():
    rules = (
        "As Anointed Peacekeeper enters, look at an opponent's hand, then choose any card name. "
        "Spells your opponents cast with the chosen name cost {2} more to cast. "
        "Activated abilities of sources with the chosen name cost {2} more to activate unless they're mana abilities."
    )
    source = {"grp_id": 82054, "name": "Anointed Peacekeeper", "oracle_text": rules}
    bridge_poll = poll()
    bridge_poll.update({"source_card": "Ability 9005", "source_instance_id": 9005})
    bridge_poll["request_payload"] = {
        "sourceId": 9005,
        "prompt": {"parameters": [{"parameterName": "CardId", "numberValue": 226}]},
    }
    decision = build_pending_decision(
        bridge_poll, resolve_card=lambda iid: source if iid == 226 else CARDS.get(iid, {})
    )
    assert decision.source_instance_id == 226
    assert decision.source_label == "Anointed Peacekeeper"
    assert decision.source_oracle_text == rules
    state = {
        "_bridge_request_payload": {"sourceId": 9005},
        "decision_context": {"prompt_source_id": 9005},
        "stack": [{"instance_id": 9005, "name": "Ability 9005", "oracle_text": "Choose any card name."}],
    }
    assert asking_card(decision, state) == ("Anointed Peacekeeper", rules)
    state["battlefield"] = [{"instance_id": 226, "name": "Anointed Peacekeeper"}]
    assert asking_card(decision, state) == ("Anointed Peacekeeper", rules)
    note = select_n_note(decision, state)
    assert "Spells your opponents cast" in note
    assert "Activated abilities" in note


def test_typed_submission_matches_successful_manual_wire_response():
    game = FakeGame(request(), {"Type": enum("SelectN", 9)}, gsid=107, msg=150)
    adapter = adapter_for(game)
    decision = build_pending_decision(
        adapter.handle({"action": "get_pending_actions"}), resolve_card=lambda iid: CARDS.get(iid, {})
    )
    bridge = GREBridge.__new__(GREBridge)
    bridge._cost_selection_via_adapter = False
    bridge._send_safe = adapter.handle
    assert submit_option(bridge, decision, ["sel:21415"])
    assert submitted_uint_values(game.submits()[0]) == [21415]


@pytest.mark.parametrize("ids", [[77529], [219], [], [21415, 926667], [999999]])
def test_adapter_refuses_invalid_card_name_namespace_or_count(ids):
    game = FakeGame(request(), {"Type": enum("SelectN", 9)})
    response = adapter_for(game).handle(
        {
            "action": "submit_selection",
            "ids": ids,
            "selection_id_type": "card_name",
        }
    )
    assert response["ok"] is False
    assert "TitleIds" in response["error"]
    assert not game.submits()


def test_legacy_submission_cannot_pass_an_instance_id_that_equals_a_real_title():
    game = FakeGame(request(), {"Type": enum("SelectN", 9)})
    assert not adapter_for(game).handle({"action": "submit_selection", "ids": [21415]})["ok"]
    assert not game.submits()


def test_any_name_includes_opponent_public_cards_outside_revealed_hand(monkeypatch):
    snapshot = {
        "local_seat_id": 1,
        "battlefield": [{**CARDS[219], "owner_seat_id": 2}],
        "graveyard": [{**CARDS[160], "owner_seat_id": 2}],
    }
    state = SimpleNamespace(get_published_snapshot=lambda: snapshot)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)
    bridge_poll = poll()
    bridge_poll["select_n_unfiltered_ids"] = []
    bridge_poll.pop("request_payload", None)
    decision = build_pending_decision(bridge_poll)
    assert decision.option_ids() == {"sel:21415", "sel:926667"}
    # A card known only through hidden hand data is not a public candidate.
    snapshot.clear()
    snapshot.update({"hand": [CARDS[219]]})
    assert build_pending_decision(bridge_poll) is None


def test_static_subset_only_offers_and_submits_allowed_title_ids(monkeypatch):
    restricted = request()
    restricted.update({"ListType": enum("StaticSubset", 3), "Ids": listing(21415)})
    game = FakeGame(restricted, {"Type": enum("SelectN", 9)})
    adapter = adapter_for(game)
    decision = build_pending_decision(
        adapter.handle({"action": "get_pending_actions"}), resolve_card=lambda iid: CARDS.get(iid, {})
    )
    assert decision.option_ids() == {"sel:21415"}
    assert not adapter.handle(
        {
            "action": "submit_selection",
            "ids": [926667],
            "selection_id_type": "card_name",
        }
    )["ok"]
    assert not game.submits()


def test_log_candidates_cover_bridge_without_unfiltered_ids_and_reject_stale_context(monkeypatch):
    state = GameState()
    _handle_decision_message(
        state,
        "GREMessageType_SelectNReq",
        {
            "type": "GREMessageType_SelectNReq",
            "msgId": 150,
            "gameStateId": 107,
            "selectNReq": {
                "minSel": 1,
                "maxSel": 1,
                "listType": "SelectionListType_Static",
                "staticList": "StaticList_CardNames",
                "unfilteredIds": [160, 219, 223],
            },
        },
    )
    assert state.decision_context["unfiltered_ids"] == [160, 219, 223]
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)
    bridge_poll = poll()
    bridge_poll.pop("select_n_unfiltered_ids")
    bridge_poll.pop("request_payload", None)
    decision = build_pending_decision(bridge_poll, resolve_card=lambda iid: CARDS.get(iid, {}))
    assert decision.option_ids() == {"sel:21415", "sel:926667"}
    bridge_poll["msg_id"] += 1
    assert build_pending_decision(bridge_poll, resolve_card=lambda iid: CARDS.get(iid, {})) is None


def test_title_lookup_uses_printing_name_id_and_excludes_tokens(tmp_path):
    path = tmp_path / "cards.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE Cards(GrpId INTEGER, TitleId INTEGER, IsToken INTEGER)")
        conn.execute("CREATE TABLE Localizations_enUS(LocId INTEGER, Loc TEXT, Formatted INTEGER)")
        conn.execute("INSERT INTO Localizations_enUS VALUES (21415, 'Channel', 1)")
        conn.executemany(
            "INSERT INTO Cards VALUES (?, ?, ?)", [(77529, 21415, 0), (95516, 926667, 0), (1, 7, 1)]
        )
    db = MTGADatabase(path)
    try:
        assert db.get_card_title_id(77529) == 21415
        assert db.get_card_title_id(95516) == 926667
        assert db.get_card_title_id(999) == 0
        assert db.is_card_title_id(21415)
        assert db.get_card_title_name(21415) == "Channel"
        assert db.get_card_title_name(77529) == ""
        assert not db.is_card_title_id(77529)
        assert not db.is_card_title_id(219)
        assert not db.is_card_title_id(7)
    finally:
        db.close()
