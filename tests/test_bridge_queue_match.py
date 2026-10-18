"""GREBridge.queue_match: the auto-queue's requeue command on the Windows bridge."""

from unittest.mock import MagicMock

from arenamcp.gre_bridge import GREBridge


def test_queue_match_success():
    bridge = GREBridge()
    bridge._send_safe = MagicMock(return_value={"ok": True})

    result = bridge.queue_match(event="Ladder", deck_id="test-deck-guid")
    assert result is True
    bridge._send_safe.assert_called_once_with(
        {"action": "queue_match", "event": "Ladder", "deck_id": "test-deck-guid"},
        timeout=10.0,
    )


def test_queue_match_default_event():
    bridge = GREBridge()
    bridge._send_safe = MagicMock(return_value={"ok": True})

    result = bridge.queue_match()
    assert result is True
    bridge._send_safe.assert_called_once_with(
        {"action": "queue_match", "event": "AIBotMatch"},
        timeout=10.0,
    )
