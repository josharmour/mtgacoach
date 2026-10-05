"""Event course progress from Player.log: which stage each joined event is in.

Arena's front door answers course requests with the player's courses:
``<== EventGetCoursesV2`` lists them all; ``EventJoin``, ``EventSetDeckV3``,
``DraftCompleteDraft`` and ``EventClaimPrize`` replies carry one ``Course``.
Each course names its ``InternalEventName`` and ``CurrentModule``:

- ``PlayerDraft`` / ``BotDraft``: drafting
- ``DeckSelect`` / ``GrantCardPool``: building the limited deck (``CardPool`` set)
- ``CreateMatch``: ready to queue the event's next match
- ``ClaimPrize``: matches done, prize waiting
- ``Complete``: finished

The draft-event driver reads this (log-first) to decide what to do next.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

LIMITED_MARKERS = ("draft", "sealed", "cube")
DRAFT_MODULES = {"PlayerDraft", "BotDraft", "HumanDraft", "Draft"}
BUILD_MODULES = {"DeckSelect", "GrantCardPool"}
PLAY_MODULES = {"CreateMatch"}
PRIZE_MODULES = {"ClaimPrize"}
DONE_MODULES = {"Complete"}


@dataclass
class Course:
    event_name: str
    course_id: str = ""
    module: str = ""
    wins: int = 0
    losses: int = 0
    card_pool: list[int] = field(default_factory=list)
    deck_id: str = ""
    main_deck: list[dict] = field(default_factory=list)
    updated_at: float = 0.0

    @property
    def is_limited(self) -> bool:
        lowered = self.event_name.lower()
        return any(marker in lowered for marker in LIMITED_MARKERS) or self.module in DRAFT_MODULES

    @property
    def is_bot_draft(self) -> bool:
        return self.module == "BotDraft" or "quickdraft" in self.event_name.lower().replace("_", "")

    @property
    def stage(self) -> str:
        if self.module in DRAFT_MODULES:
            return "draft"
        if self.module in BUILD_MODULES:
            return "build"
        if self.module in PLAY_MODULES:
            return "play"
        if self.module in PRIZE_MODULES:
            return "prize"
        if self.module in DONE_MODULES:
            return "done"
        return "other"


def _course_dicts(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    found = []
    courses = payload.get("Courses")
    if isinstance(courses, list):
        found += [course for course in courses if isinstance(course, dict)]
    course = payload.get("Course")
    if isinstance(course, dict):
        found.append(course)
    if "InternalEventName" in payload and "CurrentModule" in payload:
        found.append(payload)
    return found


def _int_list(value: Any) -> list[int]:
    result = []
    for item in value if isinstance(value, list) else []:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


class CourseTracker:
    """Latest known state of each joined event, updated from course payloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._courses: dict[str, Course] = {}

    def observe(self, payload: Any) -> list[Course]:
        """Fold every course in a front-door payload into the tracker."""
        updated = []
        for raw in _course_dicts(payload):
            name = str(raw.get("InternalEventName") or "")
            if not name:
                continue
            summary = raw.get("CourseDeckSummary") if isinstance(raw.get("CourseDeckSummary"), dict) else {}
            deck = raw.get("CourseDeck") if isinstance(raw.get("CourseDeck"), dict) else {}
            with self._lock:
                course = self._courses.get(name) or Course(event_name=name)
                course.course_id = str(raw.get("CourseId") or course.course_id)
                course.module = str(raw.get("CurrentModule") or course.module)
                course.wins = int(raw.get("CurrentWins") or 0)
                course.losses = int(raw.get("CurrentLosses") or 0)
                pool = _int_list(raw.get("CardPool"))
                if pool or "CardPool" in raw:
                    course.card_pool = pool
                course.deck_id = str(summary.get("DeckId") or course.deck_id)
                main = deck.get("MainDeck")
                if isinstance(main, list):
                    course.main_deck = [entry for entry in main if isinstance(entry, dict)]
                course.updated_at = time.time()
                self._courses[name] = course
            updated.append(course)
        return updated

    def course(self, event_name: str) -> Course | None:
        with self._lock:
            return self._courses.get(event_name)

    def active_limited(self) -> Course | None:
        """The most recently updated limited course that is not finished."""
        with self._lock:
            live = [c for c in self._courses.values() if c.is_limited and c.stage not in {"done", "other"}]
        return max(live, key=lambda c: c.updated_at) if live else None

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "event": c.event_name,
                    "module": c.module,
                    "stage": c.stage,
                    "wins": c.wins,
                    "losses": c.losses,
                }
                for c in self._courses.values()
            ]
