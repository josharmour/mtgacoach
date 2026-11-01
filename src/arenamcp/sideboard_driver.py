"""One automatic sideboard plan and submission attempt per completed Limited game."""

from __future__ import annotations

import time

from arenamcp.sideboard_plan import unchanged_plan, validate_plan


class SideboardDriver:
    def __init__(self, command, status, advisor_fn=None, context_fn=None, clock=time.monotonic):
        self._command, self._status = command, status
        self._advisor_fn, self._context_fn = advisor_fn, context_fn
        self._attempted: dict[tuple, tuple] = {}
        self._clock = clock
        self._attempted_at = 0.0
        self._last_status = ""
        self._awaiting_result = False

    def _say(self, text):
        if text != self._last_status:
            self._last_status = text
            self._status(text)

    def left_sideboard(self) -> None:
        self._awaiting_result = False

    def step(self, cancelled) -> None:
        state = self._command("get_sideboard_state", timeout=5.0)
        if not state.get("ok"):
            if self._awaiting_result:
                if self._clock() - self._attempted_at > 15:
                    self._say(
                        "Sideboarding has not advanced; finish it in Arena. The submission will not be repeated."
                    )
                return  # The spent request/editor may linger while Arena accepts the deck.
            self._say(f"Sideboarding needs manual review: {state.get('error') or 'native deck unavailable'}")
            return
        identity = state.get("request_id")
        if not isinstance(identity, list) or len(identity) != 5:
            self._say("Sideboarding needs manual review: request identity unavailable")
            return
        key = tuple(identity[:2])
        if key in self._attempted:
            if tuple(identity) != self._attempted[key]:
                self._say(
                    "Arena requested another deck for this game; finish sideboarding manually. Not resubmitting."
                )
            elif self._clock() - self._attempted_at > 15:
                self._say(
                    "Sideboarding has not advanced; finish it in Arena. The submission will not be repeated."
                )
            return  # Server rejection or a lost reply is not permission to send a second deck.
        try:
            plan = unchanged_plan(state)
            reason = "Keeping the current deck."
            remaining = state.get("remaining_seconds")
            if self._advisor_fn and (remaining is None or remaining > 15):
                context = self._context_fn(state) if self._context_fn else {}
                advisor = self._advisor_fn()
                if advisor is not None:
                    plan, reason = advisor.choose(
                        state, context.get("cards", []), context.get("opponent", [])
                    )
            plan = validate_plan(state, plan)
        except Exception as exc:
            self._say(f"Sideboarding needs manual review: {exc}")
            return
        if cancelled():
            return
        self._attempted[key] = tuple(identity)
        self._attempted_at = self._clock()
        self._awaiting_result = True
        self._say(f"Sideboarding: {reason}")
        try:
            reply = self._command(
                "submit_sideboard", request_id=identity, source_deck=state["deck"], plan=plan, timeout=8.0
            )
        except Exception as exc:
            reply = {"ok": False, "outcome_unknown": True, "error": str(exc)}
        if reply.get("ok"):
            self._say("Sideboard submitted; waiting for Arena to start the next game.")
        else:
            self._say(
                f"Sideboard submission stopped; not retrying: {reply.get('error') or 'outcome unknown'}"
            )
