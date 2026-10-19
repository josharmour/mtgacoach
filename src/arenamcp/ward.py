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

Each ward instance triggers on its own (CR 702.21b), so a permanent's wards
add up. Match a9cd5a2e (2026-10-19): Skyward Spider has Ward {2} and wore
Sheltered by Ghosts ("has lifelink and ward {2}"); GRE listed ability 141939
twice and two triggers asked for {4} in all. The autopilot read one {2}, paid
the first and lost Impractical Joke to the second (T8). On T10 the ward tax
on our Burst Lightning was taken for an optional cost of our own spell,
declined, and the spell was countered.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
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
# An Aura or Equipment giving what it is attached to ward: Sheltered by Ghosts'
# "Enchanted creature gets +1/+0 and has lifelink and ward {2}."
_GRANTED_WARD = re.compile(
    r"\b(?:enchanted|equipped) (?:creature|permanent|planeswalker|artifact|land|battle)\b"
    r"[^.\n]*?\b(?:has|gains?)\b[^.\n]*?\bward\s*(?:(?P<mana>(?:\{[^}]+\})+)|[—–-]\s*(?P<other>[^.\n]+))",
    re.IGNORECASE,
)
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_TIMES = {2: "twice", 3: "three times"}


@dataclass(frozen=True)
class Ward:
    """A ward cost: mana (``mana`` set), life, discard, or another cost.

    Several ward instances together (``parts``: each one's label) carry their
    totals in ``mana``/``life``/``discard``; ``unmodelled`` counts the parts
    whose cost is none of those.
    """

    text: str
    mana: int | None = None
    life: int | None = None
    discard: int | None = None
    parts: tuple[str, ...] = ()
    unmodelled: int = 0

    @property
    def count(self) -> int:
        return len(self.parts) or 1

    @property
    def modelled(self) -> bool:
        """Every part's cost is mana, life or a discard."""
        if self.parts:
            return not self.unmodelled
        return self.mana is not None or self.life is not None or self.discard is not None

    @property
    def label(self) -> str:
        if not self.parts:
            return f"Ward {self.text}" if self.mana is not None else f"Ward—{self.text}"
        if len(set(self.parts)) == 1:
            label = f"{self.parts[0]} {_TIMES.get(len(self.parts), f'{len(self.parts)} times')}"
        else:
            label = " and ".join(self.parts)
        return f"{label} ({self.mana} mana in all)" if self.mana else label


def combine_wards(wards: Any) -> Ward | None:
    """Several ward instances as one cost to pay (each triggers and is paid separately)."""
    wards = [ward for ward in wards or () if ward is not None]
    if len(wards) <= 1:
        return wards[0] if wards else None

    def total(attribute: str) -> int | None:
        values = [getattr(ward, attribute) for ward in wards if getattr(ward, attribute) is not None]
        return sum(values) if values else None

    parts = tuple(part for ward in wards for part in (ward.parts or (ward.label,)))
    return Ward(
        " + ".join(ward.text for ward in wards),
        mana=total("mana"),
        life=total("life"),
        discard=total("discard"),
        parts=parts,
        unmodelled=sum(ward.unmodelled if ward.parts else int(not ward.modelled) for ward in wards),
    )


def _rules(text: Any) -> str:
    text = _normalize_mana_symbols(re.sub(r"<[^>]*>", "", str(text or "")))
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    return text


def _ward_from(mana: str | None, other: str | None) -> Ward:
    if mana:
        symbols = "".join(re.findall(r"\{[^}]+\}", mana))
        return Ward(symbols, mana=mana_cost_to_cmc(symbols))
    other = " ".join(str(other or "").split())
    amount = re.match(r"pay (\d+) life", other, re.IGNORECASE)
    discard = re.match(r"discard (a|an|one|two|three|\d+) cards?", other, re.IGNORECASE)
    return Ward(
        other,
        life=int(amount.group(1)) if amount else None,
        discard=(_WORDS.get(discard.group(1).lower()) or int(discard.group(1))) if discard else None,
    )


