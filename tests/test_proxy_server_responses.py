from __future__ import annotations

import copy
import importlib.util
import json
import sys
import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return copy.deepcopy(self._payload)


class _FakeProvider:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list[dict] = []
        self.name = "fake"

    async def forward_chat(self, body: dict, _client) -> _FakeResponse:
        self.requests.append(copy.deepcopy(body))
        if not self.payloads:
            raise AssertionError("No fake payload left for forward_chat")
        return _FakeResponse(self.payloads.pop(0))

    def mark_success(self) -> None:
        return None

    def mark_failure(self) -> None:
        return None


def _load_proxy_app(tmp_path: Path, monkeypatch, fake_provider: _FakeProvider):
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

    module_name = "proxy_app_responses_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, proxy_dir / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    module.router.select_provider = lambda _model=None: fake_provider
    module.router.get_all_models = lambda: [{"id": "gpt-5.4", "object": "model", "owned_by": "fake"}]
    return module, sys.modules["db"]


def _usage() -> dict:
    return {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def test_responses_create_and_previous_response_id_round_trip(tmp_path: Path, monkeypatch) -> None:
    provider = _FakeProvider([
        {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "created": 1000,
            "model": "gpt-5.4-2026-03-05",
            "choices": [{"message": {"role": "assistant", "content": "alpha"}}],
            "usage": _usage(),
        },
        {
            "id": "chatcmpl_2",
            "object": "chat.completion",
            "created": 1001,
            "model": "gpt-5.4-2026-03-05",
            "choices": [{"message": {"role": "assistant", "content": "beta"}}],
            "usage": _usage(),
        },
    ])
    proxy_app, proxy_db = _load_proxy_app(tmp_path, monkeypatch, provider)
    existing = proxy_db.create_subscriber(email="coder@example.com", name="Coder", days=30)
    auth = {"Authorization": f"Bearer {existing['license_key']}"}

    with TestClient(proxy_app.app) as client:
        first = client.post("/v1/responses", headers=auth, json={"model": "gpt-5.4", "input": "hello"})
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["object"] == "response"
        assert first_data["output"][0]["type"] == "message"
        assert first_data["output"][0]["content"][0]["text"] == "alpha"

        response_id = first_data["id"]
        second = client.post(
            "/v1/responses",
            headers=auth,
            json={"model": "gpt-5.4", "previous_response_id": response_id, "input": "next"},
        )
        assert second.status_code == 200
        second_data = second.json()
        assert second_data["output"][0]["content"][0]["text"] == "beta"

        retrieved = client.get(f"/v1/responses/{response_id}", headers=auth)
        assert retrieved.status_code == 200
        assert retrieved.json()["id"] == response_id

    assert provider.requests[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert provider.requests[1]["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "alpha"},
        {"role": "user", "content": "next"},
    ]


def test_responses_builtin_local_shell_tool_translation(tmp_path: Path, monkeypatch) -> None:
    provider = _FakeProvider([
        {
            "id": "chatcmpl_tool",
            "object": "chat.completion",
            "created": 2000,
            "model": "gpt-5.4-2026-03-05",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_local_1",
                        "type": "function",
                        "function": {
                            "name": "__mtgacoach_local_shell",
                            "arguments": json.dumps({
                                "type": "exec",
                                "command": ["git", "status"],
                                "env": {},
                            }),
                        },
                    }],
                },
            }],
            "usage": _usage(),
        },
    ])
    proxy_app, proxy_db = _load_proxy_app(tmp_path, monkeypatch, provider)
    existing = proxy_db.create_subscriber(email="tooler@example.com", name="Tooler", days=30)
    auth = {"Authorization": f"Bearer {existing['license_key']}"}

    with TestClient(proxy_app.app) as client:
        response = client.post(
            "/v1/responses",
            headers=auth,
            json={
                "model": "gpt-5.4",
                "input": "check repo state",
                "tools": [{"type": "local_shell"}],
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["output"][0]["type"] == "local_shell_call"
    assert data["output"][0]["id"] == "call_local_1"
    assert data["output"][0]["action"]["command"] == ["git", "status"]
    assert provider.requests[0]["tools"][0]["function"]["name"] == "__mtgacoach_local_shell"


def test_responses_stream_returns_synthetic_sse_events(tmp_path: Path, monkeypatch) -> None:
    provider = _FakeProvider([
        {
            "id": "chatcmpl_stream",
            "object": "chat.completion",
            "created": 3000,
            "model": "gpt-5.4-2026-03-05",
            "choices": [{"message": {"role": "assistant", "content": "streamed text"}}],
            "usage": _usage(),
        },
    ])
    proxy_app, proxy_db = _load_proxy_app(tmp_path, monkeypatch, provider)
    existing = proxy_db.create_subscriber(email="stream@example.com", name="Stream", days=30)
    auth = {"Authorization": f"Bearer {existing['license_key']}"}

    with TestClient(proxy_app.app) as client:
        with client.stream(
            "POST",
            "/v1/responses",
            headers=auth,
            json={"model": "gpt-5.4", "input": "hello", "stream": True},
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())

    assert '"type": "response.created"' in body
    assert '"type": "response.output_text.delta"' in body
    assert '"type": "response.completed"' in body
    assert "streamed text" in body
