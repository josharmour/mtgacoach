"""Guards for the release-version plumbing.

Every one of these covers a defect that shipped: the version literal moved
into ``src/arenamcp/__init__.py`` when pyproject.toml switched to hatch
dynamic versioning (commit 8a274d2), but three consumers kept parsing
``version = "..."`` out of pyproject.toml — where that line no longer
exists. The Windows installer build threw "Could not read version" and the
.iss carried a hardcoded fallback that silently stamped the *previous*
version onto every installer built after a bump.

These are cheap text assertions on purpose: the real consumers are
PowerShell and Inno Setup, neither of which can run in pytest, so the thing
we can still verify from here is that their version-extraction patterns
actually match the file that holds the version.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import arenamcp
from arenamcp.desktop import runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_PY = REPO_ROOT / "src" / "arenamcp" / "__init__.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
BUILD_PS1 = REPO_ROOT / "installer" / "build-installer.ps1"
ISS = REPO_ROOT / "installer" / "mtgacoach.iss"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# A PowerShell single-quoted regex literal, e.g.
#   -Pattern '^\s*__version__\s*=\s*"([^"]+)"'
_PS_PATTERN = re.compile(r"-Pattern\s+'([^']+)'")


def _ps_version_patterns(script: str) -> list[str]:
    """Every ``-Pattern '...'`` regex literal in a PowerShell snippet."""
    return _PS_PATTERN.findall(script)


# ---------------------------------------------------------------------------
# The premise: exactly one source of truth for the version.
# ---------------------------------------------------------------------------


def test_pyproject_uses_dynamic_versioning_and_has_no_literal() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    literal = re.search(r"^version\s*=\s*[\"']", text, re.MULTILINE)
    assert literal is None, (
        "pyproject.toml grew a literal version line again — that creates a "
        "second source of truth that will drift from src/arenamcp/__init__.py"
    )


def test_init_py_version_is_parseable() -> None:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', INIT_PY.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    assert match.group(1) == arenamcp.__version__


# ---------------------------------------------------------------------------
# Consumer 1: installer/build-installer.ps1
# ---------------------------------------------------------------------------


def test_build_installer_ps1_extracts_the_real_version() -> None:
    script = BUILD_PS1.read_text(encoding="utf-8")
    patterns = _ps_version_patterns(script)
    assert patterns, "build-installer.ps1 no longer has a -Pattern version regex"

    init_text = INIT_PY.read_text(encoding="utf-8")
    extracted = [m.group(1) for p in patterns if (m := re.search(p, init_text, re.MULTILINE)) is not None]
    assert arenamcp.__version__ in extracted, (
        f"none of {patterns!r} extracts {arenamcp.__version__!r} from "
        f"src/arenamcp/__init__.py — the installer build will throw"
    )


def test_build_installer_ps1_does_not_read_version_from_pyproject() -> None:
    script = BUILD_PS1.read_text(encoding="utf-8")
    for line in script.splitlines():
        if "-Pattern" in line:
            assert "pyproject" not in line.lower(), (
                "pyproject.toml has no version line (hatch dynamic versioning); "
                "read __version__ from src/arenamcp/__init__.py instead"
            )


# ---------------------------------------------------------------------------
# Consumer 2: .github/workflows/installer.yml
# ---------------------------------------------------------------------------


def test_installer_workflow_extracts_the_real_version() -> None:
    workflow = (WORKFLOWS / "installer.yml").read_text(encoding="utf-8")
    patterns = _ps_version_patterns(workflow)
    assert patterns, "installer.yml no longer has a -Pattern version regex"

    init_text = INIT_PY.read_text(encoding="utf-8")
    extracted = [m.group(1) for p in patterns if (m := re.search(p, init_text, re.MULTILINE)) is not None]
    assert arenamcp.__version__ in extracted, (
        f"none of {patterns!r} extracts {arenamcp.__version__!r} from "
        f"src/arenamcp/__init__.py — the workflow_dispatch fallback is dead"
    )


def test_installer_workflow_does_not_read_version_from_pyproject() -> None:
    workflow = (WORKFLOWS / "installer.yml").read_text(encoding="utf-8")
    for line in workflow.splitlines():
        if "-Pattern" in line:
            assert "pyproject" not in line.lower()


# ---------------------------------------------------------------------------
# Consumer 3: installer/mtgacoach.iss
# ---------------------------------------------------------------------------


def test_iss_has_no_hardcoded_version_fallback() -> None:
    """A literal ``#define AppVersion "x.y.z"`` goes stale on the next bump
    and stamps the wrong number with nothing failing."""
    text = ISS.read_text(encoding="utf-8")
    hardcoded = re.search(r'^\s*#define\s+AppVersion\s+"', text, re.MULTILINE)
    assert hardcoded is None, (
        "mtgacoach.iss hardcodes AppVersion again — it will silently stamp a "
        "stale version onto the installer after the next version bump"
    )
    assert "#error" in text, "mtgacoach.iss must fail loudly when AppVersion is not passed in"


def test_release_builders_pass_appversion_to_iss() -> None:
    """Since the .iss refuses to guess, both builders must supply it."""
    assert "/DAppVersion=" in BUILD_PS1.read_text(encoding="utf-8")
    assert "/DAppVersion=" in (WORKFLOWS / "installer.yml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Consumer 4: desktop title bar
# ---------------------------------------------------------------------------


def test_read_version_matches_package() -> None:
    assert runtime.read_version() == arenamcp.__version__


def test_read_version_dev_fallback_parses_init_py(monkeypatch, tmp_path: Path) -> None:
    """With the package version and wheel metadata both unavailable, the dev
    fallback must still find the version — it used to parse pyproject.toml,
    which stopped carrying one, so it could only return "unknown"."""

    def _boom(_name: str) -> str:
        raise RuntimeError("no distribution metadata")

    monkeypatch.setattr(arenamcp, "__version__", "")
    monkeypatch.setattr("importlib.metadata.version", _boom)

    package_dir = tmp_path / "src" / "arenamcp"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(runtime, "get_app_root", lambda: str(tmp_path))

    assert runtime.read_version() == "9.9.9"


def test_read_version_does_not_fall_back_to_pyproject(monkeypatch, tmp_path: Path) -> None:
    def _boom(_name: str) -> str:
        raise RuntimeError("no distribution metadata")

    monkeypatch.setattr(arenamcp, "__version__", "")
    monkeypatch.setattr("importlib.metadata.version", _boom)

    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n', encoding="utf-8")
    monkeypatch.setattr(runtime, "get_app_root", lambda: str(tmp_path))

    assert runtime.read_version() == "unknown"


# ---------------------------------------------------------------------------
# CI shape: one pytest run, one ruff run.
# ---------------------------------------------------------------------------


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def test_only_one_workflow_runs_the_test_suite() -> None:
    """tests.yml and ci.yml both ran the full ~700-test suite on every push
    to master, with different Python versions and different extras."""
    runners = [p.name for p in _workflow_files() if "pytest tests" in p.read_text(encoding="utf-8")]
    assert runners == ["tests.yml"], f"expected exactly one pytest workflow, found {runners}"


def test_only_one_workflow_runs_ruff() -> None:
    runners = [p.name for p in _workflow_files() if "ruff check" in p.read_text(encoding="utf-8")]
    assert len(runners) == 1, f"expected exactly one ruff workflow, found {runners}"


def test_ruff_pin_matches_pre_commit() -> None:
    """A stale pin fails CI on correctly-formatted code: 0.15.2 formats
    differently than 0.15.22, and 0.6.9 cannot even parse the UP045
    selector in [tool.ruff.lint]. Bump both together."""
    workflow = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
    ci_pin = re.search(r'ruff-action@[^\n]*\n\s*with:\s*\n\s*version:\s*"([^"]+)"', workflow)
    assert ci_pin is not None, "tests.yml no longer pins a ruff version"

    pre_commit = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook_pin = re.search(r"ruff-pre-commit\s*\n\s*rev:\s*v?([0-9][^\s]*)", pre_commit)
    if hook_pin is None:
        pytest.skip("no ruff-pre-commit hook configured")

    assert ci_pin.group(1) == hook_pin.group(1), (
        f"CI ruff pin {ci_pin.group(1)} != pre-commit ruff rev {hook_pin.group(1)}"
    )


def test_workflows_are_valid_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    for path in _workflow_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
        assert "jobs" in data, f"{path.name} has no jobs"
