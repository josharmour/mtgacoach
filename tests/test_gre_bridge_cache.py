from __future__ import annotations

import time
from unittest.mock import MagicMock

from arenamcp.gre_bridge import GREBridge


def test_get_game_state_caches_within_ttl():
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    call_count = 0
    def mock_send(cmd, timeout=None):
        nonlocal call_count
        call_count += 1
        return {"ok": True, "turn": {"turn_number": call_count}}

    bridge._send_command = mock_send

    # First call fetches fresh state
    state1 = bridge.get_game_state()
    assert state1 is not None
    assert state1["turn"]["turn_number"] == 1
    assert call_count == 1

    # Immediate second call reuses cache without sending command
    state2 = bridge.get_game_state()
    assert state2 is not None
    assert state2["turn"]["turn_number"] == 1
    assert call_count == 1

    # Force bypasses cache
    state3 = bridge.get_game_state(force=True)
    assert state3 is not None
    assert state3["turn"]["turn_number"] == 2
    assert call_count == 2

    # Invalidation forces fresh fetch
    bridge.invalidate_game_state_cache()
    state4 = bridge.get_game_state()
    assert state4 is not None
    assert state4["turn"]["turn_number"] == 3
    assert call_count == 3
