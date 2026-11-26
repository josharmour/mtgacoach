"""Autoplay for joined draft events, beside the in-match autopilot.

On when autopilot is on and the ``autopilot_draft_events`` setting is not
turned off. The player enters the event; draft picks, the deck build, and the
event's matches are then played through to the end (see draft_event.py).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class _DraftEventMixin:
    def _event_approval_store(self) -> Any:
        from arenamcp.event_queue import EntryApprovalStore

        store = getattr(self, "_event_entry_approvals", None)
        if store is None:
            store = self._event_entry_approvals = EntryApprovalStore()
        return store

    def _current_event_queue_config(self) -> Any:
        mode_fn = getattr(self, "_auto_queue_mode", None)
        if callable(mode_fn) and mode_fn() != "draft":
            return None
        return getattr(self, "_event_queue_config", None)

    def _consume_event_entry_approval(self, event_name: str, currency: str, quantity: int) -> bool:
        token = str(getattr(self, "_pending_entry_token", "") or "")
        if not token:
            return False
        ok = self._event_approval_store().consume(token, event_name, currency, quantity)
        if ok:
            self._pending_entry_token = ""
        return ok

    def _emit_ui_event(self, method_name: str, payload: dict[str, Any]) -> None:
        ui = getattr(self, "ui", None)
        fn = getattr(ui, method_name, None) if ui is not None else None
        if callable(fn):
            try:
                fn(payload)
            except Exception as exc:
                logger.debug("UI event %s failed: %s", method_name, exc)

    def set_auto_queue_mode(self, mode: str) -> str:
        norm = str(mode or "").strip().lower()
        if norm not in {"same", "winning", "draft"}:
            raise ValueError(f"Unsupported auto_queue_mode: {mode!r}")
        if hasattr(self, "settings"):
            self.settings.set("auto_queue_mode", norm)
        if norm != "draft":
            self._event_approval_store().clear()
            self._pending_entry_token = ""
            self._event_queue_config = None
        cfg = self._current_event_queue_config()
        self._emit_ui_event(
            "emit_event_queue_state",
            {
                "mode": norm,
                "enabled": bool(getattr(self, "_auto_queue_enabled", False)),
                "config": cfg.to_dict() if cfg is not None else None,
            },
        )
        return norm

    def list_available_events(self) -> dict[str, Any]:
        from arenamcp.gre_bridge import get_bridge

        bridge = getattr(getattr(self, "_autopilot", None), "_gre_bridge", None) or get_bridge()
        if bridge is None or not getattr(bridge, "connected", False):
            payload: dict[str, Any] = {
                "ok": False,
                "error": "Arena bridge is not connected; open MTG Arena on the Home screen.",
                "wallet": {},
                "events": [],
            }
            self._emit_ui_event("emit_available_events", payload)
            return payload
        payload = bridge.draft_command("list_available_events", timeout=12.0)
        if payload.get("ok"):
            events = payload.get("events") or []
            self._enrich_event_titles(events)
            self._last_available_events = {
                str(ev.get("event_name") or ""): ev for ev in events if ev.get("event_name")
            }
        self._emit_ui_event("emit_available_events", payload)
        return payload

    def build_event_deck(self, request: dict[str, Any]) -> dict[str, Any]:
        """Clone & adapt a near-qualifying deck for an event using owned cards only."""
        from arenamcp import server
        from arenamcp.event_deck_builder import build_event_deck as _build_event_deck
        from arenamcp.gre_bridge import get_bridge

        bridge = getattr(getattr(self, "_autopilot", None), "_gre_bridge", None) or get_bridge()
        event_name = str(request.get("event_name") or "").strip()
        event_format = str(request.get("event_format") or "").strip()
        source_deck_id = str(request.get("source_deck_id") or "").strip()
        display_name = str(request.get("display_name") or "").strip()
        if not event_format or not source_deck_id:
            result = {
                "ok": False,
                "event_name": event_name,
                "event_format": event_format,
                "error": "event_format and source_deck_id are required",
            }
            self._emit_ui_event("emit_event_deck_built", result)
            return result

        log_path = getattr(self, "log_path", None)
        try:
            mtgadb = server._get_mtgadb()
        except Exception:
            mtgadb = None

        result = _build_event_deck(
            bridge=bridge,
            event_name=event_name,
            event_format=event_format,
            source_deck_id=source_deck_id,
            mtgadb=mtgadb,
            log_path=log_path,
            display_name=display_name,
        )
        ui = getattr(self, "ui", None)
        status_fn = getattr(ui, "status", None) if ui is not None else None
        if callable(status_fn):
            if result.get("ok"):
                status_fn("AUTO_QUEUE_DETAIL", str(result.get("summary") or "Deck built."))
            elif result.get("error"):
                status_fn("AUTO_QUEUE_DETAIL", str(result["error"]))
        self._emit_ui_event("emit_event_deck_built", result)
        return result

    def _enrich_event_titles(self, events: list[dict[str, Any]]) -> None:
        try:
            import sqlite3

            from arenamcp.mtgadb import find_mtga_database

            card_db = find_mtga_database()
            if card_db is None:
                return
            files = sorted(
                card_db.parent.glob("Raw_ClientLocalization*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not files:
                return
            with sqlite3.connect(f"{files[0].as_uri()}?mode=ro", uri=True) as conn:
                for ev in events:
                    loc_key = str(ev.get("title_loc_key") or "").strip()
                    if loc_key and (not ev.get("display_name") or "_" in ev["display_name"]):
                        row = conn.execute(
                            "SELECT enUS FROM Loc WHERE Key = ? LIMIT 1", (loc_key,)
                        ).fetchone()
                        if row and row[0]:
                            ev["display_name"] = str(row[0]).strip()
        except Exception:
            pass

    def configure_event_queue(self, raw: dict[str, Any]) -> dict[str, Any]:
        from arenamcp.draft_event import DraftRun
        from arenamcp.event_queue import EventQueueConfig, event_eligibility

        cfg = EventQueueConfig.from_dict(raw)
        if not cfg.event_name:
            raise ValueError("event_name is required")
        live_ev = (getattr(self, "_last_available_events", None) or {}).get(cfg.event_name)
        if isinstance(live_ev, dict):
            decks_fn = getattr(self, "_event_mode_decks_list", None)
            decks = decks_fn() if callable(decks_fn) else None
            eligible, reason = event_eligibility(live_ev, decks)
            if eligible and not live_ev.get("in_progress") and not cfg.is_free:
                fees = list(live_ev.get("entry_fees") or [])
                req_curr = cfg.entry_currency.removeprefix("CurrencyType_")
                if req_curr == "Gems":
                    req_curr = "Gem"
                matched = any(
                    (
                        str(f.get("currency_type") or "").removeprefix("CurrencyType_") == req_curr
                        or (
                            str(f.get("currency_type") or "").removeprefix("CurrencyType_") == "Gems"
                            and req_curr == "Gem"
                        )
                    )
                    and int(f.get("quantity") or 0) == cfg.entry_quantity
                    and f.get("affordable")
                    for f in fees
                )
                if not matched:
                    eligible = False
                    reason = f"Insufficient funds for {cfg.entry_currency} {cfg.entry_quantity}"
            if not eligible:
                msg = f"Cannot start {cfg.display_name or cfg.event_name}: {reason}"
                self.ui.status("AUTO_QUEUE_DETAIL", msg)
                raise ValueError(msg)
        store = self._event_approval_store()
        store.clear()
        self._pending_entry_token = ""
        if not cfg.is_free and raw.get("confirm_paid") is True:
            self._pending_entry_token = store.mint(cfg.event_name, cfg.entry_currency, cfg.entry_quantity)

        prev = getattr(self, "_event_queue_config", None)
        self._event_queue_config = cfg
        self._last_event_blocker = ""
        if hasattr(self, "settings"):
            self.settings.set("auto_queue_mode", "draft")
        if hasattr(self, "set_auto_queue"):
            self.set_auto_queue(True)

        driver = getattr(self, "_draft_event_driver", None)
        if driver is not None:
            if prev is None or prev.event_name != cfg.event_name:
                driver.run = DraftRun(event_name=cfg.event_name)
            driver.run.awaiting_reentry_decision = False
            driver.run.finished = False
            driver.run.go_to_event_attempts = 0
            driver.run.entry_attempts = 0
            driver.resume()

        state = {
            "mode": "draft",
            "enabled": bool(getattr(self, "_auto_queue_enabled", True)),
            "config": cfg.to_dict(),
        }
        self._emit_ui_event("emit_event_queue_state", state)
        ap_on = bool(getattr(self, "_autopilot_enabled", False))
        ctrl_fn = getattr(self, "_autopilot_control_status", None)
        if callable(ctrl_fn) and ctrl_fn() != "AP:ON":
            ap_on = False
        detail = (
            f"Event mode active: {cfg.display_name or cfg.event_name}"
            if ap_on
            else f"Event mode ready ({cfg.display_name or cfg.event_name}) — turn Autoplay on to start."
        )
        self.ui.status("AUTO_QUEUE_DETAIL", detail)
        return state

    def _on_event_run_complete(self, summary: dict[str, Any]) -> None:
        request_id = uuid.uuid4().hex
        pending = self.__dict__.setdefault("_pending_reentry_requests", {})
        pending[request_id] = dict(summary)
        # Keep only newest 10 pending prompts.
        while len(pending) > 10:
            oldest = next(iter(pending))
            pending.pop(oldest, None)
        payload = {"request_id": request_id, **summary}
        self._emit_ui_event("emit_event_reentry_prompt", payload)

    def respond_event_reentry(
        self,
        request_id: str,
        action: str,
        currency: str = "",
        quantity: int = 0,
    ) -> dict[str, Any]:
        from arenamcp.draft_event import DraftRun

        pending = self.__dict__.setdefault("_pending_reentry_requests", {})
        prompt = pending.pop(request_id, None) if request_id else None
        cfg = getattr(self, "_event_queue_config", None)
        driver = getattr(self, "_draft_event_driver", None)

        if action in ("pause", "stop"):
            if hasattr(self, "set_auto_queue"):
                self.set_auto_queue(False)
            state = {
                "mode": self._auto_queue_mode() if hasattr(self, "_auto_queue_mode") else "draft",
                "enabled": False,
                "config": cfg.to_dict() if cfg is not None else None,
            }
            self._emit_ui_event("emit_event_queue_state", state)
            return state

        if action == "switch_event":
            if hasattr(self, "set_auto_queue"):
                self.set_auto_queue(False)
            return self.list_available_events()

        if cfg is None:
            raise ValueError("No active event configuration to re-enter")

        if action == "resign_and_reenter":
            from arenamcp.gre_bridge import get_bridge

            bridge = getattr(getattr(self, "_autopilot", None), "_gre_bridge", None) or get_bridge()
            if bridge is None or not getattr(bridge, "connected", False):
                err = f"Cannot resign {cfg.event_name}: Arena bridge is not connected."
                self.ui.status("AUTO_QUEUE_DETAIL", err)
                return {"ok": False, "error": err}
            resign_resp = bridge.draft_command(
                "resign_event",
                event_name=cfg.event_name,
                require_free=cfg.is_free,
                timeout=10.0,
            )
            if not resign_resp.get("ok"):
                err = str(resign_resp.get("error") or f"Failed to resign {cfg.event_name}")
                self.ui.status("AUTO_QUEUE_DETAIL", err)
                return {"ok": False, "error": err}

        if action in ("reenter", "resign_and_reenter"):
            if currency:
                cfg.entry_currency = str(currency)
                cfg.entry_quantity = max(0, int(quantity or 0))
                cfg.is_free = (
                    cfg.entry_currency.removeprefix("CurrencyType_") in {"Free", "None"}
                    and cfg.entry_quantity == 0
                )
            elif prompt and prompt.get("entry_fees") and not cfg.is_free:
                # Retain existing currency/quantity when omitted.
                pass
            store = self._event_approval_store()
            store.clear()
            self._pending_entry_token = ""
            if not cfg.is_free:
                self._pending_entry_token = store.mint(cfg.event_name, cfg.entry_currency, cfg.entry_quantity)
            if driver is not None:
                driver.run = DraftRun(event_name=cfg.event_name)
                driver.resume()
            if hasattr(self, "set_auto_queue") and not getattr(self, "_auto_queue_enabled", False):
                self.set_auto_queue(True)
            state = {
                "mode": "draft",
                "enabled": bool(getattr(self, "_auto_queue_enabled", True)),
                "config": cfg.to_dict(),
            }
            self._emit_ui_event("emit_event_queue_state", state)
            return state

        raise ValueError(f"Unknown re-entry action: {action!r}")

    def _event_mode_decks_list(self) -> list[dict[str, Any]]:
        try:
            library, _ = self.collect_decks()
            return [
                {
                    "deck_id": r.deck_id,
                    "name": r.name,
                    "format": r.format,
                }
                for r in library.values()
                if getattr(r, "deck_id", "")
            ]
        except Exception:
            return []

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
        from arenamcp import server

        # SubmitDeckReq preserves the previous game's turn (#318). It can
        # arrive before the result, so that turn must not hide the sideboard UI.
        if getattr(server.game_state, "pending_decision", None) == "Sideboard":
            return False
        return server._match_in_progress()

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
        from arenamcp.sideboard_plan import SideboardAdvisor, opponent_ids_for_sideboard

        card_db = server._get_mtgadb()
        primers = self._set_primer_service()
        advisors: dict[str, Any] = {}

        def advisor(kind: str, timeout: float) -> Any:
            backend = self._draft_event_backend()
            current = advisors.get(kind)
            if current is None or getattr(current, "_backend", None) is not backend:
                cls = SideboardAdvisor if kind == "sideboard" else DraftAdvisor
                advisors[kind] = cls(backend, timeout=timeout)
            return advisors[kind]

        def status(detail: str) -> None:
            if detail:
                self.ui.log(f"[DRAFT AUTOPLAY] {detail}\n")
                self.ui.status("DRAFT_AUTOPLAY", detail)
                if self._current_event_queue_config() is not None:
                    self.ui.status("AUTO_QUEUE_DETAIL", detail)

        def sideboard_context(state: dict) -> dict:
            def details(gid: int) -> dict:
                card = card_db.get_card(gid)
                return {
                    "grp_id": gid,
                    **{
                        key: getattr(card, key, "") or ""
                        for key in ("name", "type_line", "oracle_text", "mana_cost")
                    },
                }

            deck = state["deck"]
            cards = [details(gid) for gid in sorted(set(deck["main"] + deck["sideboard"]))]
            # Never import another match's opponent evidence into the plan.
            opponent = opponent_ids_for_sideboard(server.game_state, state["request_id"])
            return {"cards": cards, "opponent": [details(gid) for gid in opponent]}

        driver = self._draft_event_driver = DraftEventDriver(
            bridge_fn=get_bridge,
            tracker_fn=server.get_course_tracker,
            primer_service=primers,
            pick_advisor_fn=lambda: advisor("pick", 8.0),
            deck_advisor_fn=lambda: advisor("deck", 60.0),
            sideboard_advisor_fn=lambda: advisor("sideboard", 8.0),
            sideboard_context_fn=sideboard_context,
            pack_fn=self._mcp.get_draft_pack,
            pool_cards_fn=server.limited_pool_cards,
            picked_fn=lambda: list(server.draft_state.picked_cards),
            card_db=card_db,
            in_match_fn=self._draft_event_in_match,
            status_fn=status,
            deck_preview_fn=getattr(self.ui, "limited_deck_preview", None),
            speak_fn=lambda text: self.speak_advice(text, blocking=False),
            narrate_fn=self._narrate_draft_pick,
            narrate_first_fn=lambda: bool(
                getattr(self, "settings", None) and self.settings.get("draft_narrate_before_pick", False)
            ),
            commentary_fn=lambda: bool(
                getattr(self, "settings", None) and self.settings.get("draft_commentary", True)
            ),
            queue_fn=lambda: bool(getattr(self, "_auto_queue_enabled", False)),
            event_mode_fn=self._current_event_queue_config,
            consume_entry_approval_fn=self._consume_event_entry_approval,
            on_run_complete_fn=self._on_event_run_complete,
            get_decks_fn=self._event_mode_decks_list,
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
            event_queue_active = (
                bool(getattr(self, "_auto_queue_enabled", False))
                and callable(getattr(self, "_auto_queue_mode", None))
                and self._auto_queue_mode() == "draft"
                and self._current_event_queue_config() is not None
            )
            draft_events_enabled = (
                bool(settings.get("autopilot_draft_events", True)) if settings is not None else False
            )
            active = (
                bool(getattr(self, "_autopilot_enabled", False))
                and (draft_events_enabled or event_queue_active)
                and getattr(self, "_mcp", None) is not None
                and not getattr(self, "_autopilot_dry_run", False)
                and not getattr(self, "_autopilot_afk", False)
                and self._autopilot_control_status() == "AP:ON"
                and self._draft_bridge_ready()
            )
            if not active:
                if driver is not None:
                    driver.set_enabled(False)
                if event_queue_active:
                    cfg = self._current_event_queue_config()
                    target_name = (
                        (cfg.display_name or cfg.event_name) if cfg is not None else "the selected event"
                    )
                    if (
                        not getattr(self, "_autopilot_enabled", False)
                        or self._autopilot_control_status() != "AP:ON"
                    ):
                        blocker = f"Waiting for Autoplay — turn Autoplay on to start {target_name}."
                    elif getattr(self, "_autopilot_dry_run", False) or getattr(self, "_autopilot_afk", False):
                        blocker = "Event mode paused while Dry Run or AFK mode is on."
                    elif not self._draft_bridge_ready():
                        blocker = "Event mode waiting: Arena bridge is not connected."
                    else:
                        blocker = ""
                    if blocker and blocker != getattr(self, "_last_event_blocker", ""):
                        self._last_event_blocker = blocker
                        self.ui.status("AUTO_QUEUE_DETAIL", blocker)
                return False
            self._last_event_blocker = ""
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
