"""Conservative quest planning from observed Arena responses, never gameplay estimates.

Schemas verified against native Arena Player.log and Raw_ClientLocalization (2026-10-26).
Only free, previously played constructed queues are candidates. No crafting or entry fees.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
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
            stamp = datetime.strptime(match[1], "%m/%d/%Y %I:%M:%S %p").timestamp()
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
            match = "Land" not in types and bool(colors.intersection(card.get("colors") or []))
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
) -> QuestPlan:
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
            "Available mastery XP earned—come back after the next reset."
            if goal == "mastery"
            else "Detected quests and selected reward goal are complete.",
        )
    candidates = []
    gaps = {text for text, _, _ in active}
    for deck_id, deck in decks.items():
        event = snapshot.pairs.get(deck_id)
        if event not in FREE_EVENTS or not deck.complete or deck.snapshot:
            continue
        required = Counter(deck.deck_cards())
        required.update(dict(deck.sideboard))
        if any(collection.get(str(gid), collection.get(gid, 0)) < count for gid, count in required.items()):
            continue
        deck_cards = [cards.get(gid, {}) for gid in deck.deck_cards()]
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
            "No verified owned deck in a previously played free queue covers this goal. "
            "Play a suitable starter/owned deck once, then retry. No cards will be crafted.",
            coverage_gaps=sorted(gaps),
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
