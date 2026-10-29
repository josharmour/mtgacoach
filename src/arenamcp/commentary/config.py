"""Validated opt-in controls shared by the engine, desktop and headless worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass

MODES = {"actions", "commentary", "essential"}
PERSONAS = {
    "classic": "Classic",
    "esports": "Esports hype desk",
    "test_match": "Test-match",
    "pro_tour": "Pro Tour",
}
FOCUSES = {"calls", "balanced", "analysis"}


@dataclass(frozen=True)
class CommentaryConfig:
    mode: str = "actions"
    persona: str = "classic"
    speakers: int = 2
    focus: str = "balanced"
    exchanges_per_minute: int = 2
    generative: bool = False
    private_information: bool = False
    muted: bool = False
    output_owner: str = "coach"
    python: str = ""
    aoa_source: str = ""

    @classmethod
    def from_settings(cls, settings):
        data = {key: settings.get("commentary_" + key, default) for key, default in asdict(cls()).items()}
        data["mode"] = settings.get("autoplay_speech", "actions")
        return cls.from_wire(data)

    @classmethod
    def from_wire(cls, data):
        values = {key: data.get(key, default) for key, default in asdict(cls()).items()}
        for key in ("generative", "private_information", "muted"):
            values[key] = values[key] is True
        for key, allowed, default in (
            ("mode", MODES, "actions"),
            ("persona", PERSONAS, "classic"),
            ("focus", FOCUSES, "balanced"),
            ("output_owner", {"coach", "standalone"}, "coach"),
        ):
            if not isinstance(values[key], str) or values[key] not in allowed:
                values[key] = default
        for key, low, high, default in (("speakers", 1, 2, 2), ("exchanges_per_minute", 1, 4, 2)):
            try:
                values[key] = max(low, min(high, int(values[key])))
            except (ValueError, TypeError):
                values[key] = default
        for key in ("python", "aoa_source"):
            values[key] = str(values[key] or "")[:4096]
        return cls(**values)

    @property
    def enabled(self):
        return self.mode == "commentary" and not self.muted and self.output_owner == "coach"

    def to_wire(self):
        return asdict(self)
