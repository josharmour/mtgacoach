import json

import pytest

from arenamcp.desktop.release_updates import pending_update
from arenamcp.standalone_auto_reload import auto_reload_default, compute_fingerprint


@pytest.mark.parametrize("automatic", [True, False])
def test_only_runtime_compatible_staged_updates_request_a_between_match_restart(
    monkeypatch, tmp_path, automatic
):
    marker = tmp_path / "pending.json"
    monkeypatch.setenv("MTGACOACH_VELOPACK_PENDING", str(marker))
    monkeypatch.delenv("ARENAMCP_AUTO_RELOAD", raising=False)
    assert auto_reload_default()[0]
    baseline = compute_fingerprint()
    marker.write_text(json.dumps({"version": "1.0.12", "automatic": automatic}))
    assert pending_update() == ("1.0.12", automatic)
    assert (compute_fingerprint(kind="release") != baseline) == automatic


@pytest.mark.parametrize("payload", ["", "[]", "{}", '{"version":"2","automatic":"false"}'])
def test_invalid_update_marker_cannot_request_a_restart(monkeypatch, tmp_path, payload):
    marker = tmp_path / "pending.json"
    marker.write_text(payload)
    monkeypatch.setenv("MTGACOACH_VELOPACK_PENDING", str(marker))
    assert pending_update() is None


def test_managed_unix_engine_uses_the_bundled_interpreter(monkeypatch, tmp_path):
    from arenamcp.desktop import runtime

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    exe = tmp_path / "runtime/bin/python3"
    exe.parent.mkdir(parents=True)
    exe.touch()
    (tmp_path / "coach-release.json").write_text("{}")
    assert runtime._is_app_root(tmp_path)
    assert runtime.is_packaged_runtime(str(exe), tmp_path)
    assert not runtime.is_packaged_runtime("/usr/bin/python3", tmp_path)
