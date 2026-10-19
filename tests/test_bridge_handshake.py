"""GREBridge.connect(): a real client dials the bridge's (ephemeral) port.

The handshake is the first thing every bridge session does: accept the
plugin, ping it, record its version and runtime, and pick the command path
(the BepInEx plugin directly, or MacBridgeAdapter's reflection for the
native-Mac and Android IL2CPP libraries). While the coach plays on an
Android phone, a desktop client must not take the single bridge slot.
Also the command log bug reports embed (recent_commands).
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from arenamcp.gre_bridge import GREBridge
from arenamcp.mac_bridge_adapter import MacBridgeAdapter


class FakePlugin:
    """Dials the bridge like MtgaCoachBridge.dll and answers each command line from a script."""

    def __init__(self, port: int, replies: list[dict]):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.file = self.sock.makefile("rwb", buffering=0)
        self.replies = list(replies)
        self.received: list[dict] = []
        self.thread = threading.Thread(target=self._serve, name="fake-plugin", daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            while self.replies:
                line = self.file.readline()
                if not line:
                    return
                self.received.append(json.loads(line))
                self.file.write((json.dumps(self.replies.pop(0)) + "\n").encode("utf-8"))
        except OSError:
            pass

    def close(self) -> None:
        for closer in (self.file.close, self.sock.close):
            try:
                closer()
            except OSError:
                pass


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")
    bridge = GREBridge(port=0)
    bridge._reconnect_cooldown = 0.0
    assert bridge.connect() is False  # listening; nothing has dialed in yet
    yield bridge
    bridge.shutdown()


def _dial(bridge: GREBridge, ping_reply: dict, *more: dict) -> tuple[bool, FakePlugin]:
    plugin = FakePlugin(bridge.bound_port, [ping_reply, *more])
    try:
        # connect() polls the listening socket without waiting; under load the
        # dial can take a moment to show up there.
        deadline = time.monotonic() + 5.0
        ok = bridge.connect()
        while not ok and not plugin.received and time.monotonic() < deadline:
            time.sleep(0.01)
            ok = bridge.connect()
        return ok, plugin
    except Exception:
        plugin.close()
        raise


PING = {"ok": True, "version": "0.6.5"}


@pytest.mark.parametrize(
    ("device", "reply", "connected", "runtime", "adapter"),
    [
        ("desktop", PING, True, "bepinex", False),
        # A plugin that speaks reflect-1 gets the adapter's limited-event hands.
        ("desktop", {**PING, "runtime": "bepinex", "protocols": ["reflect-1"]}, True, "bepinex", True),
        (
            "desktop",
            {**PING, "runtime": "il2cpp-macos", "protocols": ["reflect-1"]},
            True,
            "il2cpp-macos",
            True,
        ),
        # An older library without the reflection protocol keeps the plain path.
        ("desktop", {**PING, "runtime": "il2cpp-macos"}, True, "il2cpp-macos", False),
        (
            "android",
            {**PING, "runtime": "il2cpp-android", "protocols": ["reflect-1"]},
            True,
            "il2cpp-android",
            True,
        ),
        # Playing on the phone: a desktop client would act on the wrong game (2026-09-24).
        ("android", PING, False, None, False),
        ("android", {**PING, "runtime": "il2cpp-macos", "protocols": ["reflect-1"]}, False, None, False),
        ("desktop", {"ok": False, "error": "not ready"}, False, None, False),
    ],
    ids=[
        "bepinex",
        "bepinex_reflect",
        "mac",
        "mac_old_library",
        "android",
        "android_rejects_bepinex",
        "android_rejects_mac",
        "ping_fails",
    ],
)
def test_handshake_accepts_pings_and_picks_the_command_path(
    bridge, monkeypatch, device, reply, connected, runtime, adapter
):
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", device)
    ok, plugin = _dial(bridge, reply)
    try:
        assert plugin.received[0] == {"action": "ping"}
        assert ok is connected and bridge.connected is connected
        assert bridge.client_runtime == runtime
        assert bridge.plugin_version == ("0.6.5" if connected else None)
        assert isinstance(bridge._mac_adapter, MacBridgeAdapter) is adapter
    finally:
        plugin.close()


def test_bepinex_reflect_runs_only_limited_commands_through_the_adapter(bridge):
    """The plugin keeps its own in-game commands; screens and limited events go
    through reflect_batch, and in-game gating stays on the plugin path."""
    ok, plugin = _dial(
        bridge,
        {**PING, "runtime": "bepinex", "protocols": ["reflect-1"]},
        {"ok": True, "submitted_type": "Pass"},
        {"ok": True, "results": [None] * 16},
    )
    try:
        assert ok
        assert bridge.limited_hands is True
        assert bridge._adapter_plays is False
        assert bridge.concede_supported is True  # plugin 0.6.5, by version
        bridge._send_command({"action": "submit_pass"})
        bridge._send_command({"action": "get_screen"})
        assert plugin.received[1] == {"action": "submit_pass"}
        assert plugin.received[2]["action"] == "reflect_batch"
        assert [op["op"] for op in plugin.received[2]["ops"]][:1] == ["find"]
    finally:
        plugin.close()


def test_native_mac_adapter_plays_matches_and_bepinex_does_not():
    send = lambda command, timeout: {"ok": True}  # noqa: E731
    mac = MacBridgeAdapter(send, "il2cpp-macos")
    bepinex = MacBridgeAdapter(send, "bepinex")
    assert mac.plays_matches and mac.handles({"action": "submit_action"})
    assert not bepinex.plays_matches
    assert not bepinex.handles({"action": "submit_action"})
    assert not bepinex.handles({"action": "ping"})
    for action in ("get_screen", "set_limited_deck", "submit_limited_deck", "finish_sealed_open", "get_draft_state"):
        assert bepinex.handles({"action": action})


def test_command_log_keeps_what_changed_something_and_failed_polls(bridge):
    ok, plugin = _dial(
        bridge,
        PING,
        {"ok": True, "submitted_type": "Pass"},
        {"ok": True, "has_pending": False},
        {"ok": False, "error": "no GameManager"},
        {"ok": True},
    )
    try:
        assert ok
        assert bridge.submit_pass() is True
        bridge.get_pending_actions()  # a routine poll: not logged
        bridge.get_pending_actions()  # a failed poll: logged
        bridge._send_command({"action": "submit_selection", "ids": list(range(200))})
        log = bridge.recent_commands()
    finally:
        plugin.close()
    assert [entry["action"] for entry in log] == ["submit_pass", "get_pending_actions", "submit_selection"]
    passed, failed_poll, selection = log
    assert passed["ok"] is True and passed["submitted_type"] == "Pass" and "args" not in passed
    assert passed["ms"] >= 0 and passed["at"] > 0
    assert failed_poll["ok"] is False and failed_poll["error"] == "no GameManager"
    assert selection["args"].startswith('{"ids": [0, 1, 2') and selection["args"].endswith("…")
    assert len(selection["args"]) == 401  # truncated at 400 characters
