"""Instant deck profile computed from the deck list and card facts (G1).

Before the first playbook arrives (the strategic call takes tens of seconds),
every prompt is seeded only with the static deck reference. This module turns
the deck list plus card facts into a deterministic, card-name-agnostic
profile: mana curve, colour needs by turn, mana sources, role counts,
fact-derived engines, and keep criteria. The profile is rendered as seed text
so the very first plan of a game is written from what the deck actually is.

Everything here is derived from generic card fields (type lines, mana costs,
oracle text, card-facts effect tags and relationships) — never from card
names. Counts are copies, so a 99-card deck with few unique cards profiles
honestly instead of pretending every card is a one-of.

Standard library only; no network access (facts come from the S3 client's
cache when available, and every fact lookup degrades gracefully).
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from arenamcp.card_facts.client import hash_for_card
from arenamcp.mulligan_policy import (
    _FACE_ONLY,
    _HITS_CREATURES,
    COLORS,
    _mana_value,
    _pips,
)
from arenamcp.rules_engine import _normalize_mana_symbols

logger = logging.getLogger(__name__)

# Fixed presentation order for roles (counts are sorted by size elsewhere).
ROLE_ORDER = ("land", "ramp", "removal", "card_advantage", "threat", "other", "unknown")

# Card-facts effect tags (ability-synthesizer ``effect_type`` values) that map
# cleanly onto a role. Tags not listed here simply do not influence the role.
_EFFECT_TAG_ROLES = {
    "addmana": "ramp",
    "destroy": "removal",
    "exile": "removal",
    "bounce": "removal",
    "draw": "card_advantage",
}

_RAMP_TEXT = re.compile(
    r"add \{|add one mana|add .{0,20}mana of any colou?r"
    r"|search your library for a[n]? .{0,40}?(basic )?(land|forest|plains|island|swamp|mountain)"
    r"|\btreasure\b|additional land|land card from your library",
    re.I,
)
_ADVANTAGE_TEXT = re.compile(
    r"\bdraw (?:a|one|two|three|\d+|x|up to)\b.{0,30}?\bcards?\b"
    r"|\binvestigate\b|\bimpulse\b|look at the top",
    re.I,
)
_TAPPED_TEXT = re.compile(r"enters the battlefield tapped|enters tapped", re.I)
_RESTRICTED_TEXT = re.compile(
    r"pay \d+ life|sacrifice|as an additional cost|exile a card from your hand"
    r"|doesn't untap|does not untap",
    re.I,
)
_PRODUCED_PIP = re.compile(r"\{([WUBRG](?:/[WUBRG])?)\}")
# An 'add' keyword introduces a mana-production clause; everything after it in
# the sentence is output, everything before it (activation costs) is input.
_ADD_KEYWORD = re.compile(r"\badds?\b", re.I)
_ANY_COLOUR_TEXT = re.compile(r"\bany\b[^.;]{0,60}?\bcolou?rs?\b", re.I)
# Production phrased without an 'add' clause ('gain/produce one mana of any
# color'). Spending modifications ('spend mana as though it were mana of any
# color') deliberately do not match: they change how existing mana is spent,
# they do not produce any.
_ANY_COLOUR_PRODUCTION = re.compile(
    r"\b(?:adds?|gains?|produces?)\b[^.;]{0,60}?\bany\b[^.;]{0,60}?\bcolou?rs?\b",
    re.I,
)
_SOURCE_CONDITION_TEXT = re.compile(
    r"(activate only if[^.;]*|spend this mana only[^.;]*)",
    re.I,
)
_BASIC_LAND_COLOURS = (
    ("forest", "G"),
    ("plains", "W"),
    ("island", "U"),
    ("swamp", "B"),
    ("mountain", "R"),
)


@dataclass(frozen=True)
class ManaSources:
    """Mana sources the deck can rely on, and how costly they are to use."""

    total: int
    colours: frozenset[str]
    enters_tapped: int
    restricted: int
    # Sources able to make any colour ('one mana of any color').
    flexible: int = 0
    # Sources whose production carries a stated requirement.
    conditional: int = 0
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Engine:
    """A fact-derived engine: ``source`` feeds ``target`` (kind describes how)."""

    source: str
    target: str
    kind: str


@dataclass(frozen=True)
class KeepCriteria:
    """The numeric keep guide the mulligan prompt quotes."""

    land_count: int
    card_count: int
    lands_by_turn: tuple[tuple[int, int], ...]
    lands_in_play_by_turn: tuple[tuple[int, int], ...]
    early_play_ceiling: int
    colour_priority: tuple[str, ...]


@dataclass(frozen=True)
class DeckProfile:
    """The full profile of one deck list."""

    card_count: int
    land_count: int
    curve: tuple[tuple[int, int], ...]
    colour_needs: tuple[tuple[int, frozenset[str]], ...]
    sources: ManaSources
    roles: tuple[tuple[str, int], ...]
    engines: tuple[Engine, ...]
    key_cards: tuple[str, ...]
    unknown_cards: int
    criteria: KeepCriteria
    seed_text: str = field(default="", compare=False)

    def keep_criteria_lines(self) -> list[str]:
        """Prompt lines stating what this deck's hands need (mulligan aid).

        Two distinct numbers, never conflated: how many lands the deck is
        expected to draw by a turn (availability) and how many it can
        legally put in play by then (the achievable keep goal — one land
        drop per turn, plus one more only when the list's own rules text
        grants additional land drops).
        """
        drawn = dict(self.criteria.lands_by_turn)
        in_play = dict(self.criteria.lands_in_play_by_turn)
        priority = ", ".join(self.criteria.colour_priority) or "no committed colours"
        return [
            f"DECK PROFILE KEEP GUIDE: this deck puts ~{in_play.get(3, self.criteria.land_count)} lands in play by turn 3 "
            f"(~{drawn.get(3, self.criteria.land_count)} expected drawn) and has cheap "
            f"plays at MV<={self.criteria.early_play_ceiling}; colour priority {priority}."
        ]


def _card_field(card: dict, key: str) -> str:
    value = card.get(key, "")
    return value if isinstance(value, str) else ""


def _copy_count(card: dict) -> int:
    raw = card.get("count", 1)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _effect_tags(record: dict | None) -> frozenset[str]:
    """Lowercased effect_type tags on a card-facts record (dict envelope shape)."""
    if not record:
        return frozenset()
    tags: set[str] = set()
    for entry in record.get("effects") or ():
        if not isinstance(entry, dict):
            continue
        payload = entry.get("effect")
        if isinstance(payload, dict):
            tag = payload.get("effect_type")
            if isinstance(tag, str) and tag:
                tags.add(tag.lower())
    return frozenset(tags)


def _mana_productions(record: dict | None) -> list[dict]:
    """Mana productions a card-facts record claims.

    Each entry is ``{"mana": <str>, "condition": <str>}``; the condition, when
    the record states one, is a requirement on using that production.
    """
    if not record:
        return []
    produced = []
    for entry in record.get("mana") or ():
        if isinstance(entry, dict):
            mana = entry.get("mana")
            if isinstance(mana, str) and mana:
                condition = entry.get("condition")
                produced.append(
                    {
                        "mana": mana,
                        "condition": condition if isinstance(condition, str) else "",
                    }
                )
    return produced


def classify_role(type_line: str, mana_cost: str, text: str, tags: frozenset[str]) -> str:
    """Primary role of one card from generic fields only.

    Precedence: land > ramp > removal > card advantage > threat > other;
    ``unknown`` when the card has no parsable information at all.
    """
    tl = (type_line or "").lower()
    if not tl and not mana_cost and not text:
        return "unknown"
    if "land" in tl:
        return "land"
    if _RAMP_TEXT.search(text) or "addmana" in tags:
        return "ramp"
    if _HITS_CREATURES.search(text) or tags & {"destroy", "exile", "bounce"}:
        return "removal"
    if _ADVANTAGE_TEXT.search(text) or "draw" in tags:
        return "card_advantage"
    if "creature" in tl or "planeswalker" in tl or (_FACE_ONLY.search(text) and mana_cost):
        return "threat"
    return "other"


def _production_segments(text: str) -> list[str]:
    """Substrings that follow an 'add' keyword, one per producing clause.

    Only text after 'add' counts as production: pips in an activation cost
    (``{R}, {T}: Add {G}``) are inputs, not outputs.
    """
    segments: list[str] = []
    for line in (text or "").splitlines():
        for match in _ADD_KEYWORD.finditer(line):
            tail = line[match.end() :].split(".")[0]
            if tail:
                segments.append(tail)
    return segments


def _pip_colours(token: str) -> set[str]:
    return {part for part in token.split("/") if part in COLORS}


def _produced(type_line: str, text: str, productions: list[dict]) -> tuple[set[str], bool]:
    """``(colours, any_colour)`` this card can produce.

    Colours come from basic land types, pips in 'add'-clause outputs and
    card-facts productions. Prose like 'one mana of any color' is flexible
    production: it contributes every colour and raises the any-colour flag;
    it is never resolved by scanning letters out of the sentence.
    """
    colours: set[str] = set()
    tl = (type_line or "").lower()
    for needle, symbol in _BASIC_LAND_COLOURS:
        if needle in tl:
            colours.add(symbol)
    flexible = False
    segments = _production_segments(text)
    for segment in segments:
        for match in _PRODUCED_PIP.finditer(segment):
            colours |= _pip_colours(match.group(1))
        if _ANY_COLOUR_TEXT.search(segment):
            flexible = True
    if not segments:
        # Fact-only records may state any-colour production without an
        # 'add' clause surviving in the visible text ('gain/produce one
        # mana of any color'). Spending modifications ('spend mana as
        # though it were mana of any color') are not production and must
        # not flag flexibility.
        if _ANY_COLOUR_PRODUCTION.search(text or ""):
            flexible = True
    for production in productions:
        mana = production.get("mana", "")
        for match in _PRODUCED_PIP.finditer(mana):
            colours |= _pip_colours(match.group(1))
        if _ANY_COLOUR_TEXT.search(mana):
            flexible = True
    if flexible:
        colours |= set(COLORS)
    return colours, flexible


def _member_name(endpoint: object, by_hash: dict[str, str], by_name: dict[str, str]) -> str:
    """Resolve a relationship endpoint (rules hash or name) to a deck member."""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return ""
    if endpoint in by_hash:
        return by_hash[endpoint]
    return by_name.get(endpoint.lower(), "")


def _engines_from_facts(
    facts: dict[str, dict], by_hash: dict[str, str], by_name: dict[str, str]
) -> tuple[Engine, ...]:
    """Engines from card-facts relationships where both ends are deck members.

    The relationships payload is free-form on the record; the tolerated shape
    is ``{"links": [{"from": ..., "to": ..., "kind": ...}, ...]}`` with
    ``from``/``to`` given as rules hashes or card names.
    """
    engines: list[Engine] = []
    seen: set[tuple[str, str, str]] = set()
    for record in facts.values():
        relationships = (record or {}).get("relationships")
        if not isinstance(relationships, dict):
            continue
        links = relationships.get("links")
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            source = _member_name(link.get("from") or link.get("source"), by_hash, by_name)
            target = _member_name(link.get("to") or link.get("target"), by_hash, by_name)
            kind = str(link.get("kind") or link.get("relation") or "")
            if not source or not target or source == target:
                continue
            key = (source, target, kind)
            if key in seen:
                continue
            seen.add(key)
            engines.append(Engine(source=source, target=target, kind=kind))
    return tuple(engines)


def _grants_extra_land_drop(text: str) -> bool:
    """True when a card's own rules text grants additional land drops.

    Matches a sentence that both names extra/additional lands and uses a play
    verb ("you may play an additional land on each of your turns"). Mana
    producers, land tutors and sacrifice costs never match: making mana or
    finding a land is not playing one, so ramp role counts never stand in for
    a real extra-drop effect.
    """
    for sentence in re.split(r"[.;]", (text or "").lower()):
        if (
            re.search(r"\b(?:additional|extra|one more)\b", sentence)
            and re.search(r"\blands?\b", sentence)
            and re.search(r"\bplay\b", sentence)
        ):
            return True
    return False


def build_profile(
    cards: list[dict],
    *,
    facts: dict[str, dict] | None = None,
    commanders: tuple[str, ...] | list[str] = (),
) -> DeckProfile:
    """Profile a deck list.

    ``cards`` are card dicts (``name``, ``type_line``, ``mana_cost``,
    ``oracle_text``, optional ``count``). ``facts`` maps rules hashes to
    card-facts record dicts; when a card's hash is present its type line,
    mana cost, effect tags and mana productions override the raw fields.
    ``commanders`` are names used only to prioritise key cards.
    """
    facts = facts or {}
    rows = []
    by_hash: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for card in cards:
        digest = hash_for_card(card)
        record = facts.get(digest) if digest else None
        name = (_card_field(record, "name") if record else "") or _card_field(card, "name")
        type_line = (_card_field(record, "type_line") if record else "") or _card_field(card, "type_line")
        mana_cost = (_card_field(record, "mana_cost") if record else "") or _card_field(card, "mana_cost")
        text = (_card_field(record, "oracle_text") if record else "") or _card_field(card, "oracle_text")
        # Arena dialect ({oT}, {o4oG}) → Scryfall form so pip parsing sees it.
        text = _normalize_mana_symbols(text)
        tags = _effect_tags(record)
        productions = _mana_productions(record)
        count = _copy_count(card)
        rows.append(
            {
                "name": name,
                "count": count,
                "type_line": type_line,
                "mana_cost": mana_cost,
                "text": text,
                "tags": tags,
                "productions": productions,
                "role": classify_role(type_line, mana_cost, text, tags),
            }
        )
        if digest and name:
            by_hash[digest] = name
        if name:
            by_name.setdefault(name.lower(), name)

    card_count = sum(row["count"] for row in rows)
    land_count = sum(row["count"] for row in rows if row["role"] == "land")
    spell_rows = [row for row in rows if row["role"] != "land"]

    # Curve of the castable spells (commanders included when listed), MV capped at 7+.
    curve_counter: Counter[int] = Counter()
    for row in spell_rows:
        curve_counter[min(max(_mana_value(row["mana_cost"]), 0), 7)] += row["count"]
    curve = tuple((mv, n) for mv, n in sorted(curve_counter.items()) if n)

    # Colour needs by turn: cumulative pips on spells castable by that turn.
    needs: dict[int, Counter[str]] = {turn: Counter() for turn in range(1, 6)}
    for row in spell_rows:
        mv = max(_mana_value(row["mana_cost"]), 1)
        for pip in _pips(row["mana_cost"]):
            for colour in pip:
                for turn in range(mv, 6):
                    needs[turn][colour] += row["count"]
    colour_needs = tuple((turn, frozenset(needs[turn])) for turn in range(1, 6))

    # Mana sources: lands plus nonland cards that produce mana. Production is
    # read from 'add' clauses and fact records only — never from pips anywhere
    # in the text (activation-cost pips are inputs, not outputs).
    source_colours: set[str] = set()
    entered_tapped = 0
    restricted = 0
    producers = 0
    flexible_sources = 0
    conditional_sources = 0
    conditions: list[str] = []
    producer_re = re.compile(r"\{T\}: Add|add one mana of any colou?r|add \{", re.I)
    for row in rows:
        text = row["text"]
        colours, any_colour = _produced(row["type_line"], text, row["productions"])
        row_conditions = [
            production["condition"] for production in row["productions"] if production["condition"]
        ]
        for match in _SOURCE_CONDITION_TEXT.finditer(text):
            row_conditions.append(match.group(1).strip())
        is_producer = (
            row["role"] == "land"
            or bool(colours)
            or any_colour
            or bool(row["productions"])
            or "addmana" in row["tags"]
            or bool(producer_re.search(text))
        )
        if not is_producer:
            continue
        if row["role"] != "land":
            producers += row["count"]
        source_colours |= colours
        if any_colour:
            flexible_sources += row["count"]
        if row_conditions:
            conditional_sources += row["count"]
            for condition in row_conditions:
                if not any(condition.lower() in seen.lower() for seen in conditions):
                    conditions.append(condition)
        if _TAPPED_TEXT.search(text):
            entered_tapped += row["count"]
        if _RESTRICTED_TEXT.search(text):
            restricted += row["count"]
    sources = ManaSources(
        total=land_count + producers,
        colours=frozenset(source_colours),
        enters_tapped=entered_tapped,
        restricted=restricted,
        flexible=flexible_sources,
        conditional=conditional_sources,
        conditions=tuple(conditions),
    )

    # Role census over copies (not unique cards).
    role_counter: Counter[str] = Counter()
    for row in rows:
        role_counter[row["role"]] += row["count"]
    roles = tuple((role, role_counter[role]) for role in ROLE_ORDER if role_counter[role])

    engines = _engines_from_facts(facts, by_hash, by_name)

    # Key cards: commanders first, then engine endpoints, then biggest threats.
    key: list[str] = []
    for name in commanders:
        if name and name not in key:
            key.append(name)
    for engine in engines:
        for name in (engine.source, engine.target):
            if name and name not in key:
                key.append(name)
    threat_rows = [row for row in spell_rows if row["role"] == "threat"]
    threat_rows.sort(key=lambda r: (-r["count"], -_mana_value(r["mana_cost"]), r["name"]))
    for row in threat_rows:
        if len(key) >= 6:
            break
        if row["name"] and row["name"] not in key:
            key.append(row["name"])

    key_cards = tuple(key[:6])

    # Keep criteria: opening-hand arithmetic from the deck's own numbers.
    ratio = (land_count / card_count) if card_count else 0.0
    lands_by_turn = tuple((turn, min(land_count, round((7 + turn) * ratio))) for turn in range(1, 6))
    # Achievable on-board lands: the ordinary legal land development of one
    # drop per turn, raised by at most one extra drop per turn from turn 2
    # onward and only when the list's OWN rules text grants additional land
    # drops (e.g. "you may play an additional land on each of your turns").
    # Cheap ramp creatures are mana capacity, not land drops, and a deck
    # count never stands in for a drawn, cast extra-drop effect. Expected
    # draws are availability, not a keep goal — a 92-land Commander deck
    # draws ~9 lands by turn 3 but can only put ~3 in play.
    extra_drop_effects = any(_grants_extra_land_drop(row["text"]) for row in spell_rows)
    lands_in_play_by_turn = tuple(
        (
            turn,
            min(drawn, turn + (1 if extra_drop_effects and turn >= 2 else 0)),
        )
        for turn, drawn in lands_by_turn
    )
    spell_copies = sum(row["count"] for row in spell_rows)
    threshold = max(3, spell_copies // 5)
    max_mv = max((_mana_value(row["mana_cost"]) for row in spell_rows), default=0)
    ceiling = max_mv
    cumulative = curve_counter.get(0, 0)
    for mv in range(1, 8):
        cumulative += curve_counter.get(mv, 0)
        if cumulative >= threshold:
            ceiling = mv
            break
    first_needed: dict[str, int] = {}
    for turn, colours in colour_needs:
        for colour in colours:
            first_needed.setdefault(colour, turn)
    priority = tuple(sorted(first_needed, key=lambda c: (first_needed[c], -needs[5][c], c)))
    criteria = KeepCriteria(
        land_count=land_count,
        card_count=card_count,
        lands_by_turn=lands_by_turn,
        lands_in_play_by_turn=lands_in_play_by_turn,
        early_play_ceiling=ceiling,
        colour_priority=priority,
    )

    # Seed text: the profile as prompt lines.
    inplay3 = dict(lands_in_play_by_turn).get(3, land_count)
    pct = round(100 * land_count / card_count) if card_count else 0
    curve_bits = ", ".join(f"T{mv} x{n}" for mv, n in curve)
    role_bits = ", ".join(f"{role} {n}" for role, n in roles)
    lines = [
        "DECK PROFILE (computed from the deck list; no playbook cached yet):",
        f"{card_count} cards, {land_count} lands ({pct}%)."
        + (f" Curve (spells): {curve_bits}." if curve_bits else ""),
        (
            f"Colour priority {' then '.join(priority) if priority else 'undeclared'}; "
            f"by turn 3 the deck puts ~{inplay3} lands in play and casts cheap plays at MV<={ceiling}."
        ),
        (
            f"Mana sources: {sources.total}"
            + (f" ({sources.enters_tapped} enter tapped)" if sources.enters_tapped else "")
            + (f" ({sources.restricted} with a drawback)" if sources.restricted else "")
            + (f" ({sources.flexible} can make any colour)" if sources.flexible else "")
            + (f" (source requirements: {'; '.join(sources.conditions[:3])})" if sources.conditions else "")
            + "."
            + (f" Roles: {role_bits}." if role_bits else "")
        ),
    ]
    if engines:
        lines.append(
            "Engines: " + "; ".join(f"{e.source} -> {e.target} ({e.kind})" for e in engines[:4]) + "."
        )
    if key_cards:
        lines.append("Key cards: " + ", ".join(key_cards) + ".")
    seed_text = "\n".join(line.rstrip() for line in lines)

    return DeckProfile(
        card_count=card_count,
        land_count=land_count,
        curve=curve,
        colour_needs=colour_needs,
        sources=sources,
        roles=roles,
        engines=engines,
        key_cards=key_cards,
        unknown_cards=role_counter["unknown"],
        criteria=criteria,
        seed_text=seed_text,
    )


def profile_for_state(state: dict) -> DeckProfile | None:
    """Profile the deck list in a game state; None when there is nothing to profile."""
    grp_ids = state.get("deck_cards") or []
    try:
        grp_ids = [int(grp) for grp in grp_ids]
    except (TypeError, ValueError):
        return None
    if not grp_ids:
        return None
    try:
        from arenamcp.deck_strategy import commander_ids

        commander_grps = [int(grp) for grp in commander_ids(state)]
    except Exception as error:  # pragma: no cover - defensive
        logger.debug("commander lookup failed: %s", error)
        commander_grps = []

    counts = Counter(grp_ids)
    cards: list[dict] = []
    commander_names: list[str] = []
    for grp_id, count in sorted(counts.items()):
        card = _resolve(grp_id)
        card["count"] = count
        cards.append(card)
        if grp_id in commander_grps:
            commander_names.append(str(card.get("name", "")))
    facts: dict[str, dict] = {}
    try:
        from arenamcp.card_facts.client import facts_for

        facts = facts_for(cards) or {}
    except Exception as error:  # pragma: no cover - defensive
        logger.debug("card facts unavailable for deck profile: %s", error)
    try:
        profile = build_profile(cards, facts=facts, commanders=commander_names)
    except Exception as error:  # pragma: no cover - defensive
        logger.debug("deck profile build failed: %s", error)
        return None
    return profile


def profile_seed_for_state(state: dict) -> str:
    """Seed text for a game state's deck list; "" when there is nothing to profile."""
    profile = profile_for_state(state)
    return profile.seed_text if profile is not None else ""


def _resolve(grp_id: int) -> dict:
    """Resolve one grp id through the coach's local card database."""
    try:
        from arenamcp.match_context import _local_card

        rolling_epoch = int(time.monotonic() // 30)
        return dict(_local_card(grp_id, rolling_epoch) or {})
    except Exception as error:  # pragma: no cover - defensive
        logger.debug("card resolution failed for grp %s: %s", grp_id, error)
        return {"name": f"Unknown({grp_id})"}
