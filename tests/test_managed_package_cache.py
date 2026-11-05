from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def package():
    path = Path(__file__).resolve().parents[1] / "installer/velopack/package.py"
    spec = importlib.util.spec_from_file_location("coach_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_inputs_and_platform_invalidate_cache(package, tmp_path, monkeypatch):
    monkeypatch.setattr(package, "ROOT", tmp_path)
    dependency = tmp_path / "dependencies"
    dependency.write_text("PySide6==6.9")
    first = package.cache_key("osx-arm64", [dependency], "python-3.11")
    assert first == package.cache_key("osx-arm64", [dependency], "python-3.11")
    assert first != package.cache_key("win-x64", [dependency], "python-3.11")
    assert first != package.cache_key("osx-arm64", [dependency], "python-3.12")
    dependency.write_text("PySide6==6.10")
    assert first != package.cache_key("osx-arm64", [dependency], "python-3.11")


def test_incomplete_runtime_copy_is_not_a_cache_hit(package, tmp_path, monkeypatch):
    target = tmp_path / "ready"

    def interrupted(source, destination, **kwargs):
        destination.mkdir()
        (destination / "partial").touch()
        raise OSError("copy interrupted")

    monkeypatch.setattr(package.shutil, "copytree", interrupted)
    with pytest.raises(OSError):
        package.save_runtime_cache(tmp_path / "runtime", target)
    assert not target.exists()


def test_qualified_bridge_cache_avoids_compilation(package, tmp_path, monkeypatch):
    monkeypatch.setattr(package, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(package, "STAGE", tmp_path / "stage")
    monkeypatch.setattr(package.subprocess, "check_output", lambda *args, **kwargs: "compiler-sdk")
    source = package.ROOT / "spikes/mac-il2cpp/probe.cpp"
    key = package.cache_key(
        "osx-arm64",
        [source, source.parent / "tests/probe_batch_test.cpp", Path(package.__file__)],
        "compiler-sdkcompiler-sdk",
    )
    cached = package.CACHE / "native" / key / "libmtgacoach_probe.dylib"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"qualified bridge")
    run = Mock()
    monkeypatch.setattr(package, "run", run)
    package.build_mac_bridge("osx-arm64")
    library = package.STAGE / "native/libmtgacoach_probe.dylib"
    assert library.read_bytes() == b"qualified bridge"
    run.assert_called_once_with("codesign", "--verify", "--strict", library)


@pytest.mark.parametrize("stale", [False, True])
def test_package_qualification_rejects_stale_cached_code(package, tmp_path, stale):
    import arenamcp

    expected = Path(arenamcp.__file__).read_bytes()
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("arenamcp/__init__.py", expected + (b"\n# newer source" if stale else b""))
    if stale:
        with pytest.raises(subprocess.CalledProcessError):
            package.verify_installed_wheel(sys.executable, wheel)
    else:
        package.verify_installed_wheel(sys.executable, wheel)
