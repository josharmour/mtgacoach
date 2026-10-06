"""Autoplay for joined draft events, beside the in-match autopilot.

On when autopilot is on and the ``autopilot_draft_events`` setting is not
turned off. The player enters the event; draft picks, the deck build, and the
event's matches are then played through to the end (see draft_event.py).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _DraftEventMixin:
    def _draft_event_backend(self) -> Any:
        return getattr(self, "_autopilot_backend", None) or getattr(
            getattr(self, "_coach", None), "_backend", None
        )

    def _draft_event_in_match(self) -> bool:
        pack = getattr(self, "_draft_event_pack", None) or {}
        if pack.get("is_active") or pack.get("is_building"):
            return False  # Player.log says a draft or deck build is open
        state = self._mcp.get_game_state() if getattr(self, "_mcp", None) else {}
        state = state or {}
        turn = (state.get("turn") or {}).get("turn_number", 0) or 0
        if not state.get("match_id") or turn <= 0 or state.get("last_game_result"):
            return False
        # A finished match whose end was consumed keeps its board until the
        # next match starts; its completion record says it is over.
        from arenamcp.server import get_completed_match_for_navigation

        completed = get_completed_match_for_navigation()
        return not (completed.get("match_complete") and completed.get("match_id") == state.get("match_id"))

    def _draft_event_driver_instance(self) -> Any:
        driver = getattr(self, "_draft_event_driver", None)
        if driver is not None:
            return driver
        from arenamcp import server
        from arenamcp.draft_advisor import DraftAdvisor
        from arenamcp.draft_event import DraftEventDriver
        from arenamcp.gre_bridge import get_bridge
        from arenamcp.set_primer import SetPrimerService, arena_rules_lookup

        stats = server._get_draft_stats()
        card_db = server._get_mtgadb()
        primers = SetPrimerService(
            backend_fn=self._draft_event_backend,
            ratings_fn=stats.get_raw_ratings,
            color_stats_fn=stats.get_color_pair_stats,
            rules_lookup=arena_rules_lookup(card_db),
        )
        advisors: dict[str, Any] = {}

        def advisor(kind: str, timeout: float) -> Any:
            backend = self._draft_event_backend()
            current = advisors.get(kind)
            if current is None or getattr(current, "_backend", None) is not backend:
                advisors[kind] = DraftAdvisor(backend, timeout=timeout)
            return advisors[kind]

        def status(detail: str) -> None:
            if detail:
                self.ui.log(f"[DRAFT AUTOPLAY] {detail}\n")
                self.ui.status("DRAFT_AUTOPLAY", detail)

        driver = self._draft_event_driver = DraftEventDriver(
            bridge_fn=get_bridge,
            tracker_fn=server.get_course_tracker,
            primer_service=primers,
            pick_advisor_fn=lambda: advisor("pick", 8.0),
            deck_advisor_fn=lambda: advisor("deck", 60.0),
            pack_fn=self._mcp.get_draft_pack,
            pool_cards_fn=server.limited_pool_cards,
            picked_fn=lambda: list(server.draft_state.picked_cards),
            card_db=card_db,
            in_match_fn=self._draft_event_in_match,
            status_fn=status,
            speak_fn=lambda text: self.speak_advice(text, blocking=False),
            review_fn=self._narrate_deck_review,
            commentary_fn=lambda: bool(
                getattr(self, "settings", None) and self.settings.get("draft_commentary", True)
            ),
        )
        return driver

    def _narrate_deck_review(self, text: str, cancelled: Any) -> bool:
        """Speak the deck comparison; True only once it has been heard.

        The desktop owns audio, so completion comes back over the pipe as
        speech_status acknowledgments (speech_completion.py).
        """
        from arenamcp.speech_completion import narrate_and_wait

        ui = getattr(self, "ui", None)
        voice = getattr(self, "_voice_output", None)
        completion = getattr(ui, "speech_completion", None)
        emit = getattr(ui, "emit_speech_request", None)
        logger.info("SPEAK (deck review): %r", text[:160])
        if voice is None:
            return narrate_and_wait(text, send=None, completion=None, cancelled=cancelled)
        muted = bool(getattr(voice, "muted", False))
        if completion is not None and callable(emit):
            voice_id, voice_name = voice.current_voice
            speed = float(getattr(voice, "speed", 1.0) or 1.0)

            def send(narration: str, speech_id: str) -> None:
                emit(
                    text=narration,
                    voice_id=voice_id,
                    voice_name=voice_name,
                    speed=speed,
                    speech_id=speech_id,
                )

            return narrate_and_wait(
                text, send=send, completion=completion, cancelled=cancelled, speed=speed, muted=muted
            )
        if muted:
            return narrate_and_wait(text, send=None, completion=None, cancelled=cancelled, muted=True)
        # In-process TTS (no desktop) plays synchronously when blocking.
        voice.speak(text, blocking=True)
        return not cancelled()

    @staticmethod
    def _draft_bridge_ready() -> bool:
        """Only the native bridge (il2cpp runtimes) has the draft, deck and event hands."""
        try:
            from arenamcp.gre_bridge import get_bridge

            bridge = get_bridge()
        except Exception:
            return False
        runtime = str(getattr(bridge, "client_runtime", "") or "")
        return bool(
            bridge is not None and getattr(bridge, "connected", False) and runtime.startswith("il2cpp")
        )

    def _poll_draft_event(self, draft_pack: dict | None = None) -> bool:
        """True while draft autoplay owns the limited-event screens this iteration.

        An open pack or deck build is owned from the first iteration, so the
        advice-only draft path never speaks over (or races) an autoplay pick.
        """
        driver = getattr(self, "_draft_event_driver", None)
        self._draft_event_pack = draft_pack
        try:
            settings = getattr(self, "settings", None)
            active = (
                bool(getattr(self, "_autopilot_enabled", False))
                and settings is not None
                and bool(settings.get("autopilot_draft_events", True))
                and getattr(self, "_mcp", None) is not None
                and not getattr(self, "_autopilot_dry_run", False)
                and not getattr(self, "_autopilot_afk", False)
                and self._autopilot_control_status() == "AP:ON"
                and self._draft_bridge_ready()
            )
            if not active:
                if driver is not None:
                    driver.set_enabled(False)
                return False
            driver = self._draft_event_driver_instance()
            driver.set_enabled(True)
            owned = driver.tick()
            if driver.paused_reason:
                return False
            pack = draft_pack or {}
            return owned or bool(pack.get("is_active") or pack.get("is_building"))
        except Exception:
            logger.exception("Draft autoplay poll failed")
            return False
