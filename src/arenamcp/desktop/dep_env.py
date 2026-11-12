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
successful import probe of the snapshot's entry package. The build/repair
marker schema is part of the environment identity, so a schema bump publishes
the rebuilt environment ADDITIVELY under a new identity directory while the
previously prepared environment stays untouched at its original path (active
processes, absolute launcher shebangs and rollback references keep working).
Only an invalid tree sitting exactly at the target identity path (crash
debris, a lost promotion race) is moved aside to a retained ``.superseded-*``
name (kept for rollback; operators clean these up deliberately, never the
code path in this module).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .runtime_snapshots import SnapshotError, dependency_fingerprint

logger = logging.getLogger(__name__)

ENV_MARKER_NAME = "dep-env.json"

#: Marker schema for prepared dependency environments. Bumped to 3 when native
#: Windows qualification showed the previous ``Scripts/*.exe`` retarget pass
#: rejecting distlib's native LF+CRLF shebang terminator and silently leaving
#: stale staging interpreters inside promoted launchers (``pip3.exe`` and
#: friends exited 1 with empty output). The schema participates in
#: :func:`env_identity`, so environments prepared by older code keep their own
#: identity directory at its ORIGINAL path — never reused, never relocated,
#: never mutated — while a rebuild publishes additively under the new
#: schema-3 identity.
MARKER_SCHEMA = 3

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
    """Canonical interpreter path binding an environment identity.

    Absolute and symlink-resolved. When the interpreter sits inside a
    virtualenv — a Windows venv ``python.exe`` is a *copy* of its base, so
    plain path resolution cannot recover the base — resolves through
    verified ``pyvenv.cfg`` provenance to the base interpreter instead, so
    the identity is the same whether computed by a preparer holding the base
    executable or by a process running inside the prepared environment
    itself. Malformed or unverifiable provenance raises :class:`SnapshotError`
    (fail visibly and safely; never guess an identity).
    """
    raw = Path(python_executable or sys.executable)
    return str(_canonical_interpreter(raw.resolve()))


_INTERPRETER_PROVENANCE_MAX_DEPTH = 4


def _pyvenv_cfg_for(python_path: Path) -> Path | None:
    """``pyvenv.cfg`` governing a venv-layout interpreter, or ``None``.

    A CPython venv places its marker at ``<venv>/pyvenv.cfg`` while the
    interpreter lives at ``<venv>/bin/python*`` (POSIX) or
    ``<venv>/Scripts/python.exe`` (Windows).
    """
    cfg = python_path.parent.parent / "pyvenv.cfg"
    return cfg if cfg.is_file() else None


def _parse_pyvenv_cfg(cfg_path: Path) -> dict[str, str]:
    try:
        text = cfg_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SnapshotError(f"virtual-environment provenance marker {cfg_path} is unreadable: {exc}") from exc
    data: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip().lower()] = value.strip()
    return data


def _base_executable_candidates(cfg: dict[str, str], home: Path) -> list[Path]:
    """Provenance-declared base interpreter paths, most specific first."""
    candidates: list[Path] = []
    declared = cfg.get("executable")
    if declared:
        candidates.append(Path(declared))
    if sys.platform == "win32":
        candidates.append(home / "python.exe")
    else:
        match = re.match(r"(\d+\.\d+)", cfg.get("version", ""))
        if match:
            candidates.append(home / f"python{match.group(1)}")
        candidates.append(home / "python3")
        candidates.append(home / "python")
    return candidates


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def _canonical_interpreter(resolved: Path) -> Path:
    """Follow VERIFIED virtual-environment provenance to the base interpreter.

    A Windows venv's ``python.exe`` is a copy of the base interpreter, so a
    UI process running inside a prepared environment would otherwise compute
    a different identity than preparation did and fail to find its own
    engine. When the resolved interpreter sits in venv layout, resolve
    through its ``pyvenv.cfg`` provenance instead:

    - the marker must parse and carry a usable ``home`` and ``version``;
    - a base interpreter executable must exist at the proven location;
    - when the interpreter IS this running process, the claim is fully
      verified against runtime ground truth (``sys.prefix`` must be that
      venv root and the recorded version must match the running version —
      a base upgraded underneath the environment is a real provenance
      change and must not silently alias to the old identity).

    Interpreters outside venv layout are returned unchanged. Malformed or
    unverifiable provenance raises :class:`SnapshotError` so callers fail
    visibly and safely: lookup returns nothing and creation is never
    attempted on a guessed identity.
    """
    current = resolved
    for _depth in range(_INTERPRETER_PROVENANCE_MAX_DEPTH):
        cfg_path = _pyvenv_cfg_for(current)
        if cfg_path is None:
            return current
        cfg = _parse_pyvenv_cfg(cfg_path)
        home_raw = cfg.get("home")
        version_raw = cfg.get("version")
        if not home_raw or not version_raw or not re.match(r"\d+\.\d+", version_raw):
            raise SnapshotError(
                f"interpreter {current} claims virtual-environment provenance "
                f"via {cfg_path} but the marker is malformed (missing or "
                "invalid 'home'/'version')"
            )
        home = Path(home_raw)
        if not home.is_dir():
            raise SnapshotError(
                f"interpreter {current} claims base installation {home} "
                f"(via {cfg_path}) but that directory does not exist"
            )
        base = next(
            (cand for cand in _base_executable_candidates(cfg, home) if cand.is_file()),
            None,
        )
        if base is None:
            raise SnapshotError(
                f"interpreter {current} claims base installation {home} "
                f"(via {cfg_path}) but no interpreter executable exists there"
            )
        if current == Path(sys.executable).resolve():
            # Full verification against runtime ground truth: this process
            # really runs inside the venv the marker describes.
            if Path(sys.prefix).resolve() != cfg_path.parent.resolve():
                raise SnapshotError(
                    f"interpreter {current} claims virtual-environment "
                    f"provenance via {cfg_path} but sys.prefix "
                    f"{Path(sys.prefix)} is not that venv root"
                )
            if _version_tuple(version_raw) != _version_tuple(platform.python_version()):
                raise SnapshotError(
                    f"interpreter {current} claims provenance version "
                    f"{version_raw} (via {cfg_path}) but this process runs "
                    f"{platform.python_version()}: the base interpreter "
                    "changed under the environment"
                )
        current = base
    raise SnapshotError(
        f"interpreter {resolved} exceeds virtual-environment provenance "
        f"depth {_INTERPRETER_PROVENANCE_MAX_DEPTH}"
    )


