import io
import json
import urllib.error

from arenamcp import gateway_models as gm


def setup_function():
    gm._cache.clear()


def install(monkeypatch, ids=None, error=None):
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append((req.full_url, req.get_header("Authorization"), timeout))
            if error:
                raise error
            return io.BytesIO(json.dumps({"data": [{"id": m} for m in ids]}).encode())

    monkeypatch.setattr(gm.urllib.request, "build_opener", lambda *a: Opener())
    return calls


def test_prefers_current_and_caches_by_gateway_and_key(monkeypatch):
    calls = install(monkeypatch, ["unrelated-first", "current", "glm-5.3-flash"])
    for _ in range(2):
        assert gm.current_models("https://api.mtgacoach.com/v1", "test", {}) == [
            ("Current hosted model", "current")
        ]
    assert calls == [("https://api.mtgacoach.com/v1/models", "Bearer test", 3)]
    gm.current_models("http://localhost:8444/v1", "other", {})
    assert len(calls) == 2


def test_authoritative_missing_current_never_selects_first(monkeypatch):
    install(monkeypatch, ["unrelated"])
    assert gm.current_models("https://example.invalid/v1", "test", {}) == []


def test_offline_uses_stable_alias_and_retries_later(monkeypatch):
    calls = install(monkeypatch, error=urllib.error.URLError("offline"))
    result = gm.current_models("https://example.invalid/v1", "test", {})
    assert result[0][1] == "current" and "unavailable" in result[0][0]
    assert gm.current_models("https://example.invalid/v1", "test", {}) == result
    assert len(calls) == 1


def test_auth_or_redirect_failure_never_falls_back(monkeypatch):
    for status in [401, 403, 302]:
        install(monkeypatch, error=urllib.error.HTTPError("x", status, "", {}, None))
        assert gm.current_models("https://example.invalid/v1", "test", {}) == []


def test_missing_key_does_not_send_request(monkeypatch):
    calls = install(monkeypatch, ["current"])
    assert gm.current_models("https://example.invalid/v1", "", {}) == []
    assert calls == []
