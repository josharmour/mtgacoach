"""Ward: what targeting an opposing permanent costs (CR 702.21).

Ward is a triggered ability: when a permanent with ward becomes the target of
a spell or ability an opponent controls, that spell or ability is countered
unless its controller pays the ward cost. The WHOLE spell or ability is
countered, including its other targets and clauses.

2026-10-06 17:48 (bug_20261006_174855): the autopilot cast Seasoned Cryomancer
with all three of its lands before playing the Island in its hand, then aimed
the stun trigger at Unflinching Hortimancer (Ward {1}). With no mana left the
ward trigger countered the stun. Nothing in the prompt said the target had
ward or how much mana would be left after the cast.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from arenamcp.mana import get_local_seat_id, mana_cost_to_cmc
from arenamcp.rules_engine import RulesEngine, _normalize_mana_symbols

logger = logging.getLogger(__name__)

_KINDS = "creature|planeswalker|permanent|artifact|enchantment|land|battle"
# Words that may sit between "target" and the kind ("target attacking or
# blocking creature"). A closed list, so "target player sacrifices a creature"
# is not read as targeting a creature.
_ADJECTIVES = (
    r"(?:attacking|blocking|tapped|untapped|legendary|basic|nonbasic|token|nontoken|non-?[a-z]+|"
    r"white|blue|black|red|green|colorless|multicolored|monocolored|other|or|and/or)"
)
_WARD = re.compile(
    r"(?:^|[\n.;,])\s*ward\s*(?:(?P<mana>(?:\{[^}]+\})+)|[—–-]\s*(?P<other>[^.\n]+))",
    re.IGNORECASE,
)
_TARGET = re.compile(
    rf"\bany target\b|\btarget (?:{_ADJECTIVES},? ){{0,3}}(?P<kind>{_KINDS})s?\b"
    rf"(?P<more>(?:,? (?:or|and/or) (?:{_ADJECTIVES} ){{0,2}}(?:{_KINDS})s?\b)*)"
    r"(?P<tail> cards?\b| spells?\b| you control\b| from\b)?"
)
_ENCHANT = re.compile(rf"(?m)^enchant (?P<kind>{_KINDS})(?P<tail> you control)?")
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


@dataclass(frozen=True)
class Ward:
    """One ward cost: mana (``mana`` set), life, discard, or another cost."""

    text: str
    mana: int | None = None
    life: int | None = None
    discard: int | None = None

    @property
    def label(self) -> str:
        return f"Ward {self.text}" if self.mana is not None else f"Ward—{self.text}"


def _rules(text: Any) -> str:
    text = _normalize_mana_symbols(re.sub(r"<[^>]*>", "", str(text or "")))
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    return text


def ward_of_text(text: Any) -> Ward | None:
    """The ward cost printed in rules text, if any (the highest when several)."""
    found: list[Ward] = []
    for match in _WARD.finditer(_rules(text)):
        if match.group("mana"):
            symbols = "".join(re.findall(r"\{[^}]+\}", match.group("mana")))
            found.append(Ward(symbols, mana=mana_cost_to_cmc(symbols)))
            continue
        other = " ".join(match.group("other").split())
        amount = re.match(r"pay (\d+) life", other, re.IGNORECASE)
        discard = re.match(r"discard (a|an|one|two|three|\d+) cards?", other, re.IGNORECASE)
        found.append(
            Ward(
                other,
                life=int(amount.group(1)) if amount else None,
                discard=(_WORDS.get(discard.group(1).lower()) or int(discard.group(1))) if discard else None,
            )
        )
    if not found:
        return None
    return max(found, key=lambda ward: (ward.mana is not None, ward.mana or 0, ward.life or 0))


def ward_of(card: dict[str, Any] | None) -> Ward | None:
    return ward_of_text((card or {}).get("oracle_text")) if card else None


def _seat(state: dict[str, Any]) -> int | None:
    return get_local_seat_id(state)


def _controller(card: dict[str, Any]) -> int | None:
    try:
        return int(card.get("controller_seat_id") or card.get("owner_seat_id") or 0) or None
    except (TypeError, ValueError):
        return None


def available_mana(state: dict[str, Any], seat: int | None = None) -> int:
    """Untapped mana sources we control plus floating mana (one mana each)."""
    seat = _seat(state) if seat is None else seat
    if seat is None:
        return 0
    try:
        return int(RulesEngine._count_available_mana({"turn": {}, **state}, seat))
    except Exception:
        return 0


def _life(state: dict[str, Any], seat: int | None) -> int | None:
    for player in state.get("players") or []:
        if player.get("seat_id") == seat:
            try:
                return int(player.get("life_total"))
            except (TypeError, ValueError):
                return None
    return None


def ward_payable(ward: Ward, state: dict[str, Any], mana: int, seat: int | None = None) -> bool | None:
    """Whether we could pay this ward cost now; None when the cost is not modelled."""
    seat = _seat(state) if seat is None else seat
    if ward.mana is not None:
        return mana >= ward.mana
    if ward.life is not None:
        life = _life(state, seat)
        return None if life is None else life > ward.life
    if ward.discard is not None:
        hand = [card for card in state.get("hand") or [] if _controller(card) in (None, seat)]
        return len(hand) >= ward.discard
    return None


def pending_spell_cost(state: dict[str, Any], source: dict[str, Any] | None) -> int:
    """Mana still owed by the spell whose targets are being chosen.

    Arena asks for a spell's targets before it pays the cost: in Player.log
    the cast moves the card to the stack, SelectTargetsReq follows, and the
    ManaPaid annotations come only after the targets are submitted. Triggers
    and abilities owe nothing here (an activation's cost is unknown, so it
    counts as zero, which can only overstate the mana left).
    """
    if not source or str(source.get("object_kind") or "").upper() == "ABILITY":
        return 0
    if _controller(source) != _seat(state):
        return 0
    on_stack = any(
        entry.get("instance_id") == source.get("instance_id") for entry in state.get("stack") or []
    )
    if not on_stack or "ability" in str(source.get("type_line") or "").lower():
        return 0
    return mana_cost_to_cmc(_normalize_mana_symbols(str(source.get("mana_cost") or "")))


def targeting_mana(state: dict[str, Any], source: dict[str, Any] | None) -> int:
    """Mana left to pay ward once the source's own cost is paid."""
    return max(0, available_mana(state) - pending_spell_cost(state, source))


