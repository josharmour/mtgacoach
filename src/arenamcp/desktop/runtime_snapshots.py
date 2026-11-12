"""Immutable runtime snapshots for the Coach desktop app.

Publication pipeline (offline, operator driven):

1. ``stage_snapshot`` exports a pinned commit with ``git archive`` into a
   private staging directory. Nothing is read from the working tree, so dirty
   tracked files, untracked files and ``.git`` internals can never leak into a
   snapshot that claims an older SHA.
2. A content-hash manifest is written and validated inside staging.
3. ``promote_snapshot`` renames the validated staging directory to its final
   per-commit identity with a single atomic ``os.replace`` and marks it
   read-only. Identical content is reused only after full re-validation.
4. ``publish_pointer`` atomically publishes the next-launch pointer, but only
   for a fully validated snapshot directory.

Readers (``read_pointer_once``, ``resolve_launch_selection``) validate the
complete snapshot content on every read: a tampered or truncated snapshot is
never launched, and a pointer whose commit/hash disagree with the manifest it
points at is rejected.

Secrets (credential files, keys, ``.env``) are excluded at export time and the
exclusions are declared in the manifest; user data, logs and caches stay
external to snapshots by construction.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA = 2
MANIFEST_NAME = "snapshot-manifest.json"
POINTER_SCHEMA = 2
POINTER_NAME = "next-launch.json"
STAGING_PREFIX = ".staging-"

#: Files that must exist for a snapshot to be launchable.
ENTRY_POINT_FILES = ("pyproject.toml", "src/arenamcp/__init__.py")

#: Dependency specification files bound into the snapshot fingerprint.
#: ``uv.lock`` is included so a locked resolution change forces a new
#: dependency environment even when ``requirements.txt`` is untouched.
DEPENDENCY_SPEC_FILES = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "uv.lock")

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "node_modules"}
)
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")
SECRET_BASENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "credentials.json",
        "aws_credentials",
        "secrets.json",
        "secrets_prod.json",
        "service-account.json",
        "id_rsa",
        "id_ed25519",
    }
)
SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12")

#: Protocol versions this codebase declares and has tested end to end. A
#: pointer claiming anything else is unknown territory: the UI must visibly
#: require a full Restart Coach instead of adopting it silently.
UI_PROTOCOL_VERSION: tuple[int, int] = (1, 0)
ENGINE_PROTOCOL_VERSION: tuple[int, int] = (1, 0)
DECLARED_UI_PROTOCOL_VERSIONS = frozenset({(1, 0), (1, 2)})
DECLARED_ENGINE_PROTOCOL_VERSIONS = frozenset({(1, 0), (1, 2), (1, 3)})


class SnapshotError(RuntimeError):
    """Snapshot publication or resolution failed."""


class SnapshotValidationError(SnapshotError):
    """A snapshot's content does not match its manifest."""


@dataclass(frozen=True)
class LaunchSelection:
    """The single selection a launcher resolves once per launch."""

    snapshot_root: Path | None
    commit: str | None
    content_hash: str | None


@dataclass(frozen=True)
class EngineReloadPlan:
    """What a Reload Engine press/request should do right now."""

    action: str  # reload_unmanaged | reload_current | adopt | require_full_restart
    reason: str
    target_root: str | None = None
    target_commit: str | None = None
    ui_protocol: object = None
    engine_protocol: object = None


# ---------------------------------------------------------------------------
# hashing / classification helpers


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_secret_path(rel: PurePosixPath) -> bool:
    name = rel.name.lower()
    if name in SECRET_BASENAMES:
        return True
    if name.endswith(SECRET_SUFFIXES):
        return True
    # Any dotenv-style file (.env, .env.test, .env.production, ...) may carry
    # credentials; never copy one into a snapshot or its manifest.
    if name.startswith(".env"):
        return True
    return name.startswith(("credentials", "aws_credentials", "secrets"))


def _is_excluded_rel(rel: PurePosixPath) -> bool:
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in rel.parts):
        return True
    if rel.suffix.lower() in EXCLUDED_FILE_SUFFIXES:
        return True
    return _is_secret_path(rel)


