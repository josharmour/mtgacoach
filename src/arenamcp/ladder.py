"""Arena ladder standings, active event courses, and ranked queue classification."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

LADDERS = ("Constructed", "Limited")
RANK_HISTORY_LIMIT = 10

_TIER_PIPS = {
    "Bronze": 4,
    "Silver": 5,
    "Gold": 6,
    "Platinum": 6,
    "Diamond": 6,
}

_LIMITED_RANKED_PREFIXES = (
    "premierdraft",
    "quickdraft",
    "picktwodraft",
    "picktwoquickdraft",
    "rankeddraft",
    "contenderdraft",
)

_NON_EVENT_QUEUES = frozenset(
    {
        "play",
        "ladder",
        "alchemyplay",
        "alchemyladder",
        "historicplay",
        "historicladder",
        "explorerplay",
        "explorerladder",
        "timelessplay",
        "timelessladder",
        "playbrawlhistoric",
        "brawlladder",
        "playbrawlstandard",
        "standardbrawl",
        "traditionalladder",
        "constructedbestof3",
        "aibotmatch",
        "directgame",
        "dualcolorprecons",
    }
)

_NON_EVENT_PREFIXES = (
    "colorchallenge",
    "sparky",
    "spark",
    "npe",
    "aibotmatch",
    "directgame",
)

_EVENT_PREFIX_LABELS = (
    ("ContenderDraft", "Contender Draft"),
    ("PremierDraft", "Premier Draft"),
    ("QuickDraft", "Quick Draft"),
    ("PickTwoDraft", "Pick-Two Draft"),
    ("PickTwoQuickDraft", "Pick-Two Quick Draft"),
    ("TradDraft", "Trad Draft"),
    ("TraditionalDraft", "Trad Draft"),
    ("Midweek_Magic", "Midweek Magic"),
    ("MidweekMagic", "Midweek Magic"),
    ("Jump_In", "Jump In"),
    ("JumpIn", "Jump In"),
    ("Sealed", "Sealed"),
)

_DATE_SUFFIX_RE = re.compile(r"[_ ]20\d{6}$")
_EVENT_ID_RE = re.compile(r'"eventId"\s*:\s*"([^"]+)"')
_COURSE_HEADER_RE = re.compile(
    rb"<== (EventGetCoursesV2|EventJoin|EventSetDeckV3|DraftCompleteDraft|EventClaimPrize)\("
)


def normalize_event_key(event_name: str) -> str:
    return "".join((event_name or "").lower().split()).replace("_", "").replace("-", "")


def ranked_ladder(event_name: str, *, extra_ranked: dict[str, str] | None = None) -> str | None:
    """Identify ranked queues in current and older MatchHistory format names."""
    name = normalize_event_key(event_name)
    if not name:
        return None
    if extra_ranked:
        matched = extra_ranked.get(name)
        if matched in LADDERS:
            return matched
        base = normalize_event_key(_DATE_SUFFIX_RE.sub("", str(event_name or "").strip()))
        matched = extra_ranked.get(base)
        if matched in LADDERS:
            return matched
    if name.startswith(_LIMITED_RANKED_PREFIXES):
        return "Limited"
    if "unranked" not in name and (name.endswith("ladder") or "ranked" in name):
        return "Limited" if any(word in name for word in ("limited", "draft", "sealed")) else "Constructed"
    return None


def pip_progress(snapshot: dict, ladder: str) -> tuple[int, int] | None:
    """Return (filled_pips, total_pips) for non-Mythic tiers, or None if unavailable/Mythic."""
    prefix = ladder.lower()
    tier = snapshot.get(prefix + "Class")
    level = snapshot.get(prefix + "Level")
    if not tier and not level:
        return None
    tier = tier or "Bronze"
    if tier == "Mythic":
        return None
    steps = snapshot.get(prefix + "Step", 0)
    total = _TIER_PIPS.get(tier, 6)
    return (max(0, min(total, int(steps or 0))), total)


def rank_short(snapshot: dict, ladder: str) -> str:
    """Compact rank label without the trailing pip count (e.g. 'Bronze 2' or 'Mythic 88.2%')."""
    prefix = ladder.lower()
    tier = snapshot.get(prefix + "Class")
    level = snapshot.get(prefix + "Level")
    if not tier and not level:
        return "Unavailable"
    tier = tier or "Bronze"
    if tier == "Mythic":
        place = snapshot.get(prefix + "LeaderboardPlace")
        if isinstance(place, int) and place > 0:
            return f"Mythic #{place}"
        pct = snapshot.get(prefix + "Percentile")
        return f"Mythic {pct:.1f}%" if isinstance(pct, (int, float)) else "Mythic"
    return f"{tier} {level}" if level else tier


def rank_text(snapshot: dict, ladder: str) -> str:
    prefix = ladder.lower()
    tier = snapshot.get(prefix + "Class")
    level = snapshot.get(prefix + "Level")
    if not tier and not level:
        return "Unavailable"
    # Arena omits the zero-valued Bronze class and zero progress from its JSON.
    tier = tier or "Bronze"
    if tier == "Mythic":
        place = snapshot.get(prefix + "LeaderboardPlace")
        if isinstance(place, int) and place > 0:
            return f"Mythic #{place}"
        pct = snapshot.get(prefix + "Percentile")
        return f"Mythic {pct:.1f}%" if isinstance(pct, (int, float)) else "Mythic"
    rank = f"{tier} {level}" if level else tier
    steps = snapshot.get(prefix + "Step", 0)
    return f"{rank} · {steps} {'pip' if steps == 1 else 'pips'}"


def event_display_name(internal_name: str) -> str:
    """Readable display name for an MTGA event (e.g. 'ContenderDraft_TRK_20261123' -> 'Contender Draft · TRK')."""
    raw = _DATE_SUFFIX_RE.sub("", str(internal_name or "").strip()).strip("_ ")
    if not raw:
        return "Active Event"
    for prefix, label in _EVENT_PREFIX_LABELS:
        if raw.lower().startswith(prefix.lower()):
            rest = raw[len(prefix) :].strip("_ ")
            if rest:
                return f"{label} · {rest.replace('_', ' ')}"
            return label
    return raw.replace("_", " ")


def event_rule_summary(internal_name: str, module: str = "") -> str:
    """Concise stage or end-condition label for the active event banner."""
    if module in {"PlayerDraft", "BotDraft", "HumanDraft", "Draft"}:
        return "Drafting"
    if module in {"DeckSelect", "GrantCardPool"}:
        return "Building deck"
    if module == "ClaimPrize":
        return "Prize ready to claim"
    if module == "Jumpstart":
        return "Choose packets"
    low = normalize_event_key(internal_name)
    if "trad" in low and any(k in low for k in ("draft", "sealed")):
        return "3 matches"
    if any(k in low for k in ("draft", "sealed", "cube")):
        return "7W prize · 3L out"
    if "jumpin" in low or "jumpstart" in low:
        return "Win for prize"
    if "midweek" in low:
        return "3 wins for prizes"
    return "Event active"


def _is_banner_event(internal_name: str) -> bool:
    key = normalize_event_key(internal_name)
    if not key or key in _NON_EVENT_QUEUES:
        return False
    return not any(key.startswith(p) for p in _NON_EVENT_PREFIXES)


def _extract_courses(payload: dict) -> list[dict]:
    found: list[dict] = []
    if isinstance(payload.get("Courses"), list):
        found.extend(c for c in payload["Courses"] if isinstance(c, dict))
    if isinstance(payload.get("Course"), dict):
        found.append(payload["Course"])
    if "InternalEventName" in payload and "CurrentModule" in payload:
        found.append(payload)
    return found


class RankLog:
    """Follow complete combined-rank, course, and match-event replies in Player.log."""

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.snapshot: dict = {}
        self.account_name = ""
        self._account_id = ""
        self._identity = None
        self._offset = 0
        self._anchor = b""
        self._awaiting_rank = False
        self._awaiting_course = ""
        self._courses: dict[str, dict] = {}
        self._course_seq = 0
        self._active_event_name = ""
        self._last_match_event = ""
        self.observed_ranked_events: dict[str, str] = {}

    def _observe_account(self, line: bytes) -> None:
        """Use only local login identity, never an opponent or a credential."""
        text = line.decode("utf-8", errors="replace")
        marker = "[Accounts - Login] Logged in successfully. Display Name: "
        if marker in text:
            name = text.split(marker, 1)[1].strip()
            if name != self.account_name:
                self.snapshot = {}
                self._account_id = ""
                self._courses = {}
                self._active_event_name = ""
            self.account_name = name
        elif "[Accounts - Logout]" in text:
            self.account_name = ""
            self._account_id = ""
            self.snapshot = {}
            self._courses = {}
            self._active_event_name = ""
        if '"authenticateResponse"' not in text:
            return
        # Authentication is embedded in a prefixed log JSON line. Restrict the
        # extraction to this object so other players' screen names cannot win.
        match = re.search(r'"authenticateResponse"\s*:\s*(\{[^{}]*\})', text)
        if not match:
            return
        try:
            auth = json.loads(match.group(1))
        except ValueError:
            return
        account_id = auth.get("clientId")
        name = auth.get("screenName")
        if not isinstance(account_id, str) or not account_id:
            return
        if self._account_id and account_id != self._account_id:
            self.snapshot = {}
            self.account_name = ""
            self._courses = {}
            self._active_event_name = ""
        self._account_id = account_id
        if isinstance(name, str) and name.strip():
            if self.account_name and self.account_name != name.strip():
                self.snapshot = {}
                self._courses = {}
                self._active_event_name = ""
            self.account_name = name.strip()

    def _observe_rank_snapshot(self, payload: dict) -> None:
        prev = self.snapshot
        if prev and self._last_match_event:
            prev_lim = sum(
                int(prev.get(k) or 0)
                for k in ("limitedMatchesWon", "limitedMatchesLost", "limitedMatchesDrawn")
            )
            new_lim = sum(
                int(payload.get(k) or 0)
                for k in ("limitedMatchesWon", "limitedMatchesLost", "limitedMatchesDrawn")
            )
            prev_con = sum(
                int(prev.get(k) or 0)
                for k in ("constructedMatchesWon", "constructedMatchesLost", "constructedMatchesDrawn")
            )
            new_con = sum(
                int(payload.get(k) or 0)
                for k in ("constructedMatchesWon", "constructedMatchesLost", "constructedMatchesDrawn")
            )
            key = normalize_event_key(self._last_match_event)
            if key:
                if new_lim > prev_lim:
                    self.observed_ranked_events[key] = "Limited"
                elif new_con > prev_con:
                    self.observed_ranked_events[key] = "Constructed"
        self.snapshot = payload

    def _observe_course_payload(self, kind: str, payload: dict) -> None:
        courses = _extract_courses(payload)
        if not courses:
            return
        seen_names: set[str] = set()
        for raw in courses:
            name = str(raw.get("InternalEventName") or "").strip()
            if not name:
                continue
            seen_names.add(name)
            module = str(raw.get("CurrentModule") or "")
            wins = int(raw.get("CurrentWins") or 0)
            losses = int(raw.get("CurrentLosses") or 0)
            prev = self._courses.get(name)
            changed = prev is None or (prev["module"], prev["wins"], prev["losses"]) != (
                module,
                wins,
                losses,
            )
            self._course_seq += 1
            self._courses[name] = {
                "event_name": name,
                "module": module,
                "wins": wins,
                "losses": losses,
                "seq": self._course_seq,
            }
            if kind != "EventGetCoursesV2":
                if _is_banner_event(name):
                    if module in {"Complete", "None", ""}:
                        if self._active_event_name == name:
                            self._active_event_name = ""
                    else:
                        self._active_event_name = name
            elif changed and prev is not None and _is_banner_event(name):
                if module in {"Complete", "None", ""}:
                    if self._active_event_name == name:
                        self._active_event_name = ""
                else:
                    self._active_event_name = name

        if kind == "EventGetCoursesV2" and self._active_event_name:
            cur = self._courses.get(self._active_event_name)
            if (
                self._active_event_name not in seen_names
                or cur is None
                or cur["module"] in {"Complete", "None", "", "Join", "Pay", "PayEntry"}
            ):
                self._active_event_name = ""

    def active_event(self, recent_records: list | None = None) -> dict | None:
        """Return the active event course for the Live Event Banner, or None."""
        inactive = {"Complete", "None", "", "Join", "Pay", "PayEntry"}
        chosen: dict | None = None

        if self._active_event_name:
            cand = self._courses.get(self._active_event_name)
            if cand and cand["module"] not in inactive and _is_banner_event(cand["event_name"]):
                chosen = cand

        if chosen is None and recent_records:
            newest = recent_records[0]
            fmt_key = normalize_event_key(getattr(newest, "format_name", "") or "")
            if fmt_key:
                for cand in self._courses.values():
                    if (
                        cand["module"] not in inactive
                        and _is_banner_event(cand["event_name"])
                        and normalize_event_key(cand["event_name"]) == fmt_key
                    ):
                        chosen = cand
                        break

        if chosen is None:
            limited_live = [
                c
                for c in self._courses.values()
                if c["module"] not in inactive
                and _is_banner_event(c["event_name"])
                and any(w in c["event_name"].lower() for w in ("draft", "sealed", "cube"))
            ]
            if limited_live:
                chosen = max(limited_live, key=lambda c: c["seq"])

        if chosen is None:
            return None

        name = chosen["event_name"]
        ranked = ranked_ladder(name, extra_ranked=self.observed_ranked_events)
        return {
            "event_name": name,
            "display_name": event_display_name(name),
            "module": chosen["module"],
            "wins": chosen["wins"],
            "losses": chosen["losses"],
            "ranked": ranked,
            "rule": event_rule_summary(name, chosen["module"]),
        }

    def read(self, path: Path) -> bool:
        """Read a bounded batch. Return True when more complete lines are ready."""
        try:
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (path, stat.st_dev, stat.st_ino)
                # Unity can truncate and refill the same inode between polls.
                stream.seek(self._offset - len(self._anchor))
                if identity != self._identity or stream.read(len(self._anchor)) != self._anchor:
                    self._reset()
                    self._identity = identity
                stream.seek(self._offset)
                for line in stream.readlines(512 * 1024):
                    if not line.endswith(b"\n"):
                        break  # Retry the incomplete line on the next poll.
                    self._offset += len(line)
                    self._observe_account(line)
                    if b'"eventId"' in line:
                        m = _EVENT_ID_RE.search(line.decode("utf-8", errors="replace"))
                        if m:
                            ev = m.group(1).strip()
                            self._last_match_event = ev
                            if _is_banner_event(ev):
                                self._active_event_name = ev
                            else:
                                self._active_event_name = ""
                    if self._awaiting_rank:
                        try:
                            payload = json.loads(line)
                        except (ValueError, UnicodeError):
                            payload = None
                        if isinstance(payload, dict) and (
                            "constructedSeasonOrdinal" in payload or "limitedSeasonOrdinal" in payload
                        ):
                            self._observe_rank_snapshot(payload)
                    elif self._awaiting_course:
                        try:
                            payload = json.loads(line)
                        except (ValueError, UnicodeError):
                            payload = None
                        if isinstance(payload, dict):
                            self._observe_course_payload(self._awaiting_course, payload)
                    self._awaiting_rank = b"<== RankGetCombinedRankInfo(" in line
                    course_m = _COURSE_HEADER_RE.search(line)
                    self._awaiting_course = course_m.group(1).decode("ascii") if course_m else ""
                more = stream.tell() < stat.st_size
                stream.seek(max(0, self._offset - 128))
                self._anchor = stream.read(min(128, self._offset))
                return more
        except OSError:
            self._reset()
            return False
