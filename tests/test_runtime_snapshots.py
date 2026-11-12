"""Runtime snapshot publication/resolution: atomicity, integrity, protocols.

Fixtures build real git repositories and stage real commits, so tests exercise
the actual ``git archive`` export path, real commit SHAs and real working-tree
dirtiness -- not string stand-ins for SHAs.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from arenamcp.desktop import runtime_snapshots as rs

_REPO_COUNTER = iter(range(10000))


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def make_repo(
    tmp_path: Path, *, engine_value: int = 1, extra_files: dict[str, str] | None = None
) -> Path:
    """A minimal but real Coach-shaped git repository with one commit."""
    root = tmp_path / f"repo-{next(_REPO_COUNTER)}"
    (root / "src" / "arenamcp" / "desktop").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "mtgacoach"\n', encoding="utf-8")
    (root / "src" / "arenamcp" / "__init__.py").write_text('__version__ = "1"\n', encoding="utf-8")
    (root / "src" / "arenamcp" / "desktop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "arenamcp" / "desktop" / "engine.py").write_text(
        f"VALUE = {engine_value}\n", encoding="utf-8"
    )
    for rel, content in (extra_files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "init")
    return root


def commit_change(root: Path, rel: str, content: str) -> str:
    """Commit a file change and return the new full SHA."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", rel)
    return _git(root, "rev-parse", "HEAD")