@lru_cache(maxsize=4096)
def _printed_wards(text: str) -> tuple[Ward, ...]:
    """Each ward keyword in a permanent's own rules text, one per instance."""
    return tuple(_ward_from(m.group("mana"), m.group("other")) for m in _WARD.finditer(_rules(text)))


@lru_cache(maxsize=4096)
def _granted_wards(text: str) -> tuple[Ward, ...]:
    """Wards an Aura or Equipment gives the permanent it is attached to."""
    return tuple(_ward_from(m.group("mana"), m.group("other")) for m in _GRANTED_WARD.finditer(_rules(text)))


def ward_of_text(text: Any) -> Ward | None:
    """The ward printed in rules text, if any (several instances add up)."""
    return combine_wards(_printed_wards(str(text or "")))


def _ability_text(ability_id: int) -> str | None:
    try:
        from arenamcp.card_db import get_card_database

        return get_card_database().get_ability_text(int(ability_id))
    except Exception as error:
        logger.debug("ability text lookup failed for %s: %s", ability_id, error)
        return None


_UNREAD = object()
_ABILITY_WARDS: dict[int, Ward | None] = {}
_UNREAD_UNTIL: dict[int, float] = {}
_UNREAD_RETRY_S = 30.0


def _ability_ward(ability_id: int) -> Any:
    """The ward an ability grpId is (Ward {o2} is 141939), None for any other
    ability, ``_UNREAD`` while its text cannot be read (asked again after a while:
    the database may still be loading)."""
    if ability_id in _ABILITY_WARDS:
        return _ABILITY_WARDS[ability_id]
    if time.monotonic() < _UNREAD_UNTIL.get(ability_id, 0.0):
        return _UNREAD
    text = _ability_text(ability_id)
    if text is None:
        _UNREAD_UNTIL[ability_id] = time.monotonic() + _UNREAD_RETRY_S
        return _UNREAD
    found = _printed_wards(text)
    _ABILITY_WARDS[ability_id] = found[0] if found else None
    return _ABILITY_WARDS[ability_id]


def attached_to(card: dict[str, Any] | None, battlefield: Any) -> list[dict[str, Any]]:
    """The Auras and Equipment attached to this permanent."""
    instance_id = (card or {}).get("instance_id")
    if not instance_id:
        return []
    wearing = set((card or {}).get("attached_with_ids") or ())
    return [
        other
        for other in battlefield or ()
        if isinstance(other, dict)
        and other is not card
        and (other.get("attached_to_id") == instance_id or other.get("instance_id") in wearing)
    ]


def ward_instances(card: dict[str, Any] | None, attached: Any = None) -> list[Ward]:
    """Every ward the permanent has now, one per trigger it would put on the stack.

    GRE's ability list (``ability_grp_ids``: printed and granted alike) when the
    card database can read all of it; otherwise its printed ward plus what each
    ``attached`` Aura/Equipment grants ("enchanted creature ... has ward {2}").
    """
    if not card:
        return []
    from_text = list(_printed_wards(str(card.get("oracle_text") or "")))
    for gear in attached or ():
        if isinstance(gear, dict):
            from_text += _granted_wards(str(gear.get("oracle_text") or ""))
    ability_ids = card.get("ability_grp_ids") or ()
    if ability_ids:
        found: list[Ward] = []
        complete = True
        for ability_id in ability_ids:
            try:
                ward = _ability_ward(int(ability_id))
            except (TypeError, ValueError):
                continue
            if ward is _UNREAD:
                complete = False
            elif ward is not None:
                found.append(ward)
        if complete or len(found) >= len(from_text):
            return found
    return from_text


