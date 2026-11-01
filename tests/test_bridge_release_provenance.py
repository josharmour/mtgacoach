"""A stale DLL or changed C# source must not become a release artifact."""

import json

import pytest
from scripts.verify_bridge_release import DLL, MANIFEST, PROJECT, REFERENCES, sha256, source_hashes, verify


@pytest.fixture
def build(tmp_path):
    (tmp_path / PROJECT).mkdir(parents=True)
    (tmp_path / PROJECT / "Plugin.cs").write_text("current source")
    (tmp_path / PROJECT / "MtgaCoachBridge.csproj").write_text("current project")
    (tmp_path / DLL).parent.mkdir(parents=True)
    (tmp_path / DLL).write_bytes(b"fresh compiled bridge")
    manifest = {
        "schema_version": 1,
        "source_sha256": source_hashes(tmp_path),
        "dll_sha256": sha256(tmp_path / DLL),
        "compiler": "dotnet test SDK",
        "built_at_utc": "2026-11-01T08:00:00Z",
        "reference_sha256": {name: "a" * 64 for name in REFERENCES},
    }
    (tmp_path / MANIFEST).write_text(json.dumps(manifest))
    return tmp_path


def test_current_build_passes(build):
    assert verify(build)["dll_sha256"] == sha256(build / DLL)


@pytest.mark.parametrize(
    "change", ["edit_source", "new_source", "delete_source", "project", "dll", "manifest"]
)
def test_stale_or_untracked_build_fails(build, change):
    if change == "edit_source":
        (build / PROJECT / "Plugin.cs").write_text("new unbuilt fix")
    elif change == "new_source":
        (build / PROJECT / "New.cs").write_text("new unbuilt feature")
    elif change == "delete_source":
        (build / PROJECT / "Plugin.cs").unlink()
    elif change == "project":
        (build / PROJECT / "MtgaCoachBridge.csproj").write_text("changed references")
    elif change == "dll":
        (build / DLL).write_bytes(b"stale packaged bridge")
    else:
        (build / MANIFEST).unlink()
    with pytest.raises(ValueError):
        verify(build)


def test_source_newlines_are_portable_between_build_and_packaging_hosts(build):
    source = build / PROJECT / "Plugin.cs"
    source.write_bytes(b"one\ntwo\n")
    before = source_hashes(build)
    source.write_bytes(b"one\r\ntwo\r\n")
    assert source_hashes(build) == before


def test_incomplete_reference_inventory_fails(build):
    manifest = json.loads((build / MANIFEST).read_text())
    del manifest["reference_sha256"]["Core.dll"]
    (build / MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="inventory"):
        verify(build)
