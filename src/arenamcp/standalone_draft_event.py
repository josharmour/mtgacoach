"""Autoplay for joined draft events, beside the in-match autopilot.

On when autopilot is on and the ``autopilot_draft_events`` setting is not
turned off. The player enters the event; draft picks, the deck build, and the
event's matches are then played through to the end (see draft_event.py).
"""

from __future__ import annotations

import logging
import time
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
        # Player.log alone: get_game_state() would also read the bridge's board
        # every 3 s, a main-thread reflection call on BepInEx/Proton.
        from arenamcp.server import _match_in_progress

        return _match_in_progress()

    def _set_primer_service(self) -> Any:
        """The set research service, shared by the pre-draft prefetch and the draft driver."""
        service = getattr(self, "_set_primers", None)
        if service is not None:
            return service
        from arenamcp import server
        from arenamcp.set_primer import SetPrimerService, arena_rules_lookup

        stats = server._get_draft_stats()

        def research(set_code: str) -> dict:
            settings = getattr(self, "settings", None)
            if not (settings and settings.get("draft_research_online", False)):
                return {}
            from arenamcp.set_research import fetch_set_research

            return fetch_set_research(set_code)

        service = self._set_primers = SetPrimerService(
            backend_fn=self._draft_event_backend,
            ratings_fn=stats.get_raw_ratings,
            color_stats_fn=stats.get_color_pair_stats,
            rules_lookup=arena_rules_lookup(server._get_mtgadb()),
            research_fn=research,
        )
        return service

    def _upcoming_draft_set(self, draft_pack: dict | None) -> str:
        """The set of a draft or sealed event we joined, before (or during) its first pick."""
        pack = draft_pack or {}
        if pack.get("is_active") and pack.get("set_code"):
            return str(pack["set_code"]).upper()
        try:
            from arenamcp import server
            from arenamcp.draftstate import extract_set_code

            state = getattr(server, "draft_state", None)
            if state is not None and getattr(state, "is_active", False) and getattr(state, "set_code", ""):
                return str(state.set_code).upper()
            tracker = server.get_course_tracker()
            course = tracker.active_limited() if tracker else None
            if course is not None:
                from arenamcp.limited_formats import use_event_expansion

                use_event_expansion(course.event_name)
                return extract_set_code(course.event_name).upper()
        except Exception as exc:
            logger.debug("upcoming draft set unavailable: %s", exc)
        return ""

    def _prefetch_draft_research(self, draft_pack: dict | None) -> None:
        """Research the set as soon as a limited event is joined, not at the first pick.

        2026-10-13: the research (17lands data + 11 model calls, 82 s) started at
        P1p1, so the first three picks had none. Joining a pod showed the set
        2.5 minutes earlier; a course already in progress shows it at startup.
        """
        code = self._upcoming_draft_set(draft_pack)
        if not code:
            return
        service = self._set_primer_service()
        seen = self.__dict__.setdefault("_research_announced", {})
        status = service.status(code)
        if status in ("missing", "stale"):
            service.ensure(code)
            status = service.status(code)
        if seen.get(code) == status:
            return
        seen[code] = status
        ui = getattr(self, "ui", None)
        if status == "ready":
            from arenamcp.set_primer import research_summary

            primer = service.get(code)
            line = f"[DRAFT RESEARCH] {code} ready. {research_summary(primer)}" if primer else ""
        elif status in ("building", "refreshing"):
            line = f"[DRAFT RESEARCH] {code}: researching the set before the first pick" + (
                " (serving the previous research meanwhile)" if status == "refreshing" else ""
            )
        else:
            line = ""
        if line:
            logger.info(line)
            if ui is not None:
                ui.log(line + "\n")

    def _draft_event_driver_instance(self) -> Any:
        driver = getattr(self, "_draft_event_driver", None)
        if driver is not None:
            return driver
        from arenamcp import server
        from arenamcp.draft_advisor import DraftAdvisor
        from arenamcp.draft_event import DraftEventDriver
        from arenamcp.gre_bridge import get_bridge

        card_db = server._get_mtgadb()
        primers = self._set_primer_service()
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
            narrate_fn=self._narrate_draft_pick,
            narrate_first_fn=lambda: bool(
                getattr(self, "settings", None) and self.settings.get("draft_narrate_before_pick", False)
            ),
            commentary_fn=lambda: bool(
                getattr(self, "settings", None) and self.settings.get("draft_commentary", True)
            ),
            queue_fn=lambda: bool(getattr(self, "_auto_queue_enabled", False)),
        )
        return driver

    def _narrate_draft_pick(self, text: str, cancelled: Any) -> bool:
        """Speak why autoplay is choosing these cards, before it confirms them.

        The desktop owns audio, so completion comes back over the pipe as
        speech_status acknowledgments (speech_completion.py); ``cancelled`` also
        turns true at the pick's deadline, so this never holds the draft past
        the pick clock.
        """
        return self._narrate_until_heard(text, cancelled, "draft pick")

    def _narrate_until_heard(
        self,
        text: str,
        cancelled: Any,
        label: str,
        *,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
    ) -> bool:
        from arenamcp.speech_completion import narrate_and_wait, speaking_seconds

        ui = getattr(self, "ui", None)
        voice = getattr(self, "_voice_output", None)
        completion = getattr(ui, "speech_completion", None)
        emit = getattr(ui, "emit_speech_request", None)
        logger.info("SPEAK (%s): %r", label, text[:160])
        if voice is None:
            return narrate_and_wait(text, send=None, completion=None, cancelled=cancelled, sleep=sleep)
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
                text,
                send=send,
                completion=completion,
                cancelled=cancelled,
                speed=speed,
                muted=muted,
                clock=clock,
                sleep=sleep,
            )
        if muted:
            return narrate_and_wait(
                text, send=None, completion=None, cancelled=cancelled, muted=True, sleep=sleep
            )
        # A pick must stay interruptible: play in the background, hold for its length.
        voice.speak(text, blocking=False)
        end = clock() + speaking_seconds(text, float(getattr(voice, "speed", 1.0) or 1.0))
        while clock() < end and not cancelled():
            sleep(0.25)
        return not cancelled()

    @staticmethod
    def _draft_bridge_ready() -> bool:
        """Only a reflect-1 bridge (native Mac/Android library, or a BepInEx plugin
        that advertises it) has the draft, deck and event hands."""
        try:
            from arenamcp.gre_bridge import get_bridge

            bridge = get_bridge()
        except Exception:
            return False
        return bool(bridge is not None and getattr(bridge, "limited_hands", False))

    def _poll_draft_event(self, draft_pack: dict | None = None) -> bool:
        """True while draft autoplay owns the limited-event screens this iteration.

        An open pack or deck build is owned from the first iteration, so the
        advice-only draft path never speaks over (or races) an autoplay pick.
        """
        driver = getattr(self, "_draft_event_driver", None)
        self._draft_event_pack = draft_pack
        try:
            # Research the set while the pod fills, autoplay or not.
            self._prefetch_draft_research(draft_pack)
        except Exception:
            logger.debug("Draft research prefetch failed", exc_info=True)
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
