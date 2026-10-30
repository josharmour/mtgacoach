"""Versioned, JSON-safe boundaries for the local headless Arena On Air worker.

Local workers use the same operating-system monotonic clock as Coach (Python
3.10+). They echo observation/epoch identifiers; this is not a remote protocol.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1
TERMINAL_STATES = frozenset({"finished", "stopped", "superseded", "failed", "muted"})


class SpeechCategory(str, Enum):
    ESSENTIAL = "essential"
    WORKFLOW = "workflow"
    COMMENTARY = "commentary"
    ROUTINE = "routine"
    AMBIENT = "ambient"


@dataclass(frozen=True)
class Epoch:
    match_id: str
    game_number: int

    def __post_init__(self) -> None:
        if not isinstance(self.match_id, str) or not self.match_id.strip():
            raise ValueError("A match identity is required")
        if type(self.game_number) is not int or self.game_number < 1:
            raise ValueError("A positive game number is required")


@dataclass(frozen=True)
class Evidence:
    """An observed event; submission is deliberately NOT an observed outcome."""

    event_id: str
    kind: str
    source_sequence: int
    snapshot_id: str
    visibility: str = "public"
    payload: dict[str, Any] = field(default_factory=dict)
    seat: int | None = None
    received_at: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in {
            "action_submitted",
            "action_observed",
            "spell_resolved",
            "combat",
            "game_end",
            "game_start",
            "turn_start",
            "life_change",
            "land_drop",
        }:
            raise ValueError("Unknown evidence kind")
        if (
            not self.event_id
            or not self.snapshot_id
            or type(self.source_sequence) is not int
            or self.source_sequence < 0
        ):
            raise ValueError("Evidence requires source identity and sequence")
        if self.visibility not in {"public", "private"}:
            raise ValueError("Unknown visibility")


@dataclass(frozen=True)
class Observation:
    epoch: Epoch
    source_sequence: int
    snapshot_id: str
    arrived_at: float
    public_state: dict[str, Any]
    events: tuple[Evidence, ...] = ()
    private_state: dict[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("Unsupported commentary protocol")
        if not self.snapshot_id or type(self.source_sequence) is not int or self.source_sequence < 0:
            raise ValueError("Observation requires snapshot identity and sequence")
        if not math.isfinite(self.arrived_at) or self.arrived_at < 0:
            raise ValueError("Invalid monotonic arrival time")

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_wire(cls, value):
        if value.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("Unsupported commentary protocol")
        data = dict(value)
        data["epoch"] = Epoch(**data["epoch"])
        data["events"] = tuple(Evidence(**event) for event in data.get("events", ()))
        return cls(**data)


@dataclass(frozen=True)
class Utterance:
    utterance_id: str
    text: str
    category: SpeechCategory
    epoch: Epoch
    expires_at: float
    provider: str = "kokoro"
    voice: str = "af_heart"
    interruptible: bool = True
    anchor_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    snapshot_id: str = ""
    role: str = "play_by_play"
    speed: float = 1.0
    event_bound: bool = False
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError("Unsupported commentary protocol")
        if (
            not self.utterance_id
            or not self.text.strip()
            or len(self.text)
            > (600 if self.category in {SpeechCategory.COMMENTARY, SpeechCategory.AMBIENT} else 16000)
        ):
            raise ValueError("Utterance requires an ID and 1–600 characters")
        if not isinstance(self.category, SpeechCategory):
            raise ValueError("Unknown speech category")
        if not math.isfinite(self.expires_at) or self.expires_at < 0:
            raise ValueError("Invalid expiry")
        if type(self.event_bound) is not bool or (self.event_bound and not self.evidence_ids):
            raise ValueError("Event-bound speech requires evidence")
        if self.anchor_id == self.utterance_id:
            raise ValueError("An utterance cannot depend on itself")

    def to_wire(self) -> dict[str, Any]:
        result = asdict(self)
        result["category"] = self.category.value
        return result

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> Utterance:
        data = dict(value)
        if data.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("Unsupported commentary protocol")
        data["epoch"] = Epoch(**data["epoch"])
        data["category"] = SpeechCategory(data["category"])
        data["evidence_ids"] = tuple(data.get("evidence_ids", ()))
        return cls(**data)


@dataclass(frozen=True)
class Acknowledgment:
    utterance_id: str
    state: str
    epoch: Epoch
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION or not self.utterance_id:
            raise ValueError("Invalid acknowledgment identity or version")
        if self.state not in TERMINAL_STATES | {"accepted", "started"}:
            raise ValueError("Unknown delivery state")
