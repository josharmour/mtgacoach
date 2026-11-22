"""Deterministic between-match coaching brief from local match history.

Compares the current deck's recent win/loss record against the current session
and the player's other recently played decks so Coach can give concrete,
grounded advice between matches instead of generically suggesting a new deck.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arenamcp.match_history import MatchRecord

BETWEEN_MATCH_LABEL = "BETWEEN MATCHES"
QUEUE_PLAN_LABEL = "AUTO-QUEUE PLAN"

# Constructed / Brawl free or ranked queues eligible for automatic queueing.
# Paid events, drafts, sealed, and limited queues are intentionally excluded.
QUEUEABLE_FREE_EVENTS = frozenset(
    {
        "Play",
        "Ladder",
        "Alchemy_Play",
        "Alchemy_Ladder",
        "Historic_Play",
        "Historic_Ladder",
        "Explorer_Play",
        "Explorer_Ladder",
        "Timeless_Play",
        "Timeless_Ladder",
        "Play_Brawl_Historic",
        "Brawl_Ladder",
        "Play_Brawl_Standard",
        "Standard_Brawl",
    }
)

_MIN_CURRENT_GAMES = 6
_MIN_WINNING_GAMES = 8
_MIN_ALT_GAMES = 5
_MIN_ALT_RATE = 0.50
_MIN_ALT_ADVANTAGE = 0.08


@dataclass(frozen=True)
class DeckStats:
    """Aggregated recent win/loss stats for one deck."""

    deck_key: str
    deck_name: str
    wins: int
    losses: int

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def rate(self) -> float:
        return self.wins / self.total if self.total > 0 else 0.0

    @property
    def label(self) -> str:
        return self.deck_name.strip() or "this deck"


@dataclass(frozen=True)
class BetweenMatchBrief:
    """Structured between-match coaching recommendation."""

    action: str  # "keep_queueing", "switch_deck", "review_or_tune", "small_sample"
    headline: str
    detail: str
    current_deck: DeckStats
    session_wins: int
    session_losses: int
    consecutive_losses: int
    best_alternative: DeckStats | None = None

    @property
    def message(self) -> str:
        return f"{self.headline}\n{self.detail}"

    @property
    def suggest_deck_building(self) -> bool:
        """True only when the deck is struggling and no stronger existing deck is available."""
        return self.action == "review_or_tune"


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _deck_key(rec: MatchRecord) -> str:
    ident = (getattr(rec, "deck_identity", "") or "").strip()
    if ident:
        return f"id:{ident}"
    name = (getattr(rec, "deck_name", "") or "").strip()
    if name:
        return f"name:{name.casefold()}"
    return ""


def summarize_between_matches(
    records: Iterable[MatchRecord],
    latest: MatchRecord | None = None,
    *,
    consecutive_losses: int = 0,
    now: datetime | None = None,
    window_days: int = 7,
    session_hours: int = 6,
) -> BetweenMatchBrief | None:
    """Build an evidence-backed between-match coaching brief from recent MatchRecords."""
    all_recs = [r for r in records if isinstance(r, MatchRecord) and r.result in ("win", "loss")]
    if not all_recs:
        return None

    # Deduplicate by match_id (keep the latest entry for each match_id).
    deduped: list[MatchRecord] = []
    seen_ids: set[str] = set()
    for rec in reversed(all_recs):
        if rec.match_id:
            if rec.match_id in seen_ids:
                continue
            seen_ids.add(rec.match_id)
        deduped.append(rec)
    deduped.reverse()

    target = latest if (latest is not None and latest.result in ("win", "loss")) else deduped[-1]
    ref_now = now or _parse_ts(target.timestamp) or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    window_cutoff = ref_now - timedelta(days=max(1, window_days))
    session_cutoff = ref_now - timedelta(hours=max(1, session_hours))

    account_id = (getattr(target, "local_account_id", "") or "").strip()

    recent: list[tuple[MatchRecord, datetime | None]] = []
    for rec in deduped:
        if account_id:
            rec_acct = (getattr(rec, "local_account_id", "") or "").strip()
            if rec_acct and rec_acct != account_id:
                continue
        ts = _parse_ts(rec.timestamp)
        if ts is not None and ts < window_cutoff:
            continue
        recent.append((rec, ts))

    if not recent:
        return None

    # Session stats (recent matches within session_hours)
    session_wins = 0
    session_losses = 0
    for rec, ts in recent:
        if ts is None or ts >= session_cutoff:
            if rec.result == "win":
                session_wins += 1
            else:
                session_losses += 1

    cur_key = _deck_key(target)
    by_deck: dict[str, list[MatchRecord]] = {}
    for rec, _ts in recent:
        key = _deck_key(rec)
        if not key:
            continue
        by_deck.setdefault(key, []).append(rec)

    if cur_key and cur_key in by_deck:
        cur_recs = by_deck[cur_key]
        cur_name = next((r.deck_name for r in reversed(cur_recs) if r.deck_name), target.deck_name)
    else:
        # No deck identity/name available: aggregate across all recent matches.
        cur_recs = [rec for rec, _ts in recent]
        cur_name = target.deck_name or "your current deck"

    cur_wins = sum(1 for r in cur_recs if r.result == "win")
    cur_losses = len(cur_recs) - cur_wins
    current = DeckStats(cur_key, cur_name, cur_wins, cur_losses)

    # Trailing loss streak on this deck
    trailing_losses = 0
    for r in reversed(cur_recs):
        if r.result == "loss":
            trailing_losses += 1
        else:
            break
    eff_losses = max(int(consecutive_losses or 0), trailing_losses)

    # Find best alternative deck the player ALREADY plays
    best_alt: DeckStats | None = None
    if cur_key:
        candidates: list[DeckStats] = []
        for key, recs in by_deck.items():
            if key == cur_key or len(recs) < _MIN_ALT_GAMES:
                continue
            w = sum(1 for r in recs if r.result == "win")
            l = len(recs) - w
            name = next((r.deck_name for r in reversed(recs) if r.deck_name), "")
            if not name:
                continue
            stats = DeckStats(key, name, w, l)
            if stats.rate >= _MIN_ALT_RATE and stats.rate >= current.rate + _MIN_ALT_ADVANTAGE:
                candidates.append(stats)
        if candidates:
            best_alt = max(candidates, key=lambda s: (s.rate, s.total))

    sw, sl = session_wins, session_losses

    # 1. Clear underperformance on current deck while a proven existing deck is available -> switch back
    if (
        current.total >= _MIN_CURRENT_GAMES
        and (current.rate < 0.45 or (eff_losses >= 3 and current.rate < 0.48))
        and best_alt is not None
    ):
        gap_pp = round((best_alt.rate - current.rate) * 100)
        return BetweenMatchBrief(
            action="switch_deck",
            headline=(
                f"Switch back to {best_alt.label} ({best_alt.wins}W-{best_alt.losses}L, "
                f"{best_alt.rate:.0%}) instead of queueing {current.label}."
            ),
            detail=(
                f"{current.label} is {current.wins}W-{current.losses}L ({current.rate:.0%}) over "
                f"the last {window_days}d (session {sw}W-{sl}L). {best_alt.label} is ahead by "
                f"{gap_pp} pp — switch back before trying to build a new deck."
            ),
            current_deck=current,
            session_wins=sw,
            session_losses=sl,
            consecutive_losses=eff_losses,
            best_alternative=best_alt,
        )

    # 2. Winning deck (>= 50% over >= 8 games) -> stay the course, even through a 3-loss variance skid
    if current.total >= _MIN_WINNING_GAMES and current.rate >= 0.50:
        if eff_losses >= 3:
            return BetweenMatchBrief(
                action="keep_queueing",
                headline=(
                    f"Keep queueing {current.label} — {eff_losses} straight losses look like normal variance."
                ),
                detail=(
                    f"{current.label} is still {current.wins}W-{current.losses}L ({current.rate:.0%}) "
                    f"over your last {current.total} games (session {sw}W-{sl}L). No deck switch needed."
                ),
                current_deck=current,
                session_wins=sw,
                session_losses=sl,
                consecutive_losses=eff_losses,
                best_alternative=best_alt,
            )
        detail = (
            f"Two losses in a row, but {current.label} is net positive over {current.total} games — "
            "stick with the plan."
            if eff_losses == 2
            else f"{current.label} is winning above 50% over your last {current.total} games — keep queueing."
        )
        return BetweenMatchBrief(
            action="keep_queueing",
            headline=(
                f"Stay on {current.label} ({current.wins}W-{current.losses}L, "
                f"{current.rate:.0%}) · Session {sw}W-{sl}L."
            ),
            detail=detail,
            current_deck=current,
            session_wins=sw,
            session_losses=sl,
            consecutive_losses=eff_losses,
            best_alternative=best_alt,
        )

    # 3. Current deck is struggling and no stronger existing deck is available -> review/tune
    cur_cap = current.label[:1].upper() + current.label[1:]
    if current.total >= _MIN_CURRENT_GAMES and current.rate < 0.48:
        return BetweenMatchBrief(
            action="review_or_tune",
            headline=(
                f"{cur_cap} is struggling ({current.wins}W-{current.losses}L, "
                f"{current.rate:.0%}) — review your losses or tune the list."
            ),
            detail=(
                f"Session {sw}W-{sl}L. Check your recent losses for curve or interaction gaps "
                "before spending wildcards on a brand-new deck."
            ),
            current_deck=current,
            session_wins=sw,
            session_losses=sl,
            consecutive_losses=eff_losses,
            best_alternative=best_alt,
        )

    # 4. Small sample (< 6 games, or 6-7 games near 50%)
    if eff_losses >= 3 and best_alt is not None:
        return BetweenMatchBrief(
            action="switch_deck",
            headline=(
                f"{cur_cap} started {current.wins}W-{current.losses}L — consider switching "
                f"back to {best_alt.label} ({best_alt.rate:.0%})."
            ),
            detail=(
                f"Small sample on {current.label} ({current.total} games, session {sw}W-{sl}L), "
                f"while {best_alt.label} is proven at {best_alt.wins}W-{best_alt.losses}L "
                f"({best_alt.rate:.0%}) — switch back before trying to build a new deck."
            ),
            current_deck=current,
            session_wins=sw,
            session_losses=sl,
            consecutive_losses=eff_losses,
            best_alternative=best_alt,
        )

    return BetweenMatchBrief(
        action="small_sample",
        headline=(
            f"{cur_cap}: {current.wins}W-{current.losses}L ({current.total} recent games) · "
            f"Session {sw}W-{sl}L."
        ),
        detail="Sample size is still small — play a few more matches before changing decks.",
        current_deck=current,
        session_wins=sw,
        session_losses=sl,
        consecutive_losses=eff_losses,
        best_alternative=best_alt,
    )


@dataclass(frozen=True)
class QueueableDeck:
    """One deck/queue pair Arena can auto-queue, scored by recent win rate."""

    deck_id: str
    deck_name: str
    event_id: str
    deck_identity: str
    wins: int
    losses: int

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def rate(self) -> float:
        return self.wins / self.total if self.total > 0 else 0.0

    @property
    def score(self) -> float:
        """Wilson lower bound (z=1.28) so proven 52%+ decks beat 1-0 noise and cold streaks."""
        n = self.total
        if n <= 0:
            return 0.35
        z = 1.28
        p = self.wins / n
        z2 = z * z
        denom = 1.0 + z2 / n
        center = p + z2 / (2.0 * n)
        margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
        return (center - margin) / denom

    @property
    def label(self) -> str:
        return self.deck_name.strip() or self.deck_id or "Unknown deck"

    def to_target(self) -> dict[str, object]:
        return {
            "deck_id": self.deck_id,
            "deck_name": self.deck_name,
            "event_id": self.event_id,
            "quest_mode": True,
        }


@dataclass(frozen=True)
class QueuePlan:
    """Ranked list of queueable decks and a concise recommendation."""

    decks: tuple[QueueableDeck, ...]
    recommended: QueueableDeck | None
    summary: str


def load_queueable_pairs(log_path: Path | None = None) -> list[dict[str, str]]:
    """Read recently submitted free/ranked (deck_id, deck_name, event_id) pairs from Arena's Player.log."""
    import os

    from arenamcp.deck_library import load_log_decks
    from arenamcp.watcher import DEFAULT_LOG_PATH, _normalize_log_path

    path = log_path
    if path is None:
        env_path = os.environ.get("MTGA_LOG_PATH")
        path = _normalize_log_path(env_path or DEFAULT_LOG_PATH)
    path = Path(path)
    if not path.exists():
        return []

    raw_pairs: dict[tuple[str, str], dict[str, str]] = {}
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 8_000_000))
            chunk = stream.read().decode("utf-8", errors="ignore")
    except OSError:
        return []

    for line in chunk.splitlines():
        if "==> EventSetDeckV3 " not in line:
            continue
        try:
            envelope = json.loads(line.split("==> EventSetDeckV3 ", 1)[1])
            if not isinstance(envelope, dict):
                continue
            request = json.loads(envelope["request"])
            if not isinstance(request, dict):
                continue
            event_id = str(request.get("EventName") or "").strip()
            if event_id not in QUEUEABLE_FREE_EVENTS:
                continue
            summary = request.get("Summary")
            if not isinstance(summary, dict):
                continue
            deck_id = str(summary.get("DeckId") or "").strip()
            if not deck_id:
                continue
            deck_name = str(summary.get("Name") or "").strip()
            raw_pairs[(deck_id, event_id)] = {
                "deck_id": deck_id,
                "deck_name": deck_name,
                "event_id": event_id,
                "deck_identity": "",
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            continue

    if not raw_pairs:
        return []

    try:
        log_decks = load_log_decks([path])
    except Exception:
        log_decks = {}

    # Enrich deck names and deck identities from Player.log's deck library
    result: list[dict[str, str]] = []
    for entry in raw_pairs.values():
        rec = log_decks.get(entry["deck_id"])
        if rec is not None:
            if rec.name:
                entry["deck_name"] = rec.name
            try:
                entry["deck_identity"] = rec.identity()
            except Exception:
                pass
        if entry["deck_name"]:
            result.append(entry)
    return result


def evaluate_queueable_decks(
    records: Iterable[MatchRecord],
    pairs: Iterable[dict[str, str]],
    *,
    now: datetime | None = None,
    window_days: int = 14,
    account_id: str = "",
) -> QueuePlan:
    """Rank queueable constructed/Brawl decks using Wilson lower-bound recent win rates."""
    valid_pairs: list[dict[str, str]] = []
    seen_pair: set[tuple[str, str]] = set()
    for p in pairs:
        event_id = str(p.get("event_id") or "").strip()
        deck_id = str(p.get("deck_id") or "").strip()
        deck_name = str(p.get("deck_name") or "").strip()
        if not deck_id or not deck_name or event_id not in QUEUEABLE_FREE_EVENTS:
            continue
        key = (deck_id, event_id)
        if key in seen_pair:
            continue
        seen_pair.add(key)
        valid_pairs.append(
            {
                "deck_id": deck_id,
                "deck_name": deck_name,
                "event_id": event_id,
                "deck_identity": str(p.get("deck_identity") or "").strip(),
            }
        )

    if not valid_pairs:
        return QueuePlan(
            decks=(),
            recommended=None,
            summary="No recent free/ranked constructed decks found in Arena's log yet — play one match first.",
        )

    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    cutoff = ref_now - timedelta(days=max(1, window_days))

    # Deduplicate completed records within the evaluation window
    deduped: list[MatchRecord] = []
    seen_ids: set[str] = set()
    for rec in reversed([r for r in records if isinstance(r, MatchRecord) and r.result in ("win", "loss")]):
        if rec.match_id:
            if rec.match_id in seen_ids:
                continue
            seen_ids.add(rec.match_id)
        if account_id:
            rec_acct = (getattr(rec, "local_account_id", "") or "").strip()
            if rec_acct and rec_acct != account_id:
                continue
        ts = _parse_ts(rec.timestamp)
        if ts is not None and ts < cutoff:
            continue
        deduped.append(rec)

    by_ident: dict[str, list[MatchRecord]] = {}
    by_name: dict[str, list[MatchRecord]] = {}
    for rec in deduped:
        ident = (getattr(rec, "deck_identity", "") or "").strip()
        if ident:
            by_ident.setdefault(ident, []).append(rec)
        name = (getattr(rec, "deck_name", "") or "").strip().casefold()
        if name:
            by_name.setdefault(name, []).append(rec)

    scored: list[QueueableDeck] = []
    for p in valid_pairs:
        ident = p["deck_identity"]
        name_low = p["deck_name"].casefold()
        matched = by_ident.get(ident) if ident else None
        if not matched:
            matched = by_name.get(name_low) or []
        wins = sum(1 for r in matched if r.result == "win")
        losses = len(matched) - wins
        scored.append(
            QueueableDeck(
                deck_id=p["deck_id"],
                deck_name=p["deck_name"],
                event_id=p["event_id"],
                deck_identity=ident,
                wins=wins,
                losses=losses,
            )
        )

    scored.sort(key=lambda d: (d.score, d.total, d.wins, d.deck_name), reverse=True)
    recommended = scored[0]
    lines = [f"Queueable decks ({len(scored)} found):"]
    for idx, deck in enumerate(scored[:5]):
        rec_str = f"{deck.wins}W-{deck.losses}L ({deck.rate:.0%})" if deck.total > 0 else "no recent record"
        tag = " ← Recommended" if idx == 0 else ""
        lines.append(f"• {deck.label} ({deck.event_id}): {rec_str}{tag}")
    return QueuePlan(decks=tuple(scored), recommended=recommended, summary="\n".join(lines))


def evaluate_live_queue_plan(log_path: Path | None = None) -> QueuePlan:
    """Best-effort live evaluation of queueable decks from Player.log + MatchHistory."""
    try:
        from arenamcp.match_history import MatchHistory

        history = MatchHistory()
        records = history.get_recent(history.total_games)
        account_id = ""
        for rec in reversed(records):
            acct = (getattr(rec, "local_account_id", "") or "").strip()
            if acct:
                account_id = acct
                break
        pairs = load_queueable_pairs(log_path=log_path)
        return evaluate_queueable_decks(records, pairs, account_id=account_id)
    except Exception:
        return QueuePlan(
            decks=(),
            recommended=None,
            summary="Could not read Arena's recent decks yet — Auto-queue will repeat your latest queue.",
        )
