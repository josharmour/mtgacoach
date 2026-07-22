import pytest
from unittest.mock import MagicMock, patch

from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.gre_bridge import GREBridge, BridgeDecisionPoller
from arenamcp.mtgadb import MTGACard, MTGADatabase


def _mock_card(card_name):
    # MagicMock(name=...) sets the mock's repr name, not a .name attribute,
    # so build the card mock explicitly.
    card = MagicMock()
    card.name = card_name
    return card


def test_gamestate_connect_resp_prewarms_card_cache():
    gs = GameState()
    assert len(gs._card_name_cache) == 0

    msg = {
        "type": "GREMessageType_ConnectResp",
        "connectResp": {
            "deckMessage": {
                "deckCards": [1001, 1002, 1003],
                "sideboardCards": [2001],
                "commanderGrpIds": [3001],
            }
        }
    }

    mock_card_db = MagicMock()
    mock_card_db.get_card_by_arena_id.side_effect = lambda gid: _mock_card(f"CardName_{gid}") if gid in (1001, 1002, 1003, 2001, 3001) else None

    with patch("arenamcp.card_db.get_card_database", return_value=mock_card_db):
        handler = create_game_state_handler(gs)
        handler({"greToClientMessages": [msg]})
        assert gs.deck_cards == [1001, 1002, 1003]
        assert gs.sideboard_cards == [2001]
        assert mock_card_db.prewarm_cards.called
        assert gs._resolve_card_name(1001) == "CardName_1001"
        assert gs._resolve_card_name(2001) == "CardName_2001"
        assert gs._resolve_card_name(3001) == "CardName_3001"


def test_gre_bridge_prewarm_grp_ids():
    bridge = GREBridge()
    mock_card_db = MagicMock()
    mock_card_db.get_card_by_arena_id.side_effect = lambda gid: _mock_card(f"MockCard_{gid}") if gid in (100, 200) else None

    with patch("arenamcp.card_db.get_card_database", return_value=mock_card_db):
        resolved = bridge.prewarm_grp_ids([100, 200])
        assert resolved == {100: "MockCard_100", 200: "MockCard_200"}
        assert mock_card_db.prewarm_cards.called


def test_bridge_decision_poller_prewarms_action_grp_ids():
    mock_bridge = MagicMock()
    mock_bridge.connected = True
    mock_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_type": "ActionsAvailableReq",
        "actions": [
            {"actionType": "Cast", "grpId": 5001, "instanceId": 1},
            {"actionType": "Play", "grpId": 5002, "instanceId": 2},
        ],
        "can_pass": True,
    }

    poller = BridgeDecisionPoller(mock_bridge)
    res = poller.poll()

    assert res is not None
    assert res["trigger"] == "decision_required"
    mock_bridge.prewarm_grp_ids.assert_called_once_with([5001, 5002])
