import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def versions():
    path = Path(__file__).resolve().parents[1] / "scripts/release_version.py"
    spec = importlib.util.spec_from_file_location("coach_release_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifests(versions):
    return {
        rid: {"platform": rid, "lane": "master", "version": "4.0.33", "commit": "a" * 40}
        for rid in versions.PLATFORMS
    }


def test_master_and_stable_keep_one_version(versions):
    master = versions.master_version(33)
    assert master == "4.0.33"
    assert versions.promotion_version("v" + master, "a" * 40, manifests(versions)) == master
    assert versions.master_version(34) == "4.0.34"


@pytest.mark.parametrize("tag", ["v3.8.4", "v1.0.30", "4.0.33", "v4.0.033", "v4.0.33\n"])
def test_promotion_rejects_legacy_or_invalid_tags(versions, tag):
    with pytest.raises(ValueError):
        versions.promotion_version(tag, "a" * 40, manifests(versions))


@pytest.mark.parametrize(
    "field,value", [("version", "4.0.32"), ("commit", "b" * 40), ("lane", "stable"), ("platform", "wrong")]
)
def test_promotion_rejects_partial_or_mismatched_master_publication(versions, field, value):
    published = manifests(versions)
    published["win-x64"][field] = value
    with pytest.raises(ValueError, match="win-x64"):
        versions.promotion_version("v4.0.33", "a" * 40, published)


def test_promotion_requires_every_supported_platform(versions):
    published = manifests(versions)
    del published["osx-arm64"]
    with pytest.raises(ValueError, match="osx-arm64"):
        versions.promotion_version("v4.0.33", "a" * 40, published)


def test_master_requires_positive_run_number(versions):
    with pytest.raises(ValueError):
        versions.master_version(0)
