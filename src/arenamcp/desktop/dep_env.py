"""Versioned dependency environments for runtime snapshots.

A snapshot is bound to a dependency environment by a full *identity*: the
snapshot's ``dependency_fingerprint`` (which covers requirements.txt,
requirements-dev.txt, pyproject.toml AND uv.lock — see
``runtime_snapshots.DEPENDENCY_SPEC_FILES``) combined with the resolved
interpreter that will run the engine. Environments live under
``<runtime_root>/dep-envs/<identity[:16]>`` and are created with
``python -m venv`` plus an install driven by the snapshot's own lockfile
(``uv export --frozen`` from ``uv.lock``) falling back to
``requirements.txt`` only when the snapshot ships no lock at all; a lock
that cannot be exported fails visibly instead of degrading silently.

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

# --- Windows path-length safety -------------------------------------------------
#
# With LongPathsEnabled=0 (the observed qualification machine state), every
# path must fit the classic MAX_PATH budget of 260 characters. Native Windows
# qualification showed a locked PySide6/shiboken install failing deep inside
# its build tree (``qrc_qmake_Qt_labs_assetdownloader_init.cpp.obj``) under a
# normal-length runtime root while the *same* source and lock succeeded with a
# drive-root runtime root (``C:\\rq3fd``). The fix is deterministic short-root
# selection BEFORE any bytes are written — never a registry change and never a
# loose fallback.
#
# Budget accounting (all lengths are characters of the final layout):
#   <base>/dep-envs/<identity[:16]>          final environment directory
#   <base>/dep-envs/.dep-staging-<12>-<8>    private staging venv during build
#   .../Scripts/python.exe                   deepest path WE construct ourselves
# Everything beyond that belongs to package internals (site-packages nesting,
# compiler/build artifacts), which is what actually blew the budget in
# qualification. The observed success envelope bounds that depth at roughly
# 190 characters (success prefix at ``C:\\rq3fd`` was ~70), so the reserve
# below is deliberately conservative against it.
_WIN_MAX_PATH = 260
_WIN_OWN_LAYOUT_RESERVE = (
    len("/dep-envs")
    # "/" + identity[:16]
    + len("/" + "x" * 16)
    # ".dep-staging-" + 12 identity chars + "-" + 8 randomness chars
    + len("/.dep-staging-" + "x" * 12 + "-" + "x" * 8)
    + len("/Scripts/python.exe")
)
_WIN_PACKAGE_HEADROOM = 130

#: Fallback base under user-writable storage when the requested runtime root
#: cannot fit the budget. Kept short on purpose so it fits for realistic
#: profile paths ("%LOCALAPPDATA%\\mtgacoach\\r").
_WIN_FALLBACK_BASE_PARTS = ("mtgacoach", "r")


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _windows_safe_base(base: Path) -> Path:
    """Return ``base``, or a deterministic short user-storage base on Windows.

    Pure function of ``base`` (+ platform), so preparation and later prepared-
    only lookups always agree on where an environment lives. Raises a clear,
    actionable :class:`SnapshotError` when NO user-writable location can fit
    the budget — there is deliberately no loose fallback onto arbitrary paths.
    """
    limit = _WIN_MAX_PATH - _WIN_OWN_LAYOUT_RESERVE - _WIN_PACKAGE_HEADROOM

    def fits(candidate: Path) -> bool:
        return len(str(candidate)) + len("/dep-envs") <= limit

    if fits(base):
        return base
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    fallback = Path(local_appdata).joinpath(*_WIN_FALLBACK_BASE_PARTS) if local_appdata else None
    if fallback is not None and fallback != base and fits(fallback):
        logger.info(
            "Runtime root %s risks Windows MAX_PATH failures for deep package "
            "layouts; dependency environments will use %s instead",
            base,
            fallback,
        )
        return fallback
    raise SnapshotError(
        f"runtime root {base} is too long for Windows MAX_PATH-limited "
        f"dependency-environment layouts ({_WIN_MAX_PATH}-character budget "
        f"minus {_WIN_OWN_LAYOUT_RESERVE} characters of staging/interpreter "
        f"layout minus {_WIN_PACKAGE_HEADROOM} characters reserved for "
        f"package internals), and no shorter user-writable fallback fits "
        f"either. Set MTGACOACH_RUNTIME_ROOT to a short path near a drive "
        f"root (for example C:\\rq) and prepare again."
    )


def dep_env_root(runtime_root: Path | None = None) -> Path:
    base = Path(runtime_root) if runtime_root is not None else _default_runtime_base()
    if _is_windows():
        base = _windows_safe_base(base)
    return base / "dep-envs"


def _default_runtime_base() -> Path:
    try:
        from .runtime import get_runtime_root

        return Path(get_runtime_root())
    except Exception:  # pragma: no cover - bootstrap contexts without the package
        return Path.home() / ".local" / "share" / "mtgacoach"


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
    ``None`` when the snapshot ships no ``uv.lock`` (the caller then installs
    the loose ``requirements.txt``, recorded honestly in the marker).

    A snapshot that DOES ship a lock must never silently degrade to loose
    requirements: a missing ``uv`` binary or a failed export raises so the
    caller fails visibly instead of promoting an environment that merely
    looks like it satisfies the lock.
    """
    if not (snapshot_root / "uv.lock").is_file():
        return None
    uv_exe = shutil.which("uv")
    if not uv_exe:
        raise SnapshotError(
            "snapshot ships uv.lock but uv is not installed; refusing an "
            "unpinned requirements.txt fallback (install uv to build this "
            "dependency environment)"
        )
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
        raise SnapshotError(
            f"uv.lock export failed; refusing an unpinned requirements.txt fallback: {exc}"
        ) from exc
    if not export_path.is_file():
        raise SnapshotError("uv.lock export produced no requirements file; refusing an unpinned fallback")
    return export_path


