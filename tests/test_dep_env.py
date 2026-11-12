"""Dependency-environment isolation: versioned envs, no mutation of active envs.

These tests exercise ``dep_env.ensure_dep_env`` / ``child_env_for`` with a
scripted ``subprocess.run`` so the venv/pip steps are simulated deterministically;
the atomicity, marker-validation and scrubbing contracts are asserted against
real filesystem state.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from arenamcp.desktop import dep_env
from arenamcp.desktop.dep_env import (
    ENV_MARKER_NAME,
    dep_env_root,
    ensure_dep_env,
    env_dir_for,
    env_identity,
    read_env_marker,
    venv_python,
)
from arenamcp.desktop.runtime_snapshots import SnapshotError, dependency_fingerprint


@pytest.fixture()
def snapshot_root(tmp_path: Path) -> Path:
    """A minimal snapshot tree whose fingerprint derives from requirements.txt."""
    root = tmp_path / "snapshots" / "abc123-src"
    (root / "src" / "arenamcp").mkdir(parents=True)
    (root / "requirements.txt").write_text("PySide6==6.7.0\n", encoding="utf-8")
    (root / "src" / "arenamcp" / "__init__.py").write_text("", encoding="utf-8")
    return root


class _Result:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRuns:
    """Records subprocess invocations; simulates venv/ensurepip/pip/probe."""

    def __init__(self, fail_step: str | None = None):
        self.calls: list[list[str]] = []
        self.lock = threading.Lock()
        self.fail_step = fail_step

    def __call__(self, argv, **kwargs):
        with self.lock:
            self.calls.append(list(argv))
        # Simulate `uv export -o <file>`: materialize the pinned reqs file.
        if "-o" in argv:
            if self.fail_step == "export":
                raise subprocess.CalledProcessError(1, argv, stderr=b"uv export boom")
            out = Path(argv[argv.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("pinned-from-lock==1\n", encoding="utf-8")

        def step_result(rc: int, err: bytes):
            # Mirror subprocess.run(..., check=True): nonzero exits raise.
            if rc != 0:
                raise subprocess.CalledProcessError(rc, argv, stderr=err)
            return _Result(rc, b"", err)

        if "-m" in argv and "venv" in argv:
            target = Path(argv[-1])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            return _Result(0, b"", b"")
        if "-m" in argv and "ensurepip" in argv:
            if self.fail_step == "ensurepip":
                return step_result(1, b"ensurepip boom")
            return _Result(0, b"", b"")
        if "-m" in argv and "pip" in argv:
            if self.fail_step == "pip":
                return step_result(1, b"pip resolve failed")
            return _Result(0, b"", b"")
        # Import probe: prove the venv python can import the app package.
        if self.fail_step == "probe":
            return _Result(1, b"", b"ModuleNotFoundError: arenamcp")
        return _Result(0, b"ok", b"")


@pytest.fixture()
def fake_runs(monkeypatch):
    runs = FakeRuns()
    monkeypatch.setattr(dep_env.subprocess, "run", runs)
    return runs


def _fake_uv_which(fake_uv: str):
    """Return a ``shutil.which`` stand-in that resolves only ``uv``.

    The dependency environment is hermetic: the locked-install path must be
    driven by an explicitly supplied ``uv`` resolver, never by whatever
    happens to sit on the ambient CI runner's PATH. Any other lookup is
    delegated to whichever resolver was current when the factory ran —
    captured before the stand-in itself is installed, so delegating never
    re-enters the stand-in (that would recurse for every non-uv name).
    """
    real_which = shutil.which

    def _which(name, *args, **kwargs):
        if name == "uv":
            return fake_uv
        return real_which(name, *args, **kwargs)

    return _which


def test_fake_uv_which_delegates_non_uv_lookups_to_the_original_resolver(monkeypatch):
    """The stand-in must not recurse when asked about anything besides uv."""
    # Deterministic original resolver: a stub installed BEFORE the factory
    # captures it, so delegation never depends on the host PATH (a CI runner
    # may have no python3 at all; Windows ships python.exe instead).
    delegated = []

    def original_stub(name, *args, **kwargs):
        delegated.append((name, args, kwargs))
        if name == "python3":
            return "/stub/bin/python3"
        return None

    monkeypatch.setattr(shutil, "which", original_stub)
    stand_in = _fake_uv_which("/fake/bin/uv")
    monkeypatch.setattr(shutil, "which", stand_in)

    assert shutil.which("uv") == "/fake/bin/uv"
    # Non-uv lookups go through the captured original — no RecursionError,
    # and the exact arguments are forwarded with the exact result returned.
    assert shutil.which("python3") == "/stub/bin/python3"
    assert shutil.which("definitely-not-a-real-binary-xyz") is None
    assert delegated == [
        ("python3", (), {}),
        ("definitely-not-a-real-binary-xyz", (), {}),
    ]


def test_ensure_dep_env_creates_versioned_env_with_binding_marker(snapshot_root, fake_runs, tmp_path):
    runtime_root = tmp_path / "runtime"
    env_dir = ensure_dep_env(snapshot_root, runtime_root=runtime_root)

    identity = env_identity(snapshot_root)
    assert env_dir == env_dir_for(identity, runtime_root)
    assert env_dir.parent == dep_env_root(runtime_root)
    marker = read_env_marker(env_dir)
    assert marker is not None
    assert marker["identity"] == identity
    assert marker["fingerprint"] == dependency_fingerprint(snapshot_root)
    assert marker["python"] == dep_env.resolve_interpreter()
    assert marker["install_mode"] == "requirements.txt"
    assert venv_python(env_dir).exists()
    # All build steps ran against a private staging tree, never the final name.
    venv_targets = [Path(c[-1]) for c in fake_runs.calls if "venv" in c]
    assert venv_targets
    assert all(".dep-staging-" in p.name for p in venv_targets)


def test_ensure_dep_env_reuses_matching_env_without_touching_subprocess(snapshot_root, fake_runs, tmp_path):
    runtime_root = tmp_path / "runtime"
    first = ensure_dep_env(snapshot_root, runtime_root=runtime_root)
    calls_after_first = len(fake_runs.calls)

    again = ensure_dep_env(snapshot_root, runtime_root=runtime_root)
    assert again == first
    assert len(fake_runs.calls) == calls_after_first  # pure reuse, no rebuild


def test_ensure_dep_env_rebuilds_when_python_or_fingerprint_changes(snapshot_root, fake_runs, tmp_path):
    runtime_root = tmp_path / "runtime"
    first = ensure_dep_env(snapshot_root, runtime_root=runtime_root)

    # A different interpreter gets its OWN identity directory: the old env is
    # neither reused nor deleted.
    calls_before = len(fake_runs.calls)
    rebuilt = ensure_dep_env(snapshot_root, runtime_root=runtime_root, python_executable="/other/python3")
    assert rebuilt != first
    assert rebuilt == env_dir_for(
        env_identity(snapshot_root, python_executable="/other/python3"), runtime_root
    )
    assert len(fake_runs.calls) > calls_before  # a real rebuild happened
    assert read_env_marker(rebuilt)["python"] == str(Path("/other/python3").resolve())
    # The previous environment survived intact for rollback.
    assert read_env_marker(first)["python"] == dep_env.resolve_interpreter()
    assert venv_python(first).exists()

    # A changed dependency spec yields a different fingerprint/env identity.
    (snapshot_root / "requirements.txt").write_text("PySide6==6.8.0\n", encoding="utf-8")
    other = ensure_dep_env(snapshot_root, runtime_root=runtime_root)
    assert other != rebuilt
    assert read_env_marker(other)["fingerprint"] == dependency_fingerprint(snapshot_root)


def test_ensure_dep_env_never_mutates_the_existing_environment(snapshot_root, fake_runs, tmp_path):
    runtime_root = tmp_path / "runtime"
    identity = env_identity(snapshot_root)
    env_dir = env_dir_for(identity, runtime_root)
    env_dir.mkdir(parents=True)
    sentinel_parent = env_dir / "site-packages"
    sentinel_parent.mkdir()
    sentinel = sentinel_parent / "old_pkg"
    sentinel.write_text("old", encoding="utf-8")

    ensure_dep_env(snapshot_root, runtime_root=runtime_root)

    # The old tree was replaced wholesale (moved aside), not edited in place:
    # the new env carries the fresh marker and no trace of the old content.
    assert not sentinel.exists()
    assert read_env_marker(env_dir)["identity"] == identity
    # ...but the old environment was RETAINED under a .superseded-* sibling —
    # never deleted — so an active or old env always survives for rollback.
    dep_root = dep_env_root(runtime_root)
    superseded = [p for p in dep_root.iterdir() if p.name.startswith(".superseded-")]
    assert len(superseded) == 1
    assert (superseded[0] / "site-packages" / "old_pkg").read_text(encoding="utf-8") == "old"
    # Every build step targeted a private staging dir under the dep-env root.
    for call in fake_runs.calls:
        targets = [Path(a) for a in call if ".dep-staging-" in a]
        if targets:
            assert all(dep_root in t.parents for t in targets)


def test_ensure_dep_env_failure_leaves_existing_env_usable(snapshot_root, tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    fingerprint = dependency_fingerprint(snapshot_root)
    env_dir = env_dir_for(fingerprint, runtime_root)
    good_python = env_dir / "bin" / "python"
    good_python.parent.mkdir(parents=True)
    good_python.write_text("#!/bin/sh\n", encoding="utf-8")
    marker = {
        "schema": 1,
        "fingerprint": fingerprint,
        # Stale interpreter: forces the rebuild path instead of marker reuse.
        "python": "/retired/python",
        "created_utc": "2026-01-01T00:00:00Z",
        "snapshot": str(snapshot_root),
    }
    (env_dir / ENV_MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")

    failing = FakeRuns(fail_step="pip")
    monkeypatch.setattr(dep_env.subprocess, "run", failing)

    with pytest.raises(SnapshotError):
        ensure_dep_env(snapshot_root, runtime_root=runtime_root)

    # The previously-good environment is untouched and still validates.
    assert read_env_marker(env_dir)["fingerprint"] == fingerprint
    assert venv_python(env_dir) == good_python
    # Staging debris is cleaned up.
    leftovers = [p for p in dep_env_root(runtime_root).iterdir() if p.name.startswith(".dep-staging-")]
    assert not leftovers


def test_ensure_dep_env_allow_create_false_never_builds(snapshot_root, fake_runs, tmp_path):
    with pytest.raises(SnapshotError):
        ensure_dep_env(snapshot_root, runtime_root=tmp_path / "r", allow_create=False)
    assert fake_runs.calls == []


def test_concurrent_ensure_dep_env_yields_one_valid_environment(snapshot_root, fake_runs, tmp_path):
    runtime_root = tmp_path / "runtime"
    results: list[Path] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(ensure_dep_env(snapshot_root, runtime_root=runtime_root))
        except BaseException as exc:  # noqa: BLE001 - recorded on purpose
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    expected = env_dir_for(env_identity(snapshot_root), runtime_root)
    assert results and all(r == expected for r in results)
    assert read_env_marker(expected)["fingerprint"] == dependency_fingerprint(snapshot_root)
    assert venv_python(expected).exists()
    leftovers = [p for p in dep_env_root(runtime_root).iterdir() if p.name.startswith(".dep-staging-")]
    assert not leftovers


def test_child_env_scrubs_smuggled_paths_and_keeps_user_state(snapshot_root, fake_runs, tmp_path):
    base = {
        "HOME": "/users/josh",
        "PYTHONPATH": "/mutable/checkout/src",
        "PYTHONHOME": "/somewhere",
        "PYTHONSTARTUP": "/profile.py",
        "PATH": "/usr/bin:/bin",
        "VIRTUAL_ENV": "/some-other-venv",
        "MTGACOACH_APP_ROOT": "/mutable/checkout",
        "MTGACOACH_SNAPSHOT_SHA": "b" * 40,
        "MTGACOACH_SNAPSHOT_ROOT": "/mutable/checkout/snapshots/old",
        "MTGACOACH_LAUNCH_CHILD": "1",
        "MTGACOACH_DEV_CHECKOUT": "/mutable/checkout",
        "MTGACOACH_DEV_GIT_DIR": "/mutable/checkout/.git",
        "MTGACOACH_SNAPSHOTS_DIR": "/mutable/checkout/snapshots",
        "MTGACOACH_DISABLE_RUNTIME_SNAPSHOT": "",
        "MTGACOACH_READY_TIMEOUT_S": "",
        "_MTGACOACH_INTERNAL_FLAG": "",
        "_UNDERSCORE_KEEP_ME": "",
        "_MTGACOACH_TEST_ONLY": "",
        "_MTGACOACH_ANOTHER": "",
        "_MTGACOACH_LAST_ONE": "",
        "_MTGACOACH_EXTRA_A": "",
        "_MTGACOACH_EXTRA_B": "",
        "_MTGACOACH_EXTRA_C": "",
        "_MTGACOACH_EXTRA_D": "",
        "_MTGACOACH_EXTRA_E": "",
        "_MTGACOACH_EXTRA_F": "",
        "_MTGACOACH_EXTRA_G": "",
        "_MTGACOACH_EXTRA_H": "",
        "_MTGACOACH_EXTRA_I": "",
        "_MTGACOACH_EXTRA_J": "",
        "_MTGACOACH_EXTRA_K": "",
        "_MTGACOACH_EXTRA_L": "",
        "_MTGACOACH_EXTRA_M": "",
        "_MTGACOACH_EXTRA_N": "",
        "_MTGACOACH_EXTRA_O": "",
        "_MTGACOACH_EXTRA_P": "",
        "_MTGACOACH_EXTRA_Q": "",
        "_MTGACOACH_EXTRA_R": "",
        "_MTGACOACH_EXTRA_S": "",
        "_MTGACOACH_EXTRA_T": "",
        "_MTGACOACH_EXTRA_U": "",
        "_MTGACOACH_EXTRA_V": "",
        "_MTGACOACH_EXTRA_W": "",
        "_MTGACOACH_EXTRA_X": "",
        "_MTGACOACH_EXTRA_Y": "",
        "_MTGACOACH_EXTRA_Z": "",
    }

    runtime_root = tmp_path / "runtime"

    env = dep_env.child_env_for(snapshot_root, base, runtime_root=runtime_root)

    # Smuggled module paths are scrubbed so the child cannot import from a
    # mutable checkout or a foreign interpreter installation.
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        assert key not in env
    # User state is preserved exactly as inherited.
    assert env["HOME"] == "/users/josh"
    assert env["_UNDERSCORE_KEEP_ME"] == ""
    # The child is bound to the snapshot's dependency environment.
    identity = env_identity(snapshot_root)
    expected_env_dir = env_dir_for(identity, runtime_root)
    assert env["VIRTUAL_ENV"] == str(expected_env_dir)
    assert env["PATH"].split(os.pathsep)[0] == str(venv_python(expected_env_dir).parent)


def test_uv_lock_drives_pinned_install_mode(snapshot_root, fake_runs, tmp_path, monkeypatch):
    (snapshot_root / "uv.lock").write_text("lock content\n", encoding="utf-8")
    # A decoy loose requirements.txt must never be consumed when a lock ships.
    (snapshot_root / "requirements.txt").write_text("loose-decoy==9\n", encoding="utf-8")
    # Supply the uv resolver explicitly: the test is hermetic and does not
    # depend on a real uv binary being present on the runner's PATH.
    fake_uv = "/fake/bin/uv"
    monkeypatch.setattr(dep_env.shutil, "which", _fake_uv_which(fake_uv))
    runtime_root = tmp_path / "runtime"

    env_dir = ensure_dep_env(snapshot_root, runtime_root=runtime_root)

    marker = read_env_marker(env_dir)
    assert marker is not None
    assert marker["install_mode"] == "uv.lock"
    # The lock was exported frozen by exactly the supplied uv resolver...
    export_calls = [c for c in fake_runs.calls if "export" in c and "--frozen" in c]
    assert len(export_calls) == 1
    export_argv = export_calls[0]
    assert export_argv[0] == fake_uv
    for flag in ("--no-dev", "--no-hashes", "--no-emit-project"):
        assert flag in export_argv
    exported = Path(export_argv[export_argv.index("-o") + 1])
    # ...and pip installed THAT pinned file — exactly the exported locked
    # path — never the loose requirements.txt.
    pip_calls = [c for c in fake_runs.calls if "install" in c and "--no-input" in c]
    assert len(pip_calls) == 1
    pip_argv = pip_calls[0]
    consumed = Path(pip_argv[pip_argv.index("-r") + 1])
    assert consumed == exported
    assert consumed != snapshot_root / "requirements.txt"


def test_missing_uv_refuses_locked_snapshot_without_installing(
    snapshot_root, fake_runs, tmp_path, monkeypatch
):
    """A locked snapshot with no uv binary fails closed before any install."""
    (snapshot_root / "uv.lock").write_text("lock content\n", encoding="utf-8")
    monkeypatch.setattr(dep_env.shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(SnapshotError, match="uv is not installed"):
        ensure_dep_env(snapshot_root, runtime_root=tmp_path / "r")
    # The refusal happened before any dependency install: no pip ran, and
    # nothing was promoted under the final identity.
    assert not any("install" in c for c in fake_runs.calls)
    assert not env_dir_for(env_identity(snapshot_root), tmp_path / "r").exists()


def test_failed_uv_export_refuses_locked_snapshot_without_installing(snapshot_root, tmp_path, monkeypatch):
    """A failed frozen export fails closed instead of degrading to loose reqs."""
    (snapshot_root / "uv.lock").write_text("lock content\n", encoding="utf-8")
    runs = FakeRuns(fail_step="export")
    monkeypatch.setattr(dep_env.subprocess, "run", runs)
    monkeypatch.setattr(dep_env.shutil, "which", _fake_uv_which("/fake/bin/uv"))
    with pytest.raises(SnapshotError, match="uv.lock export failed"):
        ensure_dep_env(snapshot_root, runtime_root=tmp_path / "r")
    # No pip install ever ran against a half-exported lock...
    assert not any("install" in c for c in runs.calls)
    # ...and nothing was promoted under the final identity.
    assert not env_dir_for(env_identity(snapshot_root), tmp_path / "r").exists()


def test_import_probe_failure_blocks_promotion_of_a_broken_environment(snapshot_root, tmp_path, monkeypatch):
    failing = FakeRuns(fail_step="probe")
    monkeypatch.setattr(dep_env.subprocess, "run", failing)
    with pytest.raises(SnapshotError):
        ensure_dep_env(snapshot_root, runtime_root=tmp_path / "r")
    # Nothing was promoted under the final identity.
    assert not env_dir_for(env_identity(snapshot_root), tmp_path / "r").exists()


def test_venv_python_locates_windows_layout(tmp_path):
    env_dir = tmp_path / "env"
    (env_dir / "Scripts").mkdir(parents=True)
    (env_dir / "Scripts" / "python.exe").write_text("x", encoding="utf-8")
    assert venv_python(env_dir) == env_dir / "Scripts" / "python.exe"


def test_promoted_entrypoints_work_at_space_containing_paths(tmp_path):
    """Real execution: relocated pip3 works when the env path has spaces.

    The default macOS layout (``~/Library/Application Support/…``) contains
    spaces, and there pip emits console scripts as ``/bin/sh`` exec-wrappers
    whose second line still names the staging interpreter. Repointing must
    rewrite those wrappers too — previously only direct ``#!`` shebangs were
    rewritten, so the promoted ``pip3`` exited 127 referencing the deleted
    staging python.
    """
    staging = tmp_path / "sta ging"
    promoted = tmp_path / "pro mot ed"
    subprocess.run(
        [sys.executable, "-m", "venv", str(staging)],
        check=True,
        capture_output=True,
        timeout=300,
    )

    # Guard the premise: at a space-containing path pip really does generate
    # shell-wrapper entrypoints (if pip ever changes this, this test should
    # be revisited rather than silently passing on the wrong shape).
    bin_dir = venv_python(staging).parent
    wrappers = [
        p
        for p in bin_dir.iterdir()
        if p.is_file()
        and p.read_text(encoding="utf-8", errors="replace").startswith("#!/bin/sh")
        and str(staging) in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert wrappers, "expected /bin/sh wrapper entrypoints at a space-containing path"

    dep_env._repoint_entrypoint_shebangs(staging, promoted)
    staging.rename(promoted)

    pip3 = venv_python(promoted).parent / "pip3"
    proc = subprocess.run(
        [str(pip3), "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert str(promoted) in proc.stdout  # running from the promoted location


# --- Windows qualification regressions ------------------------------------------


def _launcher_exe(interpreter: str, payload: bytes, post: str = "", terminator: bytes = b"\n") -> bytes:
    """Stand-in for a pip/distlib Windows launcher: PE prefix + shebang + ZIP."""
    pe_prefix = b"MZ" + bytes(30)
    return pe_prefix + f"#!{interpreter}{post}".encode() + terminator + payload


def _embedded_launcher_interpreter(exe_bytes: bytes) -> tuple[str, str]:
    zip_idx = exe_bytes.rfind(b"PK\x03\x04")
    assert zip_idx != -1
    shebang_idx = exe_bytes.rfind(b"#!", 0, zip_idx)
    line = exe_bytes[shebang_idx:zip_idx].decode("utf-8")
    body = line[2:-1]  # strip '#!' and trailing newline
    if body.startswith('"'):
        end_quote = body.find('"', 1)
        return body[: end_quote + 1], body[end_quote + 1 :]
    space = body.find(" ")
    interp, post = (body, "") if space == -1 else (body[:space], body[space:])
    return interp, post


def test_windows_exe_launchers_are_retargeted_to_promoted_python(tmp_path, monkeypatch):
    """pip3/pip3.11/normalizer-style .exe launchers survive promotion.

    Native Windows qualification found these exiting 1 with empty output after
    promotion: their embedded interpreter referenced the deleted staging venv
    and the old repoint pass skipped ``.exe`` entirely. The repair must splice
    the promoted interpreter into the launcher's shebang while keeping the PE
    prefix and the ZIP entry-point payload byte-identical.
    """
    monkeypatch.setattr(dep_env, "_is_windows", lambda: True)
    staging = tmp_path / "staging"
    promoted = tmp_path / "promoted"
    bin_dir = staging / "Scripts"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python.exe").write_bytes(b"")

    staging_python = str(bin_dir / "python.exe")
    promoted_python = str(promoted / "Scripts" / "python.exe")
    payload_plain = b"PK\x03\x04entry-point-zip-pip3"
    payload_post = b"PK\x03\x04entry-point-zip-normalizer"
    foreign_payload = b"PK\x03\x04foreign-zip"

    (bin_dir / "pip3.exe").write_bytes(_launcher_exe(staging_python, payload_plain))
    (bin_dir / "pip3.11.exe").write_bytes(_launcher_exe(staging_python, payload_plain))
    (bin_dir / "normalizer.exe").write_bytes(_launcher_exe(staging_python, payload_post, post=" -E"))
    foreign_before = _launcher_exe("C:\\elsewhere\\python.exe", foreign_payload)
    (bin_dir / "foreign.exe").write_bytes(foreign_before)
    nozip_before = b"MZ" + bytes(10) + f"#!{staging_python}\n".encode()
    (bin_dir / "nozip.exe").write_bytes(nozip_before)

    dep_env._repoint_entrypoint_shebangs(staging, promoted)

    for name, expected_payload, expected_post in (
        ("pip3.exe", payload_plain, ""),
        ("pip3.11.exe", payload_plain, ""),
        ("normalizer.exe", payload_post, " -E"),
    ):
        data = (bin_dir / name).read_bytes()
        interp, post = _embedded_launcher_interpreter(data)
        assert interp == promoted_python, name
        assert post == expected_post, name
        assert data[data.rfind(b"PK\x03\x04") :] == expected_payload, name
        assert data[: data.rfind(b"#!")] == b"MZ" + bytes(30), name

    # Launchers that never pointed at staging keep their exact bytes.
    assert (bin_dir / "foreign.exe").read_bytes() == foreign_before
    assert (bin_dir / "nozip.exe").read_bytes() == nozip_before


def test_windows_spaced_profile_launcher_preserves_quoting_and_options(tmp_path, monkeypatch):
    r"""A quoted spaced-path interpreter stays quoted after retargeting.

    Reviewer-reproduced regression: pip authors launchers for spaced profile
    paths as ``#!"C:\Users\Jane Doe\...\python.exe"`` and the retarget splice
    re-emitted the promoted interpreter unquoted, splitting the path at the
    space so the entrypoint died. Quoting must be preserved (and gained when a
    path needs it) with interpreter options kept verbatim.
    """
    monkeypatch.setattr(dep_env, "_is_windows", lambda: True)
    staging = tmp_path / "Jane Doe" / "staging"
    promoted = tmp_path / "Jane Doe" / "promoted"
    bin_dir = staging / "Scripts"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python.exe").write_bytes(b"")

    staging_python = str(bin_dir / "python.exe")
    promoted_python = str(promoted / "Scripts" / "python.exe")
    assert " " in staging_python  # the fixture must exercise a spaced path
    payload = b"PK\x03\x04entry-point-zip-pip3"

    quoted_before = _launcher_exe(f'"{staging_python}"', payload, post=" -E -s")
    (bin_dir / "pip3.exe").write_bytes(quoted_before)

    dep_env._repoint_entrypoint_shebangs(staging, promoted)

    data = (bin_dir / "pip3.exe").read_bytes()
    interp, post = _embedded_launcher_interpreter(data)
    assert interp == f'"{promoted_python}"'
    assert post == " -E -s"
    assert data[data.rfind(b"PK\x03\x04") :] == payload


def test_windows_native_distlib_lf_crlf_terminator_is_accepted_and_preserved(tmp_path, monkeypatch):
    r"""Native pip launchers ending LF+CRLF are retargeted, terminator kept.

    Native Windows qualification (C:\rq7) found real ``pip3.exe`` /
    ``pip3.11.exe`` / ``normalizer.exe`` launchers whose shebang region ends
    with the quoted interpreter followed by bytes ``0A 0D 0A`` (LF then CRLF)
    immediately before the ZIP local header — not distlib's bare LF. The
    previous retarget pass rejected any second newline and returned ``None``,
    leaving the deleted staging interpreter embedded (exit 1, empty output).
    The exact native shape must be accepted, rewritten in place, and the
    terminator preserved byte-for-byte with the PE prefix and ZIP payload.
    """
    monkeypatch.setattr(dep_env, "_is_windows", lambda: True)
    staging = tmp_path / "A User" / ".dep-staging-ed12e9db3c3c-n88ui3ei"
    promoted = tmp_path / "A User" / "ed12e9db3c3cf2ac"
    bin_dir = staging / "Scripts"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python.exe").write_bytes(b"")

    staging_python = str(bin_dir / "python.exe")
    promoted_python = str(promoted / "Scripts" / "python.exe")
    assert " " in staging_python  # the native fixture path is long and spaced
    payload = b"PK\x03\x04entry-point-zip-pip3"
    pe_prefix = b"MZ" + bytes(30)

    native_before = (
        pe_prefix
        + f'#!"{staging_python}" -E'.encode()
        + b"\n\r\n"  # the exact observed native delimiter: LF + CRLF
        + payload
    )
    (bin_dir / "pip3.exe").write_bytes(native_before)

    dep_env._repoint_entrypoint_shebangs(staging, promoted)

    data = (bin_dir / "pip3.exe").read_bytes()
    zip_idx = data.rfind(b"PK\x03\x04")
    shebang_idx = data.rfind(b"#!", 0, zip_idx)
    # Terminator preserved exactly; interpreter quoted at the promoted path.
    assert data[zip_idx:] == payload
    assert data[:shebang_idx] == pe_prefix
    assert data[shebang_idx:zip_idx] == f'#!"{promoted_python}" -E'.encode() + b"\n\r\n"


@pytest.mark.parametrize("terminator", [b"\n", b"\r\n", b"\n\r\n"])
def test_retarget_launcher_shebang_accepts_and_preserves_each_terminator(terminator):
    """Each supported shebang terminator round-trips byte-identically."""
    old = r"C:\staging\Scripts\python.exe"
    new = r"C:\A User\promoted\Scripts\python.exe"
    payload = b"PK\x03\x04zip"
    data = b"MZ-pe-prefix" + f'#!"{old}" -Es'.encode() + terminator + payload

    rewritten = dep_env._retarget_launcher_shebang(data, old, new)

    assert rewritten is not None
    assert rewritten == (b"MZ-pe-prefix" + f'#!"{new}" -Es'.encode() + terminator + payload)


@pytest.mark.parametrize(
    "data",
    [
        # Two-line shebang region: not a single line before the payload.
        b"MZ" + b'#!"C:\\staging\\Scripts\\python.exe"\nextra\n' + b"PK\x03\x04zip",
        # No recognized terminator directly before the payload.
        b'MZ#!"C:\\staging\\Scripts\\python.exe"' + b"PK\x03\x04zip",
        # Unterminated quote.
        b'MZ#!"C:\\staging\\Scripts\\python.exe\n' + b"PK\x03\x04zip",
        # Embedded interpreter is not the staging python.
        b'MZ#!"C:\\elsewhere\\python.exe"\n' + b"PK\x03\x04zip",
        # No ZIP payload at all.
        b'MZ#!"C:\\staging\\Scripts\\python.exe"\n',
        # Shebang region is not decodable UTF-8.
        b"MZ#!\xff\xfe\n" + b"PK\x03\x04zip",
        # Empty shebang body.
        b"MZ#!\n" + b"PK\x03\x04zip",
    ],
)
def test_retarget_launcher_shebang_rejects_unrelated_or_malformed_shapes(data):
    """Malformed or foreign launchers are left untouched (returns None)."""
    assert (
        dep_env._retarget_launcher_shebang(data, r"C:\staging\Scripts\python.exe", r"C:\new\python.exe")
        is None
    )


def test_prepared_env_with_previous_marker_schema_is_not_treated_qualified(
    snapshot_root, tmp_path, monkeypatch
):
    r"""Environments prepared by the schema-2 code are never reused as-is.

    Native qualification showed that code leaving stale staging interpreters
    inside promoted ``Scripts/*.exe`` launchers while still writing a fully
    valid-looking marker. Such an environment must fail validation (so
    prepared-only lookups report nothing and explicit preparation rebuilds),
    even though its fingerprint and interpreter all still match. The fixture
    reproduces the schema-2 world honestly: the legacy identity is computed
    with ``MARKER_SCHEMA`` temporarily set to 2, and the stale env is placed
    at THAT path — exactly where the old code published it.
    """
    runtime_root = tmp_path / "runtime"
    fingerprint = dependency_fingerprint(snapshot_root)
    with monkeypatch.context() as legacy:
        legacy.setattr(dep_env, "MARKER_SCHEMA", dep_env.MARKER_SCHEMA - 1)
        legacy_identity = env_identity(snapshot_root, python_executable=None, fingerprint=fingerprint)
        legacy_env_dir = env_dir_for(legacy_identity, runtime_root)
    (legacy_env_dir / "bin").mkdir(parents=True)
    (legacy_env_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    marker = {
        "schema": dep_env.MARKER_SCHEMA - 1,
        "identity": legacy_identity,
        "fingerprint": fingerprint,
        "python": str(Path(sys.executable).resolve()),
        "install_mode": "requirements.txt",
        "created_utc": "2026-01-01T00:00:00Z",
        "snapshot": str(snapshot_root),
        "dep_envs_root": str(legacy_env_dir.parent),
    }
    (legacy_env_dir / ENV_MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")

    # Wrong-schema markers never validate, wherever they sit.
    assert (
        dep_env._valid_existing_env(legacy_env_dir, legacy_identity, fingerprint, marker["python"]) is False
    )
    # Prepared-only lookups address the CURRENT schema identity: nothing there.
    assert dep_env.prepared_dep_env(snapshot_root, runtime_root=runtime_root) is None
    with pytest.raises(SnapshotError):
        ensure_dep_env(snapshot_root, runtime_root=runtime_root, allow_create=False)


def test_stale_schema_environment_publishes_additively_and_old_env_stays_put(
    snapshot_root, fake_runs, tmp_path, monkeypatch
):
    """A schema bump rebuilds under a NEW identity; the old env is untouched.

    Review-requested immutability contract: rebuilding after a marker-schema
    bump must publish additively — the previously prepared environment stays
    at its ORIGINAL path with byte-identical contents so active processes,
    absolute launcher shebangs and rollback references keep working. It is
    never relocated to ``.superseded-*`` and never deleted.
    """
    runtime_root = tmp_path / "runtime"
    fingerprint = dependency_fingerprint(snapshot_root)
    with monkeypatch.context() as legacy:
        legacy.setattr(dep_env, "MARKER_SCHEMA", dep_env.MARKER_SCHEMA - 1)
        legacy_identity = env_identity(snapshot_root, python_executable=None, fingerprint=fingerprint)
        legacy_env_dir = env_dir_for(legacy_identity, runtime_root)
    (legacy_env_dir / "bin").mkdir(parents=True)
    (legacy_env_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    stale_sentinel = legacy_env_dir / "bin" / "stale-wrapper-marker"
    stale_sentinel.write_text("prepared-by-schema-2", encoding="utf-8")
    legacy_marker_text = json.dumps(
        {
            "schema": dep_env.MARKER_SCHEMA - 1,
            "identity": legacy_identity,
            "fingerprint": fingerprint,
            "python": str(Path(sys.executable).resolve()),
            "created_utc": "2026-01-01T00:00:00Z",
            "snapshot": str(snapshot_root),
        }
    )
    (legacy_env_dir / ENV_MARKER_NAME).write_text(legacy_marker_text, encoding="utf-8")

    rebuilt = ensure_dep_env(snapshot_root, runtime_root=runtime_root)

    # The rebuild landed at the CURRENT schema identity — a different path.
    current_identity = env_identity(snapshot_root, python_executable=None, fingerprint=fingerprint)
    assert current_identity != legacy_identity
    assert rebuilt == env_dir_for(current_identity, runtime_root)
    assert rebuilt != legacy_env_dir
    fresh_marker = read_env_marker(rebuilt)
    assert fresh_marker is not None
    assert fresh_marker["schema"] == dep_env.MARKER_SCHEMA
    assert fresh_marker["identity"] == current_identity
    # The old environment survived INTACT at its original path: same marker
    # bytes, same sentinel — never moved aside, never deleted.
    assert (legacy_env_dir / ENV_MARKER_NAME).read_text(encoding="utf-8") == legacy_marker_text
    assert (legacy_env_dir / "bin" / "stale-wrapper-marker").read_text(encoding="utf-8") == (
        "prepared-by-schema-2"
    )
    superseded = [p for p in dep_env_root(runtime_root).iterdir() if p.name.startswith(".superseded-")]
    assert not superseded
    # And the freshly built env is reusable without touching subprocess again.
    calls_after_build = len(fake_runs.calls)
    assert ensure_dep_env(snapshot_root, runtime_root=runtime_root) == rebuilt
    assert len(fake_runs.calls) == calls_after_build


def test_windows_long_runtime_root_redirects_dep_envs_to_short_user_storage(
    snapshot_root, fake_runs, tmp_path, monkeypatch
):
    """Deep runtime roots stage/promote under a short deterministic location.

    Qualification blocker 1: a locked PySide6 install failed on MAX_PATH under
    a normal-length runtime root while the same source/lock succeeded from a
    drive-root root. Selection must happen deterministically BEFORE any bytes
    are written so preparation and prepared-only lookups agree.
    """
    monkeypatch.setattr(dep_env, "_is_windows", lambda: True)
    # The redirection LOGIC is what's under test here (the absolute Windows
    # budget is covered by the envelope test below), so scale the budget to
    # this host's temporary-directory depth: the fallback must fit while the
    # deep root must not, whatever length pytest's tmp tree happens to be.
    with tempfile.TemporaryDirectory(prefix="rq-win-") as short_tmp:
        local_appdata = Path(short_tmp)
        monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
        long_root = local_appdata / "deep" / "-".join(["segment"] * 12)
        fallback_total = len(str(local_appdata.joinpath("mtgacoach", "r", "dep-envs")))
        monkeypatch.setattr(
            dep_env,
            "_WIN_MAX_PATH",
            fallback_total + 1 + dep_env._WIN_OWN_LAYOUT_RESERVE + dep_env._WIN_PACKAGE_HEADROOM,
        )

        env_dir = ensure_dep_env(snapshot_root, runtime_root=long_root)

        expected_base = local_appdata / "mtgacoach" / "r"
        assert env_dir.parent == expected_base / "dep-envs"
        marker = read_env_marker(env_dir)
        assert marker is not None
        assert marker["dep_envs_root"] == str(expected_base / "dep-envs")
        # Prepared-only lookups resolve to the same redirected location.
        assert dep_env.prepared_dep_env(snapshot_root, runtime_root=long_root) == env_dir


def test_windows_unfitting_roots_fail_with_actionable_guidance(
    snapshot_root, fake_runs, tmp_path, monkeypatch
):
    """No silent fallback: when nothing fits, fail loudly and build nothing."""
    monkeypatch.setattr(dep_env, "_is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / ("l" * 80)))
    long_root = tmp_path / ("-".join(["segment"] * 12))

    with pytest.raises(SnapshotError, match="MTGACOACH_RUNTIME_ROOT"):
        ensure_dep_env(snapshot_root, runtime_root=long_root)

    # Nothing was staged anywhere: the failure precedes any filesystem work.
    assert not (tmp_path / ("l" * 80)).exists() or not any((tmp_path / ("l" * 80)).rglob(".dep-staging-*"))


def test_windows_path_budget_matches_observed_qualification_envelope(tmp_path, monkeypatch):
    """Drive-root roots pass; realistic profile roots pass; deep roots redirect."""

    def fits(base: Path) -> bool:
        return dep_env._windows_safe_base(base) == base

    # The proven-good qualification root from the coordinator rerun.
    assert fits(Path("C:\\rq3fd"))
    # A realistic %LOCALAPPDATA%-style default must not be forced to redirect.
    assert fits(Path("C:\\Users\\joshu\\AppData\\Local\\mtgacoach"))

    # Deep roots are redirected to the short user-storage fallback. Build the
    # expectation the same way the implementation does (joinpath), so the
    # comparison is separator-consistent on any host OS.
    deep = Path("C:\\Users\\joshu\\AppData\\Local\\mtgacoach\\" + "\\".join(["seg"] * 20))
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\joshu\\AppData\\Local")
    expected_fallback = Path("C:\\Users\\joshu\\AppData\\Local").joinpath("mtgacoach", "r")
    assert dep_env._windows_safe_base(deep) == expected_fallback

    # And when even the fallback cannot fit, selection refuses loudly.
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\joshu\\AppData\\Local\\" + "\\".join(["s"] * 40))
    with pytest.raises(SnapshotError, match="MTGACOACH_RUNTIME_ROOT"):
        dep_env._windows_safe_base(deep)


# ---------------------------------------------------------------------------
# Prepared-interpreter provenance (Windows copy-style venvs)
#
# Native qualification failure being regressed: the UI process runs INSIDE a
# prepared environment whose ``python.exe`` is a COPY of the base interpreter
# (Windows venv semantics). Plain path resolution then computed a different
# identity than preparation did (cf62e0a3078ab46c vs 6342) and
# ``prepared_dep_env`` returned None for the engine's own environment.


def _windows_style_prepared_env(tmp_path: Path, version: str = "3.14.0"):
    """A Windows-shaped base install plus a prepared env with a COPY python.

    Mirrors the native layout: ``<base>/python.exe`` and a prepared
    environment at ``<venv>/Scripts/python.exe`` whose ``pyvenv.cfg``
    provenance points back at the base.
    """
    base_root = tmp_path / "base-python"
    base_root.mkdir(parents=True)
    base_exe = base_root / "python.exe"
    base_exe.write_bytes(b"MZ-fake-base-interpreter")
    venv_root = tmp_path / "dep-envs" / "prepared6342"
    scripts = venv_root / "Scripts"
    scripts.mkdir(parents=True)
    copy_exe = scripts / "python.exe"
    copy_exe.write_bytes(base_exe.read_bytes())  # copy semantics, not a symlink
    (venv_root / "pyvenv.cfg").write_text(
        f"home = {base_root}\n"
        "include-system-site-packages = false\n"
        f"version = {version}\n"
        f"executable = {base_exe}\n",
        encoding="utf-8",
    )
    return base_exe, copy_exe, venv_root


def _run_inside_venv(monkeypatch, copy_exe: Path, venv_root: Path, version: str = "3.14.0"):
    """Make THIS process look like the UI running inside the prepared env."""
    monkeypatch.setattr(sys, "executable", str(copy_exe))
    monkeypatch.setattr(sys, "prefix", str(venv_root))
    monkeypatch.setattr(dep_env.platform, "python_version", lambda: version)


def test_resolve_interpreter_follows_windows_copy_provenance(tmp_path):
    base_exe, copy_exe, _venv = _windows_style_prepared_env(tmp_path)
    # The copy resolves to its verified base, not to its own venv path.
    assert dep_env.resolve_interpreter(str(copy_exe)) == str(base_exe.resolve())


def test_prepared_ui_inside_env_finds_same_engine_without_nesting(
    snapshot_root, fake_runs, tmp_path, monkeypatch
):
    base_exe, copy_exe, venv_root = _windows_style_prepared_env(tmp_path)
    runtime_root = tmp_path / "runtime"

    # Explicit preparer creates the isolated env keyed by the BASE interpreter.
    prepared = ensure_dep_env(snapshot_root, runtime_root=runtime_root, python_executable=str(base_exe))
    assert read_env_marker(prepared)["python"] == str(base_exe.resolve())

    # The actual native failure scenario: the UI runs inside that prepared
    # environment and looks up its own engine with no explicit interpreter.
    _run_inside_venv(monkeypatch, copy_exe, venv_root)

    calls_before = len(fake_runs.calls)
    found = dep_env.prepared_dep_env(snapshot_root, runtime_root=runtime_root)
    assert found == prepared  # the correct prepared same-version engine...
    assert len(fake_runs.calls) == calls_before  # ...found without ANY nested env creation

    # ensure_dep_env from the same in-env context reuses, never rebuilds.
    assert ensure_dep_env(snapshot_root, runtime_root=runtime_root) == prepared
    assert len(fake_runs.calls) == calls_before


def test_compatible_target_env_keys_off_base_identity_not_venv_copy(
    snapshot_root, fake_runs, tmp_path, monkeypatch
):
    base_exe, copy_exe, venv_root = _windows_style_prepared_env(tmp_path)

    # Identity computed from inside the env equals the preparer's identity.
    preparer_identity = env_identity(snapshot_root, python_executable=str(base_exe))
    _run_inside_venv(monkeypatch, copy_exe, venv_root)
    assert env_identity(snapshot_root) == preparer_identity

    # A compatible-target launch resolves to the same env directory either way.
    runtime_root = tmp_path / "runtime"
    assert env_dir_for(preparer_identity, runtime_root) == env_dir_for(
        env_identity(snapshot_root), runtime_root
    )


def test_changed_base_interpreter_keeps_distinct_identity(snapshot_root, tmp_path):
    old_base, old_copy, _ = _windows_style_prepared_env(tmp_path / "old", version="3.14.0")
    new_base, new_copy, _ = _windows_style_prepared_env(tmp_path / "new", version="3.15.0")

    old_identity = env_identity(snapshot_root, python_executable=str(old_base))
    new_identity = env_identity(snapshot_root, python_executable=str(new_base))
    assert old_identity != new_identity

    # Each env's copy still resolves to ITS OWN base — no cross-aliasing.
    assert dep_env.resolve_interpreter(str(old_copy)) == str(old_base.resolve())
    assert dep_env.resolve_interpreter(str(new_copy)) == str(new_base.resolve())


def test_self_provenance_version_mismatch_fails_visible(tmp_path, monkeypatch):
    """A base upgraded underneath a prepared env is a real provenance change."""
    _base, copy_exe, venv_root = _windows_style_prepared_env(tmp_path, version="3.14.0")
    _run_inside_venv(monkeypatch, copy_exe, venv_root, version="3.15.0")
    with pytest.raises(SnapshotError, match="changed under the environment"):
        dep_env.resolve_interpreter()


def test_self_provenance_prefix_mismatch_fails_visible(tmp_path, monkeypatch):
    """An interpreter claiming venv provenance it does not hold must refuse."""
    _base, copy_exe, venv_root = _windows_style_prepared_env(tmp_path)
    monkeypatch.setattr(sys, "executable", str(copy_exe))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "somewhere-else"))
    monkeypatch.setattr(dep_env.platform, "python_version", lambda: "3.14.0")
    with pytest.raises(SnapshotError, match="sys.prefix"):
        dep_env.resolve_interpreter()


def test_malformed_or_unverifiable_provenance_fails_visible(tmp_path):
    base_exe = tmp_path / "base-python" / "python.exe"
    base_exe.parent.mkdir(parents=True)
    base_exe.write_bytes(b"MZ-fake-base-interpreter")
    venv_root = tmp_path / "dep-envs" / "broken"
    scripts = venv_root / "Scripts"
    scripts.mkdir(parents=True)
    copy_exe = scripts / "python.exe"
    copy_exe.write_bytes(base_exe.read_bytes())

    # Missing home/version: malformed marker.
    (venv_root / "pyvenv.cfg").write_text("include-system-site-packages = false\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="malformed"):
        dep_env.resolve_interpreter(str(copy_exe))

    # Home points nowhere: unverifiable provenance.
    (venv_root / "pyvenv.cfg").write_text(
        f"home = {tmp_path / 'vanished'}\nversion = 3.14.0\nexecutable = {base_exe}\n",
        encoding="utf-8",
    )
    with pytest.raises(SnapshotError, match="does not exist"):
        dep_env.resolve_interpreter(str(copy_exe))

    # Home exists but holds no interpreter executable.
    (tmp_path / "empty-home").mkdir()
    (venv_root / "pyvenv.cfg").write_text(
        f"home = {tmp_path / 'empty-home'}\nversion = 3.14.0\n",
        encoding="utf-8",
    )
    with pytest.raises(SnapshotError, match="no interpreter executable"):
        dep_env.resolve_interpreter(str(copy_exe))


def test_venv_symlinked_python_still_resolves_through_symlink(tmp_path):
    """POSIX symlink-style venvs keep resolving to the real target (unchanged)."""
    real_python = tmp_path / "usr" / "bin" / "python3.14"
    real_python.parent.mkdir(parents=True)
    real_python.write_bytes(b"#!/bin/sh\n")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    link = venv_bin / "python"
    link.symlink_to(real_python)
    assert dep_env.resolve_interpreter(str(link)) == str(real_python.resolve())