def _paragraphs(text: str) -> list[str]:
    """Rules paragraphs with modal bullets kept under their 'choose one' line."""
    paragraphs: list[str] = []
    for line in text.lower().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("•") and paragraphs:
            paragraphs[-1] += " " + line
        else:
            paragraphs.append(line)
    return paragraphs


def targeted_kinds(text: Any) -> set[str]:
    """Permanent kinds an effect can target on an opponent's side of the board."""
    text = _rules(text).lower()
    kinds: set[str] = set()
    for match in _TARGET.finditer(text):
        if match.group(0).startswith("any target"):
            kinds |= {"creature", "planeswalker", "battle"}
            continue
        if match.group("tail"):
            continue  # cards in a zone, spells, or our own permanents
        kinds.add(match.group("kind"))
        kinds |= set(re.findall(_KINDS, match.group("more") or ""))
    for match in _ENCHANT.finditer(text):
        if not match.group("tail"):
            kinds.add(match.group("kind"))
    return kinds


def cast_targeting_text(card: dict[str, Any]) -> str:
    """Rules text that targets when this card is cast or enters the battlefield."""
    text = _rules(card.get("oracle_text"))
    types = str(card.get("type_line") or "").lower()
    if "instant" in types or "sorcery" in types:
        return text
    return "\n".join(
        paragraph
        for paragraph in _paragraphs(text)
        if re.search(r"\benters\b|\bwhen you cast\b|^enchant\b", paragraph)
    )


def _matches(card: dict[str, Any], kinds: set[str]) -> bool:
    if "permanent" in kinds:
        return True
    types = " ".join(str(value) for value in card.get("card_types") or []).lower()
    types += " " + str(card.get("type_line") or "").lower()
    return any(kind in types for kind in kinds)


