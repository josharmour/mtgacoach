"""Conservative quest planning from observed Arena responses, never gameplay estimates.

Schemas verified against native Arena Player.log and Raw_ClientLocalization (2026-10-26).
Only observed free queues or explicitly verified generated decks are candidates.
No crafting or entry fees.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FREE_EVENTS = frozenset({"Play", "Ladder", "Play_Brawl_Historic", "Brawl_Ladder"})
COLORS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
STAMP = re.compile(r"\[UnityCrossThreadLogger\](\d+/\d+/\d+ \d+:\d+:\d+ [AP]M)")
REPLY = re.compile(r"<== (QuestGetQuests|PeriodicRewardsGetStatus)\(")


@dataclass
class QuestSnapshot:
    quests: list[dict] | None = None
    observed_at: float = 0
    rewards: dict = field(default_factory=dict)
    rewards_at: float = 0
    # Exact previously submitted pairs, not inferred format eligibility.
    pairs: dict[str, str] = field(default_factory=dict)


def read_snapshot(path: Path) -> QuestSnapshot:
    result = QuestSnapshot()
    # Bound I/O even for long-running Arena logs. Missing older state stays unknown.
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 8_000_000))
        lines = stream.read().decode("utf-8", errors="replace").splitlines()
    stamp = 0.0
    pending = None
    for line in lines:
        match = STAMP.search(line)
        if match:
            stamp = (
                datetime.strptime(match[1], "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=timezone.utc).timestamp()
            )
        match = REPLY.search(line)
        if match:
            pending = match[1]
            continue
        if pending:
            kind, pending = pending, None
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if kind == "QuestGetQuests":
                result.quests = payload.get("quests") if isinstance(payload.get("quests"), list) else None
                if result.quests is not None:
                    for quest in result.quests:
                        # QuestGetQuests omits both zero-valued progress fields.
                        # isNewQuest is transient: the same zero-progress quest
                        # loses it on the next Home refresh (2026-10-27 report).
                        # Only normalize complete server quest records; explicit
                        # invalid values or a lone startingProgress stay unknown.
                        if (
                            isinstance(quest, dict)
                            and isinstance(quest.get("questId"), str)
                            and quest["questId"]
                            and isinstance(quest.get("locKey"), str)
                            and quest["locKey"]
                            and type(quest.get("goal")) is int
                            and quest["goal"] > 0
                            and "endingProgress" not in quest
                            and "startingProgress" not in quest
                        ):
                            quest["endingProgress"] = 0
                result.observed_at = stamp
            else:
                result.rewards, result.rewards_at = payload, stamp
        if "==> EventSetDeckV3 " in line:
            try:
                envelope = json.loads(line.split("==> EventSetDeckV3 ", 1)[1])
                request = json.loads(envelope["request"])
                if request["EventName"] in FREE_EVENTS:
                    result.pairs[str(request["Summary"]["DeckId"])] = request["EventName"]
            except (ValueError, KeyError, TypeError):
                pass
    return result


def quest_descriptions() -> dict[str, str]:
    """Resolve actual English objective strings from the installed client, read-only."""
    from arenamcp.mtgadb import find_mtga_database

    card_db = find_mtga_database()
    if card_db is None:
        return {}
    files = sorted(
        card_db.parent.glob("Raw_ClientLocalization*"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not files:
        return {}
    try:
        with sqlite3.connect(f"{files[0].as_uri()}?mode=ro", uri=True) as conn:
            return dict(conn.execute("SELECT Key,enUS FROM Loc WHERE Key LIKE 'Quests/%_Text'"))
    except sqlite3.Error:
        return {}


def objective(text: str) -> tuple[str, set[str]]:
    text = text.lower()
    colors = {symbol for name, symbol in COLORS.items() if re.search(r"\b" + name + r"\b", text)}
    if text.startswith("cast ") and text.endswith(" spells."):
        if colors:
            return "colors", colors
        if "creature spells" in text:
            return "creatures", set()
    if text.startswith("attack with ") and text.endswith(" creatures."):
        return "creatures", set()
    if text.startswith("play ") and text.endswith(" lands."):
        return "lands", set()
    # Removal effectiveness and special/NPE quests require more than a type heuristic.
    return "unknown", set()


def quest_weight(text: str, cards: list[dict]) -> float:
    kind, colors = objective(text)
    if kind == "unknown":
        return 0
    count = 0
    for card in cards:
        types = card.get("type_line", "")
        if kind == "colors":
            match = "Land" not in types and bool(
                colors.intersection(card_colors(card.get("colors")) or set())
            )
        else:
            match = ("Creature" if kind == "creatures" else "Land") in types
        count += bool(match)
    return count / max(1, len(cards))


@dataclass
class QuestPlan:
    status: str
    detail: str
    deck_id: str = ""
    deck_name: str = ""
    event_id: str = ""
    coverage_gaps: list[str] = field(default_factory=list)


# Play is the verified free Standard event (see auto_queue._QUEUE_DISPLAY_NAMES).
# Do not guess event names or treat Brawl as a 60-card constructed queue.
QUEST_DECK_EVENT = "Play"
QUEST_DECK_FORMAT = "Standard"
_BASIC_COLORS = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}
_NATIVE_COLORS = {"1": "W", "2": "U", "3": "B", "4": "R", "5": "G"}


def card_colors(value: object) -> set[str] | None:
    """Normalize native Cards.Colors or CardInfo.colors; None means unknown.

    Native colors are comma-separated enum IDs, not a bit mask or characters:
    ``4`` is red, ``3,4`` is black/red. Reject partially understood metadata.
    """
    if isinstance(value, str):
        values = value.split(",") if value else []
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return None
    colors = set()
    for raw in values:
        symbol = str(raw).strip().upper()
        symbol = _NATIVE_COLORS.get(symbol, symbol)
        if symbol not in "WUBRG" or len(symbol) != 1:
            return None
        colors.add(symbol)
    return colors


def _card(cards: dict, gid: int) -> dict:
    info = cards.get(gid, cards.get(str(gid), {}))
    return info if isinstance(info, dict) else {}


def _owned(collection: dict, gid: int) -> int:
    count = collection.get(gid, collection.get(str(gid), 0))
    # Server counts are integers. Do not truncate floats or coerce booleans.
    return count if type(count) is int and count > 0 else 0


def _basic_color(info: dict) -> str | None:
    """Only the five ordinary free basics; never snow, Wastes, or name substrings."""
    type_line = str(info.get("type_line") or "")
    if (
        info.get("is_token")
        or info.get("is_primary_card") is False
        or not re.search(r"\bBasic\b", type_line)
        or not re.search(r"\bLand\b", type_line)
        or "Snow" in type_line
    ):
        return None
    name = info.get("name")
    return _BASIC_COLORS.get(name) if isinstance(name, str) else None


def _legal(info: dict, format_name: str) -> bool:
    legalities = info.get("legalities")
    if not isinstance(legalities, dict):
        return False
    value = legalities.get(format_name)
    return isinstance(value, str) and value.casefold() == "legal"


def _available_basic(info: dict, collection: dict, gid: int) -> bool:
    # Unlimited copies do not unlock an unowned artwork/printing. A provider
    # may explicitly attest globally free basics; native printings require at
    # least one inventory copy, after which all 24 copies are usable.
    return _basic_color(info) is not None and (
        _owned(collection, gid) > 0
        or (info.get("free_basic") is True and info.get("basic_requires_unlock") is not True)
    )


def _spell_cost(info: dict, color: str) -> int | None:
    """Conservative payable cost for a mono-color ordinary-basic mana base.

    mtgadb converts OldSchoolManaText ``o1oR`` into ``{1}{R}``. Accept that
    canonical representation or explicitly supplied raw OldSchoolManaText.
    Unknown symbols, X, snow, colorless requirements, hybrid and Phyrexian
    costs are excluded rather than assumed payable. Multi-face spells need
    separate face validation and are intentionally outside this builder.
    """
    cost = info.get("mana_cost")
    if not cost and isinstance(info.get("OldSchoolManaText"), str):
        raw = info["OldSchoolManaText"]
        symbols = re.findall(r"o(\d+|[WUBRG])", raw)
        if "".join("o" + symbol for symbol in symbols) != raw:
            return None
        cost = "".join("{" + symbol + "}" for symbol in symbols)
    if not isinstance(cost, str) or not cost:
        return None
    symbols = re.findall(r"\{(\d+|[WUBRG])\}", cost)
    if not symbols or "".join("{" + symbol + "}" for symbol in symbols) != cost:
        return None
    if any(len(symbol) > 2 or (not symbol.isdigit() and symbol != color) for symbol in symbols):
        return None
    mana_value = sum(int(symbol) if symbol.isdigit() else 1 for symbol in symbols)
    # 24 ordinary lands support a modest curve, not a pile of nine-mana cards.
    return mana_value if mana_value <= 5 else None


def _spell_name(info: dict, color: str, format_name: str) -> str | None:
    name = info.get("name")
    types = str(info.get("type_line") or "")
    if (
        not isinstance(name, str)
        or not name.strip()
        or "//" in name
        or "//" in types
        or not re.search(r"\b(Creature|Instant|Sorcery|Artifact|Enchantment|Planeswalker|Battle)\b", types)
        or "Land" in types
        or info.get("is_token")
        or info.get("is_primary_card") is False
        or info.get("linked_face_ids")
        or info.get("related_faces")
        or info.get("layout", "normal") not in {"normal", "class", "saga"}
        or card_colors(info.get("colors")) != {color}
        or not _legal(info, format_name)
        or _spell_cost(info, color) is None
    ):
        return None
    return " ".join(name.casefold().split())


@dataclass
class QuestDeckSpec:
    """A checked 60-card Standard shell; caller must apply and verify its format.

    ``legalities['standard'] == 'legal'`` (case-insensitive value) must come
    from current authoritative metadata, never inferred from ownership or
    absence of a ban. Native MTGACard alone does not supply this evidence.
    """

    main: list[tuple[int, int]]
    colors: set[str]
    spell_count: int
    event_id: str = QUEST_DECK_EVENT
    format: str = QUEST_DECK_FORMAT
    legality_format: str = "standard"


def build_quest_deck(
    text: str,
    collection: dict,
    cards: dict,
    *,
    fallback_lands: list[tuple[int, int]] | None = None,
) -> QuestDeckSpec | None:
    """Build 36 owned on-color spells and 24 verified free matching basics.

    The free Standard Play queue is supported. For an OR-color quest, choose
    one of its colors, never splash off-color spells. Every selected card
    needs positive Standard legality; every spell needs a known payable cost.
    Four copies are counted by name across printings. Basics do not require
    collection counts. Legacy fallback IDs confer no trust or ownership and
    may only identify basics that pass the same metadata checks.
    """
    kind, colors = objective(text)
    if kind != "colors":
        return None
    # IDs are canonicalized once so int/string aliases cannot add ownership.
    infos = {}
    for raw_gid, info in cards.items():
        if isinstance(raw_gid, bool):
            continue
        if not isinstance(raw_gid, (int, str)) or not str(raw_gid).isdigit():
            continue
        gid = int(raw_gid)
        if gid > 0 and isinstance(info, dict):
            infos[gid] = _card(cards, gid)
    # All provided metadata is eligible for basic selection. A fallback list
    # cannot supply missing metadata, legalize a card, or add arbitrary lands.
    choices = []
    for color in sorted(colors):
        basics = sorted(
            gid
            for gid, info in infos.items()
            if _basic_color(info) == color
            and _legal(info, "standard")
            and _available_basic(info, collection, gid)
        )
        if not basics:
            continue
        candidates = []
        for gid, info in infos.items():
            name = _spell_name(info, color, "standard")
            if name is not None and _owned(collection, gid):
                candidates.append((_spell_cost(info, color), "Creature" not in info["type_line"], name, gid))
        used: Counter = Counter()
        spells = []
        remaining = 36
        total_cost = 0
        for cost, _noncreature, name, gid in sorted(candidates):
            count = min(_owned(collection, gid), 4 - used[name], remaining)
            if count <= 0:
                continue
            used[name] += count
            remaining -= count
            total_cost += cost * count
            spells.append((gid, count))
            if not remaining:
                break
        if remaining:
            continue
        spec = QuestDeckSpec(spells + [(basics[0], 24)], {color}, 36)
        choices.append((total_cost, color, spec))
    return min(choices, key=lambda choice: choice[:2])[2] if choices else None


def _verified_generated_deck(deck, spec: QuestDeckSpec, collection: dict, cards: dict) -> bool:
    """Recheck the observed saved deck against an explicitly supplied build spec."""
    if (
        not isinstance(spec, QuestDeckSpec)
        or spec.event_id != QUEST_DECK_EVENT
        or spec.format != QUEST_DECK_FORMAT
        or spec.legality_format != "standard"
        or spec.spell_count != 36
        or len(spec.colors) != 1
        or not spec.colors <= set("WUBRG")
        or deck.format != spec.format
        or deck.sideboard
        or deck.command_zone
        or deck.companions
    ):
        return False
    expected: Counter = Counter()
    actual: Counter = Counter()
    for rows, counts in ((spec.main, expected), (deck.main, actual)):
        for gid, count in rows:
            if type(gid) is not int or gid <= 0 or type(count) is not int or count <= 0:
                return False
            counts[gid] += count
    if actual != expected or sum(actual.values()) != 60:
        return False
    color = next(iter(spec.colors))
    names: Counter = Counter()
    lands = 0
    for gid, count in actual.items():
        info = _card(cards, gid)
        if (
            _basic_color(info) == color
            and _legal(info, spec.legality_format)
            and _available_basic(info, collection, gid)
        ):
            lands += count
            continue
        name = _spell_name(info, color, spec.legality_format)
        if name is None or _owned(collection, gid) < count:
            return False
        names[name] += count
        if names[name] > 4:
            return False
    return lands == 24


def plan(
    snapshot: QuestSnapshot,
    *,
    now: float,
    deadline: float,
    goal: str,
    decks: dict,
    collection: dict,
    cards: dict,
    descriptions: dict,
    after: float = 0,
    verified_decks: dict[str, QuestDeckSpec] | None = None,
) -> QuestPlan:
    """Rank observed owned decks or saved decks matching explicit build specs.

    ``verified_decks`` is session-only evidence supplied after creation and
    readback, never a substitute for observed history in ``snapshot.pairs``.
    Saved format/piles, ownership, and card legality are checked again here.
    """
    if now >= deadline:
        return QuestPlan("complete", "Grind window ended.")
    if snapshot.quests is None or not max(after, now - 1800) <= snapshot.observed_at <= now:
        return QuestPlan(
            "waiting", "Open Arena Home to refresh quests; current quest state is unknown or stale."
        )
    active = []
    for raw in snapshot.quests:
        if not isinstance(raw, dict):
            return QuestPlan("blocked", "Unrecognized quest response.")
        target = raw.get("goal")
        progress = raw.get("endingProgress", 0 if raw.get("isNewQuest") is True else None)
        if type(target) is not int or type(progress) is not int or target <= 0 or progress < 0:
            return QuestPlan("blocked", "Quest progress is unknown; refresh Home.")
        if progress < target:
            text = descriptions.get(str(raw.get("locKey")) + "_Text", "")
            if objective(text)[0] == "unknown":
                return QuestPlan("blocked", f"Unsupported objective: {text or raw.get('locKey', 'unknown')}")
            active.append((text, progress, target))
    if goal not in {"mastery", "quests", "daily4", "daily10", "weekly15"}:
        return QuestPlan("blocked", "Unsupported grind goal.")
    wins_left = 0
    reward_detail = ""
    targets = []
    if goal == "mastery" and not active:
        targets = [("daily", 10), ("weekly", 15)]
    elif goal not in {"quests", "mastery"}:
        targets = [
            ("weekly" if goal == "weekly15" else "daily", {"daily4": 4, "daily10": 10, "weekly15": 15}[goal])
        ]
    remaining = {}
    for period, limit in targets:
        prefix = "_" + period + "Reward"
        value = snapshot.rewards.get(prefix + "SequenceId")
        try:
            reset = datetime.fromisoformat(
                snapshot.rewards[prefix + "ResetTimestamp"].replace("Z", "+00:00")
            ).timestamp()
        except (KeyError, ValueError, TypeError, AttributeError):
            reset = 0
        if (
            type(value) is not int
            or not 0 <= value <= 15
            or not max(after, now - 1800) <= snapshot.rewards_at <= now < reset
        ):
            return QuestPlan(
                "waiting", "Daily/weekly mastery XP availability is unknown or stale; refresh Arena Home."
            )
        remaining[period] = max(0, limit - value)
    if remaining:
        # A win advances both counters: do not add the remaining wins together.
        wins_left = max(remaining.values())
        reward_detail = "; ".join(
            f"{period}: {count} XP-awarding wins left" for period, count in remaining.items()
        )
    if not active and not wins_left:
        return QuestPlan(
            "complete",
            "Mastery XP exhausted for now—your mastery track may still be unfinished. Come back after the next reset."
            if goal == "mastery"
            else "Detected quests and selected reward goal are complete.",
        )
    candidates = []
    gaps = {text for text, _, _ in active}
    for deck_id, deck in decks.items():
        event = snapshot.pairs.get(deck_id)
        verified = (verified_decks or {}).get(deck_id)
        if verified is not None:
            if not _verified_generated_deck(deck, verified, collection, cards):
                continue
            event = verified.event_id
        if event not in FREE_EVENTS or not deck.complete or deck.snapshot:
            continue
        required = Counter(deck.deck_cards())
        for gid, count in deck.sideboard:
            required[gid] += count
        if any(
            _owned(collection, gid) < count and not _available_basic(_card(cards, gid), collection, gid)
            for gid, count in required.items()
        ):
            continue
        deck_cards = [_card(cards, gid) for gid in deck.deck_cards()]
        if any(not c.get("type_line") for c in deck_cards):
            continue
        scores = [(text, quest_weight(text, deck_cards)) for text, _, _ in active]
        for text, weight in scores:
            if weight > 0:
                gaps.discard(text)
        score = sum(weight for _, weight in scores)
        if score > 0 or wins_left:
            candidates.append((score, deck.name, deck_id, event))
    if not candidates:
        return QuestPlan(
            "blocked",
            "No verified owned deck in a supported free queue covers this goal. "
            "Play a suitable starter/owned deck once, then retry. No cards will be crafted.",
            coverage_gaps=sorted(
                text.replace("{quantity}", str(target)) for text, _, target in active if text in gaps
            ),
        )
    score, name, deck_id, event = sorted(candidates, key=lambda row: (-row[0], row[1], row[2]))[0]
    progress_text = "; ".join(
        f"{text.replace('{quantity}', str(target))} {progress}/{target}" for text, progress, target in active
    )
    return QuestPlan(
        "ready",
        f"{name} / {event}. "
        + (f"Completing quests: {progress_text}" if active else f"Earning mastery XP: {reward_detail}"),
        deck_id,
        name,
        event,
        sorted(gaps),
    )
