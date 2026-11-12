"""Versioned dependency environments for runtime snapshots.

A snapshot is bound to a dependency environment by the snapshot's
``dependency_fingerprint`` (see ``runtime_snapshots.DEPENDENCY_SPEC_FILES``).
Environments live under ``<runtime_root>/dep-envs/<fingerprint[:16]>`` and are
created with ``python -m venv`` + ``pip install`` from the snapshot's own spec
files. An active environment is never mutated: creation happens in a private
staging venv that is atomically renamed into place only after a successful
import probe of the snapshot's entry package.
"""

from __future__ import annotations

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


def dep_env_root(runtime_root: Path | None = None) -> Path:
    if runtime_root is not None:
        return Path(runtime_root) / "dep-envs"
    try:
        from .runtime import get_runtime_root

        return get_runtime_root() / "dep-envs"
    except Exception:  # pragma: no cover - bootstrap contexts without the package
        return Path.home() / ".local" / "share" / "mtgacoach" / "dep-envs"


def env_dir_for(fingerprint: str, runtime_root: Path | None = None) -> Path:
    return dep_env_root(runtime_root) / fingerprint[:16]


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
    probe = "import sys; sys.path.insert(0, sys.argv[1]); import arenamcp.desktop.app; sys.stdout.write('ok')"
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


def ensure_dep_env(
    snapshot_root: Path,
    *,
    runtime_root: Path | None = None,
    python_executable: str | None = None,
    allow_create: bool = True,
) -> Path:
    """Return a validated dependency environment for ``snapshot_root``.

    Reuses an existing environment only when its marker records the same
    fingerprint and python executable. Creation never mutates an existing
    environment: it builds into a private staging venv and atomically renames
    it into place after the import probe succeeds.
    """
    snapshot_root = Path(snapshot_root)
    fingerprint = dependency_fingerprint(snapshot_root)
    env_dir = env_dir_for(fingerprint, runtime_root)

    marker = read_env_marker(env_dir)
    expected_python = python_executable or sys.executable
    if (
        marker is not None
        and marker.get("fingerprint") == fingerprint
        and marker.get("python") == expected_python
        and venv_python(env_dir).exists()
    ):
        return env_dir

    if not allow_create:
        raise SnapshotError(f"No prepared dependency environment for fingerprint {fingerprint[:16]}")

    dep_env_root(runtime_root).mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".dep-staging-{fingerprint[:12]}-", dir=dep_env_root(runtime_root))
    )
    try:
        subprocess.run(
            [expected_python, "-m", "venv", "--without-pip", str(staging)],
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
        requirements = snapshot_root / "requirements.txt"
        if requirements.is_file():
            subprocess.run(
                [str(venv_python(staging)), "-m", "pip", "install", "--no-input", "-r", str(requirements)],
                check=True,
                capture_output=True,
                timeout=1800,
            )
        _probe_import(venv_python(staging), snapshot_root)

        marker_data = {
            "schema": 1,
            "fingerprint": fingerprint,
            "python": expected_python,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "snapshot": str(snapshot_root),
        }
        (_marker_path(staging)).write_text(
            json.dumps(marker_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        # Concurrent creators may promote between our existence check and the
        # rename (os.replace onto a non-empty directory fails); retirement and
        # promotion are retried so the last complete writer wins cleanly.
        last_error: OSError | None = None
        for attempt in range(3):
            try:
                if env_dir.exists():
                    retired = env_dir.with_name(f"{env_dir.name}.retired-{os.getpid()}-{time.time_ns()}")
                    os.replace(env_dir, retired)
                    shutil.rmtree(retired, ignore_errors=True)
                os.replace(staging, env_dir)
                last_error = None
                break
            except OSError as error:
                last_error = error
        if last_error is not None:
            raise last_error
        return env_dir
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()[-500:]
        raise SnapshotError(f"dependency environment creation failed: {detail}") from exc
    finally:
        if staging.exists():
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