def _iter_snapshot_files(root: Path) -> list[str]:
    """Relative POSIX paths of every hashed file/symlink under ``root``.

    Symlinks are hashed as their link string, never followed; directory
    symlinks are skipped entirely so validation cannot escape the snapshot.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        for name in filenames:
            path = here / name
            if path.is_symlink() or path.is_file():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def _entry_hash(path: Path) -> str:
    if path.is_symlink():
        return hashlib.sha256(b"symlink:" + os.readlink(path).encode("utf-8")).hexdigest()
    return _sha256_file(path)


# ---------------------------------------------------------------------------
# git-backed export


def _git(checkout: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout


def _resolve_commit(checkout: Path, commit: str) -> str:
    out = _git(checkout, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    resolved = out.decode("utf-8", errors="replace").strip()
    if not resolved:
        raise SnapshotError(f"commit {commit!r} does not exist in {checkout}")
    return resolved


def _export_commit_tree(checkout: Path, commit: str, destination: Path) -> list[str]:
    """Materialise exactly the pinned commit's tree via ``git archive``.

    Returns the sorted list of excluded archive members (build caches and
    secret-classified files), which the manifest declares openly.
    """
    blob = _git(checkout, "archive", "--format=tar", commit)
    excluded: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        for member in tar.getmembers():
            rel = PurePosixPath(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise SnapshotValidationError(f"unsafe archive member {member.name!r}")
            if _is_excluded_rel(rel):
                excluded.append(member.name)
                continue
            target = destination.joinpath(*rel.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(member)
                if stream is None:  # pragma: no cover - defensive
                    raise SnapshotValidationError(f"unreadable archive member {member.name!r}")
                target.write_bytes(stream.read())
            elif member.issym():
                link_target = member.linkname
                candidate = Path(link_target) if os.path.isabs(link_target) else (target.parent / link_target)
                try:
                    contained = candidate.resolve().is_relative_to(destination.resolve())
                except OSError:  # pragma: no cover - defensive
                    contained = False
                if contained:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(link_target)
                else:
                    # Part of the commit, but never followed during validation;
                    # declared in the manifest instead of copied.
                    excluded.append(f"{member.name} -> {link_target}")
            else:
                excluded.append(member.name)
    return sorted(excluded)


# ---------------------------------------------------------------------------
# manifests and validation


def load_manifest(snapshot_dir: Path) -> dict:
    path = Path(snapshot_dir) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SnapshotValidationError(f"missing manifest in {snapshot_dir}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(f"unreadable manifest in {snapshot_dir}: {exc}") from exc
    if not isinstance(data, dict):
        raise SnapshotValidationError(f"malformed manifest in {snapshot_dir}")
    return data


def validate_staged(staging_dir: Path) -> dict:
    """Validate a staged/published snapshot against its own manifest.

    Recomputes every file hash, rejects unexpected extra files, requires the
    entry points and re-derives the dependency fingerprint. Returns the
    manifest; raises :class:`SnapshotValidationError` on any mismatch.
    """
    root = Path(staging_dir)
    manifest = load_manifest(root)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise SnapshotValidationError(f"unsupported manifest schema in {root}")
    commit = manifest.get("commit")
    if not isinstance(commit, str) or len(commit) < 7:
        raise SnapshotValidationError(f"manifest in {root} has no usable commit")
    if manifest.get("source") != "git-archive":
        raise SnapshotValidationError(f"manifest in {root} was not produced by a git export")
    declared_files = manifest.get("files")
    if not isinstance(declared_files, dict) or not declared_files:
        raise SnapshotValidationError(f"manifest in {root} declares no files")

    present = {rel for rel in _iter_snapshot_files(root) if rel != MANIFEST_NAME}
    missing = sorted(set(declared_files) - present)
    extra = sorted(present - set(declared_files))
    if missing or extra:
        raise SnapshotValidationError(
            f"snapshot {root.name} content drift: missing={missing[:5]} extra={extra[:5]}"
        )
    for rel in sorted(declared_files):
        actual = _entry_hash(root / rel)
        if actual != declared_files[rel]:
            raise SnapshotValidationError(f"snapshot {root.name}: {rel} does not match its hash")

    for rel in ENTRY_POINT_FILES:
        if not (root / rel).exists():
            raise SnapshotValidationError(f"snapshot {root.name} is missing entry point {rel}")

    fingerprint = dependency_fingerprint(root)
    if manifest.get("dependency_fingerprint") != fingerprint:
        raise SnapshotValidationError(f"snapshot {root.name} dependency fingerprint mismatch")
    if manifest.get("content_hash") != _content_digest(declared_files):
        raise SnapshotValidationError(f"snapshot {root.name} content hash mismatch")
    return manifest


def validate_snapshot_dir(snapshot_dir: Path) -> dict:
    """Public alias: fully validate a published snapshot directory."""
    return validate_staged(snapshot_dir)


def dependency_fingerprint(spec_root: Path) -> str:
    """Stable fingerprint binding a snapshot to its dependency environment."""
    digest = hashlib.sha256()
    for rel in DEPENDENCY_SPEC_FILES:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        path = Path(spec_root) / rel
        digest.update(_sha256_file(path).encode("ascii") if path.is_file() else b"-")
        digest.update(b"\0")
    return digest.hexdigest()


def _content_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[rel].encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# publication


def snapshot_id_for(commit: str, content_hash: str) -> str:
    return f"{commit[:12]}-{content_hash[:8]}"


def stage_snapshot(
    checkout: Path,
    commit: str,
    *,
    staging_parent: Path | None = None,
) -> Path:
    """Export ``commit`` from ``checkout`` into a validated staging directory."""
    checkout = Path(checkout)
    if not isinstance(commit, str) or not commit.strip():
        raise SnapshotError("a commit is required to stage a snapshot")
    resolved_commit = _resolve_commit(checkout, commit.strip())

    parent = Path(staging_parent) if staging_parent is not None else snapshots_root()
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}{resolved_commit[:12]}-", dir=parent))
    try:
        excluded = _export_commit_tree(checkout, resolved_commit, staging)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "commit": resolved_commit,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "git-archive",
            "excluded": excluded,
            "dependency_fingerprint": dependency_fingerprint(staging),
            "ui_protocol": list(UI_PROTOCOL_VERSION),
            "engine_protocol": list(ENGINE_PROTOCOL_VERSION),
            "files": {},
        }
        for rel in _iter_snapshot_files(staging):
            manifest["files"][rel] = _entry_hash(staging / rel)
        manifest["content_hash"] = _content_digest(manifest["files"])
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        validate_staged(staging)
        return staging
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _mark_read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for name in dirnames + filenames:
            path = here / name
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def promote_snapshot(staging_dir: Path, snapshots_dir: Path | None = None) -> Path:
    """Atomically publish a validated staging directory under its final identity.

    An existing directory with identical content is reused only after full
    independent re-validation; different content under the same identity is a
    hard error (never silently overwritten).
    """
    snapshots_dir = Path(snapshots_dir) if snapshots_dir is not None else snapshots_root()
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(staging_dir)
    manifest = validate_staged(staging_dir)
    final = snapshots_dir / snapshot_id_for(manifest["commit"], manifest["content_hash"])

    if final.exists():
        try:
            existing = validate_snapshot_dir(final)
        except SnapshotValidationError:
            existing = None
        if existing is not None and existing["content_hash"] == manifest["content_hash"]:
            shutil.rmtree(staging_dir)
            return final
        if existing is not None:
            raise SnapshotError(f"snapshot identity {final.name} already exists with different content")
        # The published directory failed validation (tampered/truncated).
        # Quarantine it for forensics instead of deleting evidence, then take
        # its place atomically. Readers during the swap fall back safely.
        quarantine = snapshots_dir / f".corrupt-{final.name}-{os.getpid()}"
        try:
            os.replace(final, quarantine)
        except OSError as exc:
            raise SnapshotError(f"cannot replace corrupt snapshot {final.name}: {exc}") from exc

    os.replace(staging_dir, final)  # atomic rename on the same filesystem
    _mark_read_only(final)
    return final


def publish_pointer(snapshot_dir: Path, snapshots_dir: Path | None = None) -> dict:
    """Atomically publish the next-launch pointer for a validated snapshot."""
    snapshot_dir = Path(snapshot_dir)
    snapshots_dir = Path(snapshots_dir) if snapshots_dir is not None else snapshots_root()
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    manifest = validate_snapshot_dir(snapshot_dir)
    pointer = {
        "schema": POINTER_SCHEMA,
        "snapshot_id": snapshot_dir.name,
        "commit": manifest["commit"],
        "content_hash": manifest["content_hash"],
        "ui_protocol": manifest["ui_protocol"],
        "engine_protocol": manifest["engine_protocol"],
        "published_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "path": str(snapshot_dir.resolve()),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".pointer-", dir=snapshots_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(pointer, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, snapshots_dir / POINTER_NAME)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return pointer


# ---------------------------------------------------------------------------
# resolution


def snapshots_root(runtime_root: Path | None = None) -> Path:
    if runtime_root is not None:
        return Path(runtime_root) / "runtime-snapshots"
    try:
        from .runtime import get_runtime_root

        return Path(get_runtime_root()) / "runtime-snapshots"
    except Exception:  # pragma: no cover - bootstrap contexts without the package
        return Path.home() / ".local" / "share" / "mtgacoach" / "runtime-snapshots"


def read_pointer_once(snapshots_dir: Path | None = None) -> dict | None:
    """Read and fully validate the next-launch pointer, or return ``None``.

    Every read re-validates the pointed-at snapshot's complete content and
    cross-checks commit/content-hash agreement; anything inconsistent degrades
    to ``None`` so callers fall back to the checkout.
    """
    if os.environ.get("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT") == "1":
        return None
    directory = Path(snapshots_dir) if snapshots_dir is not None else snapshots_root()
    pointer_path = directory / POINTER_NAME
    try:
        data = json.loads(pointer_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable snapshot pointer %s: %s", pointer_path, exc)
        return None

    def _reject(reason: str) -> None:
        logger.warning("Rejecting snapshot pointer %s: %s", pointer_path, reason)

    if not isinstance(data, dict):
        _reject("pointer is not an object")
        return None
    if data.get("schema") != POINTER_SCHEMA:
        _reject("foreign pointer schema")
        return None

    snapshot_id = data.get("snapshot_id")
    commit = data.get("commit")
    content_hash = data.get("content_hash")
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id
        or "/" in snapshot_id
        or "\\" in snapshot_id
        or snapshot_id in {".", ".."}
    ):
        _reject("unsafe or missing snapshot_id")
        return None
    if not isinstance(commit, str) or len(commit) < 7:
        _reject("unusable commit")
        return None

    candidate = directory / snapshot_id
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        _reject(f"unresolvable snapshot path: {exc}")
        return None
    try:
        contained = resolved.parent == directory.resolve()
    except OSError:  # pragma: no cover - defensive
        contained = False
    if not contained:
        _reject("snapshot escapes the snapshots directory")
        return None

    try:
        manifest = validate_snapshot_dir(resolved)
    except SnapshotValidationError as exc:
        _reject(str(exc))
        return None

    if manifest["commit"] != commit:
        _reject("pointer commit disagrees with the snapshot manifest")
        return None
    declared_hash = manifest.get("content_hash")
    if not isinstance(content_hash, str) or content_hash != declared_hash:
        _reject("pointer content hash disagrees with the snapshot manifest")
        return None

    enriched = dict(data)
    enriched["path"] = str(resolved)
    enriched["manifest"] = manifest
    return enriched


def protocols_compatible(ui_protocol: object, engine_protocol: object) -> bool:
    """Strict versioned UI/engine compatibility over *declared* versions.

    Both sides must be declared/tested ``(major, minor)`` pairs sharing a
    major, with an engine minor greater than or equal to the UI's. Unknown
    shapes and undeclared versions are incompatible by definition.
    """

    def parse(value: object) -> tuple[int, int] | None:
        if (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
            and value[0] >= 0
            and value[1] >= 0
        ):
            return (value[0], value[1])
        return None

    ui_pair = parse(ui_protocol)
    engine_pair = parse(engine_protocol)
    if ui_pair is None or engine_pair is None:
        return False
    if ui_pair not in DECLARED_UI_PROTOCOL_VERSIONS:
        return False
    if engine_pair not in DECLARED_ENGINE_PROTOCOL_VERSIONS:
        return False
    return ui_pair[0] == engine_pair[0] and engine_pair[1] >= ui_pair[1]


def plan_engine_reload(
    current_commit: str | None,
    pointer: dict | None,
    *,
    running_ui_protocol: object = None,
) -> EngineReloadPlan:
    """Decide what a Reload Engine action must do given the published pointer.

    ``running_ui_protocol`` is the protocol version the *currently running*
    UI process declares (this code's own ``UI_PROTOCOL_VERSION``). It — not
    the candidate pointer's self-declared UI version — is what compatibility
    is judged against: an old UI may adopt a newer engine only through the
    declared tested protocol pair; anything else visibly requires a full
    Restart Coach.
    """
    if not isinstance(pointer, dict) or not pointer.get("commit"):
        return EngineReloadPlan(
            "reload_unmanaged",
            "No published engine snapshot; restarting from the current source.",
        )

    target_root_value = pointer.get("path")
    target_root_str = str(target_root_value) if target_root_value else None

    ui_protocol_value = pointer.get("ui_protocol")
    engine_protocol_value = pointer.get("engine_protocol")
    effective_ui = running_ui_protocol if running_ui_protocol is not None else ui_protocol_value
    compatible = protocols_compatible(effective_ui, engine_protocol_value)

    if current_commit and current_commit == pointer["commit"]:
        if not compatible:
            return EngineReloadPlan(
                "require_full_restart",
                (
                    "The published engine snapshot "
                    f"{pointer['commit'][:7]} declares engine protocol "
                    f"{engine_protocol_value!r}, which is not compatible with "
                    f"the running UI protocol {effective_ui!r}. A full Restart "
                    "Coach is required."
                ),
                target_root=target_root_str,
                target_commit=pointer["commit"],
                ui_protocol=ui_protocol_value,
                engine_protocol=engine_protocol_value,
            )
        return EngineReloadPlan(
            "reload_current",
            f"The published engine snapshot {pointer['commit'][:7]} is already current.",
            target_root=target_root_str,
            target_commit=pointer["commit"],
            ui_protocol=ui_protocol_value,
            engine_protocol=engine_protocol_value,
        )

    if not compatible:
        return EngineReloadPlan(
            "require_full_restart",
            (
                "Published engine snapshot "
                f"{pointer['commit'][:7]} declares engine protocol "
                f"{engine_protocol_value!r}, which is not compatible with the "
                f"running UI protocol {effective_ui!r}. A full Restart Coach "
                "is required."
            ),
            target_root=target_root_str,
            target_commit=pointer["commit"],
            ui_protocol=ui_protocol_value,
            engine_protocol=engine_protocol_value,
        )

    return EngineReloadPlan(
        "adopt",
        f"Adopting published engine snapshot {pointer['commit'][:7]} via the declared UI/engine protocol.",
        target_root=target_root_str,
        target_commit=pointer["commit"],
        ui_protocol=ui_protocol_value,
        engine_protocol=engine_protocol_value,
    )


def snapshots_disabled() -> bool:
    """Whether runtime snapshots are explicitly disabled for this process."""
    return (
        os.environ.get("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT") == "1"
        or os.environ.get("MTGACOACH_SNAPSHOTS_DISABLE", "").strip() == "1"
    )


def resolve_launch_selection(
    checkout_root: Path | None = None,
    *,
    snapshots_dir: Path | None = None,
) -> LaunchSelection:
    """Resolve the single launch selection, honouring an outer process's choice.

    Order of precedence:

    1. ``MTGACOACH_DISABLE_RUNTIME_SNAPSHOT=1`` or
       ``MTGACOACH_SNAPSHOTS_DISABLE=1`` disables snapshots entirely.
    2. ``MTGACOACH_SNAPSHOT_SHA`` (with ``MTGACOACH_SNAPSHOT_ROOT``) selects the
       exact snapshot an outer launcher process already resolved and validated;
       the root is re-validated here and must agree with the SHA.
    3. The published next-launch pointer, fully validated.
    4. ``None`` — run from the mutable checkout.
    """
    if snapshots_disabled():
        return LaunchSelection(None, None, None)

    sha_env = os.environ.get("MTGACOACH_SNAPSHOT_SHA")
    root_env = os.environ.get("MTGACOACH_SNAPSHOT_ROOT")
    if sha_env:
        if root_env:
            try:
                manifest = validate_snapshot_dir(Path(root_env))
            except SnapshotValidationError as exc:
                logger.warning(
                    "Outer-selected snapshot %s failed validation (%s); "
                    "falling back to the published pointer.",
                    root_env,
                    exc,
                )
            else:
                if manifest["commit"] == sha_env:
                    return LaunchSelection(Path(root_env), sha_env, manifest.get("content_hash"))
                logger.warning(
                    "Outer-selected snapshot root %s does not match SHA %s; "
                    "falling back to the published pointer.",
                    root_env,
                    sha_env,
                )
        else:
            # An outer process pinned the SHA but owns the root itself.
            return LaunchSelection(None, sha_env, None)

    pointer = read_pointer_once(snapshots_dir)
    if pointer is not None:
        return LaunchSelection(
            Path(pointer["path"]),
            pointer["commit"],
            pointer.get("content_hash"),
        )
    return LaunchSelection(None, None, None)
