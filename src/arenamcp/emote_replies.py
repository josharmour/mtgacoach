"""Small, deterministic replies to Arena opponents, never a chat feedback loop."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

logger = logging.getLogger(__name__)
REPLIES = {"Phrase_Basic_Hello": "hello", "Phrase_Basic_Nice": "thanks"}
MAX_AGE = 8.0
COOLDOWN = 10.0


class EmoteReplies:
    """Consume every observation; attempt each response at most once per game.

    Unknown phrases (including Thanks), repeats, stale events and events seen
    while disabled are discarded, not queued for a later surprise reply.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.games: OrderedDict = OrderedDict()
        self.last_attempt = float("-inf")

    def choose(self, events: list[dict], game: tuple, enabled: bool) -> dict | None:
        now = self.clock()
        selected = None
        if not all(game):
            return None
        seen = self.games.setdefault(game, set())
        self.games.move_to_end(game)
        while len(self.games) > 32:
            self.games.popitem(last=False)
        for event in events:
            if (event.get("match_id"), event.get("game_number")) != game:
                continue
            reply = REPLIES.get(event.get("text"))
            if not reply or reply in seen:
                continue
            seen.add(reply)
            stamp = event.get("received_at")
            if not isinstance(stamp, (float, int)) or not 0 <= now - stamp <= MAX_AGE:
                continue
            if enabled and selected is None and now - self.last_attempt >= COOLDOWN:
                selected = dict(event, reply=reply)
                self.last_attempt = now  # uncertain failures must never be retried
        return selected


class _EmoteRepliesMixin:
    def _observe_emote_replies(self, state: dict) -> None:
        try:
            from arenamcp.server import game_state

            controller = getattr(self, "_emote_replies", None)
            if controller is None:
                self._emote_replies = EmoteReplies()
                # Establish a watermark after initial log catch-up/restart.
                _, self._emote_reply_seq = game_state.opponent_emotes_since(0)
                return
            events, self._emote_reply_seq = game_state.opponent_emotes_since(self._emote_reply_seq)
            game = (state.get("match_id"), state.get("game_number"))
            event = controller.choose(events, game, self._emote_replies_allowed())
            if event:
                threading.Thread(
                    target=self._send_emote_reply, args=(event,), daemon=True, name="emote-reply"
                ).start()
        except Exception:
            logger.warning("[EMOTE REPLY] observation failed", exc_info=True)

    def _emote_replies_allowed(self) -> bool:
        from arenamcp.gre_bridge import get_bridge

        return bool(
            self.settings.get("reply_emotes", True)
            and self._oops_autopilot_on()
            and self._oops_bridge_ready()
            and get_bridge().reply_emotes_supported
            and not self._oops_game_over()
            and not self._oops_concede_active()
            and not getattr(self, "draft_mode", False)
        )

    def _send_emote_reply(self, event: dict) -> None:
        try:
            from arenamcp.gre_bridge import get_bridge
            from arenamcp.server import game_state

            if not self._emote_replies_allowed():
                return
            if not 0 <= time.time() - event["received_at"] <= MAX_AGE:
                return
            if (game_state.match_id, game_state.game_number) != (event["match_id"], event["game_number"]):
                return
            result = get_bridge().send_emote(
                event["reply"],
                expected_match_id=event["match_id"],
                expected_game_number=event["game_number"],
            )
            logger.info("[EMOTE REPLY] %s -> %s: %s", event["text"], event["reply"], result)
        except Exception:
            logger.warning("[EMOTE REPLY] send failed; not retrying", exc_info=True)
