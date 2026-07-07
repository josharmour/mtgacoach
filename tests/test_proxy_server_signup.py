from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _load_proxy_app(tmp_path: Path, monkeypatch):
    proxy_dir = Path(__file__).resolve().parents[1] / "website"
    config_path = tmp_path / "config.yaml"

    (tmp_path / "static").mkdir()
    (tmp_path / "templates").mkdir()
    config_path.write_text(
        """
server:
  host: "127.0.0.1"
  port: 8443
providers: []
default_model: "gpt-5.4"
admin:
  username: "admin"
  password: "test-admin"
database:
  path: "./data/mtgacoach.db"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.syspath_prepend(str(proxy_dir))

    for name in ("db", "providers"):
        sys.modules.pop(name, None)

    module_name = "proxy_app_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, proxy_dir / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    return module, sys.modules["db"]


def test_public_subscribe_request_no_longer_mints_or_leaks_keys(tmp_path: Path, monkeypatch) -> None:
    proxy_app, proxy_db = _load_proxy_app(tmp_path, monkeypatch)
    existing = proxy_db.create_subscriber(email="alice@example.com", name="Alice", days=30)

    with TestClient(proxy_app.app) as client:
        response = client.post(
            "/subscribe/request",
            json={"email": "alice@example.com", "name": "Alice"},
        )

    assert response.status_code == 403
    assert "Self-service key issuance is disabled" in response.json()["detail"]
    assert existing["license_key"] not in response.text
    subscribers = proxy_db.list_subscribers()
    assert len(subscribers) == 1
    assert subscribers[0]["license_key"] == existing["license_key"]


def test_existing_subscriber_keys_still_validate_after_signup_lockdown(tmp_path: Path, monkeypatch) -> None:
    proxy_app, proxy_db = _load_proxy_app(tmp_path, monkeypatch)
    existing = proxy_db.create_subscriber(email="existing@example.com", name="Existing", days=30)
    key = existing["license_key"]

    with TestClient(proxy_app.app) as client:
        sub_response = client.post(
            "/v1/subscription/check",
            headers={"Authorization": f"Bearer {key}"},
            json={"license_key": key},
        )
        model_response = client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )

    assert sub_response.status_code == 200
    assert sub_response.json()["status"] == "active"
    assert model_response.status_code == 200
    assert model_response.json() == {"object": "list", "data": []}