def ward_of(card: dict[str, Any] | None, attached: Any = None) -> Ward | None:
    """All of the permanent's ward together: what targeting it costs."""
    return combine_wards(ward_instances(card, attached)) if card else None


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
    """Whether we could pay this ward cost now (every part of several); None
    when a part's cost is not modelled and nothing else rules it out."""
    seat = _seat(state) if seat is None else seat
    verdicts: list[bool | None] = []
    if ward.mana is not None:
        verdicts.append(mana >= ward.mana)
    if ward.life is not None:
        life = _life(state, seat)
        verdicts.append(None if life is None else life > ward.life)
    if ward.discard is not None:
        hand = [card for card in state.get("hand") or [] if _controller(card) in (None, seat)]
        verdicts.append(len(hand) >= ward.discard)
    if not ward.modelled:
        verdicts.append(None)
    if False in verdicts:
        return False
    if None in verdicts or not verdicts:
        return None
    return True


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
    """Opponent permanents of these kinds that have ward (all of it, Aura grants included)."""
    seat = _seat(state)
    battlefield = state.get("battlefield") or []
    found = []
    for card in battlefield:
        if seat is None or _controller(card) in (None, seat) or not _matches(card, kinds):
            continue
        ward = ward_of(card, attached_to(card, battlefield))
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


@dataclass(frozen=True)
class WardTrigger:
    """An opposing ward trigger on the stack: pay its cost or our spell or ability is countered."""

    entry: dict[str, Any]
    permanent: dict[str, Any] | None
    ward: Ward

    @property
    def name(self) -> str:
        return str((self.permanent or {}).get("name") or "their permanent")


@dataclass(frozen=True)
class WardPayment:
    """The ward triggers asking us to pay now, the one being paid first."""

    triggers: tuple[WardTrigger, ...]
    countered: str  # what is countered unless every one is paid

    @property
    def permanent(self) -> dict[str, Any] | None:
        return self.triggers[0].permanent

    @property
    def ward(self) -> Ward:
        return combine_wards([trigger.ward for trigger in self.triggers]) or self.triggers[0].ward

    @property
    def names(self) -> str:
        names = list(dict.fromkeys(trigger.name for trigger in self.triggers))
        return " and ".join(names)

    def describe(self) -> str:
        return f"the ward of {self.names} ({self.ward.label}), or {self.countered} is countered"


def _is_ability(entry: dict[str, Any]) -> bool:
    return (
        str(entry.get("object_kind") or "").upper() == "ABILITY"
        or "ability" in str(entry.get("type_line") or "").lower()
        or bool(entry.get("parent_instance_id"))
    )


def _as_ward_trigger(entry: Any, battlefield: list[Any], seat: int | None) -> WardTrigger | None:
    """``entry`` as an opposing ward trigger, else None."""
    if not isinstance(entry, dict) or seat is None or _controller(entry) in (None, seat):
        return None
    if not _is_ability(entry):
        return None
    parent_id = entry.get("parent_instance_id")
    parent = next((card for card in battlefield if parent_id and card.get("instance_id") == parent_id), None)
    holder = parent if parent is not None else entry.get("source_card")
    holder = holder if isinstance(holder, dict) else None
    text = str(entry.get("oracle_text") or "")
    own = _printed_wards(text)
    if own:
        return WardTrigger(entry, holder, own[0])
    if text.strip():
        return None  # another of the permanent's abilities: its own text has no ward
    wards = ward_instances(holder, attached_to(holder, battlefield)) if holder else []
    return WardTrigger(entry, holder, wards[0]) if wards else None


def pending_ward_triggers(state: dict[str, Any], source_id: int = 0) -> list[WardTrigger]:
    """Opposing ward triggers on the stack that ask us to pay now.

    ``source_id`` is what the payment request names: the ward trigger itself
    (an OptionalAction), or our own spell or ability it would counter (a
    PayCostsReq's manaCost objectId — Burst Lightning #332 on 2026-10-19
    10:38:28, whose ward trigger #333 sat above it). The named trigger, or the
    first found, comes first; with our spell named, triggers from what it
    targets are preferred.
    """
    seat = _seat(state)
    stack = [item for item in state.get("stack") or [] if isinstance(item, dict)]
    battlefield = [card for card in state.get("battlefield") or [] if isinstance(card, dict)]
    entry = next((item for item in stack if source_id and item.get("instance_id") == source_id), None)
    if entry is None and not source_id and stack:
        entry = stack[-1]
    if entry is not None and _controller(entry) not in (None, seat):
        named = _as_ward_trigger(entry, battlefield, seat)
        if named is None:
            return []  # their own spell or another of their abilities asks: not a ward
        others = [
            found
            for item in stack
            if item is not entry and (found := _as_ward_trigger(item, battlefield, seat)) is not None
        ]
        return _at_most_their_wards([named, *others], battlefield)
    triggers = [found for item in stack if (found := _as_ward_trigger(item, battlefield, seat)) is not None]
    targets = set((entry or {}).get("targeting") or ())
    aimed = [trigger for trigger in triggers if (trigger.permanent or {}).get("instance_id") in targets]
    return _at_most_their_wards(aimed or triggers, battlefield)


