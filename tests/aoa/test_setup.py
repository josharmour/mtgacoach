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


@pytest.mark.parametrize("platform,interpreter", [("win32", "Scripts/python.exe"), ("darwin", "bin/python")])
@pytest.mark.parametrize("location", ["home", "runtime", "aoa"])
def test_discovers_native_local_interpreter(monkeypatch, tmp_path, platform, interpreter, location):
    home = tmp_path / "home"
    repo = tmp_path / "repos/coach"
    source = repo.parent / "arenaonair"
    (source / "src/arenaonair").mkdir(parents=True)
    (source / "src/arenaonair/llm_booth.py").touch()
    roots = {
        "home": home / "venvs/coach-chatterbox",
        "runtime": repo / "runtime/chatterbox/.venv",
        "aoa": source / ".venv",
    }
    python = roots[location] / interpreter
    python.parent.mkdir(parents=True)
    python.touch(mode=0o700)
    revision = "749d1c1a46eb10492095d68fbcf55691ccf137cd"
    (home / "models/coach-chatterbox" / revision).mkdir(parents=True)
    monkeypatch.setattr(setup.sys, "platform", platform)
    result = setup.discover({}, home=home, repository=repo)
    assert result.values["chatterbox_local_python"] == str(python)
    assert result.local_configured


def test_windows_does_not_select_shared_unix_venv(monkeypatch, tmp_path):
    python = tmp_path / "venvs/coach-chatterbox/bin/python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o700)
    monkeypatch.setattr(setup.sys, "platform", "win32")
    result = setup.discover({}, home=tmp_path, repository=tmp_path / "repo")
    assert not result.values["chatterbox_local_python"]
    assert not result.local_configured
