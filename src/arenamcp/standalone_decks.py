"""Decks window backend: list the player's decks and build their playbooks outside a match.

The expensive deck analysis (two model passes at high effort) used to run
only when a match started with a deck that had no cached playbook, in the
background, while the first turns were already being played. The Decks
window lets the player build every playbook before queueing, so matches
start with the strategy already on disk.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class _DeckLibraryMixin:
    _deck_library: dict[str, Any]
    _deck_analysis_running: set[str]

    # -- listing ---------------------------------------------------------------

    def _deck_library_bridge(self) -> Any:
        try:
            from arenamcp.gre_bridge import get_bridge

            bridge = get_bridge()
        except Exception:
            return None
        return bridge if bridge is not None and getattr(bridge, "connected", False) else None

    def _deck_library_emit(self, name: str, *args: Any) -> None:
        ui = getattr(self, "ui", None)
        emitter = getattr(ui, name, None)
        if callable(emitter):
            try:
                emitter(*args)
            except Exception as error:  # the UI never breaks the library
                logger.debug("Deck library emit %s failed: %s", name, error)

    def collect_decks(self) -> tuple[dict[str, Any], str]:
        """(records by deck id, note) from the bridge first, then Player.log."""
        from arenamcp.deck_library import load_log_decks, merge_libraries, records_from_bridge

        notes = []
        bridge_decks: dict[str, Any] = {}
        bridge = self._deck_library_bridge()
        if bridge is not None:
            response = bridge.draft_command("get_decks", timeout=12.0)
            if response.get("ok"):
                bridge_decks = records_from_bridge(response)
            else:
                notes.append(f"bridge: {response.get('error') or 'no decks'}")
        else:
            notes.append("bridge offline: showing decks seen in Player.log")
        log_decks = load_log_decks()
        library = merge_libraries(bridge_decks, log_decks)
        self._deck_library = library
        return library, "; ".join(notes)

    def deck_strategy_status(self, record: Any) -> dict[str, Any]:
        """Playbook cache status for one deck: ready (with archetype), building, or none."""
        running = getattr(self, "_deck_analysis_running", set())
        if record.deck_id in running:
            return {"status": "building", "stage": getattr(self, "_deck_analysis_stage", {}).get(record.deck_id, "")}
        if not record.complete:
            return {"status": "incomplete", "detail": "card list not seen yet: open the deck in Arena or play it once"}
        coach = getattr(self, "_coach", None)
        cached = getattr(coach, "_cached_playbook", None)
        if not callable(cached):
            return {"status": "unknown"}
        try:
            playbook = cached(record.identity(), record.game_state())
        except Exception as error:
            logger.debug("Deck library: cache read failed for %s: %s", record.name, error)
            playbook = None
        if playbook is None:
            return {"status": "none"}
        from arenamcp.deck_library import playbook_report

        return {"status": "ready", "archetype": playbook.data.get("archetype", ""), "report": playbook_report(playbook)}

    def deck_payloads(self) -> list[dict[str, Any]]:
        from arenamcp.deck_library import deck_payload

        library = getattr(self, "_deck_library", {}) or {}
        rows = [deck_payload(record, self.deck_strategy_status(record)) for record in library.values()]
        # Most recently played first; decks without a card list last.
        rows.sort(key=lambda row: (not row["complete"], row["last_played"] == "", row["name"].lower()))
        rows.sort(key=lambda row: row["last_played"], reverse=True)
        rows.sort(key=lambda row: not row["complete"])
        return rows

    def list_decks_async(self) -> None:
        def work() -> None:
            try:
                _library, note = self.collect_decks()
                self._deck_library_emit("emit_deck_list", self.deck_payloads(), note)
            except Exception as error:
                logger.warning("Deck library listing failed: %s", error)
                self._deck_library_emit("emit_deck_list", [], str(error))

        threading.Thread(target=work, name="deck-library-list", daemon=True).start()

    # -- analysis --------------------------------------------------------------

    def _deck_library_backend(self) -> Any:
        from arenamcp.coach import create_backend

        return create_backend(self._backend_name, model=self.model_name)

    def analyze_deck_record(self, record: Any, *, refresh: bool = False) -> dict[str, Any]:
        """Build the playbook for one deck (blocking); returns the deck_analysis payload."""
        from arenamcp.deck_library import playbook_report

        coach = getattr(self, "_coach", None)
        if coach is None or not hasattr(coach, "prepare_playbook"):
            return {"deck_id": record.deck_id, "status": "error", "error": "coach unavailable"}
        if not record.complete:
            return {"deck_id": record.deck_id, "status": "error", "error": "card list unknown"}
        running = self.__dict__.setdefault("_deck_analysis_running", set())
        stages = self.__dict__.setdefault("_deck_analysis_stage", {})
        running.add(record.deck_id)
        start = time.monotonic()

        def progress(stage: str) -> None:
            stages[record.deck_id] = stage
            self._deck_library_emit(
                "emit_deck_analysis", {"deck_id": record.deck_id, "name": record.name, "status": "building", "stage": stage}
            )

        try:
            playbook = coach.prepare_playbook(
                record.game_state(), backend=self._deck_library_backend(), refresh=refresh, progress=progress
            )
            payload = {
                "deck_id": record.deck_id,
                "name": record.name,
                "status": "ready",
                "archetype": playbook.data.get("archetype", ""),
                "report": playbook_report(playbook),
                "seconds": round(time.monotonic() - start, 1),
            }
            logger.info("Deck strategy ready for %s (%.0fs)", record.name, time.monotonic() - start)
            return payload
        except Exception as error:
            logger.warning("Deck strategy failed for %s: %s", record.name, error)
            return {"deck_id": record.deck_id, "name": record.name, "status": "error", "error": str(error)}
        finally:
            running.discard(record.deck_id)
            stages.pop(record.deck_id, None)

    def _deck_record(self, deck_id: str) -> Any:
        library = getattr(self, "_deck_library", None) or {}
        if deck_id not in library:
            library, _note = self.collect_decks()
        return library.get(deck_id)

    def analyze_deck_async(self, deck_id: str, *, refresh: bool = False) -> None:
        def work() -> None:
            record = self._deck_record(deck_id)
            if record is None:
                self._deck_library_emit("emit_deck_analysis", {"deck_id": deck_id, "status": "error", "error": "deck not found"})
                return
            self._deck_library_emit("emit_deck_analysis", analyze_deck_record_safe(self, record, refresh))

        threading.Thread(target=work, name="deck-strategy", daemon=True).start()

    # How often a paused analyze-all run checks whether the match is over.
    _ANALYZE_ALL_MATCH_POLL_S = 5.0

    def _deck_library_in_match(self) -> bool:
        check = getattr(self, "_draft_event_in_match", None)
        if not callable(check):
            return False
        try:
            return bool(check())
        except Exception:
            return False

    def analyze_all_decks_async(self) -> bool:
        """Build every missing playbook, one deck at a time (they share the model server).

        Single-flight: a second request while a run is going is ignored (on
        2026-10-10 the 95-deck run was started twice and analysed mid-match
        alongside the live decision calls). The next deck never starts during
        a match; the run waits for the match to end. Returns False when a run
        was already going.
        """
        if not _ANALYZE_ALL_LOCK.acquire(blocking=False):
            logger.info("Deck strategy: analyze-all already running; ignoring the new request")
            return False

        def work() -> None:
            try:
                library = getattr(self, "_deck_library", None) or self.collect_decks()[0]
                for record in list(library.values()):
                    if not record.complete or self.deck_strategy_status(record).get("status") == "ready":
                        continue
                    if self._deck_library_in_match():
                        logger.info("Deck strategy: analyze-all paused for the match")
                        while self._deck_library_in_match() and getattr(self, "_running", True) is not False:
                            time.sleep(self._ANALYZE_ALL_MATCH_POLL_S)
                        if getattr(self, "_running", True) is False:
                            return
                        logger.info("Deck strategy: analyze-all resumes after the match")
                        if self.deck_strategy_status(record).get("status") == "ready":
                            continue
                    self._deck_library_emit("emit_deck_analysis", analyze_deck_record_safe(self, record, False))
                self._deck_library_emit("emit_deck_list", self.deck_payloads(), "")
            finally:
                _ANALYZE_ALL_LOCK.release()

        try:
            threading.Thread(target=work, name="deck-strategy-all", daemon=True).start()
        except Exception:
            _ANALYZE_ALL_LOCK.release()
            raise
        return True


# One analyze-all run per process (one coach per process).
_ANALYZE_ALL_LOCK = threading.Lock()


def analyze_deck_record_safe(owner: _DeckLibraryMixin, record: Any, refresh: bool) -> dict[str, Any]:
    try:
        return owner.analyze_deck_record(record, refresh=refresh)
    except Exception as error:  # never lose the worker thread silently
        logger.warning("Deck strategy worker failed: %s", error)
        return {"deck_id": record.deck_id, "name": record.name, "status": "error", "error": str(error)}
