import sys
from unittest.mock import MagicMock

sys.modules["mcp"] = MagicMock()
sys.modules["mcp.server"] = MagicMock()
sys.modules["mcp.server.fastmcp"] = MagicMock()

from arenamcp.gre_bridge import GREBridge, GREBridgeError


def test_queue_bot_match_success():
    bridge = GREBridge()
    bridge._send_safe = MagicMock(return_value={"ok": True})

    result = bridge.queue_bot_match()
    assert result is True
    bridge._send_safe.assert_called_once_with({"action": "queue_bot_match"}, timeout=10.0)


def test_queue_bot_match_with_deck_id():
    bridge = GREBridge()
    bridge._send_safe = MagicMock(return_value={"ok": True})

    result = bridge.queue_bot_match(deck_id="12345")
    assert result is True
    bridge._send_safe.assert_called_once_with({"action": "queue_bot_match", "deck_id": "12345"}, timeout=10.0)


def test_queue_bot_match_failure():
    bridge = GREBridge()
    bridge._send_safe = MagicMock(return_value={"ok": False, "error": "test error"})

    result = bridge.queue_bot_match()
    assert result is False
    bridge._send_safe.assert_called_once_with({"action": "queue_bot_match"}, timeout=10.0)


def test_queue_bot_match_exception():
    bridge = GREBridge()
    bridge._send_safe = MagicMock(side_effect=GREBridgeError("connection error"))

    result = bridge.queue_bot_match()
    assert result is False
    bridge._send_safe.assert_called_once_with({"action": "queue_bot_match"}, timeout=10.0)
