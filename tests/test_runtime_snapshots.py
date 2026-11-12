"""Runtime snapshot publication/resolution: atomicity, integrity, protocols."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from arenamcp.desktop import runtime_snapshots as rs

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


_CHECKOUT_COUNTER = iter(range(1000))


def make_checkout(tmp_path, *, extra_files=None):
    """A minimal but real Coach-shaped checkout tree."""
    root = tmp_path / f"checkout-{next(_CHECKOUT_COUNTER)}"
    (root / "src" / "arenamcp" / "desktop").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "mtgacoach"\n', encoding="utf-8")
    (root / "src" / "arenamcp" / "__init__.py").write_text('__version__ = "1"\n', encoding="utf-8")
    (root / "src" / "arenamcp" / "desktop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "arenamcp" / "desktop" / "engine.py").write_text("VALUE = 1\n", encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def publish(root, snaps_dir, commit=COMMIT_A):
    staging = rs.stage_snapshot(root, commit, staging_parent=snaps_dir)
    final = rs.promote_snapshot(staging, snaps_dir)
    rs.publish_pointer(final, snaps_dir)
    return final


def test_stage_manifest_hashes_every_file_and_excludes_secrets(tmp_path):
    root = make_checkout(
        tmp_path,
        extra_files={
            ".env": "[REDACTED]",
            ".env.local": "[REDACTED]",
            "server.pem": "[REDACTED]",
            "server.key": "[REDACTED]",
            "credentials.json": "[REDACTED]",
            "aws_credentials": "[REDACTED]",
            "secrets_prod.json": "[REDACTED]",
            "docs/readme.md": "# hello\n",
        },
    )
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    cache = root / "src" / "arenamcp" / "desktop" / "__pycache__"
    cache.mkdir()
    (cache / "engine.pyc").write_bytes(b"\x00")

    snaps = tmp_path / "snaps"
    staging = rs.stage_snapshot(root, COMMIT_A, staging_parent=snaps)
    manifest = rs.load_manifest(staging)

    assert manifest["schema"] == rs.MANIFEST_SCHEMA
    assert manifest["commit"] == COMMIT_A
    assert manifest["files"]["src/arenamcp/desktop/engine.py"]
    assert manifest["files"]["docs/readme.md"]

    copied_relative = set(manifest["files"])
    excluded = (
        ".env",
        ".env.local",
        "server.pem",
        "server.key",
        "credentials.json",
        "aws_credentials",
        "secrets_prod.json",
        ".git/HEAD",
        "src/arenamcp/desktop/__pycache__/engine.pyc",
    )
    for secret in excluded:
        assert secret not in copied_relative, secret
        assert not (staging / secret).exists(), secret


def test_validate_staged_detects_tampered_content(tmp_path):
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    staging = rs.stage_snapshot(root, COMMIT_A, staging_parent=snaps)

    target = staging / "src" / "arenamcp" / "desktop" / "engine.py"
    target.write_text("VALUE = 999\n", encoding="utf-8")
    with pytest.raises(rs.SnapshotValidationError):
        rs.validate_staged(staging)

    staging2 = rs.stage_snapshot(root, COMMIT_A, staging_parent=snaps)
    (staging2 / "src" / "arenamcp" / "desktop" / "engine.py").unlink()
    with pytest.raises(rs.SnapshotValidationError):
        rs.validate_staged(staging2)


def test_stage_refuses_unknown_commit(tmp_path):
    root = make_checkout(tmp_path)
    with pytest.raises(rs.SnapshotError):
        rs.stage_snapshot(root, "", staging_parent=tmp_path)
    with pytest.raises(rs.SnapshotError):
        rs.stage_snapshot(root, "unknown commit", staging_parent=tmp_path)


def test_promote_renames_atomically_and_marks_read_only(tmp_path):
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    staging = rs.stage_snapshot(root, COMMIT_A, staging_parent=snaps)
    staging_name = staging.name

    final = rs.promote_snapshot(staging, snaps)

    assert final.parent == snaps
    assert final.name == rs.snapshot_id_for(COMMIT_A, rs.load_manifest(final)["content_hash"])
    assert not (snaps / staging_name).exists(), "staging must be gone after the rename"
    assert (final / "src" / "arenamcp" / "desktop" / "engine.py").is_file()

    engine_py = final / "src" / "arenamcp" / "desktop" / "engine.py"
    assert not (engine_py.stat().st_mode & 0o222), "snapshot files must be read-only"


def test_promote_is_idempotent_for_identical_content(tmp_path):
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"

    first = rs.promote_snapshot(rs.stage_snapshot(root, COMMIT_A, staging_parent=snaps), snaps)
    second = rs.promote_snapshot(rs.stage_snapshot(root, COMMIT_A, staging_parent=snaps), snaps)

    assert first == second
    leftovers = [p for p in snaps.iterdir() if p.name.startswith(rs.STAGING_PREFIX)]
    assert leftovers == []


def test_publishing_newer_snapshot_keeps_older_one_usable(tmp_path):
    """Rollback story: old snapshots are never destructively cleaned up."""
    root_a = make_checkout(tmp_path)
    root_b = make_checkout(tmp_path)
    engine_b = root_b / "src" / "arenamcp" / "desktop" / "engine.py"
    engine_b.write_text("VALUE = 2\n", encoding="utf-8")
    snaps = tmp_path / "snaps"

    snap_a = publish(root_a, snaps, commit=COMMIT_A)
    snap_b = publish(root_b, snaps, commit=COMMIT_B)

    assert snap_a.is_dir() and snap_b.is_dir()
    # Rolling back is re-pointing the pointer at the older complete snapshot.
    rs.publish_pointer(snap_a, snaps)
    pointer = rs.read_pointer_once(snaps)
    assert pointer["commit"] == COMMIT_A
    assert pointer["path"] == str(snap_a.resolve())


def test_pointer_roundtrip_carries_manifest_and_absolute_path(tmp_path):
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps)

    pointer = rs.read_pointer_once(snaps)
    assert pointer is not None
    assert pointer["schema"] == rs.POINTER_SCHEMA
    assert pointer["commit"] == COMMIT_A
    assert pointer["path"] == str(final.resolve())
    assert pointer["manifest"]["content_hash"] == rs.load_manifest(final)["content_hash"]
    assert pointer["ui_protocol"] == [1, 0]
    assert pointer["engine_protocol"] == [1, 0]


def test_unusable_pointers_degrade_to_none(tmp_path):
    """Corrupt/truncated/foreign pointers fall back to the checkout."""
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps)
    good_id = final.name

    def payload(**overrides):
        base = {
            "schema": rs.POINTER_SCHEMA,
            "snapshot_id": good_id,
            "commit": COMMIT_A,
            "content_hash": rs.load_manifest(final)["content_hash"],
            "ui_protocol": [1, 0],
            "engine_protocol": [1, 0],
        }
        base.update(overrides)
        return base

    cases = [
        "{not json",  # corrupt JSON
        json.dumps([]),  # wrong top-level shape
        json.dumps({"schema": "other/schema", "snapshot_id": good_id}),  # foreign schema
        json.dumps({"schema": rs.POINTER_SCHEMA}),  # missing snapshot_id
        json.dumps(payload(snapshot_id="../escape")),  # path traversal
        json.dumps(payload(snapshot_id=f"{COMMIT_B[:12]}-00000000")),  # vanished snapshot
        json.dumps(payload(commit="")),  # unusable commit
    ]
    for body in cases:
        (snaps / rs.POINTER_NAME).write_text(body, encoding="utf-8")
        assert rs.read_pointer_once(snaps) is None, body

    # A healthy pointer still reads back after all the failures above.
    (snaps / rs.POINTER_NAME).write_text(json.dumps(payload()), encoding="utf-8")
    pointer = rs.read_pointer_once(snaps)
    assert pointer is not None and pointer["commit"] == COMMIT_A


def test_concurrent_publication_and_reading_never_see_partial_state(tmp_path):
    """Publishers alternate pointers while readers resolve selections."""
    root_a = make_checkout(tmp_path)
    root_b = make_checkout(tmp_path)
    engine_b = root_b / "src" / "arenamcp" / "desktop" / "engine.py"
    engine_b.write_text("VALUE = 2\n", encoding="utf-8")
    snaps = tmp_path / "snaps"
    snap_a = publish(root_a, snaps, commit=COMMIT_A)
    snap_b = publish(root_b, snaps, commit=COMMIT_B)

    stop = threading.Event()
    errors: list[BaseException] = []

    def publisher():
        try:
            for _ in range(40):
                rs.publish_pointer(snap_a, snaps)
                rs.publish_pointer(snap_b, snaps)
        except BaseException as exc:  # pragma: no cover - failure capture
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                pointer = rs.read_pointer_once(snaps)
                if pointer is None:
                    continue
                # Every observation must be a complete, self-consistent pointer.
                assert pointer["schema"] == rs.POINTER_SCHEMA
                assert pointer["commit"] in (COMMIT_A, COMMIT_B)
                assert Path(pointer["path"]).is_dir()
                manifest = rs.load_manifest(Path(pointer["path"]))
                assert manifest["commit"] == pointer["commit"]
        except BaseException as exc:  # pragma: no cover - failure capture
            errors.append(exc)

    writers = [threading.Thread(target=publisher) for _ in range(2)]
    readers = [threading.Thread(target=reader) for _ in range(3)]
    for thread in writers + readers:
        thread.start()
    for thread in writers:
        thread.join(timeout=60)
    stop.set()
    for thread in readers:
        thread.join(timeout=60)

    assert not errors, errors


@pytest.mark.parametrize(
    ("ui", "engine", "expected"),
    [
        ([1, 0], [1, 0], True),  # identical
        ([1, 0], [1, 3], True),  # newer engine, same major
        ([1, 2], [1, 2], True),
        ([1, 3], [1, 2], False),  # engine older than UI minor
        ([1, 0], [2, 0], False),  # engine major bump
        ([2, 0], [1, 9], False),  # UI ahead of engine major
        (None, [1, 0], False),  # undeclared
        ([1, 0], None, False),
        ([1, 0], "1.0", False),  # wrong type
        ([1], [1, 0], False),  # malformed shape
        ([1, 0], [1, -1], False),
        ("1.0", "1.0", False),
    ],
)
def test_protocols_compatible_matrix(ui, engine, expected):
    assert rs.protocols_compatible(ui, engine) is expected


def test_declared_protocol_versions_are_paired():
    """The shipped UI and engine declare the same protocol by default."""
    assert rs.UI_PROTOCOL_VERSION == rs.ENGINE_PROTOCOL_VERSION


def test_plan_engine_reload_actions(tmp_path):
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps, commit=COMMIT_B)

    pointer = rs.read_pointer_once(snaps)
    assert pointer is not None

    # No snapshot infrastructure in play: behave exactly like today.
    plan = rs.plan_engine_reload(None, None)
    assert plan.action == "reload_unmanaged"
    assert plan.target_root is None

    # Already running the published snapshot: reload the same immutable root.
    plan = rs.plan_engine_reload(COMMIT_B, pointer)
    assert plan.action == "reload_current"
    assert plan.target_root == str(final.resolve())

    # Older UI running; published snapshot is newer but protocol-compatible.
    plan = rs.plan_engine_reload(COMMIT_A, pointer)
    assert plan.action == "adopt"
    assert plan.target_commit == COMMIT_B
    assert plan.target_root == str(final.resolve())

    # Incompatible declared protocol: never adopt silently.
    incompatible = dict(pointer)
    incompatible["engine_protocol"] = [2, 0]
    plan = rs.plan_engine_reload(COMMIT_A, incompatible)
    assert plan.action == "require_full_restart"
    assert plan.target_root == str(final.resolve())

    # Unknown protocol shape: same visible full-restart outcome.
    unknown = dict(pointer)
    unknown["engine_protocol"] = None
    plan = rs.plan_engine_reload(COMMIT_A, unknown)
    assert plan.action == "require_full_restart"


def _clear_snapshot_env(monkeypatch):
    for name in (
        "MTGACOACH_SNAPSHOT_SHA",
        "MTGACOACH_SNAPSHOT_ROOT",
        "MTGACOACH_SNAPSHOTS_DISABLE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_resolve_launch_selection_prefers_published_snapshot(tmp_path, monkeypatch):
    _clear_snapshot_env(monkeypatch)
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps)

    selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
    assert selection.snapshot_root == final.resolve()
    assert selection.commit == COMMIT_A
    assert selection.content_hash == rs.load_manifest(final)["content_hash"]


def test_resolve_launch_selection_disable_switch(tmp_path, monkeypatch):
    _clear_snapshot_env(monkeypatch)
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    publish(root, snaps)

    monkeypatch.setenv("MTGACOACH_SNAPSHOTS_DISABLE", "1")
    selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
    assert selection.snapshot_root is None
    assert selection.commit is None


def test_resolve_launch_selection_honours_outer_selection(tmp_path, monkeypatch):
    _clear_snapshot_env(monkeypatch)
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    publish(root, snaps)

    other = tmp_path / "other-root"
    other.mkdir()
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", COMMIT_B)
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_ROOT", str(other))
    selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
    assert selection.snapshot_root == other
    assert selection.commit == COMMIT_B


def test_resolve_launch_selection_falls_back_on_corrupt_pointer(tmp_path, monkeypatch):
    _clear_snapshot_env(monkeypatch)
    root = make_checkout(tmp_path)
    snaps = tmp_path / "snaps"
    snaps.mkdir(parents=True)
    (snaps / rs.POINTER_NAME).write_text("{corrupt", encoding="utf-8")

    selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
    assert selection.snapshot_root is None
    assert selection.commit is None


def test_resolve_launch_selection_falls_back_without_pointer(tmp_path, monkeypatch):
    _clear_snapshot_env(monkeypatch)
    root = make_checkout(tmp_path)
    selection = rs.resolve_launch_selection(root, snapshots_dir=tmp_path / "empty")
    assert selection.snapshot_root is None
