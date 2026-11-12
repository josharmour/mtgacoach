"""Immutable per-commit runtime snapshots with atomic publication.

Contract (see docs/RUNTIME_SNAPSHOTS.md):

- A snapshot is a complete, immutable copy of one verified commit, exported to
  ``<runtime_root>/snapshots/<commit12>-<content8>/`` with a manifest. The
  running Coach never executes from a changing checkout.
- Publication is atomic: content is staged in a sibling ``.staging-*``
  directory, fully validated (every file present, every hash matching), and
  only then renamed into place. The ``current-snapshot.json`` pointer is
  replaced with ``os.replace``, so concurrent publishers and readers never
  observe a partial or mixed state: a reader sees either the previous
  pointer, or the complete new one.
- Readers resolve the selection ONCE per launch (``resolve_launch_selection``)
  and use absolute versioned roots afterwards.
- Protocol compatibility is declared, not discovered: a snapshot carries
  ``ui_protocol`` and ``engine_protocol`` versions. Old UI + newer engine is
  allowed only through a declared compatible protocol (same major, engine
  minor >= UI minor). Unknown or incompatible versions visibly require a full
  Restart Coach; they are never adopted silently.
- Snapshots are never destructively cleaned up while they may be in use; the
  rollback path is simply re-pointing the pointer at an older complete
  snapshot (or disabling snapshots entirely via MTGACOACH_SNAPSHOTS_DISABLE).
- Secrets are never copied: secret-shaped files (.env*, *.pem, *.key,
  credentials*) are excluded from staging and therefore from manifests.

This module is importable from the checkout before the desktop app imports
anything from the snapshot root: it is deliberately dependency-free (stdlib
only) so it can act as the launch bootstrap resolver.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOTS_DIRNAME = "snapshots"
POINTER_NAME = "current-snapshot.json"
MANIFEST_NAME = "snapshot-manifest.json"
STAGING_PREFIX = ".staging-"
MANIFEST_SCHEMA = "mtgacoach-runtime-snapshot/1"
POINTER_SCHEMA = "mtgacoach-runtime-snapshot-pointer/1"

#: Declared, tested UI-engine protocol versions. Bump the minor for additive,
#: backward-compatible protocol changes; bump the major for breaking changes.
UI_PROTOCOL_VERSION: tuple[int, int] = (1, 0)
ENGINE_PROTOCOL_VERSION: tuple[int, int] = (1, 0)

#: Directories never copied into a snapshot (VCS metadata, caches, envs).
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "node_modules",
        ".worktrees",
        ".tox",
        ".eggs",
        "dist",
        "build",
        "htmlcov",
    }
)

#: File basenames/patterns excluded as potential secrets. Matching is
#: case-insensitive against the file name only.
SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "credentials*",
    "*credentials*",
    "secrets*.json",
    "*.keystore",
    "*.p12",
    "*.pfx",
)


class SnapshotError(RuntimeError):
    """Base class for snapshot publication/resolution failures."""


class SnapshotValidationError(SnapshotError):
    """A staged snapshot failed completeness/integrity validation."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_secret_file(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in SECRET_FILE_PATTERNS)


def snapshots_root(runtime_root: str | os.PathLike[str] | None = None) -> Path:
    """Absolute snapshots directory under the runtime root."""
    if runtime_root is None:
        from .runtime import get_runtime_root

        runtime_root = get_runtime_root()
    return Path(runtime_root) / SNAPSHOTS_DIRNAME


def protocols_compatible(ui_protocol: Any, engine_protocol: Any) -> bool:
    """True only for a declared, tested UI-engine protocol pairing.

    Compatible means: both sides present as integer (major, minor) pairs with
    equal majors and an engine minor >= UI minor (additive evolution).
    Anything missing, malformed or unknown is incompatible by definition —
    those require a visible full Restart Coach, never a silent reload.
    """

    def _parse(value: Any) -> tuple[int, int] | None:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                return (int(value[0]), int(value[1]))
            except (TypeError, ValueError):
                return None
        return None

    ui = _parse(ui_protocol)
    engine = _parse(engine_protocol)
    if ui is None or engine is None:
        return False
    return ui[0] == engine[0] and engine[1] >= ui[1]


