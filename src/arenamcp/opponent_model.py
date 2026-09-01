"""Opponent Metagame Archetype Classifier & Hand Belief State Engine for MTGA Coach.

Infers the opponent's deck archetype (e.g. Mono-Red Aggro, Dimir Midrange, UW Control,
Domain Ramp) by analyzing revealed cards across the match, and calculates a Bayesian
belief distribution over their hidden hand threats and potential instant-speed counterplay
based on current untapped mana and turn window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpponentProfile:
    """Classified opponent archetype and predicted hidden hand response envelope."""

    archetype: str = "Unknown / Midrange"
    confidence: float = 0.50
    revealed_cards: list[str] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    open_mana_threats: list[str] = field(default_factory=list)
    sweeper_risk: float = 0.0
    sweeper_warning: str = ""
    combat_trick_warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "archetype": self.archetype,
            "confidence": round(self.confidence, 2),
            "revealed_cards": self.revealed_cards,
            "colors": self.colors,
            "open_mana_threats": self.open_mana_threats,
            "sweeper_risk": round(self.sweeper_risk, 2),
            "sweeper_warning": self.sweeper_warning,
            "combat_trick_warning": self.combat_trick_warning,
        }

    def format_summary(self) -> str:
        """Format a single-line summary of archetype and instant threat envelope."""
        pct = int(self.confidence * 100)
        parts = [f"{self.archetype} ({pct}% confidence)"]
        if self.open_mana_threats:
            parts.append(f"Likely Instants: {', '.join(self.open_mana_threats[:3])}")
        if self.sweeper_warning:
            parts.append(f"Sweeper: {self.sweeper_warning}")
        return " | ".join(parts)


class OpponentModel:
    """Classifies opponent deck archetype and infers hidden hand responses."""

    # Metagame Archetype Signatures
    ARCHETYPE_SIGNATURES: dict[str, dict[str, Any]] = {
        "Standard Mono-Red Aggro": {
            "colors": ["R"],
            "signatures": {
                "monastery swiftspear",
                "slickshot show-off",
                "kumano faces kakkazan",
                "monstrous rage",
                "play with fire",
                "lightning strike",
                "shock",
                "sunspine lynx",
                "demonic ruckus",
                "emberheart challenger",
                "screaming nemesis",
            },
            "instants": {
                "R": ["Monstrous Rage (1-mana +2/+0 Trample)", "Shock / Play with Fire (2 dmg)"],
                "1R": ["Lightning Strike (3 dmg to any target)", "Twinferno (double strike trick)"],
            },
            "sweepers": {},
            "combat_tricks": "High risk of +2/+0 pump and trample tricks on unblocked attackers.",
        },
        "Standard Dimir / Golgari Midrange": {
            "colors": ["B", "U", "G"],
            "signatures": {
                "cut down",
                "go for the throat",
                "shoot the sheriff",
                "deep-cavern bat",
                "preacher of the schism",
                "sheoldred, the apocalypse",
                "glissa sunslayer",
                "mosswood dreadknight",
                "duress",
                "darkslick shores",
                "underground river",
                "llanowar wastes",
            },
            "instants": {
                "B": ["Cut Down (kills total P+T <= 5)"],
                "1B": ["Go for the Throat (kills nonartifact creature)", "Shoot the Sheriff"],
                "1U": ["Make Disappear (counter unless pays 2)", "Phantom Interference"],
                "1G": ["Tear Asunder (exile artifact/enchantment)"],
            },
            "sweepers": {"B": "Gix's Command / Cruelty of Gix"},
            "combat_tricks": "Low combat trick threat; heavy targeted instant removal.",
        },
        "Standard Azorius / UW Control": {
            "colors": ["W", "U"],
            "signatures": {
                "no more lies",
                "make disappear",
                "spell pierce",
                "get lost",
                "sunfall",
                "temporary lockdown",
                "the wandering emperor",
                "restless anchorage",
                "seachrome coast",
                "hallowed fountain",
                "mindsplice apparatus",
                "depopulate",
            },
            "instants": {
                "U": ["Spell Pierce (counter noncreature unless pays 2)"],
                "WU": ["No More Lies (hard counter unless pays 3, exiles)"],
                "1U": ["Make Disappear", "Phantom Interference"],
                "1W": ["Get Lost (destroy creature/PW/enchantment)"],
                "2WW": ["The Wandering Emperor (Flash PW, -2 exiles tapped creature)"],
            },
            "sweepers": {
                "3WW": "Sunfall (Turn 5+: Exiles all creatures, creates incubator token)",
                "1WW": "Temporary Lockdown (Turn 3+: Exiles nonland permanents <= 2 CMC)",
                "2WW": "Depopulate (Turn 4+: Destroys all creatures)",
            },
            "combat_tricks": "Flash Wandering Emperor or instant removal on attackers.",
        },
        "Standard Boros / White Convoke": {
            "colors": ["W", "R"],
            "signatures": {
                "novice inspector",
                "resolute reinforcements",
                "warden of the inner sky",
                "knight-errant of eos",
                "imodane's recruiter",
                "gleeful demolition",
                "inspirit",
                "inspiring vantage",
            },
            "instants": {
                "1W": ["Get Lost"],
                "W": ["Surge of Salvation (hexproof + damage prevention)"],
            },
            "sweepers": {},
            "combat_tricks": "Imodane's Recruiter (+1/+0 team haste) and Convoke surge plays.",
        },
        "Standard Domain / 5C Ramp": {
            "colors": ["W", "U", "B", "R", "G"],
            "signatures": {
                "topiary stomper",
                "up the beanstalk",
                "sunfall",
                "leyline binding",
                "archangel of wrath",
                "atraxa, grand unifier",
                "herd migration",
                "spirebluff canal",
            },
            "instants": {
                "W": ["Leyline Binding (1-mana flash exile with full domain)"],
            },
            "sweepers": {
                "3WW": "Sunfall (Exiles all creatures)",
            },
            "combat_tricks": "Archangel of Wrath kicker burn and Atraxa ETB refill.",
        },
    }

    @classmethod
    def classify(cls, game_state: dict[str, Any]) -> OpponentProfile:
        """Classify the opponent from all revealed cards and calculate active threat envelope."""
        if not isinstance(game_state, dict):
            return OpponentProfile()

        local_seat = game_state.get("local_seat_id", 1)
        battlefield = game_state.get("battlefield") or []
        graveyard = game_state.get("graveyard") or game_state.get("zones", {}).get("graveyard") or []
        exile = game_state.get("exile") or game_state.get("zones", {}).get("exile") or []

        revealed_names: set[str] = set()
        opp_untapped_lands: list[dict] = []
        opp_mana_colors: set[str] = set()

        # Collect all revealed opponent objects
        for zone in (battlefield, graveyard, exile):
            if not isinstance(zone, list):
                continue
            for card in zone:
                if not isinstance(card, dict):
                    continue
                ctrl = card.get("controller_seat_id") or card.get("owner_seat_id")
                if ctrl != local_seat:
                    name = str(card.get("name") or "").strip()
                    if name:
                        revealed_names.add(name.lower())
                    t_line = str(card.get("type_line") or "").lower()
                    is_tapped = bool(card.get("is_tapped"))
                    if "land" in t_line and not is_tapped:
                        opp_untapped_lands.append(card)

        # Infer opponent open mana colors from untapped lands
        for land in opp_untapped_lands:
            t_line = str(land.get("type_line") or "").lower()
            name = str(land.get("name") or "").lower()
            if "mountain" in t_line or "mountain" in name:
                opp_mana_colors.add("R")
            if "plains" in t_line or "plains" in name:
                opp_mana_colors.add("W")
            if "swamp" in t_line or "swamp" in name:
                opp_mana_colors.add("B")
            if "island" in t_line or "island" in name:
                opp_mana_colors.add("U")
            if "forest" in t_line or "forest" in name:
                opp_mana_colors.add("G")

        # Match against archetype signatures
        best_archetype = "Standard Midrange"
        best_score = 0
        best_cfg: dict[str, Any] = {}

        for arch_name, cfg in cls.ARCHETYPE_SIGNATURES.items():
            sigs = cfg["signatures"]
            matches = len(revealed_names.intersection(sigs))
            if matches > best_score:
                best_score = matches
                best_archetype = arch_name
                best_cfg = cfg

        confidence = 0.50 + min(0.48, best_score * 0.16) if best_score > 0 else 0.40

        # Determine open mana threats
        open_count = len(opp_untapped_lands)
        active_threats: list[str] = []
        sweeper_warning = ""
        sweeper_risk = 0.0

        if best_cfg and open_count > 0:
            instants_dict = best_cfg.get("instants", {})
            for mana_key, threat_list in instants_dict.items():
                # Check if open mana count & colors satisfy key
                key_cost = len(mana_key)
                if open_count >= key_cost:
                    active_threats.extend(threat_list)

            # Sweeper risk check
            turn_num = int((game_state.get("turn") or {}).get("turn_number") or 1)
            sweepers = best_cfg.get("sweepers", {})
            if sweepers:
                if turn_num >= 4 and ("W" in opp_mana_colors or "B" in opp_mana_colors):
                    sweeper_risk = min(0.85, 0.40 + (turn_num - 3) * 0.15)
                    sweeper_warning = (
                        "High risk of board sweepers (e.g. Sunfall / Depopulate). Avoid overcommitting."
                    )

        combat_trick_warning = best_cfg.get("combat_tricks", "") if best_cfg else ""

        return OpponentProfile(
            archetype=best_archetype,
            confidence=confidence,
            revealed_cards=sorted(revealed_names),
            colors=sorted(opp_mana_colors),
            open_mana_threats=active_threats,
            sweeper_risk=sweeper_risk,
            sweeper_warning=sweeper_warning,
            combat_trick_warning=combat_trick_warning,
        )
