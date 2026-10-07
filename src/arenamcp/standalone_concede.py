"""Coach wiring for the deterministic concede recommendation and auto-concede.

The decision and the countdown live in ``arenamcp.concede``; this mixin feeds
it the coaching loop's snapshots and gives it the coach's hands: advice text
and speech, the desktop countdown banner, the autoplay switch and the bridge.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _printed_stat(value: Any) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


class _ConcedeMixin:
    def _init_concede(self) -> None:
        from arenamcp.concede import ConcedeController

        self._printed_pt_cache: dict[int, tuple[int, int] | None] = {}
        self._concede = ConcedeController(
            get_state=self._concede_fresh_state,
            concede_fn=self._concede_via_bridge,
            advise=self._concede_advise,
            emit=self._concede_emit,
            settings_get=self.settings.get,
            autopilot_on=self._concede_autopilot_on,
            bridge_ready=self._concede_bridge_ready,
            game_over=self._concede_game_over,
            draft_active=lambda: bool(getattr(self, "draft_mode", False)),
            announce=self._concede_announce,
        )

    # -- public controls (pipe commands, hotkeys, autoplay switch) -----------

    def cancel_concede(self, source: str = "ui", *, decline: bool = True) -> bool:
        """Cancel a running auto-concede countdown; False when none is running.

        ``decline=False`` (the coach stopping or reloading) only aborts it: the
        game is not marked "keep playing".
        """
        controller = getattr(self, "_concede", None)
        if controller is None:
            return False
        try:
            return controller.cancel(source, decline=decline)
        except Exception as error:
            logger.warning("[CONCEDE] cancel failed: %s", error)
            return False

    def abort_concede(self, reason: str) -> bool:
        """Stop a running countdown without declining the game (shutdown, reload)."""
        return self.cancel_concede(reason, decline=False)

    def concede_armed(self) -> bool:
        controller = getattr(self, "_concede", None)
        return bool(controller is not None and controller.armed)

    def set_auto_concede(self, enabled: bool) -> bool:
        """Turn auto-concede on or off (saved); turning it off cancels a countdown."""
        enabled = bool(enabled)
        self.settings.set("auto_concede", enabled)
        logger.info("[CONCEDE] auto-concede turned %s", "on" if enabled else "off")
        if not enabled:
            self.cancel_concede("auto-concede turned off")
        return enabled

    # -- engine reloads -----------------------------------------------------------

    def _concede_export(self) -> dict | None:
        controller = getattr(self, "_concede", None)
        try:
            return controller.export_state() if controller is not None else None
        except Exception as error:
            logger.warning("[CONCEDE] could not save the concede record: %s", error)
            return None

    def _concede_resume_from(self, saved: Any) -> None:
        controller = getattr(self, "_concede", None)
        if controller is not None and saved:
            controller.resume_from(saved)

    # -- coaching loop --------------------------------------------------------

    def _observe_concede(self, state: dict, game_key: Any) -> None:
        controller = getattr(self, "_concede", None)
        if controller is None:
            return
        try:
            controller.observe(self._with_printed_stats(state), game_key)
        except Exception as error:  # never break the coaching loop
            logger.warning("[CONCEDE] observe failed: %s", error, exc_info=True)

    def _concede_recommended_this_game(self, game_key: Any) -> bool:
        controller = getattr(self, "_concede", None)
        return bool(controller is not None and controller.recommended(game_key))

    def _concede_snapshot(self) -> dict | None:
        controller = getattr(self, "_concede", None)
        return controller.snapshot() if controller is not None else None

    def _with_printed_stats(self, state: dict) -> dict:
        """Add printed power/toughness to battlefield creatures (on our turn).

        The loss estimate undoes "until end of turn" pumps on our turn by
        comparing current and printed stats; snapshots only carry the current
        ones. Card-database lookups are cached per grp_id.
        """
        if not isinstance(state, dict):
            return state
        turn = state.get("turn") or {}
        local = state.get("local_seat_id")
        if local is None or turn.get("active_player") != local:
            return state
        cache = getattr(self, "_printed_pt_cache", None)
        if cache is None:
            cache = self._printed_pt_cache = {}
        battlefield = []
        changed = False
        for card in state.get("battlefield") or []:
            grp_id = card.get("grp_id") if isinstance(card, dict) else None
            types = f"{card.get('type_line') or ''} {card.get('card_types') or ''}".lower() if grp_id else ""
            if not grp_id or "creature" not in types or "printed_power" in card:
                battlefield.append(card)
                continue
            if grp_id not in cache:
                cache[grp_id] = self._lookup_printed(int(grp_id))
            printed = cache[grp_id]
            if printed is None:
                battlefield.append(card)
                continue
            battlefield.append({**card, "printed_power": printed[0], "printed_toughness": printed[1]})
            changed = True
        if not changed:
            return state
        return {**state, "battlefield": battlefield}

    @staticmethod
    def _lookup_printed(grp_id: int) -> tuple[int, int] | None:
        try:
            from arenamcp import server

            info = server.enrich_with_oracle_text(grp_id)
        except Exception:
            return None
        power, toughness = _printed_stat(info.get("power")), _printed_stat(info.get("toughness"))
        return None if power is None or toughness is None else (power, toughness)

    # -- the controller's hands -------------------------------------------------

    def _concede_autopilot_on(self) -> bool:
        if not getattr(self, "_running", False):
            return False  # stopping or reloading: never concede on the way out
        return self._autopilot_control_status() == "AP:ON" and not getattr(self, "_autopilot_dry_run", False)

    def _concede_bridge_ready(self) -> bool:
        """Connected, and the connected client knows the concede command."""
        poller = getattr(self, "_bridge_poller", None)
        if poller is None or not poller.connected:
            return False
        try:
            from arenamcp.gre_bridge import get_bridge

            return bool(getattr(get_bridge(), "concede_supported", False))
        except Exception:
            return False

    def _concede_fresh_state(self) -> dict:
        mcp = getattr(self, "_mcp", None)
        state = mcp.get_game_state() if mcp is not None else {}
        if not isinstance(state, dict):
            return {}
        normalize = getattr(self, "_normalize_turn_snapshot", None)
        state = normalize(state) if callable(normalize) else state
        return self._with_printed_stats(state)

    @staticmethod
    def _concede_game_over() -> bool:
        from arenamcp.server import game_state

        return game_state.game_ended_event.is_set()

    @staticmethod
    def _concede_via_bridge(state: dict) -> dict:
        from arenamcp.gre_bridge import get_bridge

        turn = (state.get("turn") or {}).get("turn_number")
        return get_bridge().concede(expected_match_id=state.get("match_id"), expected_turn=turn)

    def _concede_record(self, text: str, state: dict | None) -> None:
        record = getattr(self, "_record_advice", None)
        if state and callable(record):
            try:
                record(text, "concede", game_state=state)
            except Exception as error:
                logger.debug("[CONCEDE] could not record advice: %s", error)

    def _concede_advise(self, text: str, state: dict | None = None) -> None:
        self.ui.advice(text, "CONCEDE")
        self.speak_advice(text, blocking=False)
        self._concede_record(text, state)

    def _concede_announce(self, text: str, state: dict | None, cancelled: Callable[[], bool]) -> bool:
        """Show and speak the auto-concede offer; return once it has been heard.

        The countdown starts after this returns, so the player hears the whole
        offer before any of its seconds run. The desktop owns audio and
        acknowledges the utterance (speech_completion); an older desktop that
        never does gets a conservative estimate of the speaking time instead.
        Muted or without a voice there is nothing to hear: return at once and
        let the banner carry the countdown.
        """
        from arenamcp.speech_completion import narrate_and_wait

        self.ui.advice(text, "CONCEDE")
        self._concede_record(text, state)
        voice = getattr(self, "_voice_output", None)
        if voice is None or getattr(voice, "muted", False):
            logger.info("[CONCEDE] offer shown, not spoken (no voice or muted)")
            return True
        ui = self.ui
        completion = getattr(ui, "speech_completion", None)
        emit = getattr(ui, "emit_speech_request", None)
        logger.info("SPEAK (concede offer): %r", text[:160])
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

            heard = narrate_and_wait(text, send=send, completion=completion, cancelled=cancelled, speed=speed)
            logger.info("[CONCEDE] offer %s", "heard" if heard else "cut short")
            return heard
        voice.speak(text, blocking=True)  # in-process TTS plays synchronously
        return not cancelled()

    def _concede_emit(self, payload: dict) -> None:
        emit = getattr(self.ui, "concede_countdown", None)
        if callable(emit):
            emit(payload)
            return
        state = payload.get("state")
        if state == "armed":
            self.ui.log(f"[CONCEDE] Auto-concede in {payload.get('seconds')}s (cancel to keep playing)")
        else:
            self.ui.log(f"[CONCEDE] {state}: {payload.get('reason') or payload.get('message') or ''}")
