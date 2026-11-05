import asyncio

import httpx
import pytest
from tests.website_app import load_website_module


@pytest.fixture
def versions(monkeypatch):
    return load_website_module(monkeypatch, "download_versions", "download_versions.py")


def test_versions_use_full_packages_in_numeric_order(versions):
    assert (
        versions.latest_version(
            {
                "Assets": [
                    {"Type": "Full", "Version": "1.0.9"},
                    {"Type": "Full", "Version": "1.0.26"},
                    {"Type": "Delta", "Version": "1.0.99"},
                    {"Type": "Full", "Version": "<script>bad</script>"},
                ]
            }
        )
        == "1.0.26"
    )


def test_partial_platform_publication_and_network_failure(versions, monkeypatch):
    requests = []

    def response(request):
        requests.append(request)
        if "stable" in request.url.path:
            return httpx.Response(404)
        version = "1.0.27" if "osx-arm64" in request.url.path else "1.0.26"
        return httpx.Response(200, json={"Assets": [{"Type": "Full", "Version": version}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        versions.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(response), **kwargs),
    )

    async def exercise():
        current = await versions.published_versions()
        assert current == {
            "master": {"osx-arm64": "1.0.27", "win-x64": "1.0.26", "linux-x64": "1.0.26"},
            "stable": {},
        }
        assert await versions.published_versions() == current
        assert len(requests) == 6  # The second page request uses the shared cache.
        assert all(request.url.host == "github.com" for request in requests)
        assert all("authorization" not in request.headers for request in requests)

    asyncio.run(exercise())


def test_failed_refresh_keeps_last_published_version(versions, monkeypatch):
    versions._versions["master"]["osx-arm64"] = "1.0.26"

    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        versions.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(unavailable), **kwargs),
    )
    assert asyncio.run(versions.published_versions()) == {
        "master": {"osx-arm64": "1.0.26"},
        "stable": {},
    }
