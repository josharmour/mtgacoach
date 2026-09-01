"""Shared test mock objects and fakes."""

import json
from typing import Any


class FakeResponse:
    """Fake HTTP response for mock HTTP clients."""

    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeLiteLLM:
    """Stands in for LiteLLM HTTP client — records /key/generate and /key/delete."""

    def __init__(self, key_prefix: str = "sk-fake-"):
        self.minted = []
        self.deleted = []
        self.fail_alias_once = False
        self.key_prefix = key_prefix

    async def post(self, url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None, **kw):
        if url.endswith("/key/generate"):
            alias = json.get("key_alias") if json else ""
            if self.fail_alias_once:
                self.fail_alias_once = False
                return FakeResponse(400, {"error": "alias already exists"})
            key = f"{self.key_prefix}{len(self.minted)}"
            self.minted.append({"alias": alias, "payload": json, "headers": headers or {}, "key": key})
            return FakeResponse(200, {"key": key})
        if url.endswith("/key/delete"):
            deleted_keys = json.get("keys", []) if json else []
            self.deleted.extend(deleted_keys)
            return FakeResponse(200, {"deleted_keys": deleted_keys})
        raise AssertionError(f"unexpected POST {url}")