def _copy_tree_filtered(source: Path, destination: Path) -> list[str]:
    """Copy the checkout tree minus caches/envs/secret-shaped files.

    Returns the sorted list of relative paths copied (POSIX separators).
    """
    copied: list[str] = []
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRECTORIES)
        for name in sorted(files):
            if _is_secret_file(name):
                continue
            src = root_path / name
            rel = src.relative_to(source).as_posix()
            dst = destination / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied.append(rel)
    return copied


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_fingerprint(checkout: Path) -> str:
    """Hash the dependency specification files so env changes are detectable."""
    digest = hashlib.sha256()
    for name in sorted(
        name
        for name in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "uv.lock")
        if (checkout / name).is_file()
    ):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((checkout / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def stage_snapshot(
    checkout: str | os.PathLike[str],
    commit: str,
    *,
    staging_parent: Path | None = None,
) -> Path:
    """Stage an immutable copy of ``checkout`` into a temp dir.

    The staging directory lives under the snapshots root (same filesystem) so
    promotion is an atomic rename. Validation happens before promotion; a
    staging directory that fails validation never becomes visible.
    """
    checkout_path = Path(checkout).resolve()
    if not (checkout_path / "pyproject.toml").is_file() or not (checkout_path / "src" / "arenamcp").is_dir():
        raise SnapshotError(f"Not a Coach checkout: {checkout_path}")
    commit = commit.strip()
    if not commit or commit == "unknown commit":
        raise SnapshotError("Refusing to stage a snapshot without a verified commit SHA")

    parent = staging_parent if staging_parent is not None else snapshots_root()
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent))
    try:
        copied = _copy_tree_filtered(checkout_path, staging)
        content_hash = hashlib.sha256()
        for rel in copied:
            content_hash.update(rel.encode("utf-8"))
            content_hash.update(b"\0")
            content_hash.update(_hash_file(staging / rel).encode("utf-8"))
            content_hash.update(b"\0")
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "commit": commit,
            "content_hash": content_hash.hexdigest(),
            "created_utc": _utc_now_iso(),
            "ui_protocol": list(UI_PROTOCOL_VERSION),
            "engine_protocol": list(ENGINE_PROTOCOL_VERSION),
            "dependency_fingerprint": _dependency_fingerprint(checkout_path),
            "file_count": len(copied),
            "files": {rel: _hash_file(staging / rel) for rel in copied},
        }
        manifest_path = staging / MANIFEST_NAME
        with manifest_path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def load_manifest(snapshot_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and structurally validate a snapshot manifest."""
    manifest_path = Path(snapshot_dir) / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(f"Unreadable snapshot manifest: {manifest_path}") from exc
    for key in ("schema", "commit", "content_hash", "files"):
        if key not in manifest:
            raise SnapshotValidationError(f"Snapshot manifest missing '{key}': {manifest_path}")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise SnapshotValidationError(f"Unknown snapshot schema: {manifest['schema']!r}")
    return manifest


def validate_staged(staging: Path) -> dict[str, Any]:
    """Verify a staged tree is complete and matches its manifest exactly.

    Every file listed must exist with a matching hash; no extra files may
    exist besides the manifest itself. This is what makes the later rename
    atomic from the reader's perspective: once promoted, the directory is
    already known-complete.
    """
    staging = Path(staging)
    manifest = load_manifest(staging)
    files: dict[str, str] = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise SnapshotValidationError("Snapshot manifest lists no files")
    for rel, expected in files.items():
        path = staging / rel
        if not path.is_file():
            raise SnapshotValidationError(f"Staged file missing: {rel}")
        if _hash_file(path) != expected:
            raise SnapshotValidationError(f"Staged file hash mismatch: {rel}")
    actual = {
        p.relative_to(staging).as_posix()
        for p in staging.rglob("*")
        if p.is_file() and p.name != MANIFEST_NAME
    }
    unexpected = actual - set(files)
    if unexpected:
        raise SnapshotValidationError(f"Staged tree has unlisted files: {sorted(unexpected)[:5]}")
    return manifest


def _make_read_only(root: Path) -> None:
    """Best-effort immutability marking; never fails publication."""
    for path in root.rglob("*"):
        try:
            if path.is_dir():
                path.chmod(
                    stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
                )
            else:
                path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            continue


def snapshot_id_for(commit: str, content_hash: str) -> str:
    return f"{commit[:12]}-{content_hash[:8]}"


def promote_snapshot(staging: Path, snapshots_dir: Path | None = None) -> Path:
    """Validate a staged snapshot and atomically rename it into place.

    Idempotent: promoting identical content twice reuses the existing complete
    snapshot and discards the redundant staging copy. Existing snapshots are
    never modified or deleted — publication is purely additive.
    """
    staging = Path(staging)
    snapshots_dir = Path(snapshots_dir) if snapshots_dir is not None else staging.parent
    manifest = validate_staged(staging)
    final = snapshots_dir / snapshot_id_for(manifest["commit"], manifest["content_hash"])
    if final.is_dir():
        try:
            existing = load_manifest(final)
        except SnapshotValidationError:
            existing = None
        if existing is not None and existing.get("content_hash") == manifest["content_hash"]:
            shutil.rmtree(staging, ignore_errors=True)
            return final
        raise SnapshotError(f"Snapshot id collision with different content: {final}")
    # Same-directory rename: atomic. Readers either see nothing or the whole
    # validated tree; no partially-visible state exists at any instant.
    os.rename(staging, final)
    _fsync_dir(snapshots_dir)
    _make_read_only(final)
    return final


def publish_pointer(snapshot_dir: str | os.PathLike[str], snapshots_dir: Path | None = None) -> Path:
    """Atomically publish ``snapshot_dir`` as the next-launch selection.

    The pointer file is written to a temp file in the same directory and moved
    with ``os.replace`` — concurrent readers observe either the old pointer or
    the new one, never a partial write.
    """
    snapshot_dir = Path(snapshot_dir)
    manifest = load_manifest(snapshot_dir)
    target_root = Path(snapshots_dir) if snapshots_dir is not None else snapshot_dir.parent
    pointer = {
        "schema": POINTER_SCHEMA,
        "snapshot_id": snapshot_dir.name,
        "commit": manifest["commit"],
        "content_hash": manifest["content_hash"],
        "ui_protocol": manifest.get("ui_protocol"),
        "engine_protocol": manifest.get("engine_protocol"),
        "dependency_fingerprint": manifest.get("dependency_fingerprint"),
        "published_utc": _utc_now_iso(),
    }
    target_root.mkdir(parents=True, exist_ok=True)
    tmp = target_root / f"{POINTER_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(pointer, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target_root / POINTER_NAME)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(target_root)
    return target_root / POINTER_NAME


def read_pointer_once(snapshots_dir: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """Read the published selection exactly once; None when absent/corrupt.

    A corrupt or half-written pointer degrades to None (launchers fall back to
    the checkout); it never raises into the launch path.
    """
    root = Path(snapshots_dir) if snapshots_dir is not None else snapshots_root()
    pointer_path = root / POINTER_NAME
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(pointer, dict) or pointer.get("schema") != POINTER_SCHEMA:
        return None
    snapshot_id = pointer.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return None
    commit = pointer.get("commit")
    if not isinstance(commit, str) or not commit.strip():
        return None
    candidate = root / snapshot_id
    try:
        manifest = load_manifest(candidate)
    except SnapshotValidationError:
        return None
    pointer["path"] = str(candidate.resolve())
    pointer["manifest"] = manifest
    return pointer


@dataclasses.dataclass(frozen=True)
class ReloadPlan:
    """Outcome of consulting published snapshots at a safe checkpoint."""

    action: str  # "reload_unmanaged" | "reload_current" | "adopt" | "require_full_restart"
    target_root: str | None
    target_commit: str | None
    reason: str


def plan_engine_reload(
    current_commit: str | None,
    pointer: dict[str, Any] | None,
    *,
    ui_protocol: Any = UI_PROTOCOL_VERSION,
) -> ReloadPlan:
    """Decide what an engine reload should do about snapshots.

    - No snapshot infrastructure in play -> behave exactly like today.
    - Pointer equals the running snapshot -> reload the same immutable root.
    - Newest published snapshot differs and declares a compatible protocol ->
      explicitly adopt it for the new engine (old UI / new engine via tested
      protocol).
    - Newest published snapshot declares an unknown/incompatible protocol ->
      do NOT reload into it; require a visible full Restart Coach and keep the
      current engine usable.
    """
    if pointer is None:
        return ReloadPlan(
            "reload_unmanaged", None, None, "No published runtime snapshot; reloading from checkout"
        )
    target_commit = str(pointer.get("commit") or "")
    target_root = pointer.get("path")
    if current_commit and target_commit == current_commit:
        return ReloadPlan(
            "reload_current", target_root, target_commit, "Already running the published snapshot"
        )
    engine_protocol = pointer.get("engine_protocol")
    if not protocols_compatible(ui_protocol, engine_protocol):
        return ReloadPlan(
            "require_full_restart",
            target_root,
            target_commit,
            f"Published engine snapshot {target_commit[:7] or '?'} declares protocol "
            f"{engine_protocol!r}, incompatible with this UI ({list(ui_protocol)}); "
            "a full Restart Coach is required",
        )
    return ReloadPlan(
        "adopt",
        target_root,
        target_commit,
        f"Adopting published engine snapshot {target_commit[:7]} "
        f"(protocol {engine_protocol!r} compatible with UI {list(ui_protocol)})",
    )


@dataclasses.dataclass(frozen=True)
class LaunchSelection:
    """Resolved launch-time selection: one absolute versioned root."""

    snapshot_root: Path | None
    commit: str | None
    content_hash: str | None


def resolve_launch_selection(
    checkout_root: str | os.PathLike[str],
    *,
    snapshots_dir: str | os.PathLike[str] | None = None,
) -> LaunchSelection:
    """Resolve the launch-time snapshot selection exactly once.

    Resolution order:

    1. ``MTGACOACH_SNAPSHOT_SHA`` already set -> an outer launcher already made
       the selection; honour it and read nothing else.
    2. ``MTGACOACH_SNAPSHOTS_DISABLE`` set -> snapshots off; run from checkout.
       This is also the documented rollback switch.
    3. Published pointer present and its snapshot validates -> run from that
       absolute versioned root.
    4. Otherwise (no pointer, corrupt pointer, invalid snapshot) -> fall back
       to the checkout so Coach still starts; never block launch on snapshot
       trouble.
    """
    already_selected = os.environ.get("MTGACOACH_SNAPSHOT_SHA", "").strip()
    if already_selected:
        root_env = os.environ.get("MTGACOACH_SNAPSHOT_ROOT", "").strip()
        if root_env:
            return LaunchSelection(Path(root_env), already_selected, None)
        return LaunchSelection(None, already_selected, None)

    if os.environ.get("MTGACOACH_SNAPSHOTS_DISABLE", "").strip():
        return LaunchSelection(None, None, None)

    try:
        pointer = read_pointer_once(snapshots_dir)
    except Exception:
        return LaunchSelection(None, None, None)
    if pointer is None:
        return LaunchSelection(None, None, None)

    snapshot_root = Path(pointer["path"])
    # Belt and braces: the pointed-at tree must still be a complete Coach root
    # before we commit the launch to it.
    if not (snapshot_root / "pyproject.toml").is_file() or not (snapshot_root / "src" / "arenamcp").is_dir():
        return LaunchSelection(None, None, None)
    return LaunchSelection(snapshot_root, str(pointer.get("commit") or ""), pointer.get("content_hash"))
