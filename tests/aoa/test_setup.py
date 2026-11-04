import io
import json
from unittest.mock import Mock

import pytest

from arenamcp.chatterbox_remote import HOSTED_BASE, ChatterboxError
from arenamcp.commentary import setup


def test_discovery_ignores_legacy_server_and_local_preferences(tmp_path):
    config = tmp_path / ".arenaonair"
    config.mkdir()
    (config / "config.toml").write_text('tts_backend="remote"\ntts_server_url="http://old-lan"\n')
    result = setup.discover(
        {
            "license_key": "test-coach-key",
            "tts_provider": "chatterbox_local",
            "chatterbox_server_url": "http://old-lan",
            "chatterbox_key_file": "/old.key",
        },
        home=tmp_path,
        repository=tmp_path / "repo",
    )
    assert result.server_configured and result.prefer_server
    assert not result.local_configured
    assert result.values["chatterbox_server_url"] == HOSTED_BASE + "/speech"
    assert result.values["chatterbox_key_file"] == ""


def test_discovery_without_license_requires_coach_sign_in(tmp_path):
    result = setup.discover({}, home=tmp_path, repository=tmp_path / "repo")
    assert result.prefer_server and not result.server_configured


def test_catalog_is_hosted_and_uses_coach_key():
    catalog = {"voices": ["atlas", "mira", "default"], "default": "atlas"}
    opener = Mock(open=Mock(return_value=io.BytesIO(json.dumps(catalog).encode())))
    assert setup.server_voices("http://old-lan", api_key="coach-key", opener=opener) == catalog
    request = opener.open.call_args.args[0]
    assert request.full_url == HOSTED_BASE + "/voices"
    assert request.headers["Authorization"] == "Bearer coach-key"


def test_catalog_errors_are_redacted():
    opener = Mock(open=Mock(side_effect=RuntimeError("PRIVATE_SECRET")))
    with pytest.raises(ChatterboxError) as error:
        setup.server_voices(api_key="coach-key", opener=opener)
    assert "PRIVATE_SECRET" not in str(error.value)
