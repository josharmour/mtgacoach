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

from arenamcp.draft_autopick import choose_picks, is_ordinary_basic, pool_lane, rank_pack, wheel_adjust
from arenamcp.draft_narration import DraftNarrator
from arenamcp.event_course import LIMITED_MARKERS
from arenamcp.set_primer import SetPrimer

logger = logging.getLogger(__name__)

BASIC_COLORS = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}
MATCH_MODULES = {"TransitionToMatches", "WinLossGate", "WinNoGate"}
UNPAID_MODULES = {"Join", "Pay", "PayEntry"}
# Model pick calls took 2.4-7.3s (8s timeout) on 2026-10-06; 25s skipped the
# model for every pick from P1p10 on.
PICK_LLM_MIN_SECONDS = 15.0
# With commentary on, a pick is selected in Arena first (viewers see the cards
# before the pack goes), explained aloud, then confirmed after a short beat.
# The whole hold is bounded so a draft never stalls: no narration with less
# than PICK_NARRATION_MIN_CLOCK_S on the pick clock, never more than
# PICK_NARRATION_MAX_S per pick, and the confirm lands PICK_CLOCK_RESERVE_S
# before the clock runs out.
PICK_NARRATION_MIN_CLOCK_S = 15.0
PICK_NARRATION_MAX_S = 12.0
PICK_CLOCK_RESERVE_S = 5.0
PICK_CONFIRM_BEAT_S = 1.0
# The model may overrule the ranking only among near-equals: within this many
# score units (~2 GIH points) of the ranking's own pick, or in its top few.
MODEL_PICK_TOLERANCE = 0.5
QUEUE_WAIT_S = 240.0
# A model deck is kept only if it scores within this of the best counted build
# (scores run ~250-280; a couple of card swaps move them a few points).
DECK_SCORE_TOLERANCE = 8.0
# The 17Lands deck-strength model ranks two builds of one pool well in sealed
# (slope 0.82 vs real results) but weakly in draft (0.30), so it may overrule
# the counted builder's first choice by these predicted win-rate margins.
STRENGTH_OVERRIDE_MARGIN = {"sealed": 0.005, "draft": 0.015}


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
    sealed_done_presses: int = 0
    # The build decided for a pool, reused on retries so a retried submit is the deck narrated.
    deck_plan: dict | None = None
    reviewed_pool: tuple = ()
    # The pick autoplay selected in Arena and has not confirmed yet (key, cards, decision).
    preview: dict | None = None
    # The deck plan the model last stated; the commentary's lane follows it, not the pool estimate.
    plan: str = ""
    plan_lane: str = ""  # the advisor's validated lane code for that plan


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
        review_fn: Callable[[str, Callable[[], bool]], bool] | None = None,
        commentary_fn: Callable[[], bool] = lambda: True,
        queue_fn: Callable[[], bool] = lambda: True,
        narrate_fn: Callable[[str, Callable[[], bool]], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
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
        # review_fn(narration, cancelled) speaks the deck comparison and returns
        # True only after it has been heard; cancelled() turns true when autoplay
        # is switched off or resumed meanwhile.
        self._review_fn = review_fn
        # Whether picks are explained aloud (setting draft_commentary).
        self._commentary_fn = commentary_fn
        # Whether the player wants the next match queued (the Auto-queue toggle).
        self._queue_fn = queue_fn
        # narrate_fn(line, cancelled) speaks a pick's explanation and returns once
        # it has been heard (or cancelled() turned true); the pick is confirmed after.
        self._narrate_fn = narrate_fn
        self._clock = clock
        self._sleep = sleep
        self._narrator = DraftNarrator()
        self._control = 0
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
            self._control += 1
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
            self._control += 1
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
        elif screen.get("sealed_open"):
            self._step_sealed_open()
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
        read_at = self._clock()
        if not state.get("is_open"):
            self._wait(1.0)
            return
        if not state.get("ok_to_pick") or state.get("animating") or not state.get("pack_cards"):
            self._wait(0.7)
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
        if state.get("reserved_count"):
            if run.preview and run.preview["key"] == key:
                # Autoplay's own selection, explained but not confirmed yet
                # (autoplay was toggled meanwhile, or the confirm missed).
                self._finish_previewed_pick(run.preview)
                return
            self._status("Cards are already reserved in this pack; letting the client finish that pick")
            self._wait(2.0)
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
            mana_costs=self._mana_costs(pack + self._drafted_pool(), primer),
            players=self._pod_size(),
        )
        to_actual = {}
        for actual, canon in canonical.items():
            to_actual.setdefault(canon, actual)
        chosen = [to_actual[pick.grp_id] for pick in ranked]
        reasons = {to_actual[pick.grp_id]: "; ".join(pick.reasons[:3]) for pick in ranked}
        source, from_model = "ranking", set()
        seconds = state.get("pick_seconds_remaining")
        if self._pick_advisor_fn and self._pack_fn and (not seconds or seconds >= PICK_LLM_MIN_SECONDS):
            refined = self._refine_pick(pack, primer, pool, ranked, required)
            if refined:
                chosen, reasons, source, from_model = refined
        views = {view["grp_id"]: view.get("title_id") for view in state.get("pack_views") or []}
        decision = {
            "key": key,
            "cards": [{"grp_id": g, "title_id": views.get(g)} for g in chosen],
            "chosen": chosen,
            "reasons": reasons,
            "source": source,
        }
        context = {
            "pack": pack,
            "pool": pool,
            "primer": primer,
            "pack_number": pack_number,
            "pick_number": pick_number,
            "model_reasons": {g: reasons.get(g, "") for g in chosen if g in from_model},
            "required": required,
        }
        deadline = self._narration_deadline(seconds, read_at)
        if deadline is not None:
            self._narrate_then_pick(decision, context, deadline)
            return
        result = self._command("submit_draft_pick", cards=decision["cards"], timeout=8.0)
        run.pick_attempts[key] += 1
        if not result.get("ok"):
            self._status(f"Pick P{pack_number}p{pick_number} not accepted yet: {result.get('error')}")
            self._wait(1.5)
            return
        names = self._record_pick(decision)
        self._say(self._pick_commentary(chosen=chosen, names=names, **context))
        self._wait(1.2)

    # -- narrated picks: select, explain, beat, confirm ---------------------------

    def _commentary_on(self) -> bool:
        try:
            return bool(self._commentary_fn())
        except Exception:
            return False

    def _narration_deadline(self, seconds: Any, read_at: float) -> float | None:
        """When a narrated pick must be confirmed by, or None to pick first and explain after."""
        if self._narrate_fn is None or not self._commentary_on():
            return None
        now = self._clock()
        deadline = now + PICK_NARRATION_MAX_S
        if seconds:
            left = float(seconds) - (now - read_at)
            if left < PICK_NARRATION_MIN_CLOCK_S:
                logger.info("Pick clock at %.0fs: picking without narrating first", left)
                return None
            deadline = min(deadline, now + left - PICK_CLOCK_RESERVE_S)
        return deadline

    def _narrate_then_pick(self, decision: dict, context: dict, deadline: float) -> None:
        """Select the cards in Arena, say why, give viewers a beat, then confirm.

        2026-10-07 (live Pick-Two): the pick was submitted first and explained
        after the cards had left the screen. Everything here ends by
        ``deadline``; switching autoplay off while it speaks leaves the cards
        selected but never confirms them (the client's own clock then drafts
        the selected cards).
        """
        run, key = self.run, decision["key"]
        control = self._control

        def cancelled() -> bool:
            return not self.enabled or self._control != control

        self.owns_ui = True
        names = [self._card_name(g) for g in decision["chosen"]]
        line = self._pick_commentary(chosen=decision["chosen"], names=names, verb="Choosing", **context)
        shown = self._draft_command_quiet("preview_draft_pick", cards=decision["cards"], timeout=8.0)
        if shown.get("ok"):
            run.preview = decision
        else:
            logger.info("Draft pick not selected before narrating: %s", shown.get("error"))
        self._status(f"P{key[0]}p{key[1]}: {line}")
        narration_ends = deadline - PICK_CONFIRM_BEAT_S
        try:
            self._narrate_fn(line, lambda: cancelled() or self._clock() >= narration_ends)
        except Exception as exc:
            logger.warning("Draft pick narration failed: %s", exc)
        if cancelled():
            logger.info(
                "Draft pick P%sp%s not confirmed: autoplay was switched off while narrating", *key[:2]
            )
            return
        self._hold(min(PICK_CONFIRM_BEAT_S, max(0.0, deadline - self._clock())), cancelled)
        if cancelled():
            logger.info("Draft pick P%sp%s not confirmed: autoplay was switched off", *key[:2])
            return
        # Arena may have moved on (the pick clock, a manual pick) while we spoke.
        fresh = self._command("get_draft_state")
        fresh_key = (
            int(fresh.get("pack_number") or 0),
            int(fresh.get("pick_number") or 0),
            tuple(sorted(int(g) for g in fresh.get("pack_cards") or [])),
        )
        if not fresh.get("is_open") or fresh_key != key:
            run.preview = None
            self._status(f"P{key[0]}p{key[1]} moved on while narrating; not picking it again")
            self._wait(1.0)
            return
        if not fresh.get("ok_to_pick") or fresh.get("animating"):
            self._wait(0.7)  # the next step finishes our selection if it is still there
            return
        self._confirm_pick(decision, selected=run.preview is decision)

    def _finish_previewed_pick(self, decision: dict) -> None:
        """Confirm a pick autoplay already selected and explained."""
        shown = self._draft_command_quiet("preview_draft_pick", cards=decision["cards"], timeout=8.0)
        if not shown.get("ok"):
            self.run.preview = None
            self._status("Different cards are selected in this pack; letting the client finish that pick")
            self._wait(2.0)
            return
        self._confirm_pick(decision, selected=True)

    def _confirm_pick(self, decision: dict, *, selected: bool) -> bool:
        """Confirm the selected cards (or pick them outright when they could not be selected)."""
        key = decision["key"]
        action = "confirm_draft_pick" if selected else "submit_draft_pick"
        result = self._command(action, cards=decision["cards"], timeout=8.0)
        self.run.pick_attempts[key] += 1
        if not result.get("ok"):
            self._status(f"Pick P{key[0]}p{key[1]} not accepted yet: {result.get('error')}")
            self._wait(1.5)
            return False
        self._record_pick(decision)
        self._wait(1.2)
        return True

    def _record_pick(self, decision: dict) -> list[str]:
        run, key, chosen = self.run, decision["key"], decision["chosen"]
        run.last_pick_key, run.last_pick_at = key, time.monotonic()
        run.pool += chosen
        run.preview = None
        names = [self._card_name(g) for g in chosen]
        run.picks.append({"key": key[:2], "grp_ids": chosen, "source": decision["source"]})
        self._status(f"P{key[0]}p{key[1]}: took {' and '.join(names)} ({decision['source']})")
        logger.info(
            "Draft pick P%sp%s %s: %s", key[0], key[1], names, [decision["reasons"].get(g) for g in chosen]
        )
        return names

    def _draft_command_quiet(self, action: str, **fields: Any) -> dict:
        """A bridge command whose absence is not fatal (an older bridge lacks the pick preview)."""
        bridge = self._bridge_fn()
        if bridge is None or not getattr(bridge, "connected", False):
            return {"ok": False, "error": "bridge not connected"}
        return bridge.draft_command(action, **fields)

    def _hold(self, seconds: float, cancelled: Callable[[], bool]) -> None:
        end = self._clock() + seconds
        while not cancelled():
            left = end - self._clock()
            if left <= 0:
                return
            self._sleep(min(0.25, left))

    def _pick_commentary(
        self,
        *,
        chosen,
        names,
        pack,
        pool,
        primer,
        pack_number,
        pick_number,
        model_reasons,
        required=1,
        verb="Taking",
    ) -> str:
        """Why these cards and where the draft is heading, or just the cards when commentary is off."""
        brief = f"{verb} {' and '.join(names)}."
        try:
            if not self._commentary_fn():
                return brief
            mana = self._mana_costs(pack + self._drafted_pool(), primer)
            canon = {g: self._canonical(g, primer) for g in pack}
            ranking = rank_pack(
                [canon[g] for g in pack],
                pool,
                primer,
                pack_number=pack_number,
                pick_number=pick_number,
                names={canon[g]: self._card_name(g) for g in pack},
                mana_costs=mana,
            )
            by_id = {pick.grp_id: pick for pick in ranking}
            spoken = [(g, name) for g, name in zip(chosen, names, strict=False) if canon.get(g) in by_id]
            taken = [canon[g] for g in chosen if g in canon]
            line = self._narrator.pick_line(
                names=[name for _g, name in spoken],
                chosen=[by_id[canon[g]] for g, _name in spoken],
                ranking=ranking,
                pool=pool + taken,
                lane_before=pool_lane(pool, primer, mana),
                lane_after=pool_lane(pool + taken, primer, mana),
                primer=primer,
                pack_number=pack_number,
                pick_number=pick_number,
                # Picks left in this pack, Pick-Two included (it takes two per pass).
                pack_size=pick_number + -(-len(pack) // max(1, required)) - 1,
                model_reasons=[model_reasons.get(g, "") for g, _name in spoken],
                mana_costs=mana,
                verb=verb,
                model_plan=self.run.plan,
                model_lane=self.run.plan_lane,
            )
            logger.info("Draft commentary P%sp%s: %s", pack_number, pick_number, line)
            return line
        except Exception as exc:
            logger.debug("draft commentary unavailable: %s", exc)
            return brief

    def _pod_size(self) -> int:
        """Drafters sharing the packs: Pick-Two pods seat four, other drafts eight."""
        name = self.run.event_name.lower().replace("_", "").replace("-", "")
        return 4 if "picktwo" in name else 8

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

    def _mana_costs(self, grp_ids: list[int], primer: SetPrimer | None) -> dict[int, str]:
        """Use Arena costs so hybrid cards are evaluated by what the deck can pay."""
        costs = {}
        for grp_id in set(grp_ids):
            card = self._db.get_card(grp_id) if self._db is not None else None
            cost = getattr(card, "mana_cost", "") or ""
            if cost:
                costs[self._canonical(grp_id, primer)] = cost
        return costs

    def _refine_pick(self, pack, primer, pool, ranked, required) -> tuple[list[int], dict, str, set] | None:
        """Let the draft advisor choose with the primer; its answer is validated against the pack.

        Returns (picks, reasons, source, the picks that are the model's own).
        """
        try:
            details = self._pack_fn() or {}
            if sorted(int(c["grp_id"]) for c in details.get("cards") or []) != sorted(pack):
                return None
            lane = pool_lane(pool, primer, self._mana_costs(self._drafted_pool(), primer))
            details = {**details, "picks_per_pack": required}
            if self._pool_cards_fn:
                # The model and ranking must see the same pool after a takeover.
                details["picked_cards"] = self._pool_cards_fn(self._drafted_pool(), self.run.set_code)
            if primer is not None:
                details["set_strategy"] = primer.prompt_context(lane.colors)
            ranking = rank_pack(
                [self._canonical(g, primer) for g in pack],
                pool,
                primer,
                pack_number=int(details.get("pack_number") or 1),
                pick_number=int(details.get("pick_number") or 1),
                names={self._canonical(g, primer): self._card_name(g) for g in pack},
                mana_costs=self._mana_costs(pack + self._drafted_pool(), primer),
            )
            # The model sees the same wheel plan the ranking would follow.
            ranking = wheel_adjust(
                ranking,
                [self._canonical(g, primer) for g in pack],
                primer,
                pick_number=int(details.get("pick_number") or 1),
                players=self._pod_size(),
                picks_per_pass=required,
            )
            evaluations = [pick.as_evaluation() for pick in ranking[:10]]
            result = self._pick_advisor_fn().recommend(details, {"evaluations": evaluations})
            if result.get("reasoning_source") != "card_rules":
                return None
            picks = [int(rec["grp_id"]) for rec in result.get("recommendations") or []]
            if len(picks) != required or len(set(picks)) != required or any(p not in pack for p in picks):
                return None
            nonbasics = sum(not is_ordinary_basic(self._card_name(g)) for g in pack)
            basics_allowed = max(0, required - nonbasics)
            # The ranking is 17Lands quality plus lane, curve and interaction
            # needs; the model only breaks near-ties with its reasoning. Each
            # card is judged on its own: in Pick-Two a far-off second card no
            # longer throws away an acceptable first (2026-10-07 P2p1).
            position = {pick.grp_id: index for index, pick in enumerate(ranking)}
            floor = ranking[min(required, len(ranking)) - 1].score - MODEL_PICK_TOLERANCE if ranking else 0.0
            kept: list[int] = []
            for grp_id in picks:
                if is_ordinary_basic(self._card_name(grp_id)):
                    if basics_allowed <= 0:
                        logger.warning("Ignoring model basic-land pick while nonbasic cards remain")
                        continue
                    basics_allowed -= 1
                canon = self._canonical(grp_id, primer)
                index = position.get(canon, len(ranking))
                if index >= required + 2 and (index >= len(ranking) or ranking[index].score < floor):
                    logger.warning(
                        "Model pick %s ranks #%d (%.2f) vs ranking floor %.2f; %s",
                        self._card_name(grp_id),
                        index + 1,
                        ranking[index].score if index < len(ranking) else float("nan"),
                        floor,
                        "replacing it with the ranking's choice"
                        if required > 1
                        else "keeping the ranking's pick",
                    )
                    continue
                kept.append(grp_id)
            if not kept:
                return None
            reasons = {
                int(rec["grp_id"]): rec.get("reason", "")
                for rec in result["recommendations"]
                if int(rec["grp_id"]) in kept
            }
            from_model = set(kept)
            if len(kept) < required:
                actual = {}
                for grp_id in pack:
                    actual.setdefault(self._canonical(grp_id, primer), grp_id)
                fill = [actual[p.grp_id] for p in list(ranked) + ranking if p.grp_id in actual]
                by_canon = {p.grp_id: p for p in list(ranked) + ranking}
                for grp_id in fill:
                    if len(kept) >= required:
                        break
                    if grp_id in kept or (is_ordinary_basic(self._card_name(grp_id)) and basics_allowed <= 0):
                        continue
                    kept.append(grp_id)
                    reasons[grp_id] = "; ".join(by_canon[self._canonical(grp_id, primer)].reasons[:3])
                    logger.info(
                        "Pick-Two: keeping model pick %s; the ranking's %s replaces the rejected one",
                        " and ".join(self._card_name(g) for g in from_model),
                        self._card_name(grp_id),
                    )
                if len(kept) < required:
                    return None
            # lane= is the plan's own colors (validated by the advisor); pool= is
            # the color-count estimate, which the commentary no longer speaks
            # when a plan names colors (2026-10-08: it said Dimir over an Izzet plan).
            logger.info(
                "Draft strategy P%sp%s: lane=%s; pool=%s; plan=%s; needs=%s",
                details.get("pack_number"),
                details.get("pick_number"),
                result.get("lane") or "open",
                lane.colors or "open",
                result.get("plan", ""),
                "; ".join(result.get("needs") or []),
            )
            source = "model" if len(from_model) == required else "model+ranking"
            self.run.plan = str(result.get("plan") or "")
            self.run.plan_lane = str(result.get("lane") or "")
            return kept, reasons, source, from_model
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
        pool_signature = tuple(sorted(counts.items()))
        plan = run.deck_plan if run.deck_plan and run.deck_plan["pool"] == pool_signature else None
        if plan is None:
            plan = self._decide_deck(pool_ids)
            if plan is None:
                return
            run.deck_plan = {**plan, "pool": pool_signature}
        build, source, cards = plan["build"], plan["source"], plan["cards"]
        target = deck_entries(build, basics, pool_view)
        if target is None:
            self._pause("no basic lands of the deck's colors are in the pool; finish the deck manually")
            return
        reviewed = False
        if self._review_fn and run.reviewed_pool != pool_signature:
            if not self._review_deck(build, plan["options"], cards, basics, counts):
                return
            run.reviewed_pool, reviewed = pool_signature, True
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
        if reviewed:
            self._say("Deck submitted.")
        else:
            from arenamcp.limited_deck import deck_choice_summary

            explanation = deck_choice_summary(build, cards)
            logger.info("Draft deck explanation: %s", explanation)
            self._say(explanation)
        self._wait(3.0)

    def _decide_deck(self, pool_ids: list[int]) -> dict | None:
        """The counted builds plus the advisor's refinement, if it holds up against them."""
        run = self.run
        if not run.set_code:
            run.set_code = self._set_code_for(pool_ids)
        primer = self._primer(run.set_code)
        cards = self._pool_cards_fn(pool_ids, run.set_code) if self._pool_cards_fn else []
        from arenamcp.limited_deck import fallback_deck, score_deck

        fmt = self._limited_format(pool_ids)
        pair_rates = {key: row["win_rate"] for key, row in (primer.pair_stats if primer else {}).items()}
        build = fallback_deck(cards, pair_rates, fmt=fmt)
        if not build.get("main_deck"):
            self._pause("no legal 40-card build was found in this pool; build it manually")
            return None
        lane = pool_lane(
            [self._canonical(g, primer) for g in pool_ids], primer, self._mana_costs(pool_ids, primer)
        )
        if primer is not None:
            build["set_strategy"] = primer.prompt_context(lane.colors)
        build["format"] = fmt
        build["pool_cards"] = cards
        options = build.get("deck_options") or []
        source = "counted build"
        strength = self._strength_scorer(cards, fmt, primer)
        preferred = self._strongest_option(options, strength, fmt)
        if preferred is not None:
            extras = {key: build[key] for key in ("set_strategy", "format", "pool_cards") if key in build}
            options = [preferred] + [option for option in options if option is not preferred]
            build = {**build, **preferred, **extras, "deck_options": options, "strength_preferred": True}
            source = "counted build, 17Lands deck model"
        best = build.get("quality") or {}
        if self._deck_advisor_fn:
            refined = self._deck_advisor_fn().recommend_deck(build)
            if refined is not build and refined.get("main_deck"):
                quality = score_deck(refined["main_deck"], cards, pair_rates, fmt=fmt)
                weaker = (
                    strength is not None
                    and fmt == "sealed"
                    and (strength(refined) < strength(build) - STRENGTH_OVERRIDE_MARGIN["sealed"])
                )
                if weaker:
                    logger.info("Model deck rated weaker by the 17Lands deck model; keeping %s", source)
                elif quality["score"] >= best.get("score", 0) - DECK_SCORE_TOLERANCE:
                    build, source, best = {**refined, "quality": quality}, "model", quality
                else:
                    logger.info(
                        "Model deck %s scored %s vs counted %s %s; keeping the counted build",
                        quality["colors"],
                        quality["score"],
                        best.get("colors"),
                        best.get("score"),
                    )
        logger.info(
            "Limited %s deck chosen (%s): %s; candidates %s", fmt, source, best, build.get("candidates")
        )
        try:  # 17Lands deck-strength estimate: logs only, never spoken, never blocks the deck
            from arenamcp.deck_strength import log_build_strength

            log_build_strength(build, options, cards, fmt, primer)
        except Exception as exc:
            logger.warning("Deck strength unavailable: %s", exc)
        return {"build": build, "source": source, "options": options, "cards": cards}

    @staticmethod
    def _strength_scorer(cards: list[dict], fmt: str, primer: SetPrimer | None):
        """Predicted win rate of a build (17Lands deck model), or None when it cannot load."""
        try:
            from arenamcp.deck_strength import SetContext, evaluate_build, set_context_from_primer

            ctx = set_context_from_primer(primer) if primer is not None else SetContext()

            def score(build: dict) -> float:
                return evaluate_build(build, cards, ctx, fmt).win_rate

            return score
        except Exception as exc:
            logger.info("Deck strength model unavailable: %s", exc)
            return None

    @staticmethod
    def _strongest_option(options: list[dict], strength, fmt: str) -> dict | None:
        """An option clearly stronger than the counted builder's first choice, if any."""
        if strength is None or len(options) < 2:
            return None
        try:
            rated = [(strength(option), index) for index, option in enumerate(options)]
        except Exception as exc:
            logger.info("Deck strength scoring failed: %s", exc)
            return None
        top_rate, top_index = max(rated)
        if top_index == 0 or top_rate < rated[0][0] + STRENGTH_OVERRIDE_MARGIN.get(fmt, 0.015):
            return None
        logger.info(
            "17Lands deck model prefers option %d (%.1f%%) over the counted first choice (%.1f%%)",
            top_index + 1,
            top_rate * 100,
            rated[0][0] * 100,
        )
        return options[top_index]

    def _limited_format(self, pool_ids: list[int]) -> str:
        """Sealed pools come from six packs; a draft pool is the picks (about 42-45 cards)."""
        names = [self.run.event_name]
        tracker = self._tracker_fn()
        course = tracker.active_limited() if tracker else None
        names.append(getattr(course, "event_name", "") or "")
        if any("sealed" in name.lower() for name in names if name) or len(pool_ids) >= 60:
            return "sealed"
        return "draft"

    def _step_sealed_open(self) -> None:
        """Reveal the sealed pool and continue to the deck builder."""
        run = self.run
        if run.sealed_done_presses >= 3:
            self._pause(
                "the sealed pool screen did not continue to the deck builder; press Done or claim the reward"
            )
            return
        result = self._command("finish_sealed_open", timeout=8.0)
        if not result.get("ok"):
            self._status(f"Sealed pool screen not advanced yet: {result.get('error')}")
            self._wait(2.0)
            return
        step = result.get("step")
        if step == "done":
            run.sealed_done_presses += 1
            self._status("Sealed pool opened; continuing to the deck builder")
        elif step == "open":
            self._status("Opening the sealed pool")
        self._wait(3.0)

    def _review_deck(self, build: dict, options: list[dict], cards: list[dict], basics, counts) -> bool:
        """Narrate the deck and its best alternative; True only when submitting is still right."""
        from arenamcp.limited_deck import deck_review_narration

        narration, _described = deck_review_narration(build, options, cards)
        logger.info("Deck review narration: %s", narration)
        self._status("Reviewing deck options before submitting: " + narration)
        self.owns_ui = True
        control = self._control

        def cancelled() -> bool:
            return not self.enabled or self._control != control

        try:
            heard = bool(self._review_fn(narration, cancelled))
        except Exception as exc:
            logger.warning("Deck review narration failed: %s", exc)
            heard = False
        if cancelled():
            return False  # autoplay was switched off or resumed; the next step starts over
        if not heard:
            self._pause(
                "deck review was stopped before it finished; turn autoplay off and on to hear it again and submit"
            )
            return False
        # The narration takes a while; Arena may have moved on meanwhile.
        if not self._command("get_screen").get("deck_builder"):
            self._wait(2.0, owns=False)
            return False
        refreshed = self._command("get_limited_pool", basic_candidates=list(basics), timeout=10.0)
        observed: Counter = Counter()
        for entry in (refreshed.get("main_deck") or []) + (refreshed.get("sideboard") or []):
            if entry["grp_id"] not in basics:
                observed[entry["grp_id"]] += entry["count"]
        if not refreshed.get("ok") or observed != counts:
            self._wait(2.0)
            return False
        return True

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
            # Not a failure: after the prize is claimed the event returns to
            # its entry stage. Pausing here outlived the player's next entry
            # (2026-10-06 15:23: P1p1 of the re-entered draft went unpicked).
            # Wait for the player to enter; that entry starts a fresh run.
            self.run = DraftRun()
            self._status(f"{event_name}: waiting for you to enter; autoplay never pays")
            self._wait(5.0, owns=False)
            return
        if module in MATCH_MODULES and not self._queue_fn():
            # 2026-10-06: with Auto-queue off, autoplay still queued every
            # PremierDraft match. Picks, the deck and prizes stay automatic.
            self._status(f"Auto-queue is off: press Play when you're ready for the next {event_name} match")
            self._wait(5.0, owns=False)
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
