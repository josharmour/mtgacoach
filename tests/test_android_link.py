"""AndroidLink log mirror: the phone stream's file-switch markers become mirror
truncations (the watcher's restart signal) and never leak into the log."""

from __future__ import annotations

import io

import pytest

from arenamcp import android_link
from arenamcp.android_link import _FILE_MARKER, AndroidLink, _partial_marker_len


class ChunkedStdout(io.RawIOBase):
    """Delivers the stream in fixed chunks, like a pipe with small reads."""

    def __init__(self, data: bytes, size: int) -> None:
        self._chunks = [data[i : i + size] for i in range(0, len(data), size)]

    def readable(self) -> bool:
        return True

    def read1(self, n: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class FakeProcess:
    def __init__(self, data: bytes, size: int) -> None:
        self.stdout = ChunkedStdout(data, size)


def marker(name: str) -> bytes:
    return b"\n" + _FILE_MARKER + b" " + name.encode() + b"\n"


@pytest.mark.parametrize("chunk", [1, 7, 19, 4096])
def test_new_session_marker_truncates_the_mirror(tmp_path, chunk):
    link = AndroidLink("adb", "SERIAL", mirror_path=tmp_path / "Player.log")
    link.mirror_path.write_bytes(b"")
    stream = (
        marker("/logs/UTC_Log - A.log")
        + b"[1] old session line\n"
        + marker("/logs/UTC_Log - B.log")
        + b"[1] new session line\n[2] GreToClientEvent\n"
    )
    link._pump(FakeProcess(stream, chunk))
    assert link.mirror_path.read_bytes() == b"[1] new session line\n[2] GreToClientEvent\n"
    assert link.source_file == "/logs/UTC_Log - B.log"


def test_quiet_stream_is_not_held_back(tmp_path):
    # The last bytes of a message must reach the watcher even when the phone
    # goes quiet (no holding back unless they could start a marker).
    link = AndroidLink("adb", "SERIAL", mirror_path=tmp_path / "Player.log")
    link.mirror_path.write_bytes(b"")
    link._pump(FakeProcess(marker("/logs/A") + b'{"greToClientEvent": {}}\n', 4096))
    assert link.mirror_path.read_bytes() == b'{"greToClientEvent": {}}\n'


def test_partial_marker_len():
    assert _partial_marker_len(b"abc") == 0
    assert _partial_marker_len(b"abc@@MTG") == len(b"@@MTG")
    assert _partial_marker_len(b"x" + _FILE_MARKER[:-1]) == len(_FILE_MARKER) - 1


def test_start_android_link_without_adb_leaves_log_path(monkeypatch):
    monkeypatch.setattr(android_link, "find_adb", lambda: None)
    monkeypatch.delenv("MTGA_LOG_PATH", raising=False)
    assert android_link.start_android_link() is None
    assert "MTGA_LOG_PATH" not in __import__("os").environ


def _fake_adb(monkeypatch, devices: list[str], serials: dict[str, str]):
    monkeypatch.setattr(android_link, "list_devices", lambda adb: list(devices))
    monkeypatch.setattr(android_link, "hardware_serial", lambda adb, s: serials.get(s, s))


def test_find_phone_prefers_usb_over_wifi(monkeypatch):
    _fake_adb(monkeypatch, ["192.168.2.182:38815", "55301FDCH006K6"], {"192.168.2.182:38815": "55301FDCH006K6"})
    assert android_link.find_phone("adb") == "55301FDCH006K6"


def test_link_follows_the_same_phone_from_usb_to_wifi(monkeypatch, tmp_path):
    monkeypatch.setattr(android_link, "STATE_PATH", tmp_path / "link.json")
    devices = ["55301FDCH006K6"]
    _fake_adb(monkeypatch, devices, {"192.168.2.182:38815": "55301FDCH006K6", "OTHER:5555": "SOMEONE_ELSE"})
    link = AndroidLink("adb", "55301FDCH006K6", mirror_path=tmp_path / "Player.log")
    link.hw_serial = "55301FDCH006K6"
    assert link._ensure_transport()

    devices[:] = ["OTHER:5555"]  # USB unplugged; a different phone is on Wi-Fi
    assert not link._ensure_transport()
    assert link.serial == "55301FDCH006K6"

    devices[:] = ["OTHER:5555", "192.168.2.182:38815"]
    assert link._ensure_transport()
    assert link.serial == "192.168.2.182:38815"
    assert android_link._load_state()["wifi_endpoint"] == "192.168.2.182:38815"


def test_missing_phone_reconnects_the_saved_wifi_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(android_link, "STATE_PATH", tmp_path / "link.json")
    android_link._save_state(wifi_endpoint="192.168.2.182:38815")
    devices: list[str] = []
    _fake_adb(monkeypatch, devices, {"192.168.2.182:38815": "HW1"})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:] == ["connect", "192.168.2.182:38815"]:
            devices.append("192.168.2.182:38815")
        return __import__("subprocess").CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(android_link.subprocess, "run", fake_run)
    link = AndroidLink("adb", "USBSERIAL", mirror_path=tmp_path / "Player.log")
    link.hw_serial = "HW1"
    assert link._ensure_transport()
    assert link.serial == "192.168.2.182:38815"
    assert ["adb", "connect", "192.168.2.182:38815"] in calls


def test_empty_streams_back_off_instead_of_spinning(monkeypatch, tmp_path):
    link = AndroidLink("adb", "S", mirror_path=tmp_path / "Player.log")
    monkeypatch.setattr(link, "_ensure_transport", lambda: False)
    waits: list[float] = []

    def fake_wait(seconds):
        waits.append(seconds)
        if len(waits) >= 6:
            link._stop.set()
        return False

    monkeypatch.setattr(link._stop, "wait", fake_wait)
    link._run()
    assert waits == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
