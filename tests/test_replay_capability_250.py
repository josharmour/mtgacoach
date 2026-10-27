from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.gre_bridge import GREBridge


@pytest.mark.parametrize("method", ["enable_replay", "disable_replay", "get_replay_status", "list_replays"])
def test_native_adapter_replay_is_unavailable_without_sending_commands(method):
    bridge = GREBridge()
    bridge._mac_adapter = SimpleNamespace(plays_matches=True)
    bridge._send_safe = Mock(return_value={"ok": False, "unsupported": True})
    result = getattr(bridge, method)()
    assert result is (False if method == "disable_replay" else None)
    bridge._send_safe.assert_not_called()


@pytest.mark.parametrize("adapter", [None, SimpleNamespace(plays_matches=False)])
@pytest.mark.parametrize("method", ["enable_replay", "disable_replay", "get_replay_status", "list_replays"])
def test_bepinex_replay_still_uses_plugin_handlers(adapter, method):
    bridge = GREBridge()
    bridge._mac_adapter = adapter
    response = {"ok": True, "replay_folder": "replays"}
    bridge._send_safe = Mock(return_value=response)
    result = getattr(bridge, method)()
    assert result == (True if method == "disable_replay" else response)
    assert bridge._send_safe.call_args.args[0]["action"] == method


def test_supported_plugin_replay_failure_still_warns(caplog):
    bridge = GREBridge()
    bridge._send_safe = Mock(return_value={"ok": False, "error": "recording failure"})
    assert bridge.enable_replay() is None
    assert "recording failure" in caplog.text
