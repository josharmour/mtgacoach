"""Deck analysis, stored strategy and presentation for StandaloneCoach."""

import logging
import threading
import time
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)


class _DeckAnalysisMixin:
    def _advise_draft_build(self, snapshot: dict[str, Any]) -> None:
        signature = snapshot.get("pool_signature")
        if not signature or signature == getattr(self, "_last_deck_build_signature", None):
            return
        if snapshot.get("editor_basis") == "live_editor" or (
            len(signature) > 3 and signature[3] == "live_editor"
        ):
            if signature != getattr(self, "_pending_build_signature", None):
                self._pending_build_signature = signature
                self._pending_build_since = time.monotonic()
                return
            if time.monotonic() - self._pending_build_since < 1.5:
                return
        result = self._mcp.analyze_draft_pool()
        if not result.get("pool_size"):
            return
        backend = getattr(getattr(self, "_coach", None), "_backend", None)
        pool_key = signature[:2]
        cached = getattr(self, "_deck_build_proposal", None)
        if cached and pool_key == getattr(self, "_deck_build_pool_key", None):
            result = {
                **cached,
                "editor_cards": result.get("editor_cards"),
                "editor_basis": result.get("editor_basis"),
            }
        elif backend is not None:
            from arenamcp.draft_advisor import DraftAdvisor

            if getattr(self, "_draft_advisor", None) is None:
                self._draft_advisor = DraftAdvisor(backend)
            result = self._draft_advisor.recommend_deck(result)
        self._deck_build_proposal = result
        self._deck_build_pool_key = pool_key
        from arenamcp.limited_deck import reconcile_logged_deck

        result = reconcile_logged_deck(result)
        self._mcp.poll_log()
        live = self._mcp.get_draft_pack()
        if not live.get("is_building") or live.get("pool_signature") != signature:
            logger.info("Deck-building window changed; discarding stale cut advice")
            return
        self._last_deck_build_signature = signature
        detailed = result.get("detailed_text") or result.get("spoken_advice", "")
        if detailed:
            self.ui.log(detailed)
            show_advice = getattr(self.ui, "advice", None)
            if callable(show_advice):
                show_advice(detailed, "DECK")
        spoken = result.get("spoken_advice", "")
        if spoken:
            logger.info("Draft deck cuts (%s): %s", result.get("reasoning_source", "heuristic"), spoken)
            self.speak_advice(spoken, blocking=False)

    def _maybe_analyze_deck(self, state: dict) -> bool:
        """Start one versioned analysis per deck; never publish a stale worker."""
        from arenamcp.deck_strategy import deck_identity

        coach = self._coach
        if (
            not self._auto_deck_strategy
            or coach is None
            or not state.get("match_id")
            or state.get("game_over")
        ):
            return False
        identity = deck_identity(state)
        key = (state.get("match_id"), identity)
        prior_key = getattr(self, "_deck_analysis_key", None)
        if key != prior_key:
            self._deck_analysis_key = key
            self._deck_analysis_attempts = 0
            self._last_deck_analysis_attempt = 0.0
            # A same-match deck/commander swap also invalidates the old plan.
            if coach._deck_analysis_identity and (
                coach._deck_analysis_identity != identity
                or (prior_key is not None and prior_key[0] != key[0])
            ):
                coach.clear_deck_strategy()
            self._deck_analyzed = False
        if coach._deck_playbook and coach._deck_playbook.identity == identity:
            self._deck_analyzed = True
            return False
        if (
            not identity
            or len(state.get("deck_cards") or []) < 20
            or coach._deck_strategy_pending
            or self._deck_analysis_attempts >= 3
            or (self._last_deck_analysis_attempt and time.monotonic() - self._last_deck_analysis_attempt < 30)
        ):
            return False
        snapshot = deepcopy(state)
        generation = coach.begin_deck_analysis(identity)
        self._deck_analysis_attempts += 1
        self._last_deck_analysis_attempt = time.monotonic()
        logger.info("Starting deck playbook analysis for %d cards", len(snapshot["deck_cards"]))

        def analyze():
            backend = None
            try:
                from arenamcp.coach import create_backend

                backend = create_backend(self._backend_name, model=self.model_name)
                strategy = coach.analyze_deck(snapshot, backend=backend, analysis_generation=generation)
                with coach._deck_analysis_lock:
                    if generation != coach._deck_analysis_generation or self._deck_analysis_key != key:
                        return
                    self._deck_analyzed = bool(strategy)
                    playbook = coach._deck_playbook
                if playbook:
                    self.ui.status("DECK", playbook.data["archetype"][:60])
                    brief = playbook.data["spoken_summary"]
                    self.ui.log(f"\n[bold green]DECK STRATEGY:[/] {brief}\n")
                    self.speak_advice(brief, blocking=False)
                else:
                    self.ui.log("Deck analysis incomplete; using current card rules while it retries.")
            except Exception as error:
                logger.warning("Background deck playbook failed: %s", error)
            finally:
                with coach._deck_analysis_lock:
                    if generation == coach._deck_analysis_generation:
                        coach._deck_strategy_pending = False
                if backend is not None and hasattr(backend, "close"):
                    backend.close()

        try:
            threading.Thread(target=analyze, daemon=True, name="deck-playbook").start()
        except Exception:
            with coach._deck_analysis_lock:
                if generation == coach._deck_analysis_generation:
                    coach._deck_strategy_pending = False
            raise
        return True

    def _generate_deck_strategy_brief(self, card_ids: list[int] | None = None) -> None:
        """Generate and speak a brief deck strategy.

        Runs in a background thread so it doesn't block the coaching loop.
        Works for any game mode — draft, sealed, or constructed.

        Args:
            card_ids: Optional pre-captured list of grpIds. If not provided,
                      uses deck_cards from the current game state (library).
        """
        if not self._coach or not self._mcp:
            return

        playbook = self._coach._deck_playbook
        if playbook is not None and card_ids is None:
            brief = playbook.data["spoken_summary"]
            self.ui.log(f"\n[bold green]DECK STRATEGY:[/] {brief}\n")
            self.speak_advice(brief)
            return

        # Capture the list now so the background thread has it
        pre_captured = list(card_ids) if card_ids else None

        def _run():
            try:
                deck_grp_ids = pre_captured or []

                # Fallback: use current game's deck (library), or reconstruct
                # from visible zones if ConnectResp was missed
                if not deck_grp_ids:
                    try:
                        gs = self._mcp.get_game_state()
                        deck_grp_ids = list(gs.get("deck_cards") or [])
                        if not deck_grp_ids:
                            local_seat = self._get_local_seat_from_state(gs)
                            if local_seat is not None:
                                seen = set()
                                for zone in ("hand", "battlefield", "graveyard", "exile", "command"):
                                    for card in gs.get(zone, []):
                                        if card.get("owner_seat_id") == local_seat:
                                            gid = card.get("grp_id", 0)
                                            if gid and gid not in seen:
                                                seen.add(gid)
                                                deck_grp_ids.append(gid)
                    except Exception:
                        pass

                if not deck_grp_ids:
                    self.ui.log("[yellow]No deck available yet. Start a game first.[/]")
                    logger.info("No deck cards available for strategy brief")
                    return

                # Use the card database directly, skipping the MCP tool
                # layer. For a 60-card deck the MCP indirection adds tens of
                # ms of pointless overhead per call.
                from arenamcp.card_db import get_card_database

                card_db = get_card_database()
                enriched = []
                for grp_id in deck_grp_ids:
                    try:
                        info = card_db.get_card_by_arena_id(grp_id)
                        if info is not None:
                            enriched.append(
                                (
                                    info.name or f"Unknown({grp_id})",
                                    info.type_line or "",
                                    info.oracle_text or "",
                                )
                            )
                        else:
                            enriched.append((f"Unknown({grp_id})", "", ""))
                    except Exception:
                        enriched.append((f"Unknown({grp_id})", "", ""))

                from arenamcp.coach import create_backend

                brief_backend = create_backend(self._backend_name, model=self.model_name)
                try:
                    strategy = self._coach.get_deck_strategy_brief(enriched, backend=brief_backend)
                finally:
                    if hasattr(brief_backend, "close"):
                        brief_backend.close()

                if strategy:
                    # Speech is presentation only; never overwrite the playbook.
                    self.ui.log(f"\n[bold green]DECK STRATEGY:[/] {strategy}\n")
                    self.speak_advice(strategy)
            except Exception as e:
                logger.error(f"Deck strategy brief failed: {e}")

        threading.Thread(target=_run, daemon=True, name="deck-strategy-brief").start()

    def get_deck_playbook(self):
        return self._coach._deck_playbook if self._coach else None

    def get_deck_strategy(self) -> str | None:
        """Return the stored deck strategy, or None if not yet analyzed."""
        if self._coach:
            return self._coach._deck_strategy
        return None

    def _resolve_unknown_cards(self, game_state: dict) -> None:
        """Card resolution uses local card database and GRE metadata."""
        pass

    @staticmethod
    def _enrich_vlm_resolved_card(card: dict, name: str) -> None:
        """Try to fill oracle_text using the unified card database."""
        try:
            from arenamcp.card_db import get_card_database

            card_db = get_card_database()
            result = card_db.get_card_by_name(name)
            if result:
                card["oracle_text"] = result.oracle_text or card.get("oracle_text", "")
                card["type_line"] = result.type_line or card.get("type_line", "")
                card["mana_cost"] = result.mana_cost or card.get("mana_cost", "")
                card["name"] = f"{result.name} (vision)"  # Use canonical name
        except Exception as e:
            logger.debug(f"Card enrichment failed for '{name}' (best effort): {e}")

    def get_sideboard_recommendations(self) -> str | None:
        """Generate Bo3 sideboarding recommendations between games."""
        if not self._coach:
            self.ui.log("[yellow]Coach engine not initialized.[/]")
            return None

        self.ui.log("[cyan]Evaluating Bo3 sideboarding options...[/]")
        try:
            game_state = self._mcp.get_game_state() if self._mcp else {}
        except Exception as e:
            logger.debug(f"Could not fetch game state for sideboarding: {e}")
            game_state = {}

        maindeck_cards = game_state.get("deck_cards", [])
        sideboard_cards = game_state.get("sideboard_cards", [])
        # The get_game_state snapshot doesn't carry opponent_played_cards —
        # fetch it from the dedicated server tool (same as _get_match_context).
        opp_cards_seen = game_state.get("opponent_played_cards", [])
        if not opp_cards_seen:
            try:
                from arenamcp.server import get_opponent_played_cards

                opp_cards_seen = get_opponent_played_cards() or []
            except Exception as e:
                logger.debug(f"Could not get opponent played cards for sideboarding: {e}")
                opp_cards_seen = []
        if not opp_cards_seen:
            # Between Bo3 games the IntermissionReq handler has already
            # reset() the game state, wiping played_cards — fall back to the
            # pre-reset stash captured by prepare_for_game_end() (last game
            # only). Returns grp_ids; _resolve_card_list enriches them.
            try:
                from arenamcp.server import game_state as _server_game_state

                opp_cards_seen = _server_game_state.get_last_game_opponent_played_cards() or []
            except Exception as e:
                logger.debug(f"Could not read last-game opponent cards for sideboarding: {e}")
                opp_cards_seen = []

        def _resolve_card_list(card_list: list[Any]) -> list[Any]:
            resolved = []
            for item in card_list:
                if isinstance(item, int) and self._mcp:
                    try:
                        info = self._mcp.get_card_info(item)
                        if info:
                            resolved.append(
                                (
                                    info.get("name", f"Card({item})"),
                                    info.get("type_line", ""),
                                    info.get("oracle_text", ""),
                                )
                            )
                            continue
                    except Exception:
                        pass
                resolved.append(item)
            return resolved

        resolved_maindeck = _resolve_card_list(maindeck_cards)
        resolved_sideboard = _resolve_card_list(sideboard_cards)
        resolved_opp = _resolve_card_list(opp_cards_seen)

        game_history = []
        if self._advice_history:
            turns = [
                e.get("game_snapshot", {}).get("turn_number", 0)
                for e in self._advice_history
                if e.get("game_snapshot")
            ]
            max_turn = max(turns) if turns else 0
            game_history.append({"result": self._detect_match_result() or "Game 1", "turns": max_turn})

        rec = self._coach.recommend_sideboard(
            maindeck_cards=resolved_maindeck,
            sideboard_cards=resolved_sideboard,
            opponent_cards_seen=resolved_opp,
            game_history=game_history,
        )

        if rec:
            self.ui.advice(rec, "SIDEBOARD")
            if self._voice_output:
                self.speak_advice(rec, blocking=False)
            return rec
        else:
            self.ui.log("[yellow]Could not generate sideboard recommendations.[/]")
            return None

    def _compute_library_summary(self, game_state: dict, detailed: bool = True) -> str:
        """Shared complete inventory; full rules are cached in the deck prefix."""
        from arenamcp.match_context import prepare_match_context, with_deck_reference

        prepared = prepare_match_context(game_state)
        summary = prepared.get("library_summary", "")
        return with_deck_reference(summary, prepared) if detailed else summary

    def _has_tutor_in_hand(self, game_state: dict) -> bool:
        return any(
            "search your library" in (c.get("oracle_text") or "").lower() for c in game_state.get("hand", [])
        )

    def _compute_tutor_library_targets(self, game_state: dict) -> str:
        """Every remaining candidate, with costs and rules in the deck reference."""
        return self._compute_library_summary(game_state, detailed=True)

    def _inject_library_summary_if_needed(self, game_state: dict) -> None:
        from arenamcp.match_context import prepare_match_context

        prepared = prepare_match_context(game_state)
        for key in ("deck_reference", "deck_catalog", "library_summary"):
            if key in prepared:
                game_state[key] = prepared[key]
