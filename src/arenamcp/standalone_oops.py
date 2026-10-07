"""Coach wiring for Arena's "Oops" emote (arenamcp.oops).

The decisions and rate limits live in ``arenamcp.oops``; this mixin feeds the
controller the autopilot's incident reports, the log facts and the coaching
loop's snapshots, and gives it the bridge to send the emote with.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _OopsMixin:
    def _init_oops(self) -> None:
        from arenamcp.oops import OopsController

        self._oops_fact_seq = 0
        self._oops_text_cache: dict[int, str] = {}
        self._oops = OopsController(
            emote_fn=self._oops_emote_via_bridge,
            settings_get=self.settings.get,
            autopilot_on=self._oops_autopilot_on,
            bridge_ready=self._oops_bridge_ready,
            game_over=self._oops_game_over,
            concede_active=self._oops_concede_active,
            still_stuck=self._oops_still_stuck,
            draft_active=lambda: bool(getattr(self, "draft_mode", False)),
            card_text=self._oops_card_text,
        )

    # -- public controls (pipe command, autoplay switch) ----------------------------

    def set_oops_emote(self, enabled: bool) -> bool:
        """Turn the "Oops" emote on or off (saved); off drops anything waiting to be sent."""
        enabled = bool(enabled)
        self.settings.set("oops_emote", enabled)
        logger.info("[OOPS] the Oops emote is turned %s", "on" if enabled else "off")
        if not enabled:
            self._oops_clear("the Oops emote was turned off")
        return enabled

    def _oops_clear(self, reason: str) -> None:
        controller = getattr(self, "_oops", None)
        if controller is not None:
            try:
                controller.clear_pending(reason)
            except Exception as error:
                logger.warning("[OOPS] clear failed: %s", error)

    # -- the autopilot's reports ---------------------------------------------------------

    def _oops_report(self, kind: str, key: Any, detail: str, state: dict | None = None) -> None:
        controller = getattr(self, "_oops", None)
        if controller is not None:
            controller.report(kind, key, detail, state)

    # -- coaching loop ------------------------------------------------------------------

    def _observe_oops(self, state: dict, game_key: Any) -> None:
        controller = getattr(self, "_oops", None)
        if controller is None:
            return
        try:
            facts: list[dict] = []
            try:
                from arenamcp.server import game_state

                facts, self._oops_fact_seq = game_state.oops_facts_since(getattr(self, "_oops_fact_seq", 0))
            except Exception as error:
                logger.debug("[OOPS] no log facts: %s", error)
            engine = getattr(self, "_autopilot", None)
            evidence_fn = getattr(engine, "oops_evidence", None)
            evidence = evidence_fn() if callable(evidence_fn) else {}
            controller.observe(state, game_key, facts=facts, evidence=evidence)
        except Exception as error:  # never break the coaching loop
            logger.warning("[OOPS] observe failed: %s", error, exc_info=True)

    def _oops_snapshot(self) -> dict | None:
        controller = getattr(self, "_oops", None)
        return controller.snapshot() if controller is not None else None

    # -- engine reloads ---------------------------------------------------------------

    def _oops_export(self) -> dict | None:
        controller = getattr(self, "_oops", None)
        try:
            return controller.export_state() if controller is not None else None
        except Exception as error:
            logger.warning("[OOPS] could not save the Oops record: %s", error)
            return None

    def _oops_resume_from(self, saved: Any) -> None:
        controller = getattr(self, "_oops", None)
        if controller is not None and saved:
            controller.resume_from(saved)

    # -- the controller's hands -------------------------------------------------------------

    def _oops_autopilot_on(self) -> bool:
        """Autoplay is driving: on (also while paused for MANUAL REQUIRED), live, not land-only."""
        if not getattr(self, "_running", False):
            return False  # stopping or reloading
        engine = getattr(self, "_autopilot", None)
        if not getattr(self, "_autopilot_enabled", False) or engine is None:
            return False
        if getattr(self, "_autopilot_dry_run", False):
            return False
        config = getattr(engine, "_config", None)
        if getattr(config, "dry_run", False) or getattr(config, "land_drop_mode", False):
            return False
        return not getattr(engine, "_land_only", False)

    def _oops_bridge_ready(self) -> bool:
        """Connected, and the connected client can send emotes."""
        poller = getattr(self, "_bridge_poller", None)
        if poller is None or not poller.connected:
            return False
        try:
            from arenamcp.gre_bridge import get_bridge

            return bool(getattr(get_bridge(), "emote_supported", False))
        except Exception:
            return False

    @staticmethod
    def _oops_game_over() -> bool:
        from arenamcp.server import game_state

        return game_state.game_ended_event.is_set()

    def _oops_concede_active(self, game_key: Any = None) -> bool:
        """A concede countdown runs, or this game's concede is being sent, sent or confirmed."""
        in_progress = getattr(self, "concede_in_progress", None)
        if callable(in_progress):
            return bool(in_progress(game_key))
        armed = getattr(self, "concede_armed", None)
        return bool(callable(armed) and armed())

    def _oops_still_stuck(self, state: dict) -> bool:
        """The autopilot still stands down on the same decision (MANUAL REQUIRED dwell)."""
        engine = getattr(self, "_autopilot", None)
        check = getattr(engine, "window_still_given_up", None)
        if not callable(check) or getattr(getattr(engine, "state", None), "value", None) != "paused":
            return False
        poller = getattr(self, "_bridge_poller", None)
        judge = getattr(self, "_bridge_judged_state", None)
        connected = bool(poller is not None and poller.connected)
        judged = judge(state, connected) if callable(judge) else state
        return bool(check(judged))

    def _oops_card_text(self, grp_id: int) -> str:
        if not grp_id:
            return ""
        cache = getattr(self, "_oops_text_cache", None)
        if cache is None:
            cache = self._oops_text_cache = {}
        if grp_id not in cache:
            try:
                from arenamcp import server

                cache[grp_id] = str(
                    (server.enrich_with_oracle_text(int(grp_id)) or {}).get("oracle_text") or ""
                )
            except Exception:
                cache[grp_id] = ""
        return cache[grp_id]

    @staticmethod
    def _oops_emote_via_bridge(state: dict) -> dict:
        from arenamcp.gre_bridge import get_bridge

        turn = (state.get("turn") or {}).get("turn_number")
        return get_bridge().send_emote("oops", expected_match_id=state.get("match_id"), expected_turn=turn)
