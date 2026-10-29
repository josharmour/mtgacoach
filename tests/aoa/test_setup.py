import io
import json
from unittest.mock import Mock

import pytest

from arenamcp.chatterbox_remote import ChatterboxError
from arenamcp.commentary import setup


def test_discovery_reuses_existing_server_without_copying_old_voice_preferences(tmp_path):
    home = tmp_path / "home"
    config = home / ".arenaonair"
    config.mkdir(parents=True)
    key = config / "speech.key"
    key.write_text("test-key")
    key.chmod(0o600)
    (config / "config.toml").write_text(
        'tts_backend="remote"\ntts_server_url="http://speech.example:18767"\ntts_server_key_file='
        + json.dumps(str(key))
        + '\nvoice="mira"\n'
    )
    repo = tmp_path / "repos/coach"
    source = repo.parent / "arenaonair/src/arenaonair"
    source.mkdir(parents=True)
    (source / "llm_booth.py").touch()
    result = setup.discover({"chatterbox_voice": "mira"}, home=home, repository=repo)
    assert result.server_configured and result.prefer_server and result.announcers_available
    assert not result.local_configured
    assert result.values["chatterbox_server_url"] == "http://speech.example:18767/v1/audio/speech"
    assert not any("voice" in key for key in result.values)


def test_catalog_uses_server_schema_not_guessed_names():
    schema = {
        "paths": {
            "/v1/audio/speech": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/SpeechRequest"}}
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "SpeechRequest": {
                    "properties": {"voice": {"enum": ["atlas", "mira", "default"], "default": "atlas"}}
                }
            }
        },
    }
    opener = Mock(open=Mock(return_value=io.BytesIO(json.dumps(schema).encode())))
    assert setup.server_voices("http://speech.example:18767", opener=opener) == {
        "voices": ["atlas", "mira", "default"],
        "default": "atlas",
    }
    assert opener.open.call_args.args[0] == "http://speech.example:18767/openapi.json"
    schema["components"]["schemas"]["SpeechRequest"]["required"] = ["voice"]
    opener.open.return_value = io.BytesIO(json.dumps(schema).encode())
    with pytest.raises(ChatterboxError):
        setup.server_voices("http://speech.example:18767", opener=opener)


def test_catalog_errors_are_redacted():
    opener = Mock(open=Mock(side_effect=RuntimeError("PRIVATE_SECRET")))
    with pytest.raises(ChatterboxError) as error:
        setup.server_voices("https://speech.example", opener=opener)
    assert "PRIVATE_SECRET" not in str(error.value)
