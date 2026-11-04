import pytest

from arenamcp import platform_integration
from arenamcp.desktop import runtime


@pytest.fixture
def mac_package(tmp_path, monkeypatch):
    source = tmp_path / "package/native/libmtgacoach_probe.dylib"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new qualified bridge")
    destination = tmp_path / "user/probe/libmtgacoach_probe.dylib"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old mapped bridge")
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "get_app_root", lambda: str(source.parent.parent))
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: False)
    monkeypatch.setattr(platform_integration, "MAC_BRIDGE_LIBRARY", destination)
    return source, destination


def test_stage_replaces_inode_without_overwriting_open_library(mac_package):
    source, destination = mac_package
    old_inode = destination.stat().st_ino
    with destination.open("rb") as old_mapping:
        assert runtime.stage_managed_mac_bridge()
        assert old_mapping.read() == b"old mapped bridge"
    assert destination.read_bytes() == source.read_bytes()
    assert destination.stat().st_ino != old_inode
    assert not runtime.stage_managed_mac_bridge()
    assert list(destination.parent.iterdir()) == [destination]


def test_running_arena_defers_packaged_bridge(mac_package, monkeypatch):
    _, destination = mac_package
    monkeypatch.setattr(runtime, "is_mtga_running", lambda: True)
    assert not runtime.stage_managed_mac_bridge()
    assert destination.read_bytes() == b"old mapped bridge"


def test_failed_stage_keeps_previous_library_and_removes_temporary(mac_package, monkeypatch):
    _, destination = mac_package

    def fail(*args):
        raise OSError("cannot replace")

    monkeypatch.setattr(runtime.os, "replace", fail)
    with pytest.raises(OSError, match="cannot replace"):
        runtime.stage_managed_mac_bridge()
    assert destination.read_bytes() == b"old mapped bridge"
    assert list(destination.parent.iterdir()) == [destination]


def test_source_client_without_packaged_bridge_keeps_existing_install(mac_package):
    source, destination = mac_package
    source.unlink()
    assert not runtime.stage_managed_mac_bridge()
    assert destination.read_bytes() == b"old mapped bridge"
