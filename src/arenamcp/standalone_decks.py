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
        # Fable Nov S3: warm the card-facts cache for every known deck in the
        # background, so lookups are already warm before a match starts.
        try:
            from arenamcp.card_facts.client import prefetch_grp_ids

            grp_ids: list[int] = []
            for record in library.values():
                grp_ids.extend(gid for gid, _count in (getattr(record, "main", None) or []))
                grp_ids.extend(getattr(record, "command_zone", None) or [])
            prefetch_grp_ids(grp_ids)
        except Exception as error:
            logger.debug("card facts prefetch skipped: %s", error)
        self._deck_library = library
        return library, "; ".join(notes)

    def deck_strategy_status(self, record: Any) -> dict[str, Any]:
        """Playbook cache status for one deck: ready (with archetype), building, or none."""
        running = getattr(self, "_deck_analysis_running", set())
        if record.deck_id in running:
            return {
                "status": "building",
                "stage": getattr(self, "_deck_analysis_stage", {}).get(record.deck_id, ""),
            }
        if not record.complete:
            return {
                "status": "incomplete",
                "detail": "card list not seen yet: open the deck in Arena or play it once",
            }
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

        return {
            "status": "ready",
            "archetype": playbook.data.get("archetype", ""),
            "report": playbook_report(playbook),
        }

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

    # -- collection, deck edits and crafting (feature #204) --------------------
    # All four go through the Mac bridge's reflect commands and only work on
    # the home screen (the front-end wrapper is unloaded during a duel).

    def _deck_bridge_command(self, action: str, emit: str, **fields: Any) -> None:
        def work() -> None:
            bridge = self._deck_library_bridge()
            if bridge is None:
                self._deck_library_emit(emit, {"ok": False, "error": "bridge offline"})
                return
            try:
                response = bridge.draft_command(action, timeout=30.0, **fields)
            except Exception as error:  # the UI never breaks on a bridge fault
                logger.warning("Deck bridge command %s failed: %s", action, error)
                response = {"ok": False, "error": str(error)}
            self._deck_library_emit(emit, response)

        threading.Thread(target=work, name=f"deck-bridge-{action}", daemon=True).start()

    def collection_async(self) -> None:
        self._deck_bridge_command("get_collection", "emit_collection")

    def update_deck_async(self, deck_id: str, piles: dict[str, Any]) -> None:
        self._deck_bridge_command("update_deck", "emit_deck_op", deck_id=deck_id, piles=piles)

    def create_deck_async(self, source_deck_id: str, name: str) -> None:
        self._deck_bridge_command("create_deck", "emit_deck_op", source_deck_id=source_deck_id, name=name)

    def craft_async(self, cards: list[dict[str, int]]) -> None:
        # The desktop's confirmation dialog already happened; confirm=True on
        # the wire is the adapter's second gate before any wildcard is spent.
        self._deck_bridge_command("craft", "emit_deck_op", cards=cards, confirm=True)

    # -- analysis --------------------------------------------------------------

    def _deck_library_backend(self) -> Any:
        from arenamcp.coach import create_backend

        return create_backend(self._backend_name, model=self.model_name)

    def _prepare_selected_deck(self, cancel: threading.Event) -> None:
        """Warm the exact deck selected for Grind, Recent Match or the last queue request."""
        from arenamcp.server import _queue_selection_from_log, get_last_queue_selection

        quest = getattr(self, "_auto_quest", None)
        if quest:
            target = quest.get("target") or {}
        else:
            bridge = self._deck_library_bridge()
            if bridge is not None and getattr(bridge, "client_runtime", "") == "il2cpp-macos":
                target = bridge.get_recent_match()
            else:
                target = get_last_queue_selection() or _queue_selection_from_log() or {}
        deck_id = target.get("deck_id")
        if not deck_id or cancel.is_set():
            return
        record = self.collect_decks()[0].get(deck_id)
        if (
            not record
            or not record.complete
            or record.snapshot
            or cancel.is_set()
            or self._deck_library_in_match()
        ):
            return
        state = record.game_state()
        self._deck_prep_identity = record.identity()
        if self._coach._cached_playbook(self._deck_prep_identity, state) is not None or cancel.is_set():
            return
        backend = self._deck_library_backend()
        try:
            self._coach.prepare_playbook(state, backend=backend, cancel=cancel)
            logger.info("Pre-match playbook ready for %s", record.name)
        finally:
            close = getattr(backend, "close", None)
            if callable(close):
                close()

    def _poll_deck_preparation(self, idle: bool, state: dict | None = None) -> None:
        """Run at most one idle lookup off the coaching thread; live play takes precedence."""
        worker = getattr(self, "_deck_prep_thread", None)
        if not idle or not self._auto_deck_strategy:
            cancel = getattr(self, "_deck_prep_cancel", None)
            if cancel is not None and not cancel.is_set() and worker is not None and worker.is_alive():
                from arenamcp.deck_strategy import deck_identity

                # The live worker joins a build for its own deck. Let that build
                # finish, subject to the existing first-plan handoff deadline.
                if (
                    not self._auto_deck_strategy
                    or not state
                    or (not self._deck_prep_identity or deck_identity(state) != self._deck_prep_identity)
                ):
                    cancel.set()
            self._deck_prep_next_check = 0.0
            return
        from arenamcp.server import get_last_queue_selection

        # Grind may choose its target after the first Home poll. A new queue
        # selection should wake the worker without waiting out the idle timer.
        quest = getattr(self, "_auto_quest", None)
        hint = (
            ("grind", (quest.get("target") or {}).get("deck_id"))
            if quest
            else ("queue", get_last_queue_selection().get("deck_id"))
        )
        if hint != getattr(self, "_deck_prep_selection", None):
            self._deck_prep_selection = hint
            self._deck_prep_next_check = 0.0
            if worker is not None and worker.is_alive():
                self._deck_prep_cancel.set()
                return
        if self._coach is None or (worker is not None and worker.is_alive()):
            return
        now = time.monotonic()
        if now < getattr(self, "_deck_prep_next_check", 0.0):
            return
        self._deck_prep_next_check = now + 60
        self._deck_prep_identity = ""
        cancel = self._deck_prep_cancel = threading.Event()

        def prepare():
            try:
                self._prepare_selected_deck(cancel)
            except Exception as error:
                if not cancel.is_set():
                    logger.warning("Pre-match playbook unavailable: %s", error)

        self._deck_prep_thread = threading.Thread(target=prepare, name="pre-match-playbook", daemon=True)
        self._deck_prep_thread.start()

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
                "emit_deck_analysis",
                {
                    "deck_id": record.deck_id,
                    "identity": record.identity(),
                    "name": record.name,
                    "status": "building",
                    "stage": stage,
                },
            )

        try:
            playbook = coach.prepare_playbook(
                record.game_state(), backend=self._deck_library_backend(), refresh=refresh, progress=progress
            )
            coach.save_deck_strategy(playbook)
            payload = {
                "deck_id": record.deck_id,
                "identity": record.identity(),
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
            return {
                "deck_id": record.deck_id,
                "identity": record.identity(),
                "name": record.name,
                "status": "error",
                "error": str(error),
            }
        finally:
            running.discard(record.deck_id)
            stages.pop(record.deck_id, None)

    def _deck_record(self, deck_id: str) -> Any:
        library = getattr(self, "_deck_library", None) or {}
        if deck_id not in library:
            library, _note = self.collect_decks()
        return library.get(deck_id)

    def _study_outside_match(self) -> bool:
        from arenamcp import server

        match_id = server.game_state.match_id
        completed = server.get_completed_match_for_navigation()
        return not match_id or (
            completed.get("match_complete") is True and completed.get("match_id") == match_id
        )

    def deck_card_study(self, request):
        """Answer a local Decks window request; no study runs from gameplay or auto-preparation."""
        from arenamcp import logging_config
        from arenamcp.deck_card_study import card_detail, card_menu, prepare_card_pass, read_card, study_card
        from arenamcp.local_study_model import LocalStudyModel, installed_models

        deck_id, identity, gid = (request.get(k) for k in ("deck_id", "identity", "card"))
        reply = {k: request.get(k) for k in ("token", "deck_id", "identity", "card")}
        record = (getattr(self, "_deck_library", {}) or {}).get(deck_id)
        if not record or not record.complete or not identity or identity != record.identity():
            return {**reply, "error": "The selected deck has changed. Refresh the Decks list."}
        book = self._coach._cached_playbook(identity, record.game_state())
        if book is None or book.identity != identity:
            return {**reply, "error": "Open a saved deck strategy before studying its cards."}
        directory = logging_config.LOG_DIR / "cache" / "local_card_studies"
        try:
            if request.get("operation") == "list":
                try:
                    models = list(installed_models())
                except (OSError, ValueError, TypeError):
                    models = []
                return {**reply, "cards": card_menu(book), "models": models}
            if request.get("operation") == "read":
                return {**reply, "detail": read_card(book, gid, directory)}
            if request.get("operation") == "prepare":
                if not self._study_outside_match():
                    raise ValueError("Card studies can run after the match has finished")
                model = LocalStudyModel(request.get("model"))
                if request.get("expected_digest") is not None and request["expected_digest"] != model.digest:
                    raise ValueError("The selected model weights changed during this deck pass")
                return {**reply, "card_pass": prepare_card_pass(book, directory, model.digest)}
            if request.get("operation") != "build":
                raise ValueError("Unknown card study action")
            # Do not reserve the live strategic lane for an outside-match job.
            # One local study runs at a time and must still own the deck at save.
            lock = self.__dict__.setdefault("_local_study_lock", threading.Lock())
            if not lock.acquire(blocking=False):
                raise ValueError("Another local card study is running")
            try:

                def allowed():
                    current = (getattr(self, "_deck_library", {}) or {}).get(deck_id)
                    return bool(current and current.identity() == identity and self._study_outside_match())

                if not allowed():
                    raise ValueError("Card studies can run after the match has finished")
                if any(r["card"] == gid for r in (book.data.get("study") or {}).get("cards", [])):
                    return {**reply, "detail": card_detail(book, gid), "reused": True}
                model = LocalStudyModel(request.get("model"))
                if request.get("expected_digest") is not None and request["expected_digest"] != model.digest:
                    raise ValueError("The selected model weights changed during this deck pass")
                _row, reused = study_card(book, gid, model, directory, allowed=allowed)
                return {
                    **reply,
                    "detail": read_card(book, gid, directory, model_digest=model.digest),
                    "reused": reused,
                }
            finally:
                lock.release()
        except (OSError, ValueError, TypeError, KeyError) as error:
            return {**reply, "error": str(error)}

    def analyze_deck_async(self, deck_id: str, *, refresh: bool = False) -> None:
        def work() -> None:
            record = self._deck_record(deck_id)
            if record is None:
                self._deck_library_emit(
                    "emit_deck_analysis", {"deck_id": deck_id, "status": "error", "error": "deck not found"}
                )
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
                    self._deck_library_emit(
                        "emit_deck_analysis", analyze_deck_record_safe(self, record, False)
                    )
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
