"""Persistent settings for mtgacoach standalone coach.

Settings are stored in ``~/.arenamcp/settings.json`` and persist between
sessions.

Configuration precedence (highest to lowest):
    1. **Environment variables** -- checked first for settings that support
       them (e.g. ``MTGA_LOG_PATH``, ``MTGA_PLAYER_ID``,
       ``ARENAMCP_LOG_LEVEL``).  Individual modules are responsible for
       reading their own env vars before falling through to the Settings
       object.
    2. **~/.arenamcp/settings.json** -- persistent user preferences written
       by the TUI / ``Settings.set()`` calls.
    3. **DEFAULTS dict below** -- compiled-in defaults used when neither an
       env var nor a settings.json entry is present.

Modules should use ``get_settings().get(key)`` and let the Settings class
merge with DEFAULTS automatically.
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# Settings file location
SETTINGS_DIR = Path.home() / ".arenamcp"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# Default settings
DEFAULTS = {
    "voice": "am_adam",  # American Male - Adam
    "voice_speed": 1.0,
    "muted": False,
    "auto_speak": True,
    "auto_deck_strategy": False,
    "auto_post_match_analysis": False,
    "voice_mode": "ptt",
    "device_index": None,
    "desktop_theme": "system",
    "desktop_debug_logging": False,
    # Language for TTS and STT (e.g., "en", "nl", "es", "fr", "de", "ja")
    "language": "en",
    # Two-mode backend: "online" or "local"
    "mode": "online",
    "model": None,  # None = use backend default
    # Subscription license key for online mode
    "license_key": "",
    # Local model endpoint config
    "local_url": "http://localhost:11434/v1",  # Default: Ollama
    "local_model": None,  # None = auto-detect first available
    "local_api_key": "ollama",  # Ollama ignores this; LM Studio needs "lm-studio"
    # Subscription messages tracking
    "last_seen_message_id": None,
}

# Keys from the old multi-provider settings that should be migrated/removed
_OLD_KEYS = {
    "backend", "ollama_url", "lmstudio_url", "proxy_url", "proxy_api_key",
    "api_url", "api_key", "known_backends",
}


def _generate_install_id() -> str:
    """Return a stable opaque identifier for this local install."""
    return f"inst_{uuid4().hex}"


def _migrate_settings(data: dict) -> bool:
    """Migrate old multi-provider settings to two-mode architecture.

    Returns True if migration happened (caller should save).
    """
    changed = False

    # Migrate old backend to mode
    old_backend = data.get("backend")
    if old_backend is not None:
        if old_backend in ("ollama", "lmstudio", "lm-studio", "lm_studio"):
            data["mode"] = "local"
            # Preserve the URL they were using
            if old_backend == "ollama" and data.get("ollama_url"):
                data["local_url"] = data["ollama_url"]
                data["local_api_key"] = "ollama"
            elif old_backend in ("lmstudio", "lm-studio", "lm_studio") and data.get("lmstudio_url"):
                data["local_url"] = data["lmstudio_url"]
                data["local_api_key"] = "lm-studio"
        else:
            data["mode"] = "online"
        changed = True

    # Clean up old keys
    for key in _OLD_KEYS:
        if key in data:
            del data[key]
            changed = True

    return changed


class Settings:
    """Persistent settings manager.

    Example:
        settings = Settings()
        voice = settings.get("voice")
        settings.set("voice", "af_heart")
        settings.save()
    """

    def __init__(self) -> None:
        """Initialize settings, loading from disk if available."""
        self._data: dict[str, Any] = DEFAULTS.copy()
        self._load()
        if self._ensure_install_id():
            self.save()

    def _load(self) -> None:
        """Load settings from disk, migrating old format if needed."""
        if not SETTINGS_FILE.exists():
            return

        try:
            with open(SETTINGS_FILE, "r") as f:
                loaded = json.load(f)
                # Merge with defaults (new settings get defaults)
                for key, value in loaded.items():
                    self._data[key] = value

            # Run migration if old keys are present
            if any(k in self._data for k in _OLD_KEYS):
                if _migrate_settings(self._data):
                    self.save()
                    logger.info("Migrated settings to two-mode architecture")

            logger.debug(f"Loaded settings from {SETTINGS_FILE}")
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")

    def _ensure_install_id(self) -> bool:
        """Ensure a generated install ID exists for this installation."""
        current = self._data.get("install_id")
        if isinstance(current, str) and current.strip():
            return False
        self._data["install_id"] = _generate_install_id()
        return True

    def save(self) -> None:
        """Save settings to disk."""
        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_FILE, "w") as f:
                json.dump(self._data, f, indent=2)
            logger.debug(f"Saved settings to {SETTINGS_FILE}")
        except Exception as e:
            logger.warning(f"Failed to save settings: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """Get a setting value.

        Args:
            key: Setting key
            default: Default value if key not found

        Returns:
            Setting value or default
        """
        return self._data.get(key, default if default is not None else DEFAULTS.get(key))

    def set(self, key: str, value: Any, save: bool = True) -> None:
        """Set a setting value.

        Args:
            key: Setting key
            value: Value to set
            save: If True (default), immediately save to disk
        """
        self._data[key] = value
        if save:
            self.save()

    def reset(self) -> None:
        """Reset all settings to defaults."""
        self._data = DEFAULTS.copy()
        self.save()


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
