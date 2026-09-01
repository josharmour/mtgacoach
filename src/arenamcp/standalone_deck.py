"""Deck-analysis helpers for StandaloneCoach, extracted from standalone.py.

Pure move: methods are unchanged and mixed back into StandaloneCoach."""

import contextlib
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class _DeckAnalysisMixin:
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
                    # Also store as the deck strategy so /deck-strategy can recall it
                    self._coach._deck_strategy = strategy
                    self.ui.log(f"\n[bold green]DECK STRATEGY:[/] {strategy}\n")
                    self.speak_advice(strategy)
            except Exception as e:
                logger.error(f"Deck strategy brief failed: {e}")

        threading.Thread(target=_run, daemon=True, name="deck-strategy-brief").start()

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
        """Compute remaining library by subtracting visible cards from deck_cards.

        Returns a compact summary like "~28 cards: 2x Mountain, 1x Lightning Bolt, ..."

        Args:
            game_state: Snapshot dict with deck_cards, players, and zone lists.
            detailed: When True, append mana cost and oracle text per card
                (win-plan/tutor prompts). When False, emit the compact
                counts-and-draw-odds form used in every advice prompt.
        """
        deck_cards = game_state.get("deck_cards", [])
        if not deck_cards:
            return ""

        # Get local player seat
        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        local_seat = local_player.get("seat_id") if local_player else 1

        # Collect grp_ids of visible cards owned by local player
        visible_grp_ids = []
        for zone in ["hand", "battlefield", "graveyard", "exile", "stack", "command"]:
            for card in game_state.get(zone, []):
                if card.get("owner_seat_id") == local_seat:
                    grp_id = card.get("grp_id", 0)
                    if grp_id:
                        visible_grp_ids.append(grp_id)

        # Remove visible cards from deck list (handles duplicates correctly)
        remaining = list(deck_cards)
        for grp_id in visible_grp_ids:
            try:
                remaining.remove(grp_id)
            except ValueError:
                pass  # Card not in deck list (token, sideboard, etc.)

        if not remaining:
            return "~0 cards remaining"

        # Enrich grp_ids with card info (deduplicate lookups)
        basic_land_types = {"basic land"}
        card_info_cache: dict[int, dict] = {}
        name_counts: dict[str, int] = {}
        for grp_id in remaining:
            if grp_id not in card_info_cache:
                try:
                    card_info_cache[grp_id] = self._mcp.get_card_info(grp_id)
                except Exception as e:
                    logger.debug(f"Card info lookup failed for grp_id={grp_id} (remaining): {e}")
                    card_info_cache[grp_id] = {"name": f"Unknown({grp_id})"}
            name = card_info_cache[grp_id].get("name", f"Unknown({grp_id})")
            name_counts[name] = name_counts.get(name, 0) + 1

        # Sort by count descending, cap at top 15 unique cards
        sorted_cards = sorted(name_counts.items(), key=lambda x: -x[1])[:15]
        total = len(remaining)
        shown = sum(count for _, count in sorted_cards)

        # Reverse map: name -> grp_id (for info lookup)
        name_to_grp: dict[str, int] = {}
        for grp_id, info in card_info_cache.items():
            name = info.get("name", "")
            if name not in name_to_grp:
                name_to_grp[name] = grp_id

        if not detailed:
            # Compact form for every advice prompt: counts + per-card draw
            # odds, no oracle text. Keeps the prompt lean while letting the
            # LLM reason about draw probability and remaining outs.
            parts = [f"{count}x {name} ({count / total:.0%})" for name, count in sorted_cards]
            if shown < total:
                parts.append(f"+{total - shown} more")
            return (
                f"MY LIBRARY ({total} cards left, deck-minus-seen; "
                f"% = draw chance per card drawn): " + ", ".join(parts)
            )

        # Build detailed summary with oracle text for non-basic lands
        # so the LLM doesn't hallucinate card abilities
        lines = [f"~{total} cards remaining in library:"]
        for name, count in sorted_cards:
            grp_id = name_to_grp.get(name)
            info = card_info_cache.get(grp_id, {}) if grp_id else {}
            type_line = info.get("type_line", "").lower()
            is_basic = any(bt in type_line for bt in basic_land_types)

            if is_basic:
                lines.append(f"  {count}x {name}")
            else:
                mana = info.get("mana_cost", "")
                oracle = info.get("oracle_text", "")
                detail = f"  {count}x {name}"
                if mana:
                    detail += f" {mana}"
                if oracle:
                    detail += f" — {oracle}"
                lines.append(detail)

        if shown < total:
            lines.append(f"  ... and {total - shown} more")

        return "\n".join(lines)

    def _has_tutor_in_hand(self, game_state: dict) -> bool:
        """Check if any card in hand is a tutor/search spell."""
        for card in game_state.get("hand", []):
            oracle = card.get("oracle_text", "").lower()
            if "search your library" in oracle:
                return True
        return False

    def _compute_tutor_library_targets(self, game_state: dict) -> str:
        """Compute library targets grouped by mana value for tutor spells.

        When a tutor/search spell is in hand, the LLM needs to know what
        creatures (and other cards) are available in the library and at
        what mana values, so it can recommend specific X values and targets.
        """
        import re

        deck_cards = game_state.get("deck_cards", [])
        if not deck_cards:
            return ""

        # Get local player seat
        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        local_seat = local_player.get("seat_id") if local_player else 1

        # Collect grp_ids of visible cards owned by local player
        visible_grp_ids = []
        for zone in ["hand", "battlefield", "graveyard", "exile", "stack", "command"]:
            for card in game_state.get(zone, []):
                if card.get("owner_seat_id") == local_seat:
                    grp_id = card.get("grp_id", 0)
                    if grp_id:
                        visible_grp_ids.append(grp_id)

        # Remove visible cards from deck list
        remaining = list(deck_cards)
        for grp_id in visible_grp_ids:
            with contextlib.suppress(ValueError):
                remaining.remove(grp_id)

        if not remaining:
            return ""

        # Look up card info for remaining library cards
        card_info_cache: dict[int, dict] = {}
        for grp_id in remaining:
            if grp_id not in card_info_cache:
                try:
                    card_info_cache[grp_id] = self._mcp.get_card_info(grp_id)
                except Exception as e:
                    logger.debug(f"Card info lookup failed for grp_id={grp_id} (library): {e}")
                    card_info_cache[grp_id] = {"name": f"Unknown({grp_id})"}

        # Group non-land cards by CMC
        by_cmc: dict[int, list[str]] = {}
        for grp_id in remaining:
            info = card_info_cache.get(grp_id, {})
            type_line = info.get("type_line", "").lower()
            # Skip basic lands (not useful tutor targets)
            if "basic" in type_line and "land" in type_line:
                continue
            name = info.get("name", f"Unknown({grp_id})")
            mana_cost = info.get("mana_cost", "")

            # Calculate CMC
            cmc = 0
            if mana_cost:
                generic = re.findall(r"\{(\d+)\}", mana_cost)
                cmc += sum(int(g) for g in generic)
                for color in "WUBRGC":
                    cmc += len(re.findall(rf"\{{{color}\}}", mana_cost))
                hybrid = re.findall(r"\{[^}]+/[^}]+\}", mana_cost)
                cmc += len(hybrid)

            # Build compact descriptor
            is_creature = "creature" in type_line
            power = info.get("power", "")
            toughness = info.get("toughness", "")
            pt = f" ({power}/{toughness})" if is_creature and power and toughness else ""

            # Type indicator for non-creatures
            type_tag = ""
            if not is_creature:
                if "instant" in type_line:
                    type_tag = " [instant]"
                elif "sorcery" in type_line:
                    type_tag = " [sorcery]"
                elif "enchantment" in type_line:
                    type_tag = " [enchant]"
                elif "artifact" in type_line:
                    type_tag = " [artifact]"
                elif "planeswalker" in type_line:
                    type_tag = " [PW]"
                elif "land" in type_line:
                    type_tag = " [land]"

            descriptor = f"{name}{pt}{type_tag}"
            if cmc not in by_cmc:
                by_cmc[cmc] = []
            # Avoid duplicate names at same CMC
            if descriptor not in by_cmc[cmc]:
                by_cmc[cmc].append(descriptor)

        if not by_cmc:
            return ""

        # Build compact summary grouped by CMC
        lines = [f"LIBRARY SEARCH TARGETS (~{len(remaining)} cards):"]
        for cmc in sorted(by_cmc.keys()):
            cards = by_cmc[cmc]
            lines.append(f"  MV {cmc}: {', '.join(cards)}")

        return "\n".join(lines)

    def _inject_library_summary_if_needed(self, game_state: dict) -> None:
        """Inject remaining-library knowledge into game_state for prompts.

        Always injects the compact deck-minus-seen summary so the coach can
        reason about draw odds and remaining outs at every decision; when a
        tutor/search spell is in hand, upgrades to the detailed mana-value
        target breakdown instead.
        """
        try:
            if self._has_tutor_in_hand(game_state):
                summary = self._compute_tutor_library_targets(game_state)
            else:
                summary = self._compute_library_summary(game_state, detailed=False)
            if summary:
                game_state["library_summary"] = summary
        except Exception as e:
            logger.debug(f"Library summary computation failed: {e}")
