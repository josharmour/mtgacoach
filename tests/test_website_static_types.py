"""Static assets carry real content types (2026-10-18: .webp went out as octet-stream)."""

from pathlib import Path

from tests.website_app import load_website_app

WEBSITE = Path(__file__).resolve().parents[1] / "website"


def test_webp_screenshots_are_served_as_images(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    img = tmp_path / "static" / "img"
    img.mkdir(parents=True)
    (img / "sidebar-coaching.webp").write_bytes((WEBSITE / "static/img/sidebar-coaching.webp").read_bytes())
    module, _db, _patreon = load_website_app(tmp_path, monkeypatch)
    response = TestClient(module.app).get("/static/img/sidebar-coaching.webp")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