def opposing_wards(state: dict[str, Any], kinds: set[str]) -> list[tuple[dict[str, Any], Ward]]:
    """Opponent permanents of these kinds that have ward."""
    seat = _seat(state)
    found = []
    for card in state.get("battlefield") or []:
        if seat is None or _controller(card) in (None, seat) or not _matches(card, kinds):
            continue
        ward = ward_of(card)
        if ward is not None:
            found.append((card, ward))
    return found


def _enters_untapped(card: dict[str, Any]) -> bool:
    return not re.search(
        r"enters (?:the battlefield )?tapped(?! unless)", _rules(card.get("oracle_text")).lower()
    )


def untapped_land_drop(state: dict[str, Any], options: Any) -> bool:
    """A land play on offer that would add an untapped mana source."""
    from arenamcp.play_safety import find_source

    try:
        for option in options or ():
            meta = getattr(option, "meta", None) or {}
            if meta.get("actionType") != "ActionType_Play":
                continue
            land = find_source(state, meta)
            if land and _enters_untapped(land):  # an unseen land earns no hint
                return True
    except Exception as error:  # a prompt fact must never break the decision
        logger.debug("untapped_land_drop failed: %s", error)
    return False


def ward_cast_note(state: dict[str, Any], meta: dict[str, Any], *, land_drop: bool = False) -> str:
    """Prompt fact for a cast whose spell or enter trigger can target a warded permanent."""
    try:
        return _ward_cast_note(state, meta, land_drop)
    except Exception as error:  # a prompt fact must never break the decision
        logger.debug("ward_cast_note failed: %s", error)
        return ""


def _ward_cast_note(state: dict[str, Any], meta: dict[str, Any], land_drop: bool) -> str:
    from arenamcp.play_safety import find_source

    card = find_source(state, meta)
    if not card:
        return ""
    warded = opposing_wards(state, targeted_kinds(cast_targeting_text(card)))
    if not warded:
        return ""
    cost = sum(
        int(part.get("count") or 0) for part in meta.get("manaCost") or [] if isinstance(part, dict)
    ) or mana_cost_to_cmc(_normalize_mana_symbols(str(card.get("mana_cost") or "")))
    left = max(0, available_mana(state) - cost)
    facts = []
    for target, ward in warded[:3]:
        name = target.get("name") or "their permanent"
        payable = ward_payable(ward, state, left)
        if ward.mana is None:
            facts.append(f"{name} has {ward.label}: targeting it costs that too")
        elif payable:
            facts.append(f"{name} has {ward.label}: you'd have {left} untapped mana left to pay it")
        else:
            hint = f" ({left + 1} if you play a land first)" if land_drop else ""
            facts.append(
                f"{name} has {ward.label}: after paying for this you'd have {left} untapped mana{hint}, "
                "so targeting it gets the whole effect countered"
            )
    return "  [WARD: " + "; ".join(facts) + "]"


def ward_trigger_source(state: dict[str, Any], source_id: int = 0) -> dict[str, Any] | None:
    """The opposing warded permanent whose ward trigger asks us to pay, if any."""
    try:
        return _ward_trigger_source(state, source_id)
    except Exception as error:
        logger.debug("ward_trigger_source failed: %s", error)
        return None


def _ward_trigger_source(state: dict[str, Any], source_id: int) -> dict[str, Any] | None:
    seat = _seat(state)
    stack = state.get("stack") or []
    entry = next((item for item in stack if source_id and item.get("instance_id") == source_id), None)
    if entry is None and not source_id and stack:
        entry = stack[-1]
    if entry is None or seat is None or _controller(entry) in (None, seat):
        return None
    parent_id = entry.get("parent_instance_id")
    parent = next(
        (
            card
            for card in state.get("battlefield") or []
            if parent_id and card.get("instance_id") == parent_id
        ),
        None,
    )
    for candidate in (parent, entry.get("source_card"), entry):
        if isinstance(candidate, dict) and ward_of(candidate) is not None:
            return parent or candidate
    return None
