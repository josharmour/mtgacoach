"""The test suite never binds the live bridge ports, and a port conflict is named once.

2026-10-18: a hung ``pytest tests`` held 127.0.0.1:44222 for two hours, so the
user's real coach could not start its bridge ("[Errno 48] Address already in
use") and logged one INFO line per keepalive tick.
"""

import logging
import socket
from pathlib import Path

import pytest

from arenamcp import gre_bridge


def _close(bridge: gre_bridge.GREBridge) -> None:
    sock = bridge._server_socket
    bridge._server_socket = None
    if sock is not None:
        sock.close()


def test_production_defaults_are_the_live_ports():
    source = Path(gre_bridge.__file__).read_text(encoding="utf-8")
    assert "\nBRIDGE_PORT = 44222\n" in source
    assert "\nBOT_BATTLE_PORT = 44223\n" in source
    assert gre_bridge.GREBridge()._port is None  # resolved from BRIDGE_PORT at bind time


def test_a_bridge_built_under_pytest_binds_an_ephemeral_port():
    assert gre_bridge.BRIDGE_PORT == 0 and gre_bridge.BOT_BATTLE_PORT == 0
    bridge = gre_bridge.GREBridge()
    try:
        assert bridge.connect() is False  # nothing dials in
        assert bridge.bound_port and bridge.bound_port not in (44222, 44223)
    finally:
        _close(bridge)
    assert gre_bridge.BotBattlePipeServer().port == 0


def test_binding_a_live_bridge_port_fails_inside_the_suite():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(OSError, match="live bridge port 44222"):
            s.bind(("127.0.0.1", 44222))


def test_a_port_held_by_another_process_is_warned_about_once(caplog):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    bridge = gre_bridge.GREBridge(port=port)
    bridge._reconnect_cooldown = 0.0
    try:
        with caplog.at_level(logging.DEBUG, logger="arenamcp.gre_bridge"):
            for _ in range(3):
                assert bridge.connect() is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert f"127.0.0.1:{port} is already in use" in warnings[0].getMessage()
        assert not [r for r in caplog.records if r.levelno == logging.INFO and "connect error" in r.getMessage()]
        assert bridge._server_socket is None
    finally:
        holder.close()
        _close(bridge)


def test_shutdown_releases_the_port_and_never_rebinds():
    # 2026-10-18 14:54: the old coach held the port ~70 s after a desktop
    # restart, so the new coach's bridge stayed offline mid-match.
    bridge = gre_bridge.GREBridge()
    bridge.connect()
    port = bridge.bound_port
    bridge.shutdown()
    assert bridge._server_socket is None
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))  # free again
    finally:
        probe.close()
    bridge._last_connect_attempt = 0.0
    assert bridge.connect() is False and bridge._server_socket is None


def test_shutdown_bridge_resets_the_singleton(monkeypatch):
    bridge = gre_bridge.GREBridge()
    monkeypatch.setattr(gre_bridge, "_bridge", bridge)
    gre_bridge.shutdown_bridge()
    assert gre_bridge._bridge is None and bridge._shut_down
    gre_bridge.shutdown_bridge()  # idempotent with no bridge


def test_offline_hint_names_a_port_conflict_not_a_relaunch(monkeypatch):
    from arenamcp.platform_integration import bridge_offline_hint

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    bridge = gre_bridge.GREBridge(port=holder.getsockname()[1])
    monkeypatch.setattr(gre_bridge, "_bridge", bridge)
    try:
        assert bridge.connect() is False and bridge.port_in_use
        hint = bridge_offline_hint()
        assert "holds the bridge port" in hint and "DYLD" not in hint
    finally:
        holder.close()
