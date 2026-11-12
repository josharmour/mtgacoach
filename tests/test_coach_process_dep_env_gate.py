"""Regression gate: engine starts never prepare dependencies implicitly.

``resolve_engine_launch`` must keep the resolved interpreter and touch no
dependency environment unless this launch is an explicitly selected snapshot
(``MTGACOACH_SNAPSHOT_SHA`` set and snapshots enabled) whose SHA, selected
root and published manifest all AGREE — and even then it may only consume an
ALREADY PREPARED environment, failing visibly when it is missing instead of
installing anything as a launch side effect.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from arenamcp.desktop import dep_env
from arenamcp.desktop.coach_process import engine_interpreter_for, resolve_engine_launch
from arenamcp.desktop.dep_env import (
    ENV_MARKER_NAME,
    dependency_fingerprint,
    env_dir_for,
    env_identity,
)
from arenamcp.desktop.runtime_snapshots import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    _content_digest,
    _entry_hash,
    _iter_snapshot_files,
)

COMMIT = "c" * 40


def _write_manifest(root: Path, commit: str) -> None:
    """Publish a valid manifest for ``root`` so selection agreement can pass."""
    files = {rel: _entry_hash(root / rel) for rel in _iter_snapshot_files(root) if rel != MANIFEST_NAME}
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "commit": commit,
        "created_utc": "1970-01-01T00:00:00Z",
        "source": "git-archive",
        "excluded": [],
        "dependency_fingerprint": dependency_fingerprint(root),
        "files": files,
        "content_hash": _content_digest(files),
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture()
def snapshot_root(tmp_path: Path) -> Path:
    """A minimal PUBLISHED snapshot tree (manifest included) for ``COMMIT``."""
    root = tmp_path / "snapshots" / f"{COMMIT[:12]}-deadbeef"
    (root / "src" / "arenamcp").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "mtgacoach"\n', encoding="utf-8")
    (root / "requirements.txt").write_text("PySide6==6.7.0\n", encoding="utf-8")
    (root / "src" / "arenamcp" / "__init__.py").write_text("", encoding="utf-8")
    _write_manifest(root, COMMIT)
    return root


@pytest.fixture()
def isolated_runtime(monkeypatch, tmp_path: Path) -> Path:
    """Point the runtime root at a throwaway dir; no dev-checkout shortcut."""
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    monkeypatch.setenv("MTGACOACH_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.delenv("MTGACOACH_DEV_CHECKOUT", raising=False)
    return runtime_root


@pytest.fixture()
def dep_env_guard(monkeypatch):
    """Record every ensure_dep_env call while keeping real lookup behavior."""
    calls: list[dict] = []
    real = dep_env.ensure_dep_env

    def guarded(snapshot_root, **kwargs):
        calls.append({"root": str(snapshot_root), **kwargs})
        return real(snapshot_root, **kwargs)

    monkeypatch.setattr(dep_env, "ensure_dep_env", guarded)
    return calls


def _clear_selection(monkeypatch) -> None:
    for var in ("MTGACOACH_SNAPSHOT_SHA", "MTGACOACH_SNAPSHOT_ROOT"):
        monkeypatch.delenv(var, raising=False)


def _prepare_env_for(root: Path, python_executable: str) -> Path:
    """Fabricate a validly-marked prepared environment for ``(root, interp)``."""
    identity = env_identity(root, python_executable=python_executable)
    env_dir = env_dir_for(identity)
    (env_dir / "bin").mkdir(parents=True)
    (env_dir / "bin" / "python").write_text("", encoding="utf-8")
    marker = {
        "schema": dep_env.MARKER_SCHEMA,
        "identity": identity,
        "fingerprint": dependency_fingerprint(root),
        "python": str(Path(python_executable).resolve()),
    }
    (env_dir / ENV_MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    return env_dir


# ---------------------------------------------------------------------------
# ordinary launches: no dependency work at all


def test_legacy_source_launch_touches_no_dependency_environment(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch
):
    _clear_selection(monkeypatch)

    python_exe, python_source, args, packaged = resolve_engine_launch(snapshot_root)

    assert dep_env_guard == [], "ordinary launch must never consult dep_env"
    assert python_source == "current"
    # Production preserves the virtualenv path verbatim (never resolves it to
    # the base interpreter); the expectation must match that exactly.
    assert Path(python_exe) == Path(sys.executable), (
        f"legacy launch must keep the exact current interpreter: {python_exe!r}"
    )
    assert packaged is False
    assert "-m" in args and "arenamcp.standalone" in args and "--pipe" in args


def test_absent_pointer_launch_touches_no_dependency_environment(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch
):
    """No SHA and no published pointer: identical to the legacy path."""
    _clear_selection(monkeypatch)

    python_exe, python_source, _args, _packaged = resolve_engine_launch(snapshot_root)

    assert dep_env_guard == []
    assert python_source == "current"


def test_disabled_snapshots_launch_touches_no_dependency_environment(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch
):
    """Even with a selection SHA present, the disable flag wins."""
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", COMMIT)
    monkeypatch.setenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", "1")

    _python_exe, python_source, _args, _packaged = resolve_engine_launch(snapshot_root)

    assert dep_env_guard == []
    assert python_source == "current"


# ---------------------------------------------------------------------------
# explicitly selected snapshot: prepared environment only


def test_selected_snapshot_binds_prepared_environment(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch
):
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", COMMIT)
    # Explicit root selection agreeing with the launch root is also accepted.
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_ROOT", str(snapshot_root))
    monkeypatch.delenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", raising=False)
    interp = engine_interpreter_for(snapshot_root)[0]
    env_dir = _prepare_env_for(snapshot_root, interp)

    python_exe, python_source, _args, _packaged = resolve_engine_launch(snapshot_root)

    assert len(dep_env_guard) == 1
    assert dep_env_guard[0]["allow_create"] is False, "lookup only — never create"
    assert python_source.startswith("dep_env:")
    assert Path(python_exe) == env_dir / "bin" / "python"


def test_selected_snapshot_sha_manifest_disagreement_launches_plain(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch
):
    """A SHA that disagrees with the published manifest binds NOTHING."""
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", "d" * 40)
    monkeypatch.delenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", raising=False)

    _python_exe, python_source, _args, _packaged = resolve_engine_launch(snapshot_root)

    assert dep_env_guard == [], "disagreeing selection must not consult dep_env"
    assert python_source == "current"


def test_selected_snapshot_foreign_root_launches_plain(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch, tmp_path
):
    """A SNAPSHOT_ROOT that is not the launch root binds NOTHING."""
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", COMMIT)
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_ROOT", str(tmp_path / "elsewhere"))
    monkeypatch.delenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", raising=False)

    _python_exe, python_source, _args, _packaged = resolve_engine_launch(snapshot_root)

    assert dep_env_guard == []
    assert python_source == "current"


# ---------------------------------------------------------------------------
# tampered selected source: never execute the rejected root


def test_tampered_launch_root_refuses_launch(snapshot_root, isolated_runtime, dep_env_guard, monkeypatch):
    """Hash-tampered source AT the launch root must never be executed.

    The disagreement is detected AND the launch root is the rejected tree, so
    resolve_engine_launch refuses outright instead of returning ordinary
    launch args for the very source that failed validation.
    """
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", COMMIT)
    monkeypatch.delenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", raising=False)
    (snapshot_root / "src" / "arenamcp" / "__init__.py").write_text(
        "# tampered after publication\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="Refusing to launch"):
        resolve_engine_launch(snapshot_root)

    assert dep_env_guard == [], "rejected source must never reach dep_env"


def test_tampered_manifest_at_launch_root_refuses_launch(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch
):
    """An unreadable/tampered manifest at the launch root also refuses."""
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", COMMIT)
    monkeypatch.delenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", raising=False)
    (snapshot_root / MANIFEST_NAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Refusing to launch"):
        resolve_engine_launch(snapshot_root)

    assert dep_env_guard == []


def test_tampered_external_root_does_not_block_the_checkout(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch, tmp_path
):
    """Control: a rejected EXTERNAL selection root only downgrades to plain.

    The checkout being launched was never rejected, so the ordinary launch of
    it proceeds — refusal is reserved for when the rejected source IS the
    launch root.
    """
    external = tmp_path / "external-snap"
    shutil.copytree(snapshot_root, external)
    (external / "src" / "arenamcp" / "__init__.py").write_text(
        "# tampered after publication\n", encoding="utf-8"
    )
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", COMMIT)
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_ROOT", str(external))
    monkeypatch.delenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", raising=False)

    _python_exe, python_source, _args, _packaged = resolve_engine_launch(snapshot_root)

    assert dep_env_guard == []
    assert python_source == "current"


def test_missing_prepared_environment_fails_visibly_without_installing(
    snapshot_root, isolated_runtime, dep_env_guard, monkeypatch
):
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", COMMIT)
    monkeypatch.delenv("MTGACOACH_DISABLE_RUNTIME_SNAPSHOT", raising=False)

    with pytest.raises(RuntimeError, match="no prepared dependency environment"):
        resolve_engine_launch(snapshot_root)

    assert len(dep_env_guard) == 1
    assert dep_env_guard[0]["allow_create"] is False


# ---------------------------------------------------------------------------
# prepared_dep_env accessor contract


def test_prepared_dep_env_returns_none_without_creating_anything(snapshot_root, isolated_runtime):
    result = dep_env.prepared_dep_env(snapshot_root)

    assert result is None
    envs = list((isolated_runtime / "dep-envs").glob("*")) if (isolated_runtime / "dep-envs").exists() else []
    assert envs == [], "lookup must not create any environment directory"


def test_prepared_dep_env_finds_matching_environment(snapshot_root, isolated_runtime):
    interp = engine_interpreter_for(snapshot_root)[0]
    env_dir = _prepare_env_for(snapshot_root, interp)

    assert dep_env.prepared_dep_env(snapshot_root) == env_dir


def _make_interpreter_shim(path: Path, version: str = "3.14.0") -> Path:
    """A real executable reporting ``version`` (stands in for a Python binary)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_prepared_dep_env_rejects_foreign_interpreter(snapshot_root, isolated_runtime, tmp_path):
    """An env built for another interpreter must not be reused."""
    other_python = _make_interpreter_shim(tmp_path / "other-base" / "other-python")
    _prepare_env_for(snapshot_root, str(other_python))

    assert dep_env.prepared_dep_env(snapshot_root) is None


def test_prepared_dep_env_rejects_changed_dependencies(snapshot_root, isolated_runtime):
    """A dependency bump invalidates the old environment (uv.lock included)."""
    interp = engine_interpreter_for(snapshot_root)[0]
    _prepare_env_for(snapshot_root, interp)
    (snapshot_root / "requirements.txt").write_text("PySide6==6.8.0\n", encoding="utf-8")

    assert dep_env.prepared_dep_env(snapshot_root) is None


# ---------------------------------------------------------------------------
# os.environ sanity for the guard fixtures


def test_selection_vars_isolated_between_tests(monkeypatch):
    _clear_selection(monkeypatch)
    assert os.environ.get("MTGACOACH_SNAPSHOT_SHA") is None
