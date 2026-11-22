"""Decks window backend: list the player's decks and build their playbooks outside a match.

The expensive deck analysis (two model passes at high effort) used to run
only when a match started with a deck that had no cached playbook, in the
background, while the first turns were already being played. The Decks
window lets the player build every playbook before queueing, so matches
start with the strategy already on disk.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


def _playbook_has_study(playbook: Any) -> bool:
    """Return True iff ``playbook`` carries a validated, unwithheld G2 study and chapters."""
    if playbook is None or getattr(playbook, "study_withheld", False):
        return False
    data = getattr(playbook, "data", None)
    if not isinstance(data, dict):
        return False
    return isinstance(data.get("study"), dict) and isinstance(data.get("study_chapters"), dict)


def _coach_supports_study(coach: Any) -> bool:
    """True when ``coach.prepare_playbook`` accepts G2 ``study_client`` / ``build_full_study``."""
    prep = getattr(coach, "prepare_playbook", None)
    if not callable(prep):
        return False
    try:
        params = list(inspect.signature(prep).parameters.values())
    except (TypeError, ValueError):
        return False
    names = {param.name for param in params}
    has_varkw = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params)
    return has_varkw or {"study_client", "build_full_study"} <= names


def _call_prepare_playbook(
    coach: Any,
    state: dict[str, Any],
    *,
    backend: Any = None,
    refresh: bool = False,
    progress: Any = None,
    cancel: threading.Event | None = None,
    in_match: Any = None,
    study_client: Any = None,
    build_full_study: bool = False,
) -> Any:
    kwargs: dict[str, Any] = {
        "backend": backend,
        "refresh": refresh,
        "progress": progress,
        "cancel": cancel,
        "in_match": in_match,
    }
    if _coach_supports_study(coach):
        kwargs["study_client"] = study_client
        kwargs["build_full_study"] = build_full_study
        try:
            return coach.prepare_playbook(state, **kwargs)
        except TypeError as error:
            msg = str(error)
            if "unexpected keyword argument" not in msg or not (
                "study_client" in msg or "build_full_study" in msg
            ):
                raise
            kwargs.pop("study_client", None)
            kwargs.pop("build_full_study", None)
    return coach.prepare_playbook(state, **kwargs)


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
        # Enrich with 14-day win/loss records and highlight the active Winning decks pick.
        try:
            from arenamcp.between_match_coach import deck_win_records, evaluate_live_queue_plan
            from arenamcp.match_history import MatchHistory

            history = MatchHistory()
            records = history.get_recent(history.total_games)
            account_id = ""
            for rec in reversed(records):
                acct = (getattr(rec, "local_account_id", "") or "").strip()
                if acct:
                    account_id = acct
                    break
            by_ident, by_name = deck_win_records(records, account_id=account_id)
            plan = evaluate_live_queue_plan()
            rec_deck = plan.recommended
            for row in rows:
                ident = str(row.get("identity") or "").strip()
                name_low = str(row.get("name") or "").strip().casefold()
                wl = by_ident.get(ident) if ident else None
                if wl is None:
                    wl = by_name.get(name_low)
                wins, losses = wl if wl is not None else (0, 0)
                total = wins + losses
                row["wins"] = wins
                row["losses"] = losses
                row["win_rate"] = (wins / total) if total > 0 else None
                is_pick = bool(
                    rec_deck is not None
                    and (
                        row.get("deck_id") == rec_deck.deck_id or (ident and ident == rec_deck.deck_identity)
                    )
                )
                row["is_winning_pick"] = is_pick
                row["winning_event"] = rec_deck.event_id if is_pick and rec_deck is not None else ""
        except Exception as error:
            logger.debug("Deck win-rate enrichment skipped: %s", error)
        # Winning decks pick first, then most recently played; decks without a card list last.
        rows.sort(
            key=lambda row: (
                not row["complete"],
                not bool(row.get("last_played")),
                str(row.get("name") or "").lower(),
            )
        )
        rows.sort(key=lambda row: str(row.get("last_played") or ""), reverse=True)
        rows.sort(key=lambda row: not bool(row.get("is_winning_pick")))
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

    def _deck_study_client(self) -> Any:
        client = getattr(self, "_study_client", None)
        if client is not None:
            return client
        from arenamcp.deck_study import DeckStudyClient

        return DeckStudyClient()

    def _has_complete_idle_playbook(self, record: Any) -> bool:
        """True when ``record`` already has a current idle playbook (including G2 study if supported)."""
        coach = getattr(self, "_coach", None)
        cached = getattr(coach, "_cached_playbook", None)
        if callable(cached) and hasattr(record, "identity") and hasattr(record, "game_state"):
            try:
                identity = record.identity()
                gs = record.game_state()
                if not identity:
                    return False
                withheld_ids = getattr(coach, "_study_withheld_identities", None)
                if isinstance(withheld_ids, set) and identity in withheld_ids:
                    return True
                try:
                    playbook = cached(identity, gs, modes=("idle",))
                except TypeError:
                    playbook = cached(identity, gs)
                if playbook is None:
                    return False
                if not _coach_supports_study(coach):
                    return True
                return _playbook_has_study(playbook) or bool(getattr(playbook, "study_withheld", False))
            except Exception as error:
                logger.debug(
                    "Deck library: idle cache read failed for %s: %s",
                    getattr(record, "name", "?"),
                    error,
                )
                return False
        return self.deck_strategy_status(record).get("status") == "ready"

    def _prepare_playbook_with_study(
        self,
        state: dict[str, Any],
        *,
        cancel: threading.Event | None = None,
        refresh: bool = False,
        progress: Any = None,
    ) -> Any:
        """Ensure an idle playbook + G2 study exists, checking shared cache before creating an LLM backend."""
        if (cancel is not None and cancel.is_set()) or self._deck_library_in_match():
            return None
        coach = getattr(self, "_coach", None)
        if coach is None or not callable(getattr(coach, "prepare_playbook", None)):
            return None

        supports_study = _coach_supports_study(coach)
        client = None
        if supports_study:
            from arenamcp.coach import _MemoStudyClient

            client = _MemoStudyClient.wrap(self._deck_study_client())
        cached_fn = getattr(coach, "_cached_playbook", None)

        if not refresh and callable(cached_fn):
            from arenamcp.deck_strategy import deck_identity

            identity = deck_identity(state)
            cached = None
            if identity:
                try:
                    try:
                        cached = cached_fn(identity, state, modes=("idle",))
                    except TypeError:
                        cached = cached_fn(identity, state)
                except Exception as error:
                    logger.debug("Deck library: idle cache read failed: %s", error)
                    cached = None
            if cached is not None:
                if (
                    _playbook_has_study(cached)
                    or getattr(cached, "study_withheld", False)
                    or not supports_study
                ):
                    if callable(progress):
                        progress("cache")
                    return cached
                result = _call_prepare_playbook(
                    coach,
                    state,
                    backend=None,
                    refresh=False,
                    progress=None,
                    cancel=cancel,
                    in_match=self._deck_library_in_match,
                    study_client=client,
                    build_full_study=False,
                )
                if _playbook_has_study(result) or getattr(result, "study_withheld", False):
                    if callable(progress):
                        progress("cache")
                    return result

        if (cancel is not None and cancel.is_set()) or self._deck_library_in_match():
            return None

        backend = self._deck_library_backend()
        try:
            return _call_prepare_playbook(
                coach,
                state,
                backend=backend,
                refresh=refresh,
                progress=progress,
                cancel=cancel,
                in_match=self._deck_library_in_match,
                study_client=client,
                build_full_study=supports_study,
            )
        finally:
            close = getattr(backend, "close", None)
            if callable(close):
                close()

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
        if self._has_complete_idle_playbook(record) or cancel.is_set():
            return
        try:
            playbook = self._prepare_playbook_with_study(state, cancel=cancel)
        except Exception:
            if cancel.is_set() or self._deck_library_in_match():
                return
            raise
        if playbook is not None and not cancel.is_set() and not self._deck_library_in_match():
            if getattr(playbook, "study_withheld", False):
                logger.info("Pre-match playbook built for %s (study withheld)", record.name)
            else:
                logger.info("Pre-match playbook ready for %s", record.name)

    def _build_missing_playbooks(self, cancel: threading.Event) -> None:
        """Idle sweep (Q3 2026-11): one thinking playbook per deck without one.

        Builds used to start only when a match began — 37-89 s in on the Mac —
        so early turns ran on generic heuristics or waited on a compact build.
        After the selected deck is warm this walks every other complete deck
        without an idle-mode playbook, most recently played first, one at a
        time (they share the model server), and stops at once when a match
        starts or the worker is cancelled.
        """
        if cancel.is_set() or self._deck_library_in_match():
            return
        library = getattr(self, "_deck_library", None) or self.collect_decks()[0]
        records = [record for record in library.values() if record.complete and not record.snapshot]
        # Winning decks pick first, then newest played; decks never played last.
        winning_id = ""
        try:
            from arenamcp.between_match_coach import evaluate_live_queue_plan

            plan = evaluate_live_queue_plan()
            if plan.recommended is not None:
                winning_id = plan.recommended.deck_id
        except Exception:
            pass
        records.sort(key=lambda record: (record.last_played == "", record.name.lower()))
        records.sort(key=lambda record: record.last_played, reverse=True)
        if winning_id:
            records.sort(key=lambda record: record.deck_id != winning_id)
        for record in records:
            if cancel.is_set() or self._deck_library_in_match():
                return
            state = record.game_state()
            identity = record.identity()
            if not identity or self._has_complete_idle_playbook(record):
                continue
            try:
                playbook = self._prepare_playbook_with_study(state, cancel=cancel)
                if playbook is None:
                    if cancel.is_set() or self._deck_library_in_match():
                        return
                    continue
                if not cancel.is_set() and not self._deck_library_in_match():
                    if getattr(playbook, "study_withheld", False):
                        logger.info("Idle playbook built for %s (study withheld)", record.name)
                    else:
                        logger.info("Idle playbook ready for %s", record.name)
            except Exception as error:
                if cancel.is_set() or self._deck_library_in_match():
                    return
                logger.warning("Idle playbook for %s failed: %s", record.name, error)

    def _poll_deck_preparation(self, idle: bool, state: dict | None = None) -> None:
        """Run at most one idle lookup off the coaching thread; live play takes precedence."""
        worker = getattr(self, "_deck_prep_thread", None)
        cancel_builds = getattr(getattr(self, "_coach", None), "_cancel_idle_playbook_builds", None)
        if not idle and callable(cancel_builds):
            cancel_builds("match in progress")
        if not idle or not self._auto_deck_strategy:
            cancel = getattr(self, "_deck_prep_cancel", None)
            if (
                cancel is not None
                and not cancel.is_set()
                and (not idle or (worker is not None and worker.is_alive()))
            ):
                # A match starting stops the idle build at once, even for the
                # same deck (Q3 rework 2026-11): a thinking build can run for
                # minutes, and live play must never wait on it. The match
                # builds its own compact playbook; the next idle window
                # replaces it with a thinking one.
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
                self._build_missing_playbooks(cancel)
            except Exception as error:
                if not cancel.is_set() and not self._deck_library_in_match():
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
            playbook = self._prepare_playbook_with_study(
                record.game_state(),
                refresh=refresh,
                progress=progress,
            )
            if playbook is None:
                raise ValueError("Deck preparation paused for active play")
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

    def deck_card_study(self, request, *, cancelled=None):
        """Answer a local Decks window request; no study runs from gameplay or auto-preparation."""
        from arenamcp import logging_config
        from arenamcp.deck_card_study import (
            card_detail,
            card_menu,
            prepare_card_pass,
            read_card,
            study_card,
            unresolved_card_count,
        )
        from arenamcp.deck_local_guidance import TOPICS, deck_guidance, printed_study_deck, study_piles
        from arenamcp.local_study_model import LocalStudyModel, installed_models

        deck_id, identity, gid = (request.get(k) for k in ("deck_id", "identity", "card"))
        reply = {k: request.get(k) for k in ("token", "deck_id", "identity", "card")}
        record = (getattr(self, "_deck_library", {}) or {}).get(deck_id)
        if not record or not record.complete or not identity or identity != record.identity():
            return {**reply, "error": "The selected deck has changed. Refresh the Decks list."}
        directory = logging_config.LOG_DIR / "cache" / "local_card_studies"
        plans = directory.parent / "local_deck_guidance"
        scope = request.get("scope", "cards")
        try:
            state = record.game_state()
            book = self._coach._cached_playbook(identity, state)
            if book is None:
                book = printed_study_deck(state, identity)
            else:
                main, commanders = study_piles(state, identity)
                if (
                    book.identity != identity
                    or sorted(book.commanders) != sorted(commanders)
                    or set(book.catalog) != set(main) | set(commanders)
                ):
                    raise ValueError("The saved strategy no longer matches this deck and commander")
            if scope not in ("cards", "report") and scope not in TOPICS:
                raise ValueError("Unknown deck study topic")
            if request.get("operation") == "list":
                try:
                    weights = installed_models()
                except (OSError, ValueError, TypeError):
                    weights = {}
                return {
                    **reply,
                    "cards": card_menu(book),
                    "models": list(weights),
                    "model_digests": weights,
                    "unresolved": unresolved_card_count(book),
                }
            if scope == "report":
                if request.get("operation") not in ("read", "save_review"):
                    raise ValueError("Study coverage is read-only")
                from arenamcp.deck_local_report import study_report

                return {
                    **reply,
                    **study_report(
                        book,
                        record.game_state(),
                        directory.parent,
                        request.get("model_digest"),
                        save_review=request.get("operation") == "save_review",
                        current_selection=lambda: bool(
                            (latest := (getattr(self, "_deck_library", {}) or {}).get(deck_id))
                            and latest.complete
                            and latest.identity() == identity
                        ),
                    ),
                }
            if request.get("operation") == "read":
                if scope in TOPICS:
                    detail, _ = deck_guidance(
                        book, record.game_state(), scope, plans, topic=request.get("topic")
                    )
                    return {**reply, "detail": detail}
                return {**reply, "detail": read_card(book, gid, directory)}
            if request.get("operation") == "prepare" and scope == "cards":
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
                    return bool(
                        current
                        and current.identity() == identity
                        and self._study_outside_match()
                        and not (cancelled is not None and cancelled())
                    )

                if not allowed():
                    raise ValueError("Card studies can run after the match has finished")
                if scope in TOPICS:
                    model = LocalStudyModel(request.get("model"))
                    detail, reused = deck_guidance(
                        book,
                        record.game_state(),
                        scope,
                        plans,
                        model=model,
                        allowed=allowed,
                        topic=request.get("topic"),
                    )
                    return {**reply, "detail": detail, "reused": reused}
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
            if cancelled is not None and cancelled():
                return {**reply, "error": "Study stopped. Completed passes were saved."}
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
                records = [
                    record
                    for record in library.values()
                    if record.complete and not getattr(record, "snapshot", False)
                ]
                records.sort(
                    key=lambda r: (
                        not bool(getattr(r, "last_played", None)),
                        str(getattr(r, "name", "") or "").lower(),
                    )
                )
                records.sort(key=lambda r: str(getattr(r, "last_played", None) or ""), reverse=True)

                def _has_idle_playbook(rec: Any) -> bool:
                    return self._has_complete_idle_playbook(rec)

                for record in records:
                    if _has_idle_playbook(record):
                        continue
                    if self._deck_library_in_match():
                        logger.info("Deck strategy: analyze-all paused for the match")
                        while self._deck_library_in_match() and getattr(self, "_running", True) is not False:
                            time.sleep(self._ANALYZE_ALL_MATCH_POLL_S)
                        if getattr(self, "_running", True) is False:
                            return
                        logger.info("Deck strategy: analyze-all resumes after the match")
                        if _has_idle_playbook(record):
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