def _repoint_entrypoint_shebangs(staging: Path, env_dir: Path) -> None:
    """Aim console-script shebangs at the *promoted* environment path.

    The venv is staged in a temporary directory and atomically renamed into
    place; console scripts baked during staging keep the staging path in
    their shebang, which stops existing after promotion. Rewrite each such
    script's interpreter reference to the final location so relocated
    entrypoints (``pip3`` and friends) stay runnable.

    Two entrypoint shapes exist. When the interpreter path has no spaces the
    script starts with a direct ``#!…/bin/python`` shebang. When it does
    (macOS ``Library/Application Support``), pip instead emits a ``/bin/sh``
    wrapper — ``'''exec' "<staging>/bin/python" "$0" "$@"`` — whose second
    line still names the doomed staging interpreter. Rewriting every
    occurrence of the staging interpreter path covers both shapes; anything
    that never pointed at staging (``activate.fish`` and friends) is left
    untouched.

    On Windows the same problem takes a third shape: pip's console/windowed
    launchers are ``.exe`` binaries laid out as ``<PE launcher> +
    b'#!' + interpreter + post_interp + b'\\n' + <ZIP payload>`` (distlib
    ``ScriptMaker._write_script``). The entry-point payload travels inside the
    appended ZIP and the interpreter reference is a plain byte shebang between
    the PE image and the ZIP. Once staging is deleted these launchers exit 1
    with empty output (observed for ``pip3.exe``, ``pip3.11.exe`` and
    ``normalizer.exe`` after promotion). They are repaired by splicing a new
    shebang line while keeping the PE prefix and ZIP payload byte-identical;
    launchers that do not point at staging are left untouched.
    """
    staging_python_path = venv_python(staging)
    staging_python = str(staging_python_path)
    # The promoted environment does not exist yet (staging is renamed into
    # place afterwards), so derive its interpreter path from the staging
    # layout instead of probing it.
    promoted_python = env_dir / staging_python_path.parent.name / staging_python_path.name
    bin_dir = venv_python(staging).parent
    if _is_windows():
        _repoint_windows_launcher_exes(bin_dir, staging_python, str(promoted_python))
    for script in sorted(bin_dir.iterdir()):
        if not script.is_file() or script.name.endswith(".exe"):
            continue
        try:
            text = script.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binaries and odd encodings are left untouched
        if not text.startswith("#!") or staging_python not in text:
            continue  # only scripts referencing the doomed staging path
        script.write_text(text.replace(staging_python, str(promoted_python)), encoding="utf-8")


