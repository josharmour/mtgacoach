from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from arenamcp.desktop.build_info import window_title


@pytest.fixture
def stamper():
    path = Path(__file__).resolve().parents[1] / "scripts/set_build_version.py"
    spec = importlib.util.spec_from_file_location("coach_version_stamp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stamp_preserves_source_and_sets_only_release_version(stamper, tmp_path):
    source = tmp_path / "__init__.py"
    source.write_bytes(b'other = "3.8.3"\r\n__version__ = "3.8.3"\r\n')
    stamper.stamp_version(source, "1.0.27")
    assert source.read_bytes() == b'other = "3.8.3"\r\n__version__ = "1.0.27"\r\n'


@pytest.mark.parametrize("version", ["v1.0.27", "1.0.27\n", "1.0.27;echo bad", "01.0.27"])
def test_invalid_release_version_does_not_change_source(stamper, tmp_path, version):
    source = tmp_path / "__init__.py"
    original = b'__version__ = "3.8.3"\n'
    source.write_bytes(original)
    with pytest.raises(ValueError):
        stamper.stamp_version(source, version)
    assert source.read_bytes() == original


def test_managed_title_shows_version_once(monkeypatch):
    monkeypatch.delenv("MTGACOACH_DEV_CHECKOUT", raising=False)
    monkeypatch.setenv("MTGACOACH_BUILD_LABEL", "master-osx-arm64 1.0.27 (abc12345)")
    assert window_title("1.0.27") == "MTGA Coach 1.0.27 · master-osx-arm64 (abc12345)"


def test_dev_title_uses_commit_instead_of_release_number(monkeypatch):
    monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", "/source")
    monkeypatch.setenv("MTGACOACH_BUILD_LABEL", "dev source (abc12345)")
    assert window_title("3.8.3") == "MTGA Coach — Dev (abc12345)"
