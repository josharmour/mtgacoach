import io
import json
import os
import threading
import urllib.error
import wave
from email.message import Message
from unittest.mock import Mock

import pytest

from arenamcp import chatterbox_remote as cb


class Response(io.BytesIO):
    def __init__(self, data=b"\x00\x00" * 24, content_type="audio/pcm", rate="24000"):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["X-Sample-Rate"] = rate


@pytest.fixture
def config(tmp_path):
    key = tmp_path / "speech.key"
    key.write_text("speech-only-test-key")
    key.chmod(0o600)
    return dict(endpoint="https://speech.example/v1", key_file=str(key), text="Hello.")


def test_pcm_is_wrapped_and_protocol_uses_only_speech_credentials(config):
    opener = Mock()
    opener.open.return_value = Response()
    result = cb.render_remote(**config, voice="mira", opener=opener)
    with wave.open(io.BytesIO(result)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (
            1,
            2,
            24000,
            24,
        )
    req = opener.open.call_args.args[0]
    assert req.full_url == "https://speech.example/v1/audio/speech"
    assert req.headers["Authorization"] == "Bearer speech-only-test-key"
    assert json.loads(req.data) == {"input": "Hello.", "voice": "mira", "speed": 1, "energy": 0.5}


@pytest.mark.parametrize(
    "response", [Response(b""), Response(b"x"), Response(content_type="audio/wav"), Response(rate="48000")]
)
def test_invalid_audio_rejected(config, response):
    with pytest.raises(cb.ChatterboxError):
        cb.render_remote(**config, opener=Mock(open=Mock(return_value=response)))


def test_oversized_response_rejected(config, monkeypatch):
    monkeypatch.setattr(cb, "MAX_AUDIO_BYTES", 4)
    with pytest.raises(cb.ChatterboxError, match="too much audio"):
        cb.render_remote(**config, opener=Mock(open=Mock(return_value=Response(b"x" * 6))))


@pytest.mark.parametrize("status", [301, 302, 401, 503])
def test_http_failures_are_redacted_and_never_retried(config, status):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError("SECRET_URL", status, "SECRET_BODY", {}, None)
    with pytest.raises(cb.ChatterboxError) as error:
        cb.render_remote(**config, opener=opener)
    assert "SECRET" not in str(error.value)
    opener.open.assert_called_once()
    assert cb._NoRedirect().redirect_request(None, None, None, None, None, None) is None


def test_cancelled_before_request_performs_no_network(config):
    cancel = threading.Event()
    cancel.set()
    opener = Mock()
    with pytest.raises(cb.SpeechCancelled):
        cb.render_remote(**config, cancelled=cancel, opener=opener)
    opener.open.assert_not_called()


def test_cancelled_during_download_never_returns_audio(config):
    cancel = threading.Event()
    response = Response()
    original = response.read1

    def read(size):
        cancel.set()
        return original(size)

    response.read1 = read
    with pytest.raises(cb.SpeechCancelled):
        cb.render_remote(**config, cancelled=cancel, opener=Mock(open=Mock(return_value=response)))


def test_deadline_rejects_late_audio(config):
    now = [0]
    response = Response()
    original = response.read1

    def read(size):
        now[0] = 5
        return original(size)

    response.read1 = read
    with pytest.raises(cb.ChatterboxError, match="deadline"):
        cb.render_remote(**config, opener=Mock(open=Mock(return_value=response)), clock=lambda: now[0])


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/key",
        "https://user:key@host",
        "https://host?q=key",
        "https://host/#key",
        "http://host:99999",
    ],
)
def test_bad_endpoints_rejected_without_echoing_secrets(url):
    with pytest.raises(cb.ChatterboxError):
        cb.speech_endpoint(url)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permission check")
def test_private_key_required_and_invalid_input_never_requests(config):
    from pathlib import Path

    Path(config["key_file"]).chmod(0o644)
    opener = Mock()
    with pytest.raises(cb.ChatterboxError):
        cb.render_remote(**config, opener=opener)
    opener.open.assert_not_called()