def env_identity(
    snapshot_root: Path,
    python_executable: str | None = None,
    *,
    fingerprint: str | None = None,
) -> str:
    """Full dependency/interpreter/schema identity of an environment.

    Includes the interpreter so an interpreter change can never silently
    reuse (or destroy) an environment built for a different one, and the
    marker schema so environments prepared by older build/repair code are
    addressed under their own identity: a schema bump therefore publishes a
    rebuilt environment additively under a NEW directory instead of replacing
    the old one at its original path. The interpreter is canonicalised
    through verified ``pyvenv.cfg`` provenance, so a process running inside
    a prepared environment resolves to the same base-interpreter identity
    the preparer recorded — while a genuinely changed base interpreter or
    version still yields a distinct identity.
    """
    if fingerprint is None:
        fingerprint = dependency_fingerprint(Path(snapshot_root))
    digest = hashlib.sha256()
    digest.update(fingerprint.encode("ascii"))
    digest.update(b"\0")
    digest.update(resolve_interpreter(python_executable).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(MARKER_SCHEMA).encode("ascii"))
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
    b'#!' + interpreter + post_interp + <terminator> + <ZIP payload>``
    (distlib ``ScriptMaker._write_script``; native Windows launchers terminate
    the shebang with LF+CRLF rather than distlib's bare LF). The entry-point
    payload travels inside the
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
    <terminator> + <ZIP payload>``. The ZIP payload starts at the last
    local-file header signature (``PK\\x03\\x04``); the shebang is the single
    line ending immediately before it. Native Windows qualification observed
    real pip-authored launchers terminating that line with LF followed by CRLF
    (``...python.exe" 0A 0D 0A PK..``) rather than the bare LF of distlib's own
    ``_build_shebang``; both shapes (plus a plain CRLF variant) are accepted
    and the terminator is re-emitted verbatim so the rewritten launcher stays
    byte-faithful apart from the interpreter reference. Returns ``None``
    (leave untouched) when the shape does not match or the embedded
    interpreter is not the staging python.
    """
    zip_idx = data.rfind(b"PK\x03\x04")
    if zip_idx == -1:
        return None
    shebang_idx = data.rfind(b"#!", 0, zip_idx)
    if shebang_idx == -1:
        return None
    line = data[shebang_idx:zip_idx]
    # Longest terminator first so the LF+CRLF native shape is not mistaken
    # for a bare-LF line followed by stray bytes.
    terminator = next(
        (candidate for candidate in (b"\n\r\n", b"\r\n", b"\n") if line.endswith(candidate)),
        None,
    )
    if terminator is None:
        return None  # not a single-line shebang sitting directly before the payload
    body_bytes = line[: len(line) - len(terminator)]
    if not body_bytes.startswith(b"#!") or b"\n" in body_bytes:
        return None
    try:
        body = body_bytes.decode("utf-8")[2:]
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
    new_line = f"#!{new_interp}{post_interp}".encode() + terminator
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
    # A marker from an older schema was written by code whose guarantees no
    # longer hold (schema 2 launchers could keep stale staging interpreters);
    # such an environment must be rebuilt, never silently reused.
    if marker.get("schema") != MARKER_SCHEMA:
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
        if (
            marker is not None
            and marker.get("schema") == MARKER_SCHEMA
            and marker.get("identity") == identity
            and venv_python(env_dir).exists()
        ):
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
            "schema": MARKER_SCHEMA,
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
