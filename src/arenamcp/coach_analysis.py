"""Post-match analysis, win planning, sideboard recommendations, and image completion mixin."""

from __future__ import annotations

import logging
import time
from typing import Any

from arenamcp.backend_health import (
    is_backend_error_text,
)
from arenamcp.backends.proxy import ProxyBackend
from arenamcp.coach_backends import _is_local_backend
from arenamcp.coach_prompts import (
    POST_MATCH_ANALYSIS_PROMPT,
    SIDEBOARD_RECOMMENDATION_PROMPT,
    WIN_PLAN_PROMPT,
)

logger = logging.getLogger(__name__)


class _CoachAnalysisMixin:
    """Post-match evaluation, sideboard guidance, win planning, and vision analysis."""

    def get_win_plan(
        self,
        game_state: dict[str, Any],
        turns: int,
        library_summary: str = "",
        backend=None,
    ) -> str:
        """Get a multi-turn strategic plan for winning in N turns.

        Args:
            game_state: Dict from get_game_state() MCP tool
            turns: Number of turns to plan for (2-8)
            library_summary: Compact summary of remaining library cards
            backend: Optional separate backend instance (e.g. thinking-enabled).
                     If provided, used instead of self._backend.

        Returns:
            Strategic plan string from the LLM
        """
        import concurrent.futures

        total_start = time.perf_counter()
        be = backend or self._backend

        # Build context (honors MTGACOACH_PROMPT_VARIANT)
        context = self._build_context(game_state)

        # Build system prompt with turn count injected
        system_prompt = WIN_PLAN_PROMPT.format(n=turns)

        # Inject deck strategy if available
        if self._deck_strategy:
            system_prompt += (
                f"\n\nDECK STRATEGY:\n{self._deck_strategy}"
                "\n\nAlign the plan with this deck's win conditions and play patterns."
            )

        # Build user message with game context and library
        user_message = context
        if library_summary:
            user_message += f"\n\nLIBRARY REMAINING:\n{library_summary}"
        user_message += f"\n\nCreate a plan to win in exactly {turns} turns."

        # Longer timeout for strategic plans (more tokens to generate).
        is_thinking = isinstance(be, ProxyBackend) and be.enable_thinking
        is_local = _is_local_backend(be)
        if is_thinking:
            api_timeout = 90
        elif is_local:
            api_timeout = 90  # Local models need much more time
        else:
            api_timeout = 45

        api_start = time.perf_counter()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Win plans need more tokens than standard advice (400).
        # Only ProxyBackend supports the max_tokens / request_timeout_s kwargs.
        if isinstance(be, ProxyBackend):
            future = executor.submit(
                be.complete,
                system_prompt,
                user_message,
                4096,
                request_timeout_s=api_timeout,
            )
        else:
            future = executor.submit(be.complete, system_prompt, user_message)
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(f"Win plan API call timed out after {api_timeout}s")
            response = ""
        executor.shutdown(wait=False)
        api_time = (time.perf_counter() - api_start) * 1000

        total_time = (time.perf_counter() - total_start) * 1000
        logger.info(
            f"[TIMING] Win plan API: {api_time:.0f}ms, total: {total_time:.0f}ms, "
            f"turns={turns}, response: {len(response)} chars"
        )

        return response

    def generate_post_match_analysis(
        self,
        advice_history: list[dict[str, Any]],
        match_result: str,
        match_duration_turns: int,
        deck_strategy: str = "",
        final_life_totals: dict | None = None,
        opponent_played_cards: list[str] | None = None,
        backend: Any | None = None,
        missed_decisions: list[dict] | None = None,
        replay_context: str | None = None,
    ) -> str:
        """Generate a post-match strategic analysis from the advice log.

        Args:
            advice_history: Chronological list of advice dicts from the match
            match_result: "win", "loss", "draw", or "unknown"
            match_duration_turns: Total turn count of the match
            deck_strategy: Deck strategy summary (from analyze_deck)
            final_life_totals: {seat_id: life} at match end
            opponent_played_cards: Card names the opponent revealed
            backend: Optional dedicated backend (avoids lock contention)
            missed_decisions: Vision watchdog detections (unmapped decision points)
            replay_context: Parsed replay decision-point summary (from .rply file)

        Returns:
            Analysis string from the LLM, or "" on failure.
        """
        import concurrent.futures

        be = backend or self._backend

        # Build chronological match narrative
        lines = []
        result_label = (
            "VICTORY"
            if match_result == "win"
            else "DEFEAT"
            if match_result == "loss"
            else "DRAW"
            if match_result == "draw"
            else "UNKNOWN"
        )
        if result_label == "UNKNOWN":
            lines.append(
                "MATCH RESULT: UNKNOWN — the result could not be determined automatically. "
                "The player may have conceded, disconnected, or the opponent won by an "
                "undetected mechanism. Do NOT assume the player won. If life totals suggest "
                "the player was ahead, they likely conceded."
            )
        else:
            lines.append(f"MATCH RESULT: {result_label}")
        lines.append(f"MATCH LENGTH: {match_duration_turns} turns")

        if final_life_totals:
            for seat, life in final_life_totals.items():
                lines.append(f"Final life (Seat {seat}): {life}")

        if deck_strategy:
            lines.append(f"\nDECK STRATEGY:\n{deck_strategy}")

        if opponent_played_cards:
            lines.append(f"\nOPPONENT CARDS SEEN: {', '.join(opponent_played_cards[:30])}")

        lines.append("\nCHRONOLOGICAL ADVICE LOG:")
        for entry in advice_history:
            snap = entry.get("game_snapshot") or {}
            turn = snap.get("turn_number", "?")
            phase = snap.get("phase", "?")
            trigger = entry.get("trigger", "unknown")
            advice_text = entry.get("advice", "")
            ctx = entry.get("game_context", "") or ""

            # Include life totals from snapshot for each entry
            life_str = ""
            players = snap.get("players", [])
            if players:
                parts = [f"Seat{p.get('seat_id')}={p.get('life_total')}" for p in players]
                life_str = f" Life: {', '.join(parts)}"

            board_info = ""
            if snap.get("battlefield_count"):
                board_info = f" Board:{snap['battlefield_count']} Hand:{snap.get('hand_count', '?')}"

            # Strip library search targets and trim context for post-match
            # analysis — the full board state per turn is useful but the
            # 90+ card library list bloats the prompt for no analytic value.
            ctx_snippet = ctx
            if "\nLIBRARY SEARCH TARGETS" in ctx_snippet:
                ctx_snippet = ctx_snippet[: ctx_snippet.index("\nLIBRARY SEARCH TARGETS")]
            # Cap each entry's context to avoid huge prompts in long games
            if len(ctx_snippet) > 2000:
                ctx_snippet = ctx_snippet[:2000] + "\n[...truncated]"

            lines.append(f"\n--- Turn {turn}, {phase} [{trigger}]{life_str}{board_info} ---")
            if ctx_snippet:
                lines.append(f"Context: {ctx_snippet}")
            lines.append(f"Advice: {advice_text}")

        if missed_decisions:
            lines.append(f"\nVISION WATCHDOG DETECTIONS ({len(missed_decisions)} missed decision points):")
            lines.append("These are moments where the game was waiting for player input")
            lines.append("but no trigger fired — detected by tempo anomaly + VLM screen analysis.")
            for i, md in enumerate(missed_decisions, 1):
                lines.append(
                    f"  {i}. Turn {md.get('turn', '?')}, {md.get('phase', '?')}: "
                    f"{md.get('decision_type', 'unknown')} — "
                    f'"{md.get("prompt_text", "")}" '
                    f"(stall={md.get('stall_duration_s', '?')}s, conf={md.get('confidence', '?')})"
                )

        if replay_context:
            lines.append("\nREPLAY DATA (authoritative GRE decision history):")
            lines.append(replay_context)

        user_message = "\n".join(lines)

        logger.info(
            f"[POST-MATCH] Generating analysis: {len(advice_history)} entries, "
            f"result={match_result}, turns={match_duration_turns}, "
            f"replay={'yes' if replay_context else 'no'}, "
            f"prompt={len(user_message)} chars"
        )

        # Scale timeout with prompt size — Opus on large prompts needs time
        api_timeout = 60
        if isinstance(be, ProxyBackend):
            api_timeout = 90
        if len(user_message) > 30000:
            api_timeout = max(api_timeout, 120)

        api_start = time.perf_counter()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        # Try with max_tokens first, fall back to 2-arg call.
        import inspect

        sig = inspect.signature(be.complete)
        accepts_kwargs = len(sig.parameters) > 2 or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_kwargs:
            submit_kwargs = {"request_timeout_s": api_timeout} if isinstance(be, ProxyBackend) else {}
            future = executor.submit(
                be.complete,
                POST_MATCH_ANALYSIS_PROMPT,
                user_message,
                4096,
                **submit_kwargs,
            )
        else:
            future = executor.submit(be.complete, POST_MATCH_ANALYSIS_PROMPT, user_message)
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(f"Post-match analysis timed out after {api_timeout}s")
            response = ""
        executor.shutdown(wait=False)

        api_time = (time.perf_counter() - api_start) * 1000
        logger.info(f"[POST-MATCH] API: {api_time:.0f}ms, response: {len(response)} chars")

        if not response or is_backend_error_text(response):
            return ""

        return response

    def recommend_sideboard(
        self,
        maindeck_cards: list[Any],
        sideboard_cards: list[Any],
        opponent_cards_seen: list[Any],
        game_history: list[dict[str, Any]] | None = None,
        backend: Any | None = None,
    ) -> str | None:
        """Generate Best-of-Three (Bo3) sideboarding recommendations.

        Args:
            maindeck_cards: Maindeck card list (tuples, dicts, or strings)
            sideboard_cards: 15-card sideboard list (tuples, dicts, or strings)
            opponent_cards_seen: Opponent cards revealed in previous game(s)
            game_history: Match context (e.g. Game 1 result, turn count)
            backend: Optional backend override

        Returns:
            Recommended swaps and strategic reasoning, or None on failure.
        """

        be = backend or self._backend
        if not be:
            return None

        start = time.perf_counter()

        def _format_card_list(cards: list[Any]) -> str:
            if not cards:
                return "(None revealed or listed)"
            from collections import Counter

            counts = Counter()
            details: dict[str, tuple[str, str]] = {}

            for item in cards:
                if isinstance(item, tuple) and len(item) >= 2:
                    name = item[0]
                    card_type = item[1] if len(item) > 1 else ""
                    oracle = item[2] if len(item) > 2 else ""
                elif isinstance(item, dict):
                    name = item.get("name", "Unknown")
                    card_type = item.get("type_line", item.get("type", ""))
                    oracle = item.get("oracle_text", item.get("text", ""))
                elif isinstance(item, str):
                    name = item
                    card_type = ""
                    oracle = ""
                else:
                    name = str(item)
                    card_type = ""
                    oracle = ""

                counts[name] += 1
                if name not in details:
                    details[name] = (card_type, oracle)

            lines = []
            for name, count in counts.most_common():
                card_type, oracle = details.get(name, ("", ""))
                type_short = card_type.split("—")[0].strip() if card_type else ""
                line = f"{count}x {name}"
                if type_short:
                    line += f" ({type_short})"
                if oracle and "basic" not in card_type.lower():
                    short_oracle = self._remove_reminder_text(oracle).strip()
                    if short_oracle:
                        if len(short_oracle) > 100:
                            short_oracle = short_oracle[:97] + "..."
                        line += f" — {short_oracle}"
                lines.append(line)
            return "\n".join(lines)

        maindeck_text = _format_card_list(maindeck_cards)
        sideboard_text = _format_card_list(sideboard_cards)
        opp_text = _format_card_list(opponent_cards_seen)

        prompt_lines = [
            "BEST-OF-THREE (Bo3) MATCH SIDEBOARDING CONTEXT:",
        ]

        if game_history:
            history_parts = []
            for i, g in enumerate(game_history, 1):
                res = g.get("result", "unknown")
                turns = g.get("turns", "?")
                history_parts.append(f"Game {i}: {res} ({turns} turns)")
            prompt_lines.append(f"Match History: {', '.join(history_parts)}")

        prompt_lines.extend(
            [
                f"\nPLAYER MAINDECK:\n{maindeck_text}",
                f"\nPLAYER SIDEBOARD:\n{sideboard_text}",
                f"\nOPPONENT CARDS SEEN:\n{opp_text}",
            ]
        )

        user_message = "\n".join(prompt_lines)

        try:
            import inspect

            sig = inspect.signature(be.complete)
            accepts_kwargs = len(sig.parameters) > 2 or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if accepts_kwargs:
                rec = be.complete(SIDEBOARD_RECOMMENDATION_PROMPT, user_message, max_tokens=2048)
            else:
                rec = be.complete(SIDEBOARD_RECOMMENDATION_PROMPT, user_message)

            if not rec or is_backend_error_text(rec):
                logger.warning(f"Sideboard recommendation failed: {rec[:80] if rec else 'empty'}")
                return None

            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"Sideboard recommendation complete: {elapsed:.0f}ms, {len(rec)} chars")
            return rec
        except Exception as e:
            logger.error(f"Sideboard recommendation error: {e}")
            return None

    def generate_win_probability(
        self, game_state: dict[str, Any], opponent_played_cards: list[dict] = None
    ) -> str:
        """Estimate win probability based on current board state.

        Returns a short analysis with a win percentage and recommendation.
        If loss probability exceeds 75%, includes a concede suggestion.
        """
        be = self._backend
        if be is None:
            return ""

        context = self._build_context(game_state)

        system_prompt = (
            "You are an expert MTG analyst. Evaluate the current game state and estimate "
            "the probability that the local player wins this game.\n\n"
            "Consider:\n"
            "- Board presence: creature count, total power/toughness, keywords\n"
            "- Life totals and life trajectory\n"
            "- Cards in hand vs opponent's likely hand size\n"
            "- Mana development (lands in play)\n"
            "- Opponent's revealed cards and likely strategy\n"
            "- Tempo and card advantage\n"
            "- Whether the local player is the beatdown or the control\n\n"
            "Output format (STRICT — follow exactly):\n"
            "Line 1: WIN: XX% (a single integer 0-100)\n"
            "Line 2-3: Brief explanation (2 sentences max) of why.\n"
            "Line 4: If WIN is 25% or below, add: RECOMMEND: Concede — [1-sentence reason]\n\n"
            "Be realistic, not optimistic. A hopeless board is 5-15%, not 30%."
        )

        opp_cards_str = ""
        if opponent_played_cards:
            names = [c.get("name", "?") for c in opponent_played_cards if c.get("name")]
            if names:
                opp_cards_str = f"\nOpponent's revealed cards this game: {', '.join(names)}"

        user_message = f"{context}{opp_cards_str}\n\nEstimate win probability."

        import concurrent.futures

        api_timeout = 30
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        submit_kwargs = {"request_timeout_s": api_timeout} if isinstance(be, ProxyBackend) else {}
        future = executor.submit(be.complete, system_prompt, user_message, 1000, **submit_kwargs)
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("Win probability timed out")
            response = ""
        executor.shutdown(wait=False)

        if not response or is_backend_error_text(response):
            return ""

        logger.info(f"[WIN-PROB] {response[:100]}")
        return response

    _NEGATIVE_BLOCK_PHRASES = (
        "don't block",
        "don’t block",
        "do not block",
        "no block",
        "take the damage",
        "take the hit",
    )

    def complete_with_image(self, system_prompt: str, user_message: str, image_data: bytes) -> str:
        """Call complete_with_image on backend if supported."""
        if hasattr(self._backend, "complete_with_image"):
            return self._backend.complete_with_image(system_prompt, user_message, image_data)
        logger.error(f"Backend {type(self._backend).__name__} does not support complete_with_image")
        return "Image analysis not supported by current backend."
