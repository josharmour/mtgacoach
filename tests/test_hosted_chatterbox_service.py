import asyncio
import json
from unittest.mock import Mock

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from gateway.chatterbox_service import create_app, segments


def app_for(engine=None, status=200, models=("current",)):
    engine = engine or Mock()
    engine._synthesize_pcm.return_value = [(np.zeros(240, dtype=np.float32), 24000)]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json={"data": [{"id": model} for model in models]})
    )
    return create_app(engine, transport=transport), engine


@pytest.mark.parametrize(
    "status,models,expected", [(401, (), 401), (403, (), 403), (500, (), 503), (200, ("other",), 403)]
)
def test_unauthorized_requests_never_use_gpu(status, models, expected):
    app, engine = app_for(status=status, models=models)
    with TestClient(app) as client:
        result = client.post(
            "/v1/chatterbox/speech", json={"input": "Hello."}, headers={"Authorization": "Bearer test-key"}
        )
    assert result.status_code == expected
    engine._synthesize_pcm.assert_not_called()


def test_missing_auth_and_catalog_contract():
    app, engine = app_for()
    with TestClient(app) as client:
        assert client.get("/v1/chatterbox/voices").status_code == 401
        result = client.get("/v1/chatterbox/voices", headers={"Authorization": "Bearer test-key"})
        assert result.json() == {"voices": ["atlas", "mira", "default"], "default": "atlas"}
    engine._synthesize_pcm.assert_not_called()


def test_first_audio_is_sent_before_next_segment_is_generated():
    events = []
    app, engine = app_for()

    def generate(text, **kwargs):
        events.append("generate:" + text)
        yield np.zeros(240, dtype=np.float32), 24000

    engine._synthesize_pcm.side_effect = generate

    async def run():
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": json.dumps({"input": "First sentence. Second sentence."}).encode(),
                }
            await asyncio.Event().wait()

        async def send(event):
            if event["type"] == "http.response.body" and event.get("body"):
                events.append("audio")

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/v1/chatterbox/speech",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json"), (b"authorization", b"Bearer test-key")],
            },
            receive,
            send,
        )

    asyncio.run(run())
    assert events == ["generate:First sentence.", "audio", "generate:Second sentence.", "audio"]


def test_segment_bound_preserves_text():
    text = "First sentence. " + "another word " * 30
    chunks = list(segments(text))
    assert " ".join(chunks) == text.strip()
    assert all(len(chunk) <= 100 for chunk in chunks)
