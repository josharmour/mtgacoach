from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.draftstate import DraftState
from arenamcp.limited_deck import fallback_deck, reconcile_logged_deck
from arenamcp.mac_bridge_adapter import AdapterError, MacBridgeAdapter, guid
from arenamcp.standalone_deck import _DeckAnalysisMixin


class Editor:
    def __init__(self, **fields):
        self.fields = {
            "_isActive": True,
            "IsLimited": True,
            "IsSideboarding": False,
            "IsReadOnly": False,
            "mainDeck": {"$n": 1, "$items": [{"Id": 42, "Quantity": 2}]},
            "sideboard": {"$n": 0, "$items": []},
            "id": {"_" + name: index for index, name in enumerate("abcdefghijk")},
            **fields,
        }
        self.operations = []

    def send(self, command, timeout):
        results = []
        for operation in command["ops"]:
            self.operations.append(operation)
            assert operation["op"] in ("find", "get", "expect", "call")
            if operation["op"] == "call":
                assert operation["method"] == "GetServerModel"
            if operation["op"] == "expect":
                actual = self.fields.get(operation["member"], True)
                if actual != operation["equals"]:
                    return {"ok": False, "error": "identity mismatch"}
            results.append(self.fields.get(operation.get("member"), {"$h": 23}))
        return {"ok": True, "results": results}


def test_live_editor_reads_unsaved_counts_without_writing():
    editor = Editor()
    result = MacBridgeAdapter(editor.send).handle({"action": "get_deck_editor"})
    assert result["ok"]
    assert result["main_deck"] == [{"grp_id": 42, "count": 2}]
    assert result["deck_id"] == "00000000-0001-0002-0304-05060708090a"
    assert any(operation.get("member") == "HasLoadedDeck" for operation in editor.operations)


@pytest.mark.parametrize(
    "fields", [{"_isActive": False}, {"IsLimited": False}, {"IsSideboarding": True}, {"IsReadOnly": True}]
)
def test_unrelated_editor_context_never_reads_or_initializes_model(fields):
    editor = Editor(**fields)
    result = MacBridgeAdapter(editor.send).handle({"action": "get_deck_editor"})
    assert result == {"ok": True, "is_open": False}
    assert not any(operation.get("member") in ("Model", "ModelProvider") for operation in editor.operations)


def test_truncated_editor_list_is_not_reported_as_a_complete_deck():
    editor = Editor(mainDeck={"$n": 3, "$items": [{"Id": 42, "Quantity": 2}]})
    result = MacBridgeAdapter(editor.send).handle({"action": "get_deck_editor"})
    assert not result["ok"]
    assert "Incomplete" in result["error"]


def test_guid_decoding_preserves_signed_fields_and_rejects_unknown():
    assert guid({"_" + name: -1 for name in "abcdefghijk"}) == "ffffffff-ffff-ffff-ffff-ffffffffffff"
    with pytest.raises(AdapterError):
        guid({"$more": True})


def test_live_deck_state_is_course_bound_and_cleared_when_closed():
    state = DraftState(deck_id="draft", picked_cards=[42, 42])
    snapshot = {
        "is_open": True,
        "is_limited": True,
        "deck_id": "other",
        "main_deck": [{"grp_id": 42, "count": 2}],
    }
    state.update_editor(snapshot)
    assert state.editor_main_deck is None
    state.update_editor({**snapshot, "deck_id": "draft"})
    assert state.editor_main_deck == {42: 2}
    assert state.editor_basis == "live_editor"
    assert state.is_building
    state.update_editor(None)
    assert not state.is_building
    assert state.editor_main_deck is None
    state.update_editor({**snapshot, "deck_id": "draft"})
    state.update_editor({"is_open": False})
    assert not state.is_building


def test_live_reconciliation_does_not_claim_current_counts_are_only_saved():
    pool = [
        {"grp_id": identity, "name": f"Creature {identity}", "type_line": "Creature", "mana_cost": "{1}{G}"}
        for identity in range(1, 24)
    ]
    build = fallback_deck(pool)
    current = [{**entry, "type_line": "Creature"} for entry in build["main_deck"]]
    current.append({"grp_id": 100, "name": "Forest", "type_line": "Basic Land — Forest", "count": 17})
    result = reconcile_logged_deck({**build, "editor_cards": current, "editor_basis": "live_editor"})
    assert "current deck has 40" in result["spoken_advice"]
    assert "Unsaved" not in result["spoken_advice"]


def test_editor_advice_waits_for_editing_to_settle_and_reuses_proposal(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("arenamcp.standalone_deck.time.monotonic", lambda: clock[0])
    signature = ["Draft", [1, 2], [[1, 1]], "live_editor"]
    snapshot = {"is_building": True, "pool_signature": signature}
    client = _DeckAnalysisMixin()
    client._mcp = Mock()
    client._mcp.get_draft_pack.side_effect = lambda: snapshot
    client._mcp.analyze_draft_pool.return_value = {
        "pool_size": 2,
        "spoken_advice": "Cut one",
        "editor_cards": [],
        "editor_basis": "live_editor",
    }
    client._coach = SimpleNamespace(_backend=object())
    client._draft_advisor = Mock()
    client._draft_advisor.recommend_deck.side_effect = lambda result: deepcopy(result)
    client.ui = Mock()
    client.speak_advice = Mock()
    client._advise_draft_build(snapshot)
    client._mcp.analyze_draft_pool.assert_not_called()
    clock[0] += 2
    client._advise_draft_build(snapshot)
    client.speak_advice.assert_called_once()
    snapshot["pool_signature"] = ["Draft", [1, 2], [[2, 1]], "live_editor"]
    client._advise_draft_build(snapshot)
    clock[0] += 2
    client._advise_draft_build(snapshot)
    assert client.speak_advice.call_count == 2
    assert client._draft_advisor.recommend_deck.call_count == 1
