"""settings.json persistence: legacy keys are stripped once and stay gone."""

from __future__ import annotations

import json
import logging

from arenamcp import settings as settings_module
from arenamcp.settings import Settings, _migrate_settings

LEGACY = {
    "backend": "ollama",
    "ollama_url": "http://ollama:11434",
    "api_key": "sk-old-provider",
    "known_backends": ["ollama", "proxy"],
}


def test_migration_strips_legacy_keys_and_the_app_stays_online():
    data = {**LEGACY, "mode": "local", "voice": "af_heart"}
    assert _migrate_settings(data) == set(LEGACY)
    assert data == {"mode": "local", "voice": "af_heart"}
    assert _migrate_settings(data) == set()  # nothing left to strip


def test_stripped_keys_do_not_come_back_from_disk(caplog):
    # save() merges on-disk keys this session did not set; it used to copy
    # the just-stripped keys straight back, so they returned in memory and
    # on disk and the "Migrated settings" save repeated every start.
    path = settings_module.SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**LEGACY, "voice": "af_heart", "license_key": "sk-kept"}))

    with caplog.at_level(logging.INFO, logger="arenamcp.settings"):
        first = Settings()
    assert "Migrated settings" in caplog.text
    on_disk = json.loads(path.read_text())
    assert not set(LEGACY) & (set(on_disk) | set(first._data))
    assert (on_disk["voice"], on_disk["license_key"]) == ("af_heart", "sk-kept")
    assert first.get("mode") == "online"

    first.set("voice", "am_adam")  # a later save in the same session
    assert not set(LEGACY) & (set(json.loads(path.read_text())) | set(first._data))

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="arenamcp.settings"):
        second = Settings()
    assert "Migrated settings" not in caplog.text
    assert second.get("voice") == "am_adam" and second.get("license_key") == "sk-kept"