def _repoint_windows_launcher_exes(bin_dir: Path, staging_python: str, promoted_python: str) -> None:
    """Retarget pip-generated ``.exe`` launchers at the promoted interpreter.

    See :func:`_repoint_entrypoint_shebangs` for the binary layout rationale.
    Only launchers whose embedded interpreter is exactly the staging python
    (compared case-insensitively, as Windows does) are rewritten; anything
    else — foreign interpreters, manifests, non-launcher binaries — keeps its
    original bytes.
    """
    old_norm = os.path.normcase(staging_python)
    for exe in sorted(bin_dir.glob("*.exe")):
        if not exe.is_file():
            continue
        try:
            data = exe.read_bytes()
        except OSError:
            continue
        rewritten = _retarget_launcher_shebang(data, old_norm, promoted_python)
        if rewritten is not None:
            exe.write_bytes(rewritten)


def _retarget_launcher_shebang(data: bytes, old_norm: str, promoted_python: str) -> bytes | None:
    """Splice ``promoted_python`` into a launcher's shebang line.

    The launcher layout is ``<PE prefix> + b'#!' + interpreter + post_interp +
    b'\\n' + <ZIP payload>``. The ZIP payload starts at the last local-file
    header signature (``PK\\x03\\x04``); the shebang is the single line ending
    immediately before it. Returns ``None`` (leave untouched) when the shape
    does not match or the embedded interpreter is not the staging python.
    """
    zip_idx = data.rfind(b"PK\x03\x04")
    if zip_idx == -1:
        return None
    shebang_idx = data.rfind(b"#!", 0, zip_idx)
    if shebang_idx == -1:
        return None
    line = data[shebang_idx:zip_idx]
    if not line.endswith(b"\n") or b"\n" in line[:-1]:
        return None  # not a single-line shebang sitting directly before the payload
    try:
        body = line[:-1].decode("utf-8")[2:]
    except UnicodeDecodeError:
        return None
    quoted = body.startswith('"')
    if quoted:
        end_quote = body.find('"', 1)
        if end_quote == -1:
            return None
        interp_token, post_interp = body[1:end_quote], body[end_quote + 1 :]
    else:
        space = body.find(" ")
        interp_token, post_interp = (body, "") if space == -1 else (body[:space], body[space:])
    if os.path.normcase(interp_token) != old_norm:
        return None
    # Re-emit with the launcher's own quoting discipline: a quoted interpreter
    # stays quoted, and a promoted path that contains a space must gain quotes
    # or the launcher would split it into interpreter + argument. Interpreter
    # options travel in ``post_interp`` and are preserved verbatim.
    new_interp = promoted_python
    if quoted or " " in promoted_python:
        new_interp = f'"{promoted_python}"'
    new_line = f"#!{new_interp}{post_interp}\n".encode()
    return data[:shebang_idx] + new_line + data[zip_idx:]


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
        _repoint_entrypoint_shebangs(staging, env_dir)

        marker_data = {
            "schema": 2,
            "identity": identity,
            "fingerprint": fingerprint,
            "python": python_resolved,
            "install_mode": install_mode,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "snapshot": str(snapshot_root),
            # Actual dep-envs root in effect (may differ from the requested
            # runtime root on Windows when path-length safety redirected it).
            "dep_envs_root": str(env_dir.parent),
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


def prepared_dep_env(
    snapshot_root: Path,
    *,
    runtime_root: Path | None = None,
    python_executable: str | None = None,
) -> Path | None:
    """Return the PREPARED dependency environment for ``snapshot_root``.

    Lookup only: never creates, installs, or mutates anything. Returns
    ``None`` when no environment matching the full identity (dependency
    fingerprint including uv.lock plus the resolved interpreter) exists.
    Engine starts consume environments exclusively through this accessor so
    an ordinary launch can never trigger dependency installation as a side
    effect; creation happens only at explicit opt-in points (the launcher's
    selected-snapshot launch and the adoption preparation checkpoint).
    """
    try:
        return ensure_dep_env(
            snapshot_root,
            runtime_root=runtime_root,
            python_executable=python_executable,
            allow_create=False,
        )
    except SnapshotError:
        return None


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
