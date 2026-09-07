"""Format Dispatcher & Format-Specific Evaluator Configuration for MTGA Coach.

Detects match format (Brawl, Constructed, Limited) via multi-source evidence fusion
(ConnectResp deckCards count, commanderGrpIds, eventId regex, and runtime state),
and parameterizes the tactical lookahead engine with format-specific rules
(25 life, commander tax, token synergy weights).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FormatProfile:
    """Immutable profile identifying the match format, rules, and commander identity."""

    family: Literal["constructed", "brawl", "limited", "unknown"] = "unknown"
    variant: str = "unknown"
    deck_size: int = 0
    singleton: bool = False
    starting_life: int = 20
    has_command_zone: bool = False
    commander_grp_ids: tuple[int, ...] = field(default_factory=tuple)
    commander_names: tuple[str, ...] = field(default_factory=tuple)
    best_of: int = 1
    confidence: float = 0.0
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def format_summary(self) -> str:
        """Format a single-line summary for prompt context and HUD tooltips."""
        parts = [f"{self.variant.replace('_', ' ').title()} ({self.family.title()}"]
        if self.deck_size:
            parts.append(f"{self.deck_size}-card")
        if self.singleton:
            parts.append("singleton")
        parts.append(f"{self.starting_life} life)")
        if self.has_command_zone and self.commander_names:
            parts.append(f"Commander: {', '.join(self.commander_names)}")
        return " ".join(parts)


@dataclass(frozen=True)
class FormatEvaluatorConfig:
    """Tuning parameters for tactical lookahead parameterized by match format."""

    life_norm: int = 20
    lethal_zone: int = 6
    board_norm: int = 10
    hand_norm: int = 4
    commander_tax_step: int = 0
    commander_recast_value: float = 0.0
    token_synergy_weight: float = 0.6
    hold_mana_value: float = 0.06
    singleton_gate: str = "Jaccard >= 0.60"

    @classmethod
    def from_profile(cls, profile: FormatProfile) -> FormatEvaluatorConfig:
        """Construct evaluator config from classified format profile."""
        if profile.family == "brawl":
            return cls(
                life_norm=profile.starting_life or 25,
                lethal_zone=8,
                board_norm=12,
                hand_norm=4,
                commander_tax_step=2,
                commander_recast_value=0.05,
                token_synergy_weight=1.0,
                hold_mana_value=0.04,
                singleton_gate="commander match + Jaccard >= 0.45",
            )
        elif profile.family == "limited":
            return cls(
                life_norm=20,
                lethal_zone=6,
                board_norm=8,
                hand_norm=3,
                commander_tax_step=0,
                commander_recast_value=0.0,
                token_synergy_weight=0.8,
                hold_mana_value=0.03,
                singleton_gate="never",
            )
        else:  # constructed standard / historic
            return cls(
                life_norm=20,
                lethal_zone=6,
                board_norm=10,
                hand_norm=4,
                commander_tax_step=0,
                commander_recast_value=0.0,
                token_synergy_weight=0.6,
                hold_mana_value=0.06,
                singleton_gate="Jaccard >= 0.60",
            )


def detect_format_profile(
    game_state: dict[str, Any],
    event_name: str = "",
    deck_cards: list[int] | None = None,
    commander_grp_ids: list[int] | None = None,
) -> FormatProfile:
    """Infer match format via evidence fusion.

    Priority order:
    1. ConnectResp deckCards count and commanderGrpIds
    2. InternalEventName / eventId regex
    3. Runtime state corroboration (life totals == 25, command zone)
    """
    evidence: list[str] = []
    confidence = 0.50

    # 1. Resolve raw deck cards and commanders from args or game_state
    resolved_deck_cards = deck_cards or game_state.get("deck_cards") or []
    deck_len = len(resolved_deck_cards) if isinstance(resolved_deck_cards, list) else 0

    resolved_cmd_ids = (
        commander_grp_ids
        or game_state.get("commander_grp_ids")
        or []
    )
    if isinstance(resolved_cmd_ids, (list, tuple)):
        cmd_id_tuple = tuple(int(x) for x in resolved_cmd_ids if x)
    else:
        cmd_id_tuple = ()

    # Check command zone in game state
    command_cards = (
        game_state.get("command")
        or game_state.get("zones", {}).get("command")
        or game_state.get("command_zone")
        or []
    )
    has_command_zone = bool(command_cards) or bool(cmd_id_tuple)

    # 2. Event name inspection
    ev_raw = event_name or game_state.get("event_name") or game_state.get("format_name") or ""
    ev_clean = str(ev_raw).strip()

    is_brawl_event = bool(re.search(r"Brawl", ev_clean, re.IGNORECASE))
    is_limited_event = bool(re.search(r"Draft|Sealed|Jump_In|Cube", ev_clean, re.IGNORECASE))
    is_bo3 = bool(re.search(r"Traditional|BestOf3|Bo3", ev_clean, re.IGNORECASE))

    # Variant detection from event name
    variant = "unknown"
    if is_brawl_event:
        variant = "standard_brawl" if "standard" in ev_clean.lower() else "historic_brawl"
    elif is_limited_event:
        variant = "draft" if "draft" in ev_clean.lower() else "sealed"
    elif ev_clean:
        for v in ("standard", "historic", "alchemy", "timeless", "explorer"):
            if v in ev_clean.lower():
                variant = v
                break

    # 3. Life total corroboration
    players = game_state.get("players") or []
    max_life = 20
    for p in players:
        if isinstance(p, dict):
            l = int(p.get("life_total") or p.get("starting_life") or 20)
            if l > max_life:
                max_life = l

    # Evidence fusion logic
    family: Literal["constructed", "brawl", "limited", "unknown"] = "unknown"
    deck_size = 0
    singleton = False
    starting_life = 20

    # Strongest signal: deck size from ConnectResp
    if deck_len in range(98, 101) or (has_command_zone and deck_len in range(90, 105)):
        family = "brawl"
        deck_size = 99
        singleton = True
        starting_life = 25
        confidence = 0.95
        evidence.append(f"deckCards count ({deck_len}) indicates 100-card Brawl")
    elif deck_len in range(40, 46):
        family = "limited"
        deck_size = 40
        singleton = False
        starting_life = 20
        confidence = 0.95
        evidence.append(f"deckCards count ({deck_len}) indicates 40-card Limited")
    elif deck_len in range(60, 76):
        family = "constructed"
        deck_size = 60
        singleton = False
        starting_life = 20
        confidence = 0.95
        evidence.append(f"deckCards count ({deck_len}) indicates 60-card Constructed")

    # Secondary signal: Event Name
    if is_brawl_event:
        family = "brawl"
        deck_size = deck_size or 99
        singleton = True
        starting_life = 25
        confidence = max(confidence, 0.90)
        evidence.append(f"Event name '{ev_clean}' specifies Brawl")
    elif is_limited_event and family == "unknown":
        family = "limited"
        deck_size = deck_size or 40
        singleton = False
        confidence = max(confidence, 0.90)
        evidence.append(f"Event name '{ev_clean}' specifies Limited")
    elif ev_clean and family == "unknown":
        family = "constructed"
        deck_size = deck_size or 60
        confidence = max(confidence, 0.70)
        evidence.append(f"Event name '{ev_clean}' indicates Constructed")

    # Tertiary signal: Command zone and 25 starting life in runtime state
    if max_life >= 25 or has_command_zone:
        if family != "brawl":
            family = "brawl"
            deck_size = deck_size or 99
            singleton = True
            starting_life = 25
            confidence = max(confidence, 0.85)
            evidence.append(f"Runtime state: starting life {max_life} + command zone active")

    # Fallback to standard constructed if unknown
    if family == "unknown":
        family = "constructed"
        variant = "standard"
        deck_size = 60
        starting_life = 20
        confidence = 0.40
        evidence.append("Fallback to standard constructed")

    if variant == "unknown":
        variant = "historic_brawl" if family == "brawl" else ("draft" if family == "limited" else "standard")

    # Resolve commander names
    commander_names: list[str] = []
    for c in command_cards:
        if isinstance(c, dict) and c.get("name"):
            commander_names.append(str(c["name"]).strip())

    if not commander_names and cmd_id_tuple:
        try:
            from arenamcp.mtgadb import MtgaDB

            for gid in cmd_id_tuple:
                card = MtgaDB.get_card_by_grp_id(gid)
                if card and card.get("name"):
                    commander_names.append(card["name"])
        except Exception:
            pass

    return FormatProfile(
        family=family,
        variant=variant,
        deck_size=deck_size,
        singleton=singleton,
        starting_life=starting_life,
        has_command_zone=has_command_zone,
        commander_grp_ids=cmd_id_tuple,
        commander_names=tuple(commander_names),
        best_of=3 if is_bo3 else 1,
        confidence=round(confidence, 2),
        evidence=tuple(evidence),
    )
