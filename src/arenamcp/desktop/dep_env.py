"""Versioned dependency environments for runtime snapshots.

A snapshot is bound to a dependency environment by a full *identity*: the
snapshot's ``dependency_fingerprint`` (which covers requirements.txt,
requirements-dev.txt, pyproject.toml AND uv.lock — see
``runtime_snapshots.DEPENDENCY_SPEC_FILES``) combined with the resolved
interpreter that will run the engine. Environments live under
``<runtime_root>/dep-envs/<identity[:16]>`` and are created with
``python -m venv`` plus an install driven by the snapshot's own lockfile
(``uv export --frozen`` from ``uv.lock``) falling back to
``requirements.txt`` only when no usable lock exists.

An active environment is never mutated and never deleted: creation happens
in a private staging venv that is atomically renamed into place only after a
successful import probe of the snapshot's entry package. A pre-existing tree
under the target identity is moved aside to a retained ``.superseded-*``
name (kept for rollback; operators clean these up deliberately, never the
code path in this module).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .runtime_snapshots import SnapshotError, dependency_fingerprint

logger = logging.getLogger(__name__)

ENV_MARKER_NAME = "dep-env.json"

#: How many times promotion retries when racing another creator of the same
#: identity. Between attempts the existing tree is re-validated: if another
#: creator already promoted a valid environment we adopt theirs.
_PROMOTION_ATTEMPTS = 3


def dep_env_root(runtime_root: Path | None = None) -> Path:
    if runtime_root is not None:
        return Path(runtime_root) / "dep-envs"
    try:
        from .runtime import get_runtime_root

        return get_runtime_root() / "dep-envs"
    except Exception:  # pragma: no cover - bootstrap contexts without the package
        return Path.home() / ".local" / "share" / "mtgacoach" / "dep-envs"


def resolve_interpreter(python_executable: str | None = None) -> str:
    """Absolute, symlink-resolved interpreter path binding an environment."""
    raw = python_executable or sys.executable
    return str(Path(raw).resolve())


def env_identity(
    snapshot_root: Path,
    python_executable: str | None = None,
    *,
    fingerprint: str | None = None,
) -> str:
    """Full dependency/interpreter identity of an environment.

    Includes the interpreter so an interpreter change can never silently
    reuse (or destroy) an environment built for a different one.
    """
    if fingerprint is None:
        fingerprint = dependency_fingerprint(Path(snapshot_root))
    digest = hashlib.sha256()
    digest.update(fingerprint.encode("ascii"))
    digest.update(b"\0")
    digest.update(resolve_interpreter(python_executable).encode("utf-8"))
    return digest.hexdigest()


def env_dir_for(identity: str, runtime_root: Path | None = None) -> Path:
    return dep_env_root(runtime_root) / identity[:16]


def _marker_path(env_dir: Path) -> Path:
    return env_dir / ENV_MARKER_NAME


def read_env_marker(env_dir: Path) -> dict | None:
    try:
        data = json.loads(_marker_path(env_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def venv_python(env_dir: Path) -> Path:
    """Interpreter inside a venv, on any platform."""
    for candidate in (
        env_dir / "bin" / "python",
        env_dir / "Scripts" / "python.exe",
        env_dir / "Scripts" / "python",
    ):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"virtual environment {env_dir} has no python interpreter")


def _probe_import(python_bin: Path, snapshot_root: Path) -> None:
    """Import the snapshot's app package using only the venv + snapshot paths.

    The probe exercises the real import surface the child will use (the
    desktop app module pulls in the heavy dependencies), with ``sys.path``
    limited to the snapshot's ``src`` — proving the environment satisfies the
    snapshot's declared dependencies.
    """
    probe = "import sys; sys.path.insert(0, sys.argv[1]); import arenamcp.standalone; sys.stdout.write('ok')"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [
            str(python_bin),
            "-c",
            probe,
            str(snapshot_root / "src"),
        ],
        capture_output=True,
        timeout=120,
        cwd=str(tempfile.gettempdir()),
        env=env,
    )
    if proc.returncode != 0 or b"ok" not in proc.stdout:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise SnapshotError(f"dependency environment import probe failed: {detail}")


def _locked_requirements(snapshot_root: Path, work_dir: Path) -> Path | None:
    """Export pinned requirements from the snapshot's ``uv.lock``.

    Returns the path to a temporary fully-pinned requirements file, or
    ``None`` when no usable lock/tool exists (the caller then falls back to
    the loose ``requirements.txt``, which is recorded in the marker).
    """
    if not (snapshot_root / "uv.lock").is_file():
        return None
    uv_exe = shutil.which("uv")
    if not uv_exe:
        logger.info("uv.lock present but uv is not installed; falling back to requirements.txt")
        return None
    export_path = work_dir / f".locked-reqs-{os.getpid()}-{time.time_ns()}.txt"
    try:
        subprocess.run(
            [
                uv_exe,
                "export",
                "--frozen",
                "--no-dev",
                "--no-hashes",
                "--no-emit-project",
                "-o",
                str(export_path),
            ],
            cwd=str(snapshot_root),
            check=True,
            capture_output=True,
            timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("uv.lock export failed (%s); falling back to requirements.txt", exc)
        return None
    if not export_path.is_file():
        return None
    return export_path


def _install_dependencies(staging_python: Path, snapshot_root: Path, work_dir: Path) -> str:
    """Install dependencies into the staging venv. Returns the install mode."""
    locked = _locked_requirements(snapshot_root, work_dir)
    if locked is not None:
        try:
            subprocess.run(
                [
                    str(staging_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "-r",
                    str(locked),
                ],
                check=True,
                capture_output=True,
                timeout=1800,
            )
            return "uv.lock"
        finally:
            try:
                locked.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    requirements = snapshot_root / "requirements.txt"
    if requirements.is_file():
        subprocess.run(
            [str(staging_python), "-m", "pip", "install", "--no-input", "-r", str(requirements)],
            check=True,
            capture_output=True,
            timeout=1800,
        )
        return "requirements.txt"
    return "none"


def _valid_existing_env(env_dir: Path, identity: str, fingerprint: str, python_resolved: str) -> bool:
    marker = read_env_marker(env_dir)
    if marker is None:
        return False
    if marker.get("identity") != identity or marker.get("fingerprint") != fingerprint:
        return False
    if marker.get("python") != python_resolved:
        return False
    try:
        return venv_python(env_dir).exists()
    except FileNotFoundError:
        return False


def _promote(staging: Path, env_dir: Path, identity: str) -> bool:
    """Atomically promote ``staging`` to ``env_dir``.

    Returns ``True`` when this call performed the promotion and ``False``
    when another creator of the same identity won the race (their promoted
    environment is adopted instead). A pre-existing tree is moved aside to a
    retained ``.superseded-*`` name — never deleted — so an active or old
    environment always survives for rollback.
    """
    last_error: OSError | None = None
    for attempt in range(_PROMOTION_ATTEMPTS):
        # Another creator may have promoted a valid environment between our
        # existence check and the rename; adopt theirs instead of clobbering.
        marker = read_env_marker(env_dir)
        if marker is not None and marker.get("identity") == identity and venv_python(env_dir).exists():
            return False
        try:
            if env_dir.exists():
                superseded = env_dir.with_name(f".superseded-{env_dir.name}-{time.time_ns()}")
                os.replace(env_dir, superseded)
                logger.info(
                    "Retained superseded dependency environment at %s (never auto-deleted)",
                    superseded,
                )
            os.replace(staging, env_dir)
            return True
        except OSError as error:
            last_error = error
            time.sleep(0.05 * (attempt + 1))
    raise (
        last_error
        if last_error is not None
        else SnapshotError(f"could not promote dependency environment {env_dir}")
    )


def ensure_dep_env(
    snapshot_root: Path,
    *,
    runtime_root: Path | None = None,
    python_executable: str | None = None,
    allow_create: bool = True,
) -> Path:
    """Return a validated dependency environment for ``snapshot_root``.

    Reuses an existing environment only when its marker records the same full
    identity (dependency fingerprint INCLUDING uv.lock plus the resolved
    interpreter). Creation never mutates or deletes an existing environment:
    it builds into a private staging venv and atomically renames it into
    place after the import probe succeeds; any tree already under the target
    identity is retained under a ``.superseded-*`` sibling.
    """
    snapshot_root = Path(snapshot_root)
    fingerprint = dependency_fingerprint(snapshot_root)
    python_resolved = resolve_interpreter(python_executable)
    identity = env_identity(snapshot_root, python_executable=python_resolved, fingerprint=fingerprint)
    env_dir = env_dir_for(identity, runtime_root)

    if _valid_existing_env(env_dir, identity, fingerprint, python_resolved):
        return env_dir

    if not allow_create:
        raise SnapshotError(f"No prepared dependency environment for identity {identity[:16]}")

    dep_env_root(runtime_root).mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".dep-staging-{identity[:12]}-", dir=dep_env_root(runtime_root)))
    renamed_away = False
    try:
        subprocess.run(
            [python_resolved, "-m", "venv", "--without-pip", str(staging)],
            check=True,
            capture_output=True,
            timeout=300,
        )
        # Bootstrap pip inside the staging venv only.
        subprocess.run(
            [str(venv_python(staging)), "-m", "ensurepip", "--upgrade"],
            check=True,
            capture_output=True,
            timeout=300,
        )
        install_mode = _install_dependencies(venv_python(staging), snapshot_root, staging)
        _probe_import(venv_python(staging), snapshot_root)

        marker_data = {
            "schema": 2,
            "identity": identity,
            "fingerprint": fingerprint,
            "python": python_resolved,
            "install_mode": install_mode,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "snapshot": str(snapshot_root),
        }
        (_marker_path(staging)).write_text(
            json.dumps(marker_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        promoted = _promote(staging, env_dir, identity)
        if promoted:
            renamed_away = True
        return env_dir
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()[-500:]
        raise SnapshotError(f"dependency environment creation failed: {detail}") from exc
    finally:
        if not renamed_away and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def child_env_for(
    snapshot_root: Path,
    base_env: dict | None = None,
    *,
    runtime_root: Path | None = None,
) -> dict:
    """Environment for a child process running from ``snapshot_root``.

    Keeps user data/configuration (HOME and friends) exactly as inherited and
    scrubs every variable that could smuggle in modules from another tree.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env_dir = ensure_dep_env(snapshot_root, runtime_root=runtime_root)
    venv_bin = str(venv_python(env_dir).parent)
    existing_path = env.get("PATH", os.defpath)
    parts = [p for p in existing_path.split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join([venv_bin, *parts])
    env["VIRTUAL_ENV"] = str(env_dir)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONSTARTUP", None)
    env.pop("PYTHONHOME", None)
    return env
