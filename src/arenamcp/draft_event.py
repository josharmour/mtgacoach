"""Autoplay for a joined limited (draft) event: picks, deck, and every match.

The player enters (and pays for) the event; from the first pick on, autopilot
plays it to the end:

1. Draft: rank each pack with the set primer (17lands data + card rules), let
   the draft advisor refine within the pick timer, then pick through the
   client's own double-click path (ReserveCardAndLockIn).
2. Deck: build 40 cards from the real pool (primer-aware, validated), write
   them into the limited deck builder and press Done.
3. Matches: on the event page press Play only in a stage that cannot pay;
   the normal in-match autopilot plays; after each match leave the result
   screen back to the event page. Claim the prize at the end.

Eyes and hands: every step re-reads the screen through the Mac bridge and the
course stage from Player.log before acting again; a step that does not take
effect is retried at most twice, then autoplay pauses with a reason instead
of guessing. Entry-fee and drop/resign paths are never called.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from arenamcp.draft_autopick import choose_picks, pool_lane, rank_pack
from arenamcp.event_course import LIMITED_MARKERS
from arenamcp.set_primer import SetPrimer

logger = logging.getLogger(__name__)

BASIC_COLORS = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}
MATCH_MODULES = {"TransitionToMatches", "WinLossGate", "WinNoGate"}
UNPAID_MODULES = {"Join", "Pay", "PayEntry"}
PICK_LLM_MIN_SECONDS = 25.0
QUEUE_WAIT_S = 240.0
# A model deck is kept only if it scores within this of the best counted build
# (scores run ~250-280; a couple of card swaps move them a few points).
DECK_SCORE_TOLERANCE = 8.0


@dataclass
class DraftRun:
    """What autoplay has done in the current event."""

    event_name: str = ""
    set_code: str = ""
    pool: list[int] = field(default_factory=list)  # grp ids autoplay picked
    picks: list[dict] = field(default_factory=list)
    last_pick_key: tuple | None = None
    last_pick_at: float = 0.0
    pick_attempts: Counter = field(default_factory=Counter)
    deck_submitted_at: float = 0.0
    deck_attempts: int = 0
    pool_waits: int = 0
    queued_at: float = 0.0
    finished: bool = False


class DraftEventDriver:
    """Single-flight background driver; tick() never blocks the coach loop."""

    def __init__(
        self,
        *,
        bridge_fn: Callable[[], Any],
        tracker_fn: Callable[[], Any],
        primer_service: Any,
        pick_advisor_fn: Callable[[], Any] | None = None,
        deck_advisor_fn: Callable[[], Any] | None = None,
        pack_fn: Callable[[], dict] | None = None,
        pool_cards_fn: Callable[[list[int], str], list[dict]] | None = None,
        picked_fn: Callable[[], list[int]] | None = None,
        card_db: Any = None,
        in_match_fn: Callable[[], bool] = lambda: False,
        status_fn: Callable[[str], None] | None = None,
        speak_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._bridge_fn = bridge_fn
        self._tracker_fn = tracker_fn
        self._primers = primer_service
        self._pick_advisor_fn = pick_advisor_fn
        self._deck_advisor_fn = deck_advisor_fn
        self._pack_fn = pack_fn
        self._pool_cards_fn = pool_cards_fn
        self._picked_fn = picked_fn
        self._db = card_db
        self._in_match_fn = in_match_fn
        self._status_fn = status_fn
        self._speak_fn = speak_fn
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._next_poll = 0.0
        self._last_status = ""
        self.enabled = False
        self.paused_reason = ""
        self.owns_ui = False
        self.run = DraftRun()
        self._basics: dict[int, str] | None = None
        self._last_screen: tuple[str, ...] | None = None

    # -- control ------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            if self.enabled == bool(enabled):
                return
            self.enabled = bool(enabled)
            self.paused_reason = ""
            self.owns_ui = False
            self._next_poll = 0.0
            self._status(
                "Draft autoplay on: enter a draft and it plays from the first pick" if enabled else ""
            )

    def tick(self) -> bool:
        """Start a step if due; True while autoplay owns the limited-event UI."""
        with self._lock:
            if not self.enabled or self.paused_reason:
                return False
            if time.monotonic() >= self._next_poll and not (self._worker and self._worker.is_alive()):
                self._worker = threading.Thread(target=self._step_safely, daemon=True, name="draft-event")
                self._worker.start()
            return self.owns_ui

    def resume(self) -> None:
        with self._lock:
            self.paused_reason = ""
            self.run.pick_attempts.clear()
            self.run.deck_attempts = 0
            self._next_poll = 0.0

    def debug_info(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "paused_reason": self.paused_reason,
            "owns_ui": self.owns_ui,
            "event": self.run.event_name,
            "set": self.run.set_code,
            "picks": len(self.run.picks),
            "deck_submitted": bool(self.run.deck_submitted_at),
            "finished": self.run.finished,
        }

    # -- helpers --------------------------------------------------------------

    def _status(self, detail: str) -> None:
        if detail and detail != self._last_status:
            logger.info("Draft autoplay: %s", detail)
        self._last_status = detail
        if self._status_fn:
            self._status_fn(detail)

    def _say(self, text: str) -> None:
        if self._speak_fn and text:
            try:
                self._speak_fn(text)
            except Exception as exc:
                logger.debug("draft speech failed: %s", exc)

    def _pause(self, reason: str) -> None:
        self.paused_reason = reason
        self.owns_ui = False
        self._status(f"Draft autoplay paused: {reason}")

    def _wait(self, seconds: float, owns: bool = True) -> None:
        self.owns_ui = owns
        self._next_poll = time.monotonic() + seconds

    def _command(self, action: str, **fields: Any) -> dict:
        bridge = self._bridge_fn()
        if bridge is None or not getattr(bridge, "connected", False):
            return {"ok": False, "error": "bridge not connected"}
        response = bridge.draft_command(action, **fields)
        if response.get("unsupported"):
            self._pause("this Arena bridge cannot drive drafts (native macOS bridge required)")
        return response

    def _card_name(self, grp_id: int) -> str:
        card = self._db.get_card(grp_id) if self._db is not None else None
        return getattr(card, "name", "") or f"Card {grp_id}"

    def _set_code_for(self, grp_ids: list[int]) -> str:
        codes = Counter()
        for grp_id in grp_ids:
            card = self._db.get_card(grp_id) if self._db is not None else None
            code = str(getattr(card, "expansion_code", "") or "").upper()
            if code:
                codes[code] += 1
        return codes.most_common(1)[0][0] if codes else ""

    def _primer(self, set_code: str) -> SetPrimer | None:
        if not set_code:
            return None
        primer = self._primers.get(set_code)
        if primer is None:
            self._primers.ensure(set_code)
            primer = self._primers.quick(set_code)
        return primer

    def _canonical(self, grp_id: int, primer: SetPrimer | None) -> int:
        """The primer's id for this card (Arena may show a preferred printing)."""
        if primer is None or primer.card(grp_id) is not None:
            return grp_id
        card = primer.by_name().get(self._card_name(grp_id).lower())
        return card.grp_id if card is not None else grp_id

    def _new_run_if_needed(self, event_name: str, pack_number: int, pick_number: int) -> None:
        run = self.run
        if (event_name and run.event_name and event_name != run.event_name) or (
            pack_number == 1 and pick_number == 1 and run.picks and run.picks[-1]["key"][:2] != (1, 1)
        ):
            self.run = DraftRun()
        if event_name and not self.run.event_name:
            self.run.event_name = event_name

    # -- the step -------------------------------------------------------------

    def _step_safely(self) -> None:
        try:
            self._step()
        except Exception as exc:
            logger.exception("Draft autoplay step failed")
            self._wait(3.0, owns=False)
            self._status(f"Draft autoplay hit an error and will retry: {exc}")

    def _step(self) -> None:
        if self._in_match_fn():
            self._wait(3.0, owns=False)
            return
        screen = self._command("get_screen")
        if not screen.get("ok"):
            self._wait(5.0, owns=False)
            return
        seen = tuple(sorted(key for key, value in screen.items() if value is True and key != "ok"))
        if seen != self._last_screen:
            logger.info("Draft autoplay screen: %s", ", ".join(seen) or "none of draft/deck/event/home")
            self._last_screen = seen
        if screen.get("draft"):
            self._step_pick()
        elif screen.get("deck_builder"):
            self._step_deck()
        elif screen.get("match_end") and self.run.event_name:
            self._step_leave_match()
        elif screen.get("event_page"):
            self._step_event_page()
        elif screen.get("home") and self.run.event_name and not self.run.finished:
            self._step_home()
        else:
            self._wait(2.0, owns=False)

    # -- draft ---------------------------------------------------------------

    def _step_pick(self) -> None:
        state = self._command("get_draft_state")
        if not state.get("is_open"):
            self._wait(1.0)
            return
        if not state.get("ok_to_pick") or state.get("animating") or not state.get("pack_cards"):
            self._wait(0.7)
            return
        if state.get("reserved_count"):
            self._status("Cards are already reserved in this pack; letting the client finish that pick")
            self._wait(2.0)
            return
        pack = [int(g) for g in state["pack_cards"]]
        pack_number, pick_number = int(state.get("pack_number") or 0), int(state.get("pick_number") or 0)
        tracker = self._tracker_fn()
        course = tracker.active_limited() if tracker else None
        self._new_run_if_needed(getattr(course, "event_name", ""), pack_number, pick_number)
        run = self.run
        key = (pack_number, pick_number, tuple(sorted(pack)))
        if key == run.last_pick_key and time.monotonic() - run.last_pick_at < 8.0:
            self._wait(1.0)  # our pick is still landing
            return
        if run.pick_attempts[key] >= 2:
            self._pause(f"pick P{pack_number}p{pick_number} did not register after 2 tries; pick it manually")
            return
        if not run.set_code:
            run.set_code = self._set_code_for(pack)
        primer = self._primer(run.set_code)
        required = max(1, int(state.get("pick_num_cards_to_take") or 1))
        canonical = {grp_id: self._canonical(grp_id, primer) for grp_id in pack}
        pool = [self._canonical(g, primer) for g in self._drafted_pool()]
        ranked = choose_picks(
            [canonical[g] for g in pack],
            pool,
            primer,
            picks_required=required,
            pack_number=pack_number,
            pick_number=pick_number,
            names={canonical[g]: self._card_name(g) for g in pack},
        )
        to_actual = {}
        for actual, canon in canonical.items():
            to_actual.setdefault(canon, actual)
        chosen = [to_actual[pick.grp_id] for pick in ranked]
        reasons = {to_actual[pick.grp_id]: "; ".join(pick.reasons[:3]) for pick in ranked}
        source = "ranking"
        seconds = state.get("pick_seconds_remaining")
        if self._pick_advisor_fn and self._pack_fn and (not seconds or seconds >= PICK_LLM_MIN_SECONDS):
            refined = self._refine_pick(pack, primer, pool, ranked, required)
            if refined:
                chosen, reasons, source = refined[0], refined[1], "model"
        views = {view["grp_id"]: view.get("title_id") for view in state.get("pack_views") or []}
        result = self._command(
            "submit_draft_pick", cards=[{"grp_id": g, "title_id": views.get(g)} for g in chosen], timeout=8.0
        )
        run.pick_attempts[key] += 1
        if not result.get("ok"):
            self._status(f"Pick P{pack_number}p{pick_number} not accepted yet: {result.get('error')}")
            self._wait(1.5)
            return
        run.last_pick_key, run.last_pick_at = key, time.monotonic()
        run.pool += chosen
        names = [self._card_name(g) for g in chosen]
        run.picks.append({"key": key[:2], "grp_ids": chosen, "source": source})
        self._status(f"P{pack_number}p{pick_number}: took {' and '.join(names)} ({source})")
        logger.info(
            "Draft pick P%sp%s %s: %s", pack_number, pick_number, names, [reasons.get(g) for g in chosen]
        )
        self._say(f"Taking {' and '.join(names)}.")
        self._wait(1.2)

    def _drafted_pool(self) -> list[int]:
        """Every card picked this draft, including picks made before autoplay took over.

        Player.log records each pick (2026-10-05: autoplay joined at P2p13 of a
        mostly blue pool and, seeing only its own picks, read the lane as BR).
        """
        logged: list[int] = []
        if self._picked_fn is not None:
            try:
                logged = [int(g) for g in self._picked_fn() or []]
            except Exception as exc:
                logger.debug("draft picked cards unavailable: %s", exc)
        return logged if len(logged) >= len(self.run.pool) else list(self.run.pool)

    def _refine_pick(self, pack, primer, pool, ranked, required) -> tuple[list[int], dict, str] | None:
        """Let the draft advisor choose with the primer; its answer is validated against the pack."""
        try:
            details = self._pack_fn() or {}
            if sorted(int(c["grp_id"]) for c in details.get("cards") or []) != sorted(pack):
                return None
            lane = pool_lane(pool, primer)
            details = {**details, "picks_per_pack": required}
            if self._pool_cards_fn:
                # Autoplay's own picks are the pool; the log misses human-draft picks.
                details["picked_cards"] = self._pool_cards_fn(list(self.run.pool), self.run.set_code)
            if primer is not None:
                details["set_strategy"] = primer.prompt_context(lane.colors)
            evaluations = [
                pick.as_evaluation()
                for pick in rank_pack([self._canonical(g, primer) for g in pack], pool, primer)[:10]
            ]
            result = self._pick_advisor_fn().recommend(details, {"evaluations": evaluations})
            if result.get("reasoning_source") != "card_rules":
                return None
            picks = [int(rec["grp_id"]) for rec in result.get("recommendations") or []]
            if len(picks) != required or any(p not in pack for p in picks):
                return None
            return (
                picks,
                {int(rec["grp_id"]): rec.get("reason", "") for rec in result["recommendations"]},
                "model",
            )
        except Exception as exc:
            logger.info("Draft pick refinement unavailable: %s", exc)
            return None

    # -- deck -----------------------------------------------------------------

    def _basic_candidates(self) -> dict[int, str]:
        if self._basics is None:
            self._basics = basic_land_ids(self._db)
        return self._basics

    def _step_deck(self) -> None:
        run = self.run
        if run.deck_submitted_at and time.monotonic() - run.deck_submitted_at < 20.0:
            self._wait(2.0)
            return
        if run.deck_attempts >= 2:
            self._pause("the deck builder did not accept the deck after 2 tries; finish it manually")
            return
        basics = self._basic_candidates()
        pool_view = self._command("get_limited_pool", basic_candidates=list(basics), timeout=10.0)
        if not pool_view.get("ok"):
            self._wait(2.0)
            return
        counts: Counter = Counter()
        for entry in (pool_view.get("main_deck") or []) + (pool_view.get("sideboard") or []):
            if entry["grp_id"] not in basics:
                counts[entry["grp_id"]] += entry["count"]
        pool_ids = [grp_id for grp_id, count in counts.items() for _ in range(count)]
        if len(pool_ids) < 23:
            # The builder fills its pool after it opens; wait before giving up.
            run.pool_waits += 1
            if run.pool_waits >= 10:
                self._pause(f"the limited pool has only {len(pool_ids)} spells; build this deck manually")
            else:
                self._wait(2.0)
            return
        if not run.set_code:
            run.set_code = self._set_code_for(pool_ids)
        primer = self._primer(run.set_code)
        cards = self._pool_cards_fn(pool_ids, run.set_code) if self._pool_cards_fn else []
        from arenamcp.limited_deck import fallback_deck, score_deck

        pair_rates = {key: row["win_rate"] for key, row in (primer.pair_stats if primer else {}).items()}
        build = fallback_deck(cards, pair_rates)
        if not build.get("main_deck"):
            self._pause("no legal 40-card build was found in this pool; build it manually")
            return
        lane = pool_lane([self._canonical(g, primer) for g in pool_ids], primer)
        if primer is not None:
            build["set_strategy"] = primer.prompt_context(lane.colors)
        build["pool_cards"] = cards
        source = "counted build"
        best = build.get("quality") or {}
        if self._deck_advisor_fn:
            refined = self._deck_advisor_fn().recommend_deck(build)
            if refined is not build and refined.get("main_deck"):
                quality = score_deck(refined["main_deck"], cards, pair_rates)
                if quality["score"] >= best.get("score", 0) - DECK_SCORE_TOLERANCE:
                    build, source, best = refined, "model", quality
                else:
                    logger.info(
                        "Model deck %s scored %s vs counted %s %s; keeping the counted build",
                        quality["colors"],
                        quality["score"],
                        best.get("colors"),
                        best.get("score"),
                    )
        logger.info("Limited deck chosen (%s): %s; candidates %s", source, best, build.get("candidates"))
        target = deck_entries(build, basics, pool_view)
        if target is None:
            self._pause("no basic lands of the deck's colors are in the pool; finish the deck manually")
            return
        run.deck_attempts += 1
        written = self._command("set_limited_deck", main_deck=target, timeout=12.0)
        if not written.get("ok"):
            self._status(f"Deck not written yet: {written.get('error') or written.get('mismatched')}")
            self._wait(2.0)
            return
        submitted = self._command("submit_limited_deck", timeout=10.0)
        if not submitted.get("ok"):
            self._status(f"Deck Done not accepted yet: {submitted.get('error')}")
            self._wait(2.0)
            return
        run.deck_submitted_at = time.monotonic()
        total = sum(entry["count"] for entry in target)
        self._status(f"Submitted a {total}-card deck ({source}): {build.get('plan') or ''}".strip())
        self._say(f"Deck submitted. {build.get('plan') or ''}".strip())
        self._wait(3.0)

    # -- event page and matches ----------------------------------------------

    def _step_event_page(self) -> None:
        page = self._command("get_event_page")
        if not page.get("is_open"):
            self._wait(1.0)
            return
        module, event_name = page.get("module", ""), page.get("event_name", "")
        run = self.run
        if not run.event_name:
            # Adopt only a limited event already past entry: the player entered it.
            limited = any(marker in event_name.lower() for marker in LIMITED_MARKERS)
            if not limited or module in UNPAID_MODULES or module in {"Complete", "None", ""}:
                self._wait(3.0, owns=False)
                return
            self._new_run_if_needed(event_name, 0, 0)
        if event_name != run.event_name:
            self._wait(3.0, owns=False)
            return
        if module == "Complete":
            run.finished = True
            course = self._tracker_fn().course(event_name) if self._tracker_fn() else None
            record = f" {course.wins}-{course.losses}" if course else ""
            self._status(f"{event_name} complete{record}")
            self._say(f"Draft event complete{record}.")
            self._wait(10.0, owns=False)
            return
        if module in UNPAID_MODULES:
            self._pause(f"{event_name} needs an entry; autoplay never pays")
            return
        if module in MATCH_MODULES and run.queued_at and time.monotonic() - run.queued_at < QUEUE_WAIT_S:
            self._wait(3.0)  # queued: pressing Play again could cancel the queue
            return
        result = self._command("event_play", event_name=event_name, timeout=8.0)
        if not result.get("ok"):
            self._status(f"Event page not advanced: {result.get('error')}")
            self._wait(3.0)
            return
        if module in MATCH_MODULES:
            run.queued_at = time.monotonic()
            self._status(f"Queued for the next {event_name} match")
        elif module == "ClaimPrize":
            self._status("Claiming the event prize")
        self._wait(4.0)

    def _step_leave_match(self) -> None:
        if self._command("leave_match", timeout=8.0).get("ok"):
            self.run.queued_at = 0.0
            self._status("Match over; back to the event page")
        self._wait(4.0)

    def _step_home(self) -> None:
        result = self._command("go_to_event", event_name=self.run.event_name, timeout=8.0)
        if not result.get("ok"):
            self._status(f"Could not open {self.run.event_name}: {result.get('error')}")
        self._wait(4.0)


