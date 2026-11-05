"""Validated opt-in controls shared by the engine, desktop and headless worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass

MODES = {"actions", "commentary", "essential"}
PERSONAS = {"hype": "Hype", "technical": "Technical"}
STYLES = {
    "hype": "Lively, funny, informative sports broadcast: vivid calls, playful situational metaphors and quick conversational payoffs. Build excitement on consequential swings; routine plays stay brief. The analyst adds substance to the fun. Let humor emerge from this position, never stock catchphrases, insults or invented facts.",
    "technical": "Engaging expert coverage with dry wit: precise card language, lucid strategic tradeoffs and measured delivery. An occasional wry observation or analogy makes a complicated position approachable. Develop both players' routes to winning; avoid lecturing, stock jokes and verdicts on the player's decisions.",
}
FOCUSES = {"calls", "balanced", "analysis"}


def normalize_persona(value):
    if not isinstance(value, str):
        return "technical"
    return {
        "esports": "hype",
        "classic": "technical",
        "test_match": "technical",
        "pro_tour": "technical",
    }.get(value, value if value in PERSONAS else "technical")


@dataclass(frozen=True)
class CommentaryConfig:
    mode: str = "actions"
    persona: str = "technical"
    speakers: int = 2
    focus: str = "analysis"
    exchanges_per_minute: int = 2
    generative: bool = False
    private_information: bool = False
    muted: bool = False
    output_owner: str = "coach"
    python: str = ""
    aoa_source: str = ""

    def __post_init__(self):
        object.__setattr__(self, "persona", normalize_persona(self.persona))

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
        values["persona"] = normalize_persona(values["persona"])
        for key, allowed, default in (
            ("mode", MODES, "actions"),
            ("persona", PERSONAS, "technical"),
            ("focus", FOCUSES, "analysis"),
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


def speech_rate(settings):
    """User-selected base speed shared by all voice engines."""
    import math

    try:
        value = float(settings.get("speech_speed", 1.5))
        return value if math.isfinite(value) and 0.5 <= value <= 2 else 1.5
    except (ValueError, TypeError):
        return 1.5
