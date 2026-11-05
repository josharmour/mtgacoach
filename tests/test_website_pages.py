"""mtgacoach.com public pages: the landing and subscribe templates render, every
asset they reference is served from website/static, and the Patreon links the
subscribe page hands out are unchanged."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from tests.website_app import WEBSITE_DIR, load_website_app

PATREON_OAUTH = (
    "https://www.patreon.com/oauth2/authorize?response_type=code&client_id="
    "uhositmCwj3sei5XK9HBYOPxZF6-eiCJEr4xdNWcUX3TOQqAjQvhhe5NPc7LFWB0&redirect_uri="
    "https%3A%2F%2Fmtgacoach.com%2Fpatreon%2Fcallback&scope=identity%20identity%5Bemail%5D%20identity.memberships"
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    # The site resolves templates/ and static/ against its working directory.
    (tmp_path / "templates").symlink_to(WEBSITE_DIR / "templates", target_is_directory=True)
    (tmp_path / "static").symlink_to(WEBSITE_DIR / "static", target_is_directory=True)
    app, _db, _patreon = load_website_app(tmp_path, monkeypatch)
    with TestClient(app.app) as test_client:
        yield test_client


def _local_assets(html: str) -> set[str]:
    return set(re.findall(r'(?:src|href)="(/static/[^"?#]+)', html))


@pytest.mark.parametrize("path", ["/", "/subscribe"])
def test_page_renders_and_its_static_assets_are_served(client, path) -> None:
    response = client.get(path)
    assert response.status_code == 200
    html = response.text
    assets = _local_assets(html)
    assert "/static/css/site.css" in assets
    for asset in assets:
        assert client.get(asset).status_code == 200, asset
    for tag in re.findall(r"<img\b[^>]*>", html):
        assert re.search(r'\balt="[^"]+"', tag), tag


def test_landing_page_content(client) -> None:
    html = client.get("/").text
    assert "A coach in your ear" in html
    assert "https://arenaonair.com" in html
    assert "7 days" in html
    feed = (
        "https://github.com/josharmour/mtgacoach/releases/download/coach-master-feed/MTGACoachMaster-master-"
    )
    for artifact in ("win-x64-Setup.exe", "osx-arm64-Setup.pkg", "linux-x64.AppImage"):
        assert f'href="{feed}{artifact}"' in html
    assert "osx-x64-Setup.pkg" not in html
    assert "macOS (Apple Silicon)" in html
    assert "Version 3.8.3" not in html
    assert 'data-release-summary="master"' in html
    assert 'data-release-platform="osx-arm64"' in html
    assert "coach-stable-feed" in html
    assert "unsigned" in html
    for clip in ("coach-turn.mp3", "coach-lethal.mp3", "coach-draft.mp3"):
        assert f"/static/audio/{clip}" in html


def test_download_versions_endpoint_is_public(client, monkeypatch) -> None:
    import download_versions

    async def published():
        return {"master": {"osx-arm64": "1.0.26"}, "stable": {}}

    monkeypatch.setattr(download_versions, "published_versions", published)
    response = client.get("/downloads/versions")
    assert response.status_code == 200
    assert response.json() == {"master": {"osx-arm64": "1.0.26"}, "stable": {}}
    assert response.headers["cache-control"] == "public, max-age=60"


def test_subscribe_page_keeps_its_patreon_links(client) -> None:
    html = client.get("/subscribe").text
    assert 'href="https://www.patreon.com/c/mtgacoach"' in html
    assert f'href="{PATREON_OAUTH}"' in html