def basic_land_ids(card_db: Any) -> dict[int, str]:
    """Every basic land printing in the Arena card database, keyed to its color."""
    connection = getattr(card_db, "_conn", None)
    if connection is None:
        return {}
    try:
        rows = connection.execute(
            "SELECT c.GrpId, l.Loc FROM Cards c JOIN Localizations_enUS l ON l.LocId = c.TitleId "
            "WHERE l.Loc IN ('Plains', 'Island', 'Swamp', 'Mountain', 'Forest')"
        ).fetchall()
    except Exception as exc:
        logger.info("Basic land lookup failed: %s", exc)
        return {}
    return {int(row[0]): BASIC_COLORS[row[1]] for row in rows if row[1] in BASIC_COLORS}


def deck_entries(build: dict, basics: dict[int, str], pool_view: dict) -> list[dict] | None:
    """Deck builder entries for a validated build: spells by grp id plus owned basics by color."""
    entries = Counter()
    for entry in build.get("main_deck") or []:
        if int(entry["grp_id"]) not in basics:
            entries[int(entry["grp_id"])] += int(entry.get("count") or 1)
    in_pool = {int(grp_id) for grp_id, count in (pool_view.get("basics_in_pool") or {}).items() if count}
    in_main = {entry["grp_id"] for entry in pool_view.get("main_deck") or [] if entry["grp_id"] in basics}
    by_color: dict[str, int] = {}
    for grp_id in sorted(in_main) + sorted(in_pool - in_main):
        by_color.setdefault(basics[grp_id], grp_id)
    for color, count in (build.get("basic_lands") or {}).items():
        if not count:
            continue
        grp_id = by_color.get(str(color).upper())
        if grp_id is None:
            return None
        entries[grp_id] += int(count)
    return [{"grp_id": grp_id, "count": count} for grp_id, count in entries.items()]