def _at_most_their_wards(triggers: list[WardTrigger], battlefield: list[Any]) -> list[WardTrigger]:
    """No more triggers per permanent than it has wards: a stale stack entry must
    not make a payable ward look unpayable (declining it loses the spell)."""
    kept: list[WardTrigger] = []
    counted: dict[Any, int] = {}
    for trigger in triggers:
        permanent = trigger.permanent
        if permanent is not None and permanent.get("instance_id"):
            key = permanent.get("instance_id")
            counted[key] = counted.get(key, 0) + 1
            limit = len(ward_instances(permanent, attached_to(permanent, battlefield)))
            if limit and counted[key] > limit:
                continue
        kept.append(trigger)
    return kept


def pending_ward_payment(state: dict[str, Any], source_id: int = 0) -> WardPayment | None:
    """The ward tax asked of us now (``pending_ward_triggers``), never raising."""
    try:
        triggers = pending_ward_triggers(state, source_id)
        if not triggers:
            return None
        seat = _seat(state)
        ours = next(
            (
                item
                for item in state.get("stack") or []
                if isinstance(item, dict)
                and source_id
                and item.get("instance_id") == source_id
                and _controller(item) == seat
            ),
            None,
        )
        countered = str((ours or {}).get("name") or "our spell or ability")
        return WardPayment(tuple(triggers), countered)
    except Exception as error:
        logger.debug("pending_ward_payment failed: %s", error)
        return None


def ward_payment_mana(state: dict[str, Any]) -> int:
    """Mana we could pay a ward with now: untapped sources, or what the
    PayCostsReq offers (one mana per source, plus floating mana) when more.

    The request is fresher than the board; the larger count wins because
    declining a payable ward loses the spell, paying an unpayable one only
    the mana.
    """
    context = state.get("decision_context") or {}
    offered = 0
    if context.get("type") == "pay_costs":
        sources = {
            source.get("instance_id")
            for source in context.get("payment_sources") or []
            if isinstance(source, dict) and source.get("instance_id")
        }
        try:
            offered = len(sources) + int(context.get("pool_mana_count") or 0)
        except (TypeError, ValueError):
            offered = len(sources)
    return max(available_mana(state), offered)


def _cost_text(context: dict[str, Any]) -> str:
    """{1}{R}-style text of a PayCostsReq's mana requirement, '' when unknown."""
    needs = context.get("mana_requirements") or {}
    try:
        generic = int(needs.get("generic") or 0)
        text = f"{{{generic}}}" if generic else ""
        for color in ("W", "U", "B", "R", "G", "C"):
            text += f"{{{color}}}" * int(needs.get(color) or 0)
    except (TypeError, ValueError):
        return ""
    return text


def pay_decision_facts(state: dict[str, Any]) -> list[str]:
    """Prompt lines for a pay-or-decline choice: the amount, the mana to pay
    it with, and when it is a ward tax, whose ward and what it counters."""
    try:
        context = state.get("decision_context") or {}
        facts = []
        cost = _cost_text(context) or str(context.get("mana_cost") or "")
        if cost:
            facts.append(f"Cost to pay now: {cost} (you have {ward_payment_mana(state)} mana to pay it with)")
        payment = pending_ward_payment(state, int(context.get("source_id") or context.get("sourceId") or 0))
        if payment is not None:
            facts.append(f"This is a WARD TAX, not an optional extra: pay {payment.describe()}.")
        return facts
    except Exception as error:  # a prompt fact must never break the decision
        logger.debug("pay_decision_facts failed: %s", error)
        return []
