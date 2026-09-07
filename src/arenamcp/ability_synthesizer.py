"""On-the-Fly Scryfall Ability Synthesizer for MTGA Coach.

Parses card oracle text into an Intermediate Representation (IR) of abilities,
triggers, costs, and effects to power the 1-ply synergy afterstate engine and
produce structural XMage features on the fly without manual templates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ── Intermediate Representation (IR) ──


@dataclass(frozen=True)
class Filter:
    """Target or sacrifice filter expression."""

    target: str = ""
    count: int = 1
    condition: str = ""


@dataclass(frozen=True)
class Cost:
    """Resource cost required to activate an ability or cast a spell."""

    mana: str = ""
    tap: bool = False
    sacrifice: Filter | None = None
    life: int = 0
    discard: int = 0
    exile_from_graveyard: int = 0


@dataclass(frozen=True)
class TokenSpec:
    """Specification of a token created by an effect."""

    name: str
    count: int = 1
    power: int | None = None
    toughness: int | None = None
    types: tuple[str, ...] = ("Creature",)
    subtypes: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    tapped: bool = False
    attacking: bool = False
    copy_of_self: bool = False
    not_legendary: bool = False
    per_opponent: bool = False


# Predefined artifact token specs with their standard built-in abilities
PREDEFINED_TOKENS: dict[str, TokenSpec] = {
    "Food": TokenSpec(
        name="Food",
        count=1,
        types=("Artifact",),
        subtypes=("Food",),
    ),
    "Clue": TokenSpec(
        name="Clue",
        count=1,
        types=("Artifact",),
        subtypes=("Clue",),
    ),
    "Treasure": TokenSpec(
        name="Treasure",
        count=1,
        types=("Artifact",),
        subtypes=("Treasure",),
    ),
    "Blood": TokenSpec(
        name="Blood",
        count=1,
        types=("Artifact",),
        subtypes=("Blood",),
    ),
    "Map": TokenSpec(
        name="Map",
        count=1,
        types=("Artifact",),
        subtypes=("Map",),
    ),
    "Powerstone": TokenSpec(
        name="Powerstone",
        count=1,
        types=("Artifact",),
        subtypes=("Powerstone",),
        tapped=True,
    ),
}


# Effect definitions
@dataclass(frozen=True)
class CreateToken:
    spec: TokenSpec
    effect_type: str = "CreateToken"


@dataclass(frozen=True)
class GainLife:
    amount: int
    effect_type: str = "GainLife"


@dataclass(frozen=True)
class Draw:
    count: int = 1
    effect_type: str = "Draw"


@dataclass(frozen=True)
class Damage:
    amount: int
    target: str = ""
    effect_type: str = "Damage"


@dataclass(frozen=True)
class Destroy:
    target: str = ""
    effect_type: str = "Destroy"


@dataclass(frozen=True)
class Exile:
    target: str = ""
    effect_type: str = "Exile"


@dataclass(frozen=True)
class CounterSpell:
    target: str = ""
    effect_type: str = "Counter"


@dataclass(frozen=True)
class Pump:
    power: int
    toughness: int
    target: str = ""
    effect_type: str = "Pump"


@dataclass(frozen=True)
class AddMana:
    mana: str
    amount: int = 1
    effect_type: str = "AddMana"


@dataclass(frozen=True)
class PutCounters:
    counter_type: str = "+1/+1"
    count: int = 1
    effect_type: str = "PutCounters"


@dataclass(frozen=True)
class ReturnFromGraveyard:
    target_filter: str = ""
    effect_type: str = "ReturnFromGraveyard"


@dataclass(frozen=True)
class Bounce:
    target: str = ""
    effect_type: str = "Bounce"


@dataclass(frozen=True)
class UnknownEffect:
    text: str
    effect_type: str = "Unknown"


Effect = (
    CreateToken
    | GainLife
    | Draw
    | Damage
    | Destroy
    | Exile
    | CounterSpell
    | Pump
    | AddMana
    | PutCounters
    | ReturnFromGraveyard
    | Bounce
    | UnknownEffect
)


@dataclass(frozen=True)
class Trigger:
    """Event trigger condition."""

    event: str  # "ETB", "ANOTHER_CREATURE_ETB", "ATTACKS", "DIES", etc.
    subject: Filter = field(default_factory=Filter)
    condition: str = ""


# Ability definitions
@dataclass(frozen=True)
class StaticAbility:
    keywords: tuple[str, ...] = ()
    anthem: str = ""
    pt_rule: str = ""
    cost_reduction: str = ""
    text: str = ""
    ability_type: str = "Static"


@dataclass(frozen=True)
class TriggeredAbility:
    trigger: Trigger
    effects: tuple[Any, ...] = ()
    text: str = ""
    ability_type: str = "Triggered"


@dataclass(frozen=True)
class ActivatedAbility:
    cost: Cost
    effects: tuple[Any, ...] = ()
    mana_ability: bool = False
    sorcery_speed: bool = False
    text: str = ""
    ability_type: str = "Activated"


Ability = StaticAbility | TriggeredAbility | ActivatedAbility


@dataclass(frozen=True)
class CardAbilities:
    """Full set of parsed abilities for a given card."""

    name: str
    abilities: tuple[Ability, ...] = field(default_factory=tuple)
    coverage: float = 0.0
    unparsed: tuple[str, ...] = field(default_factory=tuple)

    def to_afterstate_ops(self) -> list[Ability]:
        """Return abilities as executable operations for the afterstate simulator."""
        return list(self.abilities)

    def to_xmage_features(self, zone: str = "Battlefield#1") -> list[str]:
        """Emit structural XMage feature strings for this card."""
        feats = []
        for ab in self.abilities:
            if isinstance(ab, StaticAbility):
                for kw in ab.keywords:
                    feats.append(f"{kw.lower()}#1")
            elif isinstance(ab, ActivatedAbility):
                feats.append("CanActivate#1")
                if ab.mana_ability:
                    feats.append("ManaAbility#1")
        return feats


# ── Ability Parser & Grammar ──


class AbilitySynthesizer:
    """Synthesizes structured abilities from Scryfall Oracle text."""

    _CACHE_DIR = Path.home() / ".arenamcp" / "cache" / "abilities"

    STANDARD_KEYWORDS = {
        "flying",
        "first strike",
        "double strike",
        "deathtouch",
        "vigilance",
        "trample",
        "haste",
        "lifelink",
        "reach",
        "hexproof",
        "indestructible",
        "menace",
        "flash",
        "defender",
        "ward",
    }

    WORD_TO_NUM = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "a": 1,
        "an": 1,
    }

    _MEM_CACHE: dict[str, CardAbilities] = {}

    @classmethod
    def parse_card(cls, name: str, oracle_text: str, type_line: str = "") -> CardAbilities:
        """Parse card abilities with in-memory caching."""
        norm_name = name.strip()
        if not oracle_text.strip():
            for k, v in cls._MEM_CACHE.items():
                if k.startswith(f"{norm_name}::"):
                    return v
            return CardAbilities(name=norm_name, coverage=1.0)

        cache_key = f"{norm_name}::{oracle_text}"
        if cache_key in cls._MEM_CACHE:
            return cls._MEM_CACHE[cache_key]

        abilities: list[Ability] = []
        unparsed: list[str] = []
        lines = [line.strip() for line in oracle_text.split("\n") if line.strip()]
        total_lines = len(lines)
        parsed_lines = 0

        norm_name = name.strip()

        for line in lines:
            # Strip reminder text in parentheses
            clean_line = re.sub(r"\([^)]*\)", "", line).strip()
            if not clean_line:
                parsed_lines += 1
                continue

            # Normalise card name to ~
            tokenized = re.sub(re.escape(norm_name), "~", clean_line, flags=re.IGNORECASE)
            tokenized = re.sub(r"\b(this creature|this permanent|this artifact)\b", "~", tokenized, flags=re.IGNORECASE)

            ab = cls._parse_line(tokenized, clean_line, norm_name)
            if ab is not None:
                abilities.append(ab)
                parsed_lines += 1
            else:
                unparsed.append(clean_line)

        coverage = round(parsed_lines / max(1, total_lines), 2)
        result = CardAbilities(
            name=norm_name,
            abilities=tuple(abilities),
            coverage=coverage,
            unparsed=tuple(unparsed),
        )
        cls._MEM_CACHE[cache_key] = result
        cls._save_cache(cache_key, result)
        return result

    @classmethod
    def _parse_line(cls, text: str, original_line: str, card_name: str) -> Ability | None:
        """Parse a single normalized line of oracle text."""
        # 1. Keywords line check
        kw_match = cls._match_keywords(text, original_line)
        if kw_match:
            return kw_match

        # 2. Activated ability check: Cost: Effect
        if ":" in text:
            parts = text.split(":", 1)
            cost_str, effect_str = parts[0].strip(), parts[1].strip()
            cost = cls._parse_cost(cost_str)
            if cost is not None:
                effects = cls._parse_effects(effect_str, card_name)
                is_mana = all(isinstance(e, AddMana) for e in effects) if effects else False
                return ActivatedAbility(
                    cost=cost,
                    effects=tuple(effects),
                    mana_ability=is_mana,
                    text=original_line,
                )

        # 3. Triggered ability check: When / Whenever / At Trigger, Effect
        trig_match = re.match(r"^(When|Whenever|At)\s+(?P<trigger>.+?),\s+(?P<effect>.+)$", text, re.IGNORECASE)
        if trig_match:
            trig_raw = trig_match.group("trigger").strip()
            eff_raw = trig_match.group("effect").strip()
            trigger = cls._parse_trigger(trig_raw)
            effects = cls._parse_effects(eff_raw, card_name)
            return TriggeredAbility(
                trigger=trigger,
                effects=tuple(effects),
                text=original_line,
            )

        # 4. Static anthem or P/T rule
        if "creatures you control get" in text.lower():
            m = re.search(r"([+-]\d+)/([+-]\d+)", text)
            anthem_str = m.group(0) if m else "+1/+1"
            return StaticAbility(anthem=anthem_str, text=original_line)

        if "~'s power is equal to" in text.lower():
            return StaticAbility(pt_rule=text, text=original_line)

        return None

    @classmethod
    def _match_keywords(cls, text: str, original_line: str) -> StaticAbility | None:
        """Match comma-separated keyword lines (e.g. 'Flying, vigilance')."""
        words = [w.strip().lower() for w in text.split(",")]
        found = []
        for w in words:
            # Check for ward
            if w.startswith("ward"):
                found.append(w.capitalize())
            elif w in cls.STANDARD_KEYWORDS:
                found.append(w.capitalize())
        if found and len(found) == len(words):
            return StaticAbility(keywords=tuple(found), text=original_line)
        return None

    @classmethod
    def _parse_cost(cls, cost_str: str) -> Cost | None:
        """Parse activation cost string into Cost dataclass."""
        tap = "{T}" in cost_str or "{tap}" in cost_str.lower()
        mana_symbols = re.findall(r"\{[0-9WUBRGCX/]+\}", cost_str, re.IGNORECASE)
        mana_cost = "".join(mana_symbols)

        sac_filter: Filter | None = None
        m_sac = re.search(r"sacrifice\s+(?P<cnt>\w+)?\s*(?P<target>[^,]+)", cost_str, re.IGNORECASE)
        if m_sac:
            cnt_word = (m_sac.group("cnt") or "1").lower()
            cnt = cls.WORD_TO_NUM.get(cnt_word, 1)
            target = m_sac.group("target").strip()
            sac_filter = Filter(target=target, count=cnt)

        m_life = re.search(r"pay\s+(\d+)\s+life", cost_str, re.IGNORECASE)
        life = int(m_life.group(1)) if m_life else 0

        m_disc = re.search(r"discard\s+(\w+)?\s*card", cost_str, re.IGNORECASE)
        discard = cls.WORD_TO_NUM.get((m_disc.group(1) or "1").lower(), 1) if m_disc else 0

        return Cost(
            mana=mana_cost,
            tap=tap,
            sacrifice=sac_filter,
            life=life,
            discard=discard,
        )

    @classmethod
    def _parse_trigger(cls, trig_str: str) -> Trigger:
        """Parse trigger clause into Trigger dataclass."""
        low = trig_str.lower()
        # ETB triggers
        if "enters the battlefield" in low or "enters" in low:
            if "another nontoken creature" in low:
                return Trigger(
                    event="ANOTHER_CREATURE_ETB",
                    subject=Filter(target="nontoken creature", count=1),
                    condition="nontoken",
                )
            elif "another creature" in low:
                return Trigger(event="ANOTHER_CREATURE_ETB", subject=Filter(target="creature", count=1))
            elif "~" in low or "it" in low:
                return Trigger(event="ETB", subject=Filter(target="~", count=1))
            else:
                return Trigger(event="ETB", subject=Filter(target="creature", count=1))

        if "attack" in low:
            return Trigger(event="ATTACKS", subject=Filter(target="~", count=1))

        if "dies" in low:
            return Trigger(event="DIES", subject=Filter(target="~", count=1))

        return Trigger(event="UNKNOWN", condition=trig_str)

    @classmethod
    def _parse_effects(cls, effect_str: str, card_name: str) -> list[Effect]:
        """Parse effect sentence into structured Effect objects."""
        effects: list[Effect] = []
        sentences = [s.strip() for s in re.split(r"\.|\bthen\b", effect_str) if s.strip()]

        for sent in sentences:
            low = sent.lower()

            # Dedicated Copy-of-Self token rule (e.g. The Notary Hobbits)
            m_copy = re.search(
                r"create\s+(?P<cnt>\w+)\s+tokens?\s+that\s+(?:are|is)\s+cop(?:ies|y)\s+of\s+~(?:\s*,\s*except[^\.]*)?",
                sent,
                re.IGNORECASE,
            )
            if m_copy:
                cnt_word = m_copy.group("cnt").lower()
                count = cls.WORD_TO_NUM.get(cnt_word, 2)
                not_leg = "not legendary" in low or "aren't legendary" in low
                spec = TokenSpec(
                    name=card_name,
                    count=count,
                    copy_of_self=True,
                    not_legendary=not_leg,
                )
                effects.append(CreateToken(spec=spec))
                continue

            # Standard token creation: Food, Clue, Human, etc.
            m_tok = re.search(
                r"create\s+(?:a|an|two|three|four|\d+)?\s*(?:tapped\s+)?(?:(\d+)/(\d+)\s+)?(?:(?:white|blue|black|red|green|colorless)\s+)?(?P<name>[a-zA-Z\s]+?)\s+(?:creature\s+|artifact\s+)?tokens?",
                sent,
                re.IGNORECASE,
            )
            if m_tok:
                tok_name = m_tok.group("name").strip().capitalize()
                cnt = 1
                for word, num in cls.WORD_TO_NUM.items():
                    if f" {word} " in f" {low} ":
                        cnt = num
                        break
                p = int(m_tok.group(1)) if m_tok.group(1) else None
                t = int(m_tok.group(2)) if m_tok.group(2) else None
                tapped = "tapped" in low
                attacking = "attacking" in low
                per_opp = "for each opponent" in low or "per opponent" in low

                # If predefined token exists, inherit standard spec
                if tok_name in PREDEFINED_TOKENS:
                    base_spec = PREDEFINED_TOKENS[tok_name]
                    spec = TokenSpec(
                        name=base_spec.name,
                        count=cnt,
                        types=base_spec.types,
                        subtypes=base_spec.subtypes,
                        tapped=base_spec.tapped or tapped,
                        attacking=attacking,
                        per_opponent=per_opp,
                    )
                else:
                    spec = TokenSpec(
                        name=tok_name,
                        count=cnt,
                        power=p or 1,
                        toughness=t or 1,
                        tapped=tapped,
                        attacking=attacking,
                        per_opponent=per_opp,
                    )
                effects.append(CreateToken(spec=spec))
                continue

            # Life gain: you gain N life
            m_gain = re.search(r"you gain\s+(\d+)\s+life", sent, re.IGNORECASE)
            if m_gain:
                effects.append(GainLife(amount=int(m_gain.group(1))))
                continue

            # Card draw: draw N card(s)
            m_draw = re.search(r"draw\s+(?:a|an|(\d+))\s+cards?", sent, re.IGNORECASE)
            if m_draw:
                cnt = int(m_draw.group(1)) if m_draw.group(1) else 1
                effects.append(Draw(count=cnt))
                continue

            # Mana addition: add {X} or add mana
            m_mana = re.search(r"add\s+((?:\{[WUBRGC]\})+|\w+)", sent, re.IGNORECASE)
            if m_mana:
                mana_str = m_mana.group(1)
                effects.append(AddMana(mana=mana_str))
                continue

            # Return from graveyard to hand (e.g. Samwise)
            if "return target" in low and "from your graveyard to your hand" in low:
                m_filt = re.search(r"target\s+([a-zA-Z\s]+?)\s+card", sent, re.IGNORECASE)
                filt_str = m_filt.group(1).strip() if m_filt else "card"
                effects.append(ReturnFromGraveyard(target_filter=filt_str))
                continue

            effects.append(UnknownEffect(text=sent))

        return effects

    @classmethod
    def _load_cache(cls, key: str) -> CardAbilities | None:
        try:
            path = cls._CACHE_DIR / f"{key}.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                # Restore lightweight CardAbilities summary
                return CardAbilities(
                    name=data.get("name", ""),
                    coverage=float(data.get("coverage", 1.0)),
                    unparsed=tuple(data.get("unparsed", [])),
                )
        except Exception:
            pass
        return None

    @classmethod
    def _save_cache(cls, key: str, abilities: CardAbilities) -> None:
        try:
            cls._CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = cls._CACHE_DIR / f"{key}.json"
            data = {
                "name": abilities.name,
                "coverage": abilities.coverage,
                "unparsed": list(abilities.unparsed),
            }
            path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
