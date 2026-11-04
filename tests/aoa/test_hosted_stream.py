import threading
from unittest.mock import Mock

import pytest
from test_chatterbox_remote import Response

from arenamcp import chatterbox_remote as cb


def test_stream_returns_pcm_before_response_finishes_and_uses_only_public_endpoint():
    response = Response(b"\0\0" * 10000)
    opener = Mock(open=Mock(return_value=response))
    stream = cb.stream_hosted(api_key="coach-key", text="Hello.", opener=opener)
    first = next(stream)
    assert first and response.tell() < 20000 and not response.closed
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://api.mtgacoach.com/v1/chatterbox/speech"
    assert request.headers["Authorization"] == "Bearer coach-key"
    assert first + b"".join(stream) == b"\0\0" * 10000
    assert response.closed


def test_cancel_midstream_closes_connection_and_discards_remaining_audio():
    response = Response(b"\0\0" * 10000)
    cancel = threading.Event()
    stream = cb.stream_hosted(
        api_key="coach-key", text="Hello.", cancelled=cancel, opener=Mock(open=Mock(return_value=response))
    )
    assert next(stream)
    cancel.set()
    with pytest.raises(cb.SpeechCancelled):
        next(stream)
    assert response.closed


@pytest.mark.parametrize("data", [b"", b"x"])
def test_truncated_or_empty_pcm_fails(data):
    with pytest.raises(cb.ChatterboxError):
        list(
            cb.stream_hosted(
                api_key="coach-key", text="Hello.", opener=Mock(open=Mock(return_value=Response(data)))
            )
        )


def test_sign_in_required_before_network():
    opener = Mock()
    with pytest.raises(cb.ChatterboxError, match="Sign in"):
        list(cb.stream_hosted(api_key="", text="Hello.", opener=opener))
    opener.open.assert_not_called()