def head_of(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def publish(root: Path, snaps_dir: Path, commit: str | None = None) -> Path:
    staging = rs.stage_snapshot(root, commit or head_of(root), staging_parent=snaps_dir)
    final = rs.promote_snapshot(staging, snaps_dir)
    rs.publish_pointer(final, snaps_dir)
    return final


def make_writable(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            os.chmod(path, 0o755)
        elif path.is_file():
            os.chmod(path, 0o644)


@pytest.fixture(autouse=True)
def _restore_write_bits(tmp_path):
    """Promoted snapshots are read-only; restore write bits so pytest can clean up."""
    yield
    make_writable(tmp_path)


@pytest.fixture(autouse=True)
def _clear_snapshot_env(monkeypatch):
    for name in (
        "MTGACOACH_SNAPSHOT_SHA",
        "MTGACOACH_SNAPSHOT_ROOT",
        "MTGACOACH_SNAPSHOTS_DISABLE",
        "MTGACOACH_DISABLE_RUNTIME_SNAPSHOT",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# pinned export vs working tree


def test_stage_exports_the_pinned_commit_not_the_dirty_worktree(tmp_path):
    """Dirty tracked files, untracked files and external symlinks must never
    leak into a snapshot that claims an older SHA."""
    root = make_repo(tmp_path)
    commit_a = head_of(root)

    # Dirty the tracked file and add untracked noise AFTER the commit.
    (root / "src" / "arenamcp" / "desktop" / "engine.py").write_text(
        "VALUE = DIRTY\n", encoding="utf-8"
    )
    (root / "untracked.txt").write_text("noise\n", encoding="utf-8")
    external = tmp_path / "outside-secret.txt"
    external.write_text("[REDACTED]", encoding="utf-8")
    (root / "link-outside.txt").symlink_to(external)

    snaps = tmp_path / "snaps"
    staging = rs.stage_snapshot(root, commit_a, staging_parent=snaps)

    engine = (staging / "src" / "arenamcp" / "desktop" / "engine.py").read_text(encoding="utf-8")
    assert engine == "VALUE = 1\n", "snapshot must hold the committed content"
    assert not (staging / "untracked.txt").exists()
    assert not (staging / ".git").exists()
    assert not (staging / "link-outside.txt").exists()

    manifest = rs.load_manifest(staging)
    assert manifest["commit"] == commit_a
    assert manifest["source"] == "git-archive"


def test_stage_two_different_commits_produce_distinct_snapshots(tmp_path):
    root = make_repo(tmp_path)
    commit_a = head_of(root)
    commit_b = commit_change(root, "src/arenamcp/desktop/engine.py", "VALUE = 2\n")

    snaps = tmp_path / "snaps"
    snap_a = publish(root, snaps, commit_a)
    snap_b = publish(root, snaps, commit_b)

    assert snap_a != snap_b
    assert rs.load_manifest(snap_a)["commit"] == commit_a
    assert rs.load_manifest(snap_b)["commit"] == commit_b
    assert (
        snap_b / "src" / "arenamcp" / "desktop" / "engine.py"
    ).read_text(encoding="utf-8") == "VALUE = 2\n"


def test_stage_manifest_hashes_every_file_and_excludes_secrets(tmp_path):
    root = make_repo(
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
    cache = root / "src" / "arenamcp" / "desktop" / "__pycache__"
    cache.mkdir()
    (cache / "engine.pyc").write_bytes(b"\x00")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "secrets")

    snaps = tmp_path / "snaps"
    staging = rs.stage_snapshot(root, head_of(root), staging_parent=snaps)
    manifest = rs.load_manifest(staging)

    assert manifest["schema"] == rs.MANIFEST_SCHEMA
    assert manifest["files"]["src/arenamcp/desktop/engine.py"]
    assert manifest["files"]["docs/readme.md"]

    copied_relative = set(manifest["files"])
    for secret in (
        ".env",
        ".env.local",
        "server.pem",
        "server.key",
        "credentials.json",
        "aws_credentials",
        "secrets_prod.json",
        ".git/HEAD",
        "src/arenamcp/desktop/__pycache__/engine.pyc",
    ):
        assert secret not in copied_relative, secret
        assert not (staging / secret).exists(), secret


def test_validate_staged_detects_tampered_content(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    staging = rs.stage_snapshot(root, head_of(root), staging_parent=snaps)

    target = staging / "src" / "arenamcp" / "desktop" / "engine.py"
    target.write_text("VALUE = 999\n", encoding="utf-8")
    with pytest.raises(rs.SnapshotValidationError):
        rs.validate_staged(staging)

    staging2 = rs.stage_snapshot(root, head_of(root), staging_parent=snaps)
    (staging2 / "src" / "arenamcp" / "desktop" / "engine.py").unlink()
    with pytest.raises(rs.SnapshotValidationError):
        rs.validate_staged(staging2)


def test_stage_refuses_unknown_commit(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(rs.SnapshotError):
        rs.stage_snapshot(root, "", staging_parent=tmp_path)
    with pytest.raises(rs.SnapshotError):
        rs.stage_snapshot(root, "does-not-exist", staging_parent=tmp_path)


# ---------------------------------------------------------------------------
# promotion / publication


def test_promote_renames_atomically_and_marks_read_only(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    staging = rs.stage_snapshot(root, head_of(root), staging_parent=snaps)
    staging_name = staging.name

    final = rs.promote_snapshot(staging, snaps)

    assert final.parent == snaps
    assert final.name == rs.snapshot_id_for(
        rs.load_manifest(final)["commit"], rs.load_manifest(final)["content_hash"]
    )
    assert not (snaps / staging_name).exists(), "staging must be gone after the rename"
    assert (final / "src" / "arenamcp" / "desktop" / "engine.py").is_file()

    engine_py = final / "src" / "arenamcp" / "desktop" / "engine.py"
    assert not (engine_py.stat().st_mode & 0o222), "snapshot files must be read-only"


def test_promote_is_idempotent_for_identical_content(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"

    first = rs.promote_snapshot(rs.stage_snapshot(root, head_of(root), staging_parent=snaps), snaps)
    second = rs.promote_snapshot(rs.stage_snapshot(root, head_of(root), staging_parent=snaps), snaps)

    assert first == second
    leftovers = [p for p in snaps.iterdir() if p.name.startswith(rs.STAGING_PREFIX)]
    assert leftovers == []


def test_promote_quarantines_corrupt_published_snapshot(tmp_path):
    """A tampered published directory is never trusted on reuse: it is moved
    aside and the freshly staged content takes its place atomically."""
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps)

    # Tamper with the published snapshot (it is read-only; flip the bit first).
    victim = final / "src" / "arenamcp" / "desktop" / "engine.py"
    os.chmod(victim, 0o644)
    victim.write_text("VALUE = TAMPERED\n", encoding="utf-8")
    with pytest.raises(rs.SnapshotValidationError):
        rs.validate_snapshot_dir(final)

    again = rs.promote_snapshot(rs.stage_snapshot(root, head_of(root), staging_parent=snaps), snaps)
    assert again == final
    assert rs.validate_snapshot_dir(final)["content_hash"] == rs.load_manifest(
        rs.stage_snapshot(root, head_of(root), staging_parent=snaps)
    )["content_hash"]
    quarantined = [p for p in snaps.iterdir() if p.name.startswith(".corrupt-")]
    assert len(quarantined) == 1, "corrupt snapshot must be quarantined, not deleted"


def test_publishing_newer_snapshot_keeps_older_one_usable(tmp_path):
    """Rollback story: old snapshots are never destructively cleaned up."""
    root_a = make_repo(tmp_path)
    commit_a = head_of(root_a)
    root_b = make_repo(tmp_path)
    commit_b = head_of(root_b)
    snaps = tmp_path / "snaps"

    snap_a = publish(root_a, snaps, commit_a)
    snap_b = publish(root_b, snaps, commit_b)

    assert snap_a.is_dir() and snap_b.is_dir()
    # Rolling back is re-pointing the pointer at the older complete snapshot.
    rs.publish_pointer(snap_a, snaps)
    pointer = rs.read_pointer_once(snaps)
    assert pointer["commit"] == commit_a
    assert pointer["path"] == str(snap_a.resolve())


def test_failed_publication_leaves_previous_pointer_intact(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    snap_a = publish(root, snaps)

    # A staging directory that fails validation must never reach promotion.
    bad_staging = rs.stage_snapshot(root, head_of(root), staging_parent=snaps)
    (bad_staging / "src" / "arenamcp" / "desktop" / "engine.py").write_text(
        "VALUE = BROKEN\n", encoding="utf-8"
    )
    with pytest.raises(rs.SnapshotValidationError):
        rs.promote_snapshot(bad_staging, snaps)

    pointer = rs.read_pointer_once(snaps)
    assert pointer is not None
    assert pointer["path"] == str(snap_a.resolve())


# ---------------------------------------------------------------------------
# pointer integrity and resolution


def test_pointer_roundtrip_carries_manifest_and_absolute_path(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps)

    pointer = rs.read_pointer_once(snaps)
    assert pointer is not None
    assert pointer["schema"] == rs.POINTER_SCHEMA
    assert pointer["commit"] == head_of(root)
    assert pointer["path"] == str(final.resolve())
    assert pointer["manifest"]["content_hash"] == rs.load_manifest(final)["content_hash"]
    assert pointer["ui_protocol"] == [1, 0]
    assert pointer["engine_protocol"] == [1, 0]


def _pointer_payload(snaps: Path, final: Path, **overrides) -> dict:
    manifest = rs.load_manifest(final)
    base = {
        "schema": rs.POINTER_SCHEMA,
        "snapshot_id": final.name,
        "commit": manifest["commit"],
        "content_hash": manifest["content_hash"],
        "ui_protocol": [1, 0],
        "engine_protocol": [1, 0],
        "path": str(final.resolve()),
    }
    base.update(overrides)
    return base


def test_unusable_pointers_degrade_to_none(tmp_path):
    """Corrupt/truncated/foreign/mismatched pointers fall back to the checkout."""
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps)
    other_commit = "f" * 40

    def payload(**overrides):
        return _pointer_payload(snaps, final, **overrides)

    cases = [
        "{not json",  # corrupt JSON
        json.dumps([]),  # wrong top-level shape
        json.dumps({"schema": "other/schema", "snapshot_id": final.name}),  # foreign schema
        json.dumps({"schema": rs.POINTER_SCHEMA}),  # missing snapshot_id
        json.dumps(payload(snapshot_id="../escape")),  # path traversal
        json.dumps(payload(snapshot_id=f"{other_commit[:12]}-00000000")),  # vanished snapshot
        json.dumps(payload(commit="")),  # unusable commit
        json.dumps(payload(commit=other_commit)),  # commit/manifest disagreement
        json.dumps(payload(content_hash="0" * 64)),  # hash/manifest disagreement
    ]
    for body in cases:
        (snaps / rs.POINTER_NAME).write_text(body, encoding="utf-8")
        assert rs.read_pointer_once(snaps) is None, body

    # A healthy pointer still reads back after all the failures above.
    (snaps / rs.POINTER_NAME).write_text(json.dumps(payload()), encoding="utf-8")
    pointer = rs.read_pointer_once(snaps)
    assert pointer is not None and pointer["commit"] == head_of(root)

    # A forged absolute "path" field is ignored: resolution recomputes the
    # location from snapshot_id inside the snapshots directory.
    (snaps / rs.POINTER_NAME).write_text(json.dumps(payload(path="/etc/passwd")), encoding="utf-8")
    pointer = rs.read_pointer_once(snaps)
    assert pointer is not None
    assert pointer["path"] == str(final.resolve())


def test_resolve_launch_selection_rejects_tampered_published_snapshot(tmp_path):
    """A published snapshot tampered with after publication must never launch;
    resolution falls back to the checkout."""
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps)

    victim = final / "src" / "arenamcp" / "desktop" / "engine.py"
    os.chmod(victim, 0o644)
    victim.write_text("VALUE = TAMPERED\n", encoding="utf-8")

    selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
    assert selection.snapshot_root is None
    assert selection.commit is None


def test_resolve_launch_selection_prefers_published_snapshot(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    final = publish(root, snaps)

    selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
    assert selection.snapshot_root == final.resolve()
    assert selection.commit == head_of(root)
    assert selection.content_hash == rs.load_manifest(final)["content_hash"]


def test_resolve_launch_selection_disable_switch(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    publish(root, snaps)

    os.environ["MTGACOACH_SNAPSHOTS_DISABLE"] = "1"
    try:
        selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
    finally:
        os.environ.pop("MTGACOACH_SNAPSHOTS_DISABLE", None)
    assert selection.snapshot_root is None
    assert selection.commit is None


def test_resolve_launch_selection_honours_outer_selection(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    publish(root, snaps)

    # An outer launcher hands down a SHA plus the root it already validated.
    # The root must be a real, fully valid snapshot agreeing with that SHA --
    # an arbitrary directory or a SHA/root mismatch falls back to the pointer.
    other_repo = make_repo(tmp_path, engine_value=2)
    other_snaps = tmp_path / "other-snaps"
    other_final = publish(other_repo, other_snaps)
    other_commit = head_of(other_repo)

    os.environ["MTGACOACH_SNAPSHOT_SHA"] = other_commit
    os.environ["MTGACOACH_SNAPSHOT_ROOT"] = str(other_final)
    try:
        selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
        assert selection.snapshot_root == other_final
        assert selection.commit == other_commit

        # SHA/root disagreement falls back to the published pointer.
        os.environ["MTGACOACH_SNAPSHOT_SHA"] = "f" * 40
        selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
        assert selection.commit == head_of(root)

        # An arbitrary non-snapshot directory fails validation and falls back.
        os.environ["MTGACOACH_SNAPSHOT_SHA"] = other_commit
        os.environ["MTGACOACH_SNAPSHOT_ROOT"] = str(tmp_path / "not-a-snapshot")
        selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
        assert selection.commit == head_of(root)
    finally:
        os.environ.pop("MTGACOACH_SNAPSHOT_SHA", None)
        os.environ.pop("MTGACOACH_SNAPSHOT_ROOT", None)


def test_resolve_launch_selection_falls_back_on_corrupt_pointer(tmp_path):
    root = make_repo(tmp_path)
    snaps = tmp_path / "snaps"
    snaps.mkdir(parents=True)
    (snaps / rs.POINTER_NAME).write_text("{corrupt", encoding="utf-8")

    selection = rs.resolve_launch_selection(root, snapshots_dir=snaps)
    assert selection.snapshot_root is None
    assert selection.commit is None


def test_resolve_launch_selection_falls_back_without_pointer(tmp_path):
    root = make_repo(tmp_path)
    selection = rs.resolve_launch_selection(root, snapshots_dir=tmp_path / "empty")
    assert selection.snapshot_root is None


# ---------------------------------------------------------------------------
# concurrency


def test_concurrent_publication_and_reading_never_see_partial_state(tmp_path):
    """Publishers alternate pointers while readers resolve selections."""
    root_a = make_repo(tmp_path)
    commit_a = head_of(root_a)
    root_b = make_repo(tmp_path)
    commit_b = head_of(root_b)
    snaps = tmp_path / "snaps"
    snap_a = publish(root_a, snaps, commit_a)
    snap_b = publish(root_b, snaps, commit_b)

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
                assert pointer["commit"] in (commit_a, commit_b)
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


# ---------------------------------------------------------------------------
# protocol compatibility


@pytest.mark.parametrize(
    ("ui", "engine", "expected"),
    [
        ([1, 0], [1, 0], True),  # identical
        ([1, 0], [1, 3], True),  # newer engine, same major
        ([1, 2], [1, 2], True),
        ([1, 3], [1, 2], False),  # engine older than UI minor
        ([1, 0], [2, 0], False),  # engine major bump
        ([2, 0], [1, 9], False),  # UI ahead of engine major
        ([1, 0], [1, 999], False),  # unknown declared engine pair
        ([1, 999], [1, 0], False),  # unknown declared UI pair
        (None, [1, 0], False),  # undeclared
        ([1, 0], None, False),
        ([1, 0], "1.0", False),  # wrong type
        ([1], [1, 0], False),  # malformed shape
        ([1, 0], [1, -1], False),
        ("1.0", "1.0", False),
        ([True, 0], [1, 0], False),  # bool masquerading as int
    ],
)
def test_protocols_compatible_matrix(ui, engine, expected):
    assert rs.protocols_compatible(ui, engine) is expected


def test_declared_protocol_versions_are_paired():
    """The shipped UI and engine declare the same protocol by default."""
    assert rs.UI_PROTOCOL_VERSION == rs.ENGINE_PROTOCOL_VERSION


def test_plan_engine_reload_actions(tmp_path):
    root_old = make_repo(tmp_path, engine_value=1)
    commit_old = head_of(root_old)
    root_new = make_repo(tmp_path, engine_value=2)
    commit_new = head_of(root_new)
    snaps = tmp_path / "snaps"
    final_new = publish(root_new, snaps, commit_new)

    pointer = rs.read_pointer_once(snaps)
    assert pointer is not None

    # No snapshot infrastructure in play: behave exactly like today.
    plan = rs.plan_engine_reload(None, None)
    assert plan.action == "reload_unmanaged"
    assert plan.target_root is None

    # Already running the published snapshot: reload the same immutable root.
    plan = rs.plan_engine_reload(commit_new, pointer)
    assert plan.action == "reload_current"
    assert plan.target_root == str(final_new.resolve())

    # Older UI running; published snapshot is newer but protocol-compatible.
    plan = rs.plan_engine_reload(commit_old, pointer)
    assert plan.action == "adopt"
    assert plan.target_commit == commit_new
    assert plan.target_root == str(final_new.resolve())

    # Incompatible declared protocol: never adopt silently.
    incompatible = dict(pointer)
    incompatible["engine_protocol"] = [2, 0]
    plan = rs.plan_engine_reload(commit_old, incompatible)
    assert plan.action == "require_full_restart"
    assert plan.target_root == str(final_new.resolve())

    # Unknown declared protocol pair: same visible full-restart outcome.
    unknown_pair = dict(pointer)
    unknown_pair["engine_protocol"] = [1, 999]
    plan = rs.plan_engine_reload(commit_old, unknown_pair)
    assert plan.action == "require_full_restart"

    # Unknown protocol shape: same visible full-restart outcome.
    unknown_shape = dict(pointer)
    unknown_shape["engine_protocol"] = None
    plan = rs.plan_engine_reload(commit_old, unknown_shape)
    assert plan.action == "require_full_restart"
