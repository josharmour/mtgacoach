"""Coach engine with pluggable LLM backends for MTG game coaching.

This module provides the CoachEngine for getting strategic advice from LLMs,
with support for online (mtgacoach.com) and local (Ollama/LM Studio) modes.
"""

import contextlib
import json
import logging
import os
import re
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arenamcp.rules_db import RulesDB

from arenamcp.backend_health import (
    BACKEND_ERROR_PREFIX,
    LOCAL_FALLBACK_PREFIX,
    is_backend_error_text,
)
from arenamcp.backends import LLMBackend, ProxyBackend
from arenamcp.coach_postprocess import _AdvicePostprocessMixin
from arenamcp.coach_prompt_utils import (
    _ACTIONS_AVAILABLE_BRIDGE_REQUESTS,
    _build_bridge_context_lines,
    _fallback_non_action_advice,
    _format_legal_actions_raw_for_prompt,
)
from arenamcp.coach_prompts import (
    CONCISE_SYSTEM_PROMPT,
    DECISION_PROMPTS,
    DECK_ANALYSIS_PROMPT,
    DECK_STRATEGY_BRIEF_PROMPT,
    DEFAULT_SYSTEM_PROMPT,
    POST_MATCH_ANALYSIS_PROMPT,
    SIDEBOARD_RECOMMENDATION_PROMPT,
    WIN_PLAN_PROMPT,
)
from arenamcp.coach_triggers import GameStateTrigger

logger = logging.getLogger(__name__)


__all__ = [
    "CoachEngine",
    "GameStateTrigger",
    "WordUsageTracker",
    "create_backend",
    "create_local_fallback",
    "get_available_modes",
    "get_models_for_mode",
    "pick_thinking_model",
    "DEFAULT_SYSTEM_PROMPT",
    "CONCISE_SYSTEM_PROMPT",
    "DECISION_PROMPTS",
    "WIN_PLAN_PROMPT",
    "DECK_ANALYSIS_PROMPT",
    "DECK_STRATEGY_BRIEF_PROMPT",
    "POST_MATCH_ANALYSIS_PROMPT",
    "SIDEBOARD_RECOMMENDATION_PROMPT",
    "_build_bridge_context_lines",
    "_fallback_non_action_advice",
]


# LLM Backend Protocol and Implementations


def _is_local_backend(be: Any) -> bool:
    """True when `be` is a ProxyBackend pointed at a local LLM server.

    Detects vLLM (port 8000), Ollama (11434), LM Studio (1234), and the
    legacy api_key markers we wrote out before the vLLM migration. Used to
    pick the longer LLM timeouts that local inference needs.
    """
    if not isinstance(be, ProxyBackend):
        return False
    url = (getattr(be, "_base_url", "") or "").lower()
    if any(host in url for host in ("localhost", "127.0.0.1", "0.0.0.0")):
        return True
    key = (getattr(be, "_api_key", "") or "").lower()
    return key in ("vllm", "ollama", "lm-studio")


def get_available_modes() -> list[tuple[str, str]]:
    """Return available backend modes.

    Returns list of ``(display_name, mode_id)`` tuples.
    Only online mode is available.
    """
    return [
        ("Online", "online"),
    ]


def get_models_for_mode(mode: str) -> list[tuple[str, str | None]]:
    """Return models available for the given mode.

    Returns list of ``(display_name, model_id_or_None)`` tuples.
    ``None`` means "use the mode's default model".

    Queries the endpoint's /v1/models dynamically and falls back to
    a sensible default.
    """
    import urllib.request as _urlreq

    mode = mode.lower()

    if mode == "online":
        try:
            from arenamcp.backends.proxy import ONLINE_BASE_URL
            from arenamcp.settings import get_settings

            license_key = get_settings().get("license_key", "")
            headers = {"User-Agent": "mtgacoach-client/1.0"}
            if license_key:
                headers["Authorization"] = f"Bearer {license_key}"
            req = _urlreq.Request(f"{ONLINE_BASE_URL}/models", headers=headers)
            with _urlreq.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            models: list[tuple[str, str | None]] = []
            for m in data.get("data", []):
                mid = m["id"]
                models.append((mid, mid))
            if models:
                return models
        except Exception:
            pass
        return [("Default", None)]

    if mode == "local":
        try:
            from arenamcp.settings import get_settings

            local_url = get_settings().get("local_url") or "http://localhost:8000/v1"
        except Exception:
            local_url = "http://localhost:8000/v1"
        # Try OpenAI-compatible /v1/models
        try:
            req = _urlreq.Request(f"{local_url}/models")
            with _urlreq.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            models = [(m["id"], m["id"]) for m in data.get("data", []) if m.get("id")]
            if models:
                return models
        except Exception:
            pass
        # Try Ollama-specific /api/tags
        if "11434" in local_url:
            try:
                req = _urlreq.Request("http://localhost:11434/api/tags")
                with _urlreq.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                models = [(m["name"], m["name"]) for m in data.get("models", []) if m.get("name")]
                if models:
                    return models
            except Exception:
                pass
        return [("llama3.2", "llama3.2")]

    return [("Default", None)]


THINKING_MODEL_PREFERENCE = [
    "deepseek-v4-flash",
    "claude-opus-4-6",
    "claude-sonnet-4-5-20250929",
    "gemini-2.5-pro",
    "gpt-5.3-codex",
]


def pick_thinking_model() -> str | None:
    """Auto-select the best available thinking model.

    In online mode, queries the mtgacoach.com /v1/models endpoint.
    Returns the first match from THINKING_MODEL_PREFERENCE, or None.
    """
    import urllib.request

    try:
        from arenamcp.backends.proxy import ONLINE_BASE_URL
        from arenamcp.settings import get_settings

        s = get_settings()
        license_key = s.get("license_key", "")
        if not license_key or s.get("mode") != "online":
            return None

        req = urllib.request.Request(
            f"{ONLINE_BASE_URL}/models",
            headers={
                "Authorization": f"Bearer {license_key}",
                "User-Agent": "mtgacoach-client/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())

        available_ids = {m["id"] for m in data.get("data", [])}
        for model_id in THINKING_MODEL_PREFERENCE:
            if model_id in available_ids:
                logger.info(f"Thinking model selected: {model_id}")
                return model_id

        logger.info(f"No preferred thinking model found among {len(available_ids)} models")
        return None
    except Exception as e:
        logger.warning(f"Could not pick thinking model: {e}")
        return None


def create_backend(
    mode: str,
    model: str | None = None,
    progress_callback: Any | None = None,
) -> LLMBackend:
    """Factory function to create LLM backends by mode.

    Args:
        mode: "online" or "local" (or "auto" for auto-detection)
        model: Optional model override (uses mode default if not specified)
        progress_callback: Optional callback(status: str) for real-time subtask updates

    Returns:
        Configured LLMBackend instance

    Raises:
        ValueError: If mode is not recognized
    """
    mode = mode.lower()

    if mode == "auto":
        from arenamcp.backend_detect import auto_select_mode

        auto_mode, auto_model = auto_select_mode()
        logger.info(f"Auto-selected mode: {auto_mode} (model={auto_model})")
        return create_backend(
            auto_mode,
            model=model or auto_model,
            progress_callback=progress_callback,
        )

    if mode == "online":
        from arenamcp.settings import get_settings

        license_key = get_settings().get("license_key", "")
        return ProxyBackend.create_online(model=model, license_key=license_key)

    if mode == "local":
        from arenamcp.settings import get_settings

        s = get_settings()
        local_url = s.get("local_url") or "http://localhost:8000/v1"
        local_api_key = s.get("local_api_key") or "vllm"
        local_model = model or s.get("local_model")

        # If no model specified, try to auto-detect from the endpoint
        if not local_model:
            try:
                import urllib.request as _urlreq

                req = _urlreq.Request(f"{local_url}/models")
                with _urlreq.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                models_list = [m["id"] for m in data.get("data", []) if m.get("id")]
                if models_list:
                    local_model = models_list[0]
            except Exception:
                pass

        return ProxyBackend.create_local(
            model=local_model,
            url=local_url,
            api_key=local_api_key,
        )

    raise ValueError(f"Unknown mode: {mode}. Use 'auto', 'online', or 'local'.")


def create_local_fallback(
    model: str | None = None,
    progress_callback: Any | None = None,
) -> "ProxyBackend":
    """Create a local backend as a fallback when online mode fails."""
    from arenamcp.backend_detect import DEFAULT_LOCAL_MODEL

    try:
        from arenamcp.settings import get_settings

        s = get_settings()
        local_url = s.get("local_url") or "http://localhost:8000/v1"
        local_api_key = s.get("local_api_key") or "vllm"
    except Exception:
        local_url = "http://localhost:8000/v1"
        local_api_key = "vllm"
    return ProxyBackend.create_local(
        model=model or DEFAULT_LOCAL_MODEL,
        url=local_url,
        api_key=local_api_key,
    )


# Words that tend to be overused by LLMs in coaching contexts
OVERUSE_CANDIDATES = {
    "consider",
    "considering",
    "important",
    "crucial",
    "critical",
    "definitely",
    "absolutely",
    "certainly",
    "essentially",
    "basically",
    "potentially",
    "priority",
    "prioritize",
    "focus",
    "key",
}

# Threshold for blacklisting (uses in window)
OVERUSE_THRESHOLD = 3
OVERUSE_WINDOW_SECONDS = 120


class WordUsageTracker:
    """Tracks word usage over time to detect overused words."""

    def __init__(
        self,
        threshold: int = OVERUSE_THRESHOLD,
        window_seconds: float = OVERUSE_WINDOW_SECONDS,
    ):
        self._threshold = threshold
        self._window = window_seconds
        self._usage: list[tuple[float, str]] = []  # (timestamp, word)

    def record(self, text: str, exclude_words: set[str] | None = None) -> None:
        """Record words from a response.

        Args:
            text: The response text to analyze
            exclude_words: Set of words to ignore (e.g., card names)
        """
        import re

        now = time.time()

        exclude = exclude_words or set()

        # Extract words, lowercase
        words = re.findall(r"\b[a-z]+\b", text.lower())

        # Only track candidate words that aren't excluded
        for word in words:
            if word in OVERUSE_CANDIDATES and word not in exclude:
                self._usage.append((now, word))

        # Prune old entries
        cutoff = now - self._window
        self._usage = [(t, w) for t, w in self._usage if t > cutoff]

    def get_blacklisted(self, exclude_words: set[str] | None = None) -> list[str]:
        """Get words that have been overused in the current window.

        Args:
            exclude_words: Set of words to never blacklist (e.g., card names)
        """
        from collections import Counter

        exclude = exclude_words or set()
        now = time.time()
        cutoff = now - self._window

        # Count words in window
        recent_words = [w for t, w in self._usage if t > cutoff]
        counts = Counter(recent_words)

        # Return words over threshold, excluding protected words
        return [word for word, count in counts.items() if count >= self._threshold and word not in exclude]


class CoachEngine(_AdvicePostprocessMixin):
    """Engine for getting MTG coaching advice from an LLM backend."""

    def __init__(self, backend: LLMBackend | None = None, system_prompt: str | None = None):
        """Initialize the coach engine.

        Args:
            backend: LLM backend to use (default: ProxyBackend)
            system_prompt: Custom system prompt (default: MTG coach persona)
        """
        self._backend = backend if backend is not None else ProxyBackend()
        self._system_prompt = system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT
        self._word_tracker = WordUsageTracker()
        self._deck_strategy: str | None = None
        self._deck_strategy_pending = False
        self._rules_db: RulesDB | None = None
        # Persistent, adaptive strategic plan. Lazily constructed (game_plan.py
        # imports CoachEngine, so a module-level import here would cycle).
        self._game_plan_mgr = None

    def _ensure_game_plan_mgr(self):
        """Lazily construct and return the GamePlanManager (or None on failure)."""
        if self._game_plan_mgr is None:
            try:
                from arenamcp.game_plan import GamePlanManager

                self._game_plan_mgr = GamePlanManager(self._backend)
            except Exception as e:
                logger.debug(f"GamePlanManager unavailable: {e}")
                return None
        return self._game_plan_mgr

    def get_backend_info(self) -> dict[str, Any]:
        """Return diagnostic info about the current LLM backend.

        Returns:
            Dict with backend_type, model, status, and other details.
        """
        be = self._backend
        info: dict[str, Any] = {
            "backend_type": type(be).__name__,
            "model": getattr(be, "model", None) or "(default)",
        }

        if isinstance(be, ProxyBackend):
            from arenamcp.backends.proxy import ONLINE_BASE_URL

            base_url = getattr(be, "_base_url", "")
            if base_url and ONLINE_BASE_URL in base_url:
                info["backend_name"] = "online"
            else:
                info["backend_name"] = "local"
            info["base_url"] = base_url
        else:
            info["backend_name"] = "unknown"

        return info

    def _zone_cards(self, game_state: dict[str, Any], zone_name: str) -> list[dict[str, Any]]:
        zones = game_state.get("zones")
        if isinstance(zones, dict):
            zone_value = zones.get(zone_name)
            if isinstance(zone_value, list):
                return zone_value

        zone_value = game_state.get(zone_name)
        return zone_value if isinstance(zone_value, list) else []

    def _get_local_seat_id(self, game_state: dict[str, Any]) -> int | None:
        for player in game_state.get("players", []):
            if player.get("is_local"):
                return player.get("seat_id")
        return None

    def _parse_mana_value(self, mana_cost: str) -> int:
        import re

        cmc = 0
        for symbol in re.findall(r"\{([^}]+)\}", mana_cost or ""):
            if symbol.isdigit():
                cmc += int(symbol)
            elif "/" in symbol:
                cmc += 1
            elif symbol.upper() in {"W", "U", "B", "R", "G", "C", "X"}:
                cmc += 1 if symbol.upper() != "X" else 0
        return cmc

    def _available_mana_now(self, game_state: dict[str, Any]) -> int:
        local_seat = self._get_local_seat_id(game_state)
        if local_seat is None:
            return 0

        available = 0
        for card in self._zone_cards(game_state, "battlefield"):
            controller = card.get("controller_seat_id") or card.get("owner_seat_id")
            if controller != local_seat:
                continue
            if "land" not in str(card.get("type_line", "")).lower():
                continue
            if card.get("is_tapped"):
                continue
            available += 1
        return available

    def _summarize_threat_card(self, threat: dict[str, Any]) -> str:
        card = threat.get("card") if isinstance(threat.get("card"), dict) else threat
        if not isinstance(card, dict):
            return ""

        parts: list[str] = []
        type_line = str(card.get("type_line", "") or "").strip()
        if type_line:
            parts.append(type_line)

        power = card.get("power")
        toughness = card.get("toughness")
        if power not in (None, "") and toughness not in (None, ""):
            parts.append(f"{power}/{toughness}")

        loyalty = card.get("counters", {}).get("Loyalty") if isinstance(card.get("counters"), dict) else None
        if loyalty not in (None, ""):
            parts.append(f"Loyalty {loyalty}")

        oracle_text = self._clean_oracle_for_prompt(str(card.get("oracle_text", "") or "")).replace("\n", " ").strip()
        if oracle_text:
            parts.append(oracle_text[:220] + ("..." if len(oracle_text) > 220 else ""))

        return " | ".join(parts)

    def _identify_threat_answers(
        self,
        game_state: dict[str, Any],
        threat: dict[str, Any],
    ) -> list[str]:
        threat_card = threat.get("card") if isinstance(threat.get("card"), dict) else threat
        threat_type = str(threat_card.get("type_line", "") or "").lower()
        threat_name = str(threat.get("name", threat_card.get("name", "that threat")) or "that threat")
        available_mana = self._available_mana_now(game_state)

        answers: list[str] = []
        for card in self._zone_cards(game_state, "hand"):
            name = str(card.get("name", "") or "").strip()
            if not name:
                continue

            mana_cost = str(card.get("mana_cost", "") or "")
            if mana_cost and self._parse_mana_value(mana_cost) > available_mana:
                continue

            oracle = str(card.get("oracle_text", "") or "").lower()
            if not oracle:
                continue

            reason = ""
            if "creature" in threat_type:
                if (
                    "destroy target creature" in oracle
                    or "destroy target nonartifact creature" in oracle
                    or "destroy target attacking creature" in oracle
                    or "exile target creature" in oracle
                ):
                    reason = "clean creature removal"
                elif "target creature gets -" in oracle or "deals" in oracle and "target creature" in oracle:
                    reason = "can kill or shrink it"
                elif "fight target creature" in oracle:
                    reason = "can fight it off the board"
            elif "planeswalker" in threat_type:
                if "target planeswalker" in oracle or "any target" in oracle or "target permanent" in oracle:
                    reason = "can answer the planeswalker directly"
            elif ("artifact" in threat_type or "enchantment" in threat_type) and (
                "target artifact" in oracle
                or "target enchantment" in oracle
                or "target nonland permanent" in oracle
                or "target permanent" in oracle
            ):
                reason = "can remove that permanent type"

            if not reason and ("target permanent" in oracle or "target nonland permanent" in oracle):
                reason = f"can answer {threat_name}"

            if reason:
                answers.append(f"{name} ({reason})")

        return answers[:4]

    def _threat_pressure_summary(self, game_state: dict[str, Any], threat: dict[str, Any]) -> str:
        local_seat = self._get_local_seat_id(game_state)
        if local_seat is None:
            return ""

        attackers: list[str] = []
        total_power = 0
        for card in self._zone_cards(game_state, "battlefield"):
            controller = card.get("controller_seat_id") or card.get("owner_seat_id")
            if controller != local_seat:
                continue
            if card.get("is_tapped"):
                continue
            if "creature" not in str(card.get("type_line", "")).lower():
                continue
            name = str(card.get("name", "") or "?")
            power = card.get("power")
            toughness = card.get("toughness")
            if power not in (None, ""):
                with contextlib.suppress(TypeError, ValueError):
                    total_power += int(power)
            attackers.append(
                f"{name} ({power}/{toughness})"
                if power not in (None, "") and toughness not in (None, "")
                else name
            )

        if not attackers:
            return "No untapped creatures available to pressure it right now."
        return f"Untapped pressure available: {', '.join(attackers[:4])} | total power {total_power}."

    def _build_threat_trigger_description(
        self,
        game_state: dict[str, Any],
        threat: dict[str, Any],
        *,
        is_verbose: bool,
    ) -> str:
        name = str(threat.get("name", "that threat") or "that threat")
        warning = str(threat.get("warning", "") or "").strip()
        summary = self._summarize_threat_card(threat)
        answers = self._identify_threat_answers(game_state, threat)
        pressure = self._threat_pressure_summary(game_state, threat)

        lines = [
            f"THREAT ALERT: {name}",
            "Requirements:",
            f"- Name {name} explicitly in the first sentence.",
            "- Explain why it matters in this exact board state, not in general.",
            "- Give the best concrete line using our current hand, battlefield, and deck plan.",
            "- If removal is available now, say which card answers it.",
            "- If removal is not available, give the best containment plan for this turn.",
            "- Do not give generic lines like 'consider attacking it' without naming attackers or the actual plan.",
        ]
        if warning:
            lines.append(f"Threat note: {warning}")
        if summary:
            lines.append(f"Threat details: {summary}")
        if answers:
            lines.append("Available answers now: " + ", ".join(answers))
        else:
            lines.append("Available answers now: none obvious in hand.")
        if pressure:
            lines.append(pressure)

        if is_verbose:
            lines.append("Explain the trade-off if the best line is to race, block, or hold interaction.")

        return "\n".join(lines)

    def _build_threat_fallback(self, game_state: dict[str, Any], threat: dict[str, Any]) -> str:
        """Local threat advice used when the LLM errors or returns empty.

        Output is tagged [LOCAL FALLBACK] — generated locally without the
        LLM, must never be mistaken for model advice.
        """
        name = str(threat.get("name", "That card") or "That card")
        warning = str(threat.get("warning", "") or "").strip()
        answers = self._identify_threat_answers(game_state, threat)
        pressure = self._threat_pressure_summary(game_state, threat)
        threat_card = threat.get("card") if isinstance(threat.get("card"), dict) else threat
        threat_type = str(threat_card.get("type_line", "") or "").lower()

        if answers:
            msg = f"{name} is the key threat. Best line: use {answers[0].split(' (', 1)[0]} on it now, because {warning.lower() if warning else 'it will snowball if it stays in play'}."
        elif "planeswalker" in threat_type and "No untapped creatures" not in pressure:
            msg = f"{name} is the problem. Attack it this turn with the creatures you can spare and keep it from snowballing. {pressure}"
        elif "creature" in threat_type:
            msg = f"{name} is the threat to plan around. You do not have clean instant removal up, so preserve blockers, avoid bad attacks into it, and dig toward an answer."
        else:
            msg = f"{name} is the card to answer. {warning if warning else 'It will generate value if left alone.'} If you cannot remove it now, play to contain it and protect your life total."
        return f"{LOCAL_FALLBACK_PREFIX} {msg}"

    def clear_deck_strategy(self) -> None:
        """Reset deck strategy for a new match."""
        self._deck_strategy = None
        self._deck_strategy_pending = False

    def analyze_deck(self, deck_cards: list[tuple[str, str, str]], backend=None) -> str | None:
        """Analyze a deck list and store the strategy summary.

        Args:
            deck_cards: List of (card_name, card_type, oracle_text) tuples
            backend: Optional separate backend instance (avoids lock contention
                     with advice calls when run on a background thread)

        Returns:
            Strategy string, or None on failure
        """

        start = time.perf_counter()
        self._deck_strategy_pending = True

        # Use dedicated backend if provided, otherwise fall back to shared one
        be = backend or self._backend

        try:
            # Group duplicates compactly: "4x Mountain (Basic Land)"
            from collections import Counter

            # Group by (name, type) for counting, but keep oracle text
            oracle_by_name: dict[str, str] = {}
            count_key = Counter()
            for name, card_type, oracle in deck_cards:
                count_key[(name, card_type)] += 1
                if oracle and name not in oracle_by_name:
                    oracle_by_name[name] = oracle

            deck_lines = []
            for (name, card_type), count in count_key.most_common():
                type_short = card_type.split("—")[0].strip() if card_type else "Unknown"
                line = f"{count}x {name} ({type_short})"
                # Include oracle text for non-basic-land spells so the LLM
                # knows what the card actually does instead of guessing
                oracle = oracle_by_name.get(name, "")
                is_basic = "basic" in (card_type or "").lower()
                if oracle and not is_basic:
                    oracle_short = self._remove_reminder_text(oracle).strip()
                    if oracle_short:
                        line += f" — {oracle_short}"
                deck_lines.append(line)

            deck_text = "\n".join(deck_lines)
            user_message = f"DECK LIST ({len(deck_cards)} cards):\n{deck_text}"

            # Deck analysis benefits from thinking (one-time, not real-time).
            # Also needs more tokens than game advice for the full strategy output.
            try:
                strategy = be.complete(
                    DECK_ANALYSIS_PROMPT,
                    user_message,
                    max_tokens=4096,
                    use_thinking=True,
                )
            except TypeError:
                # Backend doesn't support max_tokens parameter
                strategy = be.complete(DECK_ANALYSIS_PROMPT, user_message)

            # Don't store error/fallback messages as deck strategy
            if not strategy:
                logger.warning("Deck analysis returned empty response")
                return None
            # Check for backend auth/billing errors (e.g. "Credit balance is too low")
            from arenamcp.backend_detect import is_query_failure_retriable

            if (
                is_backend_error_text(strategy)
                or "didn't catch that" in strategy
                or is_query_failure_retriable(strategy)
            ):
                logger.warning(f"Deck analysis returned error-like response: {strategy[:80]}")
                return None

            self._deck_strategy = strategy
            # Seed the persistent game plan from the deck archetype.
            try:
                mgr = self._ensure_game_plan_mgr()
                if mgr is not None:
                    mgr.seed(self._deck_strategy)
            except Exception as e:
                logger.debug(f"Game-plan seed failed (non-fatal): {e}")
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"Deck analysis complete: {elapsed:.0f}ms, {len(strategy)} chars")
            return strategy
        except Exception as e:
            logger.error(f"Deck analysis failed: {e}")
            return None
        finally:
            self._deck_strategy_pending = False

    def get_deck_strategy_brief(self, deck_cards: list[tuple[str, str, str]], backend=None) -> str | None:
        """Generate a brief 3-5 sentence spoken strategy for a deck.

        Uses a conversational prompt suited for TTS output after a draft
        or when the user asks for /deck-strategy.

        Args:
            deck_cards: List of (card_name, card_type, oracle_text) tuples
            backend: Optional separate backend instance

        Returns:
            Brief strategy string, or None on failure
        """

        start = time.perf_counter()
        be = backend or self._backend

        try:
            from collections import Counter

            oracle_by_name: dict[str, str] = {}
            count_key = Counter()
            for name, card_type, oracle in deck_cards:
                count_key[(name, card_type)] += 1
                if oracle and name not in oracle_by_name:
                    oracle_by_name[name] = oracle

            deck_lines = []
            for (name, card_type), count in count_key.most_common():
                type_short = card_type.split("—")[0].strip() if card_type else "Unknown"
                line = f"{count}x {name} ({type_short})"
                oracle = oracle_by_name.get(name, "")
                is_basic = "basic" in (card_type or "").lower()
                if oracle and not is_basic:
                    oracle_short = self._remove_reminder_text(oracle).strip()
                    if oracle_short:
                        line += f" — {oracle_short}"
                deck_lines.append(line)

            deck_text = "\n".join(deck_lines)
            user_message = f"DECK LIST ({len(deck_cards)} cards):\n{deck_text}"

            strategy = be.complete(DECK_STRATEGY_BRIEF_PROMPT, user_message)

            if not strategy or is_backend_error_text(strategy):
                logger.warning(f"Deck strategy brief failed: {strategy and strategy[:80]}")
                return None

            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"Deck strategy brief: {elapsed:.0f}ms, {len(strategy)} chars")
            return strategy
        except Exception as e:
            logger.error(f"Deck strategy brief failed: {e}")
            return None

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate for logging: ~4 chars per token.

        OPTIMIZATION: Added for prompt size monitoring.
        """
        return len(text) // 4

    def _remove_reminder_text(self, text: str) -> str:
        """Remove reminder text (text in parentheses) from oracle text."""
        import re

        # Handle nested parens if possible, but simple greedy match usually works for MTG
        # Use simple non-greedy match for multiple parens
        return re.sub(r"\(.*?\)", "", text)

    @staticmethod
    def _clean_oracle_for_prompt(text: str) -> str:
        """Make oracle text safe and compact for the model prompt.

        MTGA-derived oracle text frequently ships raw HTML (``<nobr>``, ``<i>``,
        ``<br>``) and can carry the same ability line repeated 3-4x (e.g. once
        raw, once ``<nobr>``-wrapped, then raw again) because forged/local DB
        rows concatenate multiple renderings into one field. Strip the tags and
        drop consecutive duplicate lines so each ability appears exactly once
        and no raw markup leaks into the model input.
        """
        import re

        if not text:
            return text
        out: list[str] = []
        prev: str | None = None
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            norm = " ".join(re.sub(r"<[^>]+>", "", line).split())
            if not norm:
                continue
            if norm == prev:  # drop consecutive duplicate line
                continue
            out.append(norm)
            prev = norm
        return "\n".join(out)

    @staticmethod
    def _land_has_abilities(oracle_text: str, type_line: str) -> bool:
        """Return True if a land card has any non-trivial abilities.

        Covers activated abilities (``{T}:``, ability costs), triggered
        abilities ("Whenever", "When"), static abilities, and keyword
        abilities like "Landfall" or "Matter."  Basic land reminder text
        (e.g. ``({T}: Add {G}.)``) is ignored.
        """
        # Basic land subtypes have no meaningful abilities beyond the
        # reminder text already stripped by _remove_reminder_text.
        basic_subtypes = {"plains", "island", "swamp", "mountain", "forest"}
        line_lower = type_line.lower()
        if all(sub not in line_lower for sub in basic_subtypes):
            # Non-basic land — it has abilities worth showing
            return True
        # Even a basic land might have been granted extra abilities
        # (e.g. by a spell), so check the oracle for anything beyond
        # simple mana production.
        oracle = oracle_text.lower()
        if "{t}:" not in oracle and "add " not in oracle:
            return False
        # Has some mana ability — check if there's anything beyond
        # the basic "{T}: Add {X}" pattern
        stripped = re.sub(r"\(.*?\)", "", oracle).strip().lower()
        # If after stripping parens there's still non-empty content,
        # the land has abilities beyond the basic reminder.
        tokens = set(stripped.split())
        basic_tokens = {"{t}", ":", "add", "{g}", "{w}", "{u}", "{b}", "{r}", "{c}", "{p}", "{s}"}
        return bool(tokens - basic_tokens)

    @staticmethod
    def _is_impending(card: dict) -> bool:
        """Check if a creature is in impending state (enchantment with time counters).

        When cast with impending, a card enters as an enchantment with time
        counters.  It is NOT a creature until the last counter is removed, so
        it should not be counted as an attacker, blocker, or combat threat.
        """
        counters = card.get("counters", {})
        has_time = any("time" in k.lower() for k in counters) if counters else False
        if not has_time:
            return False
        # Confirm oracle text mentions impending (avoids false positives on
        # other cards with time counters like suspend/vanishing)
        oracle = card.get("oracle_text", "").lower()
        return "impending" in oracle

    @staticmethod
    def _get_cmc(mana_cost: str) -> int:
        """Calculate converted mana cost from a mana cost string like '{1}{W}{W}'."""
        import re

        if not mana_cost:
            return 0
        cmc = 0
        generic = re.findall(r"\{(\d+)\}", mana_cost)
        cmc += sum(int(g) for g in generic)
        for color in "WUBRGC":
            cmc += len(re.findall(rf"\{{{color}\}}", mana_cost))
        hybrid = re.findall(r"\{[^}]+/[^}]+\}", mana_cost)
        cmc += len(hybrid)
        return cmc

    # ------------------------------------------------------------------
    # Helpers extracted from _format_game_context
    # ------------------------------------------------------------------

    def _compute_combat_trade(self, atk: dict, blk: dict) -> tuple[str, bool, bool] | None:
        """Compute the combat trade result between an attacker and a blocker.

        Returns (result_string, atk_dies, blk_dies), or None if the blocker
        cannot legally block the attacker (e.g. flying vs no fly/reach).
        """
        atk_name = atk.get("name", "?")
        atk_pow = atk.get("power") or 0
        atk_tgh = atk.get("toughness") or 0
        atk_oracle = self._remove_reminder_text(atk.get("oracle_text", "")).lower()
        atk_has_fly = "flying" in atk_oracle
        atk_has_dth = "deathtouch" in atk_oracle
        atk_has_trample = "trample" in atk_oracle
        atk_has_fs = "first strike" in atk_oracle or "double strike" in atk_oracle

        blk_name = blk.get("name", "?")
        blk_pow = blk.get("power") or 0
        blk_tgh = blk.get("toughness") or 0
        blk_oracle = self._remove_reminder_text(blk.get("oracle_text", "")).lower()
        blk_has_fly = "flying" in blk_oracle
        blk_has_reach = "reach" in blk_oracle
        blk_has_dth = "deathtouch" in blk_oracle
        blk_has_fs = "first strike" in blk_oracle or "double strike" in blk_oracle

        if atk_has_fly and not blk_has_fly and not blk_has_reach:
            return None

        atk_dies = (blk_pow >= atk_tgh) or blk_has_dth
        blk_dies = (atk_pow >= blk_tgh) or atk_has_dth
        if atk_has_fs and not blk_has_fs:
            if atk_pow >= blk_tgh or atk_has_dth:
                atk_dies = False
        elif blk_has_fs and not atk_has_fs and (blk_pow >= atk_tgh or blk_has_dth):
            blk_dies = False

        if atk_dies and blk_dies:
            return "TRADE (both die)", True, True
        elif atk_dies:
            return f"{atk_name} dies, {blk_name} lives ({blk_tgh - atk_pow} left)", True, False
        elif blk_dies:
            trample_note = ""
            if atk_has_trample:
                spillover = atk_pow - blk_tgh
                if spillover > 0:
                    trample_note = f", {spillover} trample through"
            return f"{blk_name} dies, {atk_name} lives ({atk_tgh - blk_pow} left){trample_note}", False, True
        else:
            return "both live", False, False

    def _compute_optimal_blocking_damage(self, attackers: list[dict], blockers: list[dict]) -> int:
        """Compute minimum damage through after optimal blocking assignment."""
        available_blk = list(blockers)
        damage_through = 0
        sorted_atk = sorted(attackers, key=lambda c: c.get("power") or 0, reverse=True)
        for atk in sorted_atk:
            atk_pow = atk.get("power") or 0
            atk_oracle = self._remove_reminder_text(atk.get("oracle_text", "")).lower()
            atk_has_fly = "flying" in atk_oracle
            atk_has_trample = "trample" in atk_oracle
            valid = []
            for i, blk in enumerate(available_blk):
                blk_oracle = self._remove_reminder_text(blk.get("oracle_text", "")).lower()
                if atk_has_fly and "flying" not in blk_oracle and "reach" not in blk_oracle:
                    continue
                valid.append((i, blk))
            if valid:
                if atk_has_trample:
                    idx, blocker = max(valid, key=lambda x: x[1].get("toughness") or 0)
                else:
                    idx, blocker = min(valid, key=lambda x: x[1].get("toughness") or 0)
                available_blk.pop(idx)
                if atk_has_trample:
                    spillover = max(0, atk_pow - (blocker.get("toughness") or 0))
                    damage_through += spillover
            else:
                damage_through += atk_pow
        return damage_through

    def _format_legal_moves(self, game_state: dict[str, Any], local_seat: int) -> tuple[list[str], str]:
        """Determine the legal moves and return (valid_moves, valid_moves_str)."""
        pending = game_state.get("pending_decision")
        if pending == "Mulligan":
            return ["KEEP", "MULLIGAN"], "KEEP, MULLIGAN"
        elif pending == "Mulligan Bottom":
            hand_cards = game_state.get("hand", [])
            card_names = [c.get("name", "Unknown") for c in hand_cards]
            return [f"Bottom: {n}" for n in card_names], ", ".join(card_names)
        else:
            try:
                from arenamcp.rules_engine import RulesEngine

                valid_moves = RulesEngine.get_legal_actions(game_state)

                # Override generic casting_time_options legal actions with
                # resolved modal option names from bridge data
                dec_ctx = game_state.get("decision_context") or {}
                if dec_ctx.get("type") == "casting_time_options":
                    modal_moves = self._resolve_modal_legal_actions(game_state)
                    if modal_moves:
                        valid_moves = modal_moves

                if not valid_moves:
                    return [], 'NONE \u2014 say "pass priority"'
                else:
                    return valid_moves, ", ".join(valid_moves)
            except Exception as e:
                logger.error(f"RulesEngine error: {e}")
                return [], "Error"

    def _resolve_modal_legal_actions(self, game_state: dict[str, Any]) -> list[str]:
        """Resolve bridge CastingTimeOption modal entries to readable legal actions."""
        bridge_actions = game_state.get("_bridge_actions") or []
        modal_actions: list[tuple[int, str]] = []

        for ba in bridge_actions:
            if ba.get("actionType") != "CastingTimeOption":
                continue
            kind = ba.get("choiceKind", "")
            opt_idx = ba.get("optionIndex", 0)
            grp_id = ba.get("grpId", 0)

            if kind == "modal" and grp_id:
                try:
                    from arenamcp import server

                    info = server.get_card_info(grp_id)
                    oracle = self._clean_oracle_for_prompt(info.get("oracle_text", ""))
                    # Modal option oracle texts are typically short single-line effects
                    label = (
                        oracle.split("\n")[0].strip() if oracle else info.get("name", f"Mode {opt_idx + 1}")
                    )
                except Exception:
                    label = f"Mode {opt_idx + 1}"
                modal_actions.append((opt_idx, f"Mode {opt_idx}: {label}"))
            elif kind == "numeric_input":
                # X chooser entries (P3-1): the plugin enumerates legal X
                # values as per-entry SubmitX actions.
                val = ba.get("numericValue")
                if val is not None:
                    modal_actions.append((500 + int(val), f"X = {val}"))
            elif kind == "done":
                modal_actions.append((999, "Done (confirm cast)"))

        if not modal_actions:
            return []

        modal_actions.sort(key=lambda x: x[0])
        return [label for _, label in modal_actions]

    def _format_post_land_planning(
        self,
        game_state: dict[str, Any],
        local_seat: int,
        valid_moves: list[str],
        is_my_turn: bool,
        phase: str,
    ) -> list[str]:
        """Compute post-land-drop planning lines."""
        import re as _re_plan

        from arenamcp.rules_engine import RulesEngine

        lines: list[str] = []
        local_player = next((p for p in game_state.get("players", []) if p.get("is_local")), None)
        lands_played_count = local_player.get("lands_played", 0) if local_player else 0
        _stack = game_state.get("stack", [])
        has_land_drop = is_my_turn and "Main" in phase and len(_stack) == 0 and lands_played_count == 0
        if not (has_land_drop and valid_moves):
            return lines

        hand_cards = game_state.get("hand", [])
        bf = game_state.get("battlefield", [])
        cur_mana = RulesEngine._count_available_mana(game_state, local_seat)

        hand_lands: dict[str, dict] = {}
        for c in hand_cards:
            if "Land" in c.get("type_line", ""):
                name = c.get("name", "")
                if name not in hand_lands:
                    hand_lands[name] = c

        if not hand_lands:
            return lines

        has_spelunking = any(
            c.get("owner_seat_id") == local_seat and "spelunking" in (c.get("name") or "").lower() for c in bf
        )

        post_land_parts = []
        for land_name, land_card in hand_lands.items():
            land_oracle = land_card.get("oracle_text", "")
            oracle_low = land_oracle.lower()
            enters_tapped = (
                "enters tapped" in oracle_low or "enters the battlefield tapped" in oracle_low
            ) and not has_spelunking

            post_mana = cur_mana if enters_tapped else cur_mana + 1
            land_colors: set[str] = set()
            for color, basic in [
                ("W", "Plains"),
                ("U", "Island"),
                ("B", "Swamp"),
                ("R", "Mountain"),
                ("G", "Forest"),
            ]:
                if basic in land_name or f"{{{color}}}" in land_oracle:
                    land_colors.add(color)
            if "any color" in oracle_low:
                land_colors = {"W", "U", "B", "R", "G"}

            # Pre-compute whether we have any creatures for targeting checks
            my_creatures = [
                c
                for c in bf
                if c.get("owner_seat_id") == local_seat
                and c.get("power") is not None
                and "land" not in c.get("type_line", "").lower()
            ]

            new_casts = []
            for c in hand_cards:
                if "Land" in c.get("type_line", ""):
                    continue
                cost = c.get("mana_cost", "")
                cmc = RulesEngine._parse_cmc(cost)
                if cur_mana < cmc <= post_mana:
                    colored_pips = set(_re_plan.findall(r"\{([WUBRG])\}", cost))
                    existing_colors: set[str] = set()
                    for bf_card in bf:
                        if bf_card.get("owner_seat_id") == local_seat and not bf_card.get("is_tapped"):
                            bf_oracle = bf_card.get("oracle_text", "")
                            bf_name = bf_card.get("name", "")
                            for clr, bsc in [
                                ("W", "Plains"),
                                ("U", "Island"),
                                ("B", "Swamp"),
                                ("R", "Mountain"),
                                ("G", "Forest"),
                            ]:
                                if bsc in bf_name or f"{{{clr}}}" in bf_oracle:
                                    existing_colors.add(clr)
                    available_colors = land_colors | existing_colors
                    if not colored_pips or colored_pips.issubset(available_colors):
                        # Skip spells that need creature targets we don't have
                        c_oracle = (c.get("oracle_text", "") or "").lower()
                        needs_my_creature = (
                            "target creature you control" in c_oracle
                            or "creature you control fights" in c_oracle
                        )
                        if needs_my_creature and not my_creatures:
                            continue
                        new_casts.append(c.get("name", "?"))
            if new_casts:
                if len(new_casts) == 1:
                    post_land_parts.append(f"Play {land_name} \u2192 Cast {new_casts[0]}")
                else:
                    post_land_parts.append(
                        f"Play {land_name} \u2192 Cast (choose one: {' or '.join(new_casts)})"
                    )
        if post_land_parts:
            lines.append(f"THEN: {'; '.join(post_land_parts)}")
        return lines

    def _format_casting_time_options(
        self, game_state: dict[str, Any], decision_context: dict[str, Any]
    ) -> list[str]:
        """Format casting-time options with resolved modal option names.

        When bridge actions contain CastingTimeOption entries with choiceKind="modal",
        resolve each option's grpId to a card name so the LLM knows exactly what
        modal_index 0 vs 1 vs 2 means (e.g. "Search library" vs "Proliferate").
        """
        lines: list[str] = []

        # Try to extract modal options from bridge actions
        bridge_actions = game_state.get("_bridge_actions") or []
        modal_options: list[tuple[int, str]] = []  # (optionIndex, resolved_name)

        for ba in bridge_actions:
            if ba.get("actionType") != "CastingTimeOption":
                continue
            if ba.get("choiceKind") != "modal":
                continue
            opt_idx = ba.get("optionIndex", 0)
            grp_id = ba.get("grpId", 0)
            if grp_id:
                try:
                    from arenamcp import server

                    info = server.get_card_info(grp_id)
                    name = info.get("name", f"Option {opt_idx}")
                    oracle = info.get("oracle_text", "")
                    # For modal options, the grpId resolves to the mode's
                    # oracle text (e.g. "Search your library for a basic land...")
                    # Use the oracle text if short enough, otherwise just the name
                    if oracle and len(oracle) < 120:
                        label = oracle.split("\n")[0].strip()
                    else:
                        label = name
                except Exception:
                    label = f"Option {opt_idx}"
            else:
                label = ba.get("label", f"Option {opt_idx}")
            modal_options.append((opt_idx, label))

        if modal_options:
            modal_options.sort(key=lambda x: x[0])
            lines.append(f"!!! DECISION: CHOOSE MODE ({len(modal_options)} options) !!!")
            for opt_idx, label in modal_options:
                lines.append(f"  modal_index={opt_idx}: {label}")
            lines.append("Set modal_index to the number of the best option.")
        else:
            # Fallback: no bridge data, generic casting-time prompt
            lines.append("!!! DECISION: CHOOSE CASTING OPTION !!!")
            lines.append("Evaluate: alternative cost vs normal cost (Foretell, Flashback, Escape)")

        return lines

    def _format_decision_lines(self, game_state: dict[str, Any]) -> list[str]:
        """Format decision context into display lines for the LLM prompt."""
        lines: list[str] = []
        pending_decision = game_state.get("pending_decision")
        decision_context = game_state.get("decision_context")
        if not pending_decision:
            return lines

        if decision_context:
            dec_type = decision_context.get("type", "unknown")
            # Bridge request type can disambiguate generic decision types
            # (e.g., "group_selection" might be scry, surveil, or mulligan_bottom)
            bridge_req = game_state.get("_bridge_request_type")
            if dec_type == "unknown_req" and bridge_req:
                from arenamcp.gre_bridge import _BRIDGE_REQUEST_TO_DECISION_TYPE

                mapped = _BRIDGE_REQUEST_TO_DECISION_TYPE.get(bridge_req)
                if mapped:
                    dec_type = mapped
            _simple = {
                "mulligan_bottom": lambda ctx: [
                    f"!!! DECISION: MULLIGAN - PUT {max(1, 7 - len(game_state.get('hand', [])) + 1)} CARD(S) ON BOTTOM !!!",
                    "Keep: lands + on-curve plays | Bottom: expensive/off-color/redundant",
                ],
                "assign_damage": lambda ctx: [
                    "!!! DECISION: ASSIGN COMBAT DAMAGE !!!",
                    "Order: kill most important blocker/attacker first",
                ],
                "order_combat_damage": lambda ctx: [
                    "!!! DECISION: ORDER COMBAT DAMAGE !!!",
                    "Order: prioritize killing the biggest threat",
                ],
                "search": lambda ctx: [
                    "!!! DECISION: SEARCH LIBRARY !!!",
                    "Choose: what you need most \u2014 land, removal, threat, or answer",
                ],
                "choose_starting_player": lambda ctx: [
                    "!!! DECISION: PLAY OR DRAW !!!",
                    "Aggro decks: PLAY (tempo). Control/limited: DRAW (card advantage)",
                ],
                "explore": lambda ctx: [
                    "!!! DECISION: EXPLORE !!!",
                    "Keep land on top if needed, otherwise bottom for a better draw",
                ],
                "select_replacement": lambda ctx: [
                    "!!! DECISION: ORDER REPLACEMENT EFFECTS !!!",
                    "Choose: apply the replacement that gives most advantage first",
                ],
                "casting_time_options": None,  # Handled below with modal option resolution
                "select_counters": lambda ctx: [
                    "!!! DECISION: SELECT COUNTERS !!!",
                    "Choose: remove least valuable counters, keep most impactful",
                ],
                "order_triggers": lambda ctx: [
                    "!!! DECISION: ORDER TRIGGERED ABILITIES !!!",
                    "Order: resolve most impactful trigger last (it resolves first)",
                ],
                "select_n_group": lambda ctx: ["!!! DECISION: SELECT FROM GROUP !!!"],
                "select_from_groups": lambda ctx: ["!!! DECISION: SELECT FROM GROUPS !!!"],
                "search_from_groups": lambda ctx: ["!!! DECISION: SEARCH FROM GROUPS !!!"],
                "gather": lambda ctx: ["!!! DECISION: GATHER !!!"],
            }
            if dec_type in _simple and _simple[dec_type] is not None:
                lines.extend(_simple[dec_type](decision_context))
            elif dec_type == "casting_time_options":
                lines.extend(self._format_casting_time_options(game_state, decision_context))
            elif dec_type == "discard":
                lines.append(f"!!! DECISION: DISCARD {decision_context.get('count', 1)} card(s) !!!")
                lines.append("Choose: excess lands > high CMC uncastables > redundant copies")
            elif dec_type == "scry":
                lines.append(f"!!! DECISION: SCRY {decision_context.get('count', 1)} !!!")
                lines.append("Keep: needed lands/threats | Bottom: dead cards")
            elif dec_type == "surveil":
                lines.append(f"!!! DECISION: SURVEIL {decision_context.get('count', 1)} !!!")
                lines.append("Keep: want to draw | Graveyard: synergy or digging")
            elif dec_type == "target_selection":
                lines.append(f"!!! DECISION: TARGET for {decision_context.get('source_card', 'spell')} !!!")
                lines.append(
                    "FIRST decide whether this spell HELPS its target (ramp, "
                    "pump, protection, value auras) or HARMS it (damage, "
                    "destroy, exile, debuff, tax). Helpful effects target "
                    "YOUR permanents (YOURS); harmful ones target the "
                    "opponent's (OPP). Then pick the best candidate of that "
                    "side. Never put a beneficial aura on an opponent's "
                    "permanent."
                )
            elif dec_type == "modal_choice":
                lines.append(
                    f"!!! DECISION: CHOOSE MODE ({decision_context.get('num_options', '?')} options) !!!"
                )
                lines.append("Evaluate: which mode solves current problem best")
            elif dec_type == "declare_attackers":
                legal = self._filter_legal_attacker_names(
                    game_state, decision_context.get("legal_attackers", [])
                )
                lines.append(f"!!! DECISION: DECLARE ATTACKERS ({len(legal)} legal) !!!")
                if legal:
                    lines.append(f"Can attack: {', '.join(legal[:8])}")
                try:
                    local_seat = next(
                        (p.get("seat_id") for p in game_state.get("players", []) if p.get("is_local")),
                        None,
                    )
                    opp_cards = [
                        c for c in game_state.get("battlefield", []) if c.get("owner_seat_id") != local_seat
                    ]
                    lines.extend(self._attack_tax_lines(opp_cards, game_state))
                except Exception:
                    pass
                lines.append("Choose: maximize damage while keeping safe blockers back")
            elif dec_type == "declare_blockers":
                legal = decision_context.get("legal_blockers", [])
                lines.append(f"!!! DECISION: DECLARE BLOCKERS ({len(legal)} legal) !!!")
                if legal:
                    lines.append(f"Can block: {', '.join(legal[:8])}")
                lines.extend(self._format_block_decision_details(game_state, decision_context))
                lines.append("Choose: trade up, double-block threats, protect life total")
                lines.append(
                    'ANSWER FORMAT: name every assignment — "Block [attacker] with '
                    '[blocker]" for each blocker you use — or say "No blocks" with '
                    'the damage you accept. NEVER say "block with X" without naming '
                    "which attacker X blocks."
                )
            elif dec_type == "pay_costs":
                source = decision_context.get("source_card", "spell")
                mana_cost = decision_context.get("mana_cost", "")
                cost_str = f" ({mana_cost})" if mana_cost else ""
                lines.append(f"!!! DECISION: PAY COSTS for {source}{cost_str} !!!")
                if decision_context.get("has_autotap", False):
                    lines.append(
                        "Auto-tap available \u2014 confirm or tap manually for better mana efficiency"
                    )
                else:
                    lines.append("Choose: tap lands that leave best mana open for responses")
            elif dec_type == "distribution":
                lines.append(
                    f"!!! DECISION: DISTRIBUTE {decision_context.get('total', '?')} from {decision_context.get('source_card', 'effect')} !!!"
                )
                lines.append("Distribute: maximize kills, finish off wounded targets first")
            elif dec_type == "numeric_input":
                source = decision_context.get("source_card", "effect")
                lines.append(
                    f"!!! DECISION: CHOOSE NUMBER for {source} ({decision_context.get('min', 0)}-{decision_context.get('max', '?')}) !!!"
                )
                lines.append("Choose: balance value vs. cost (life, mana, etc.)")
            elif dec_type == "mill":
                lines.append(f"!!! DECISION: MILL {decision_context.get('count', 1)} !!!")
            elif dec_type in ("sacrifice", "exile", "destroy", "return"):
                count = decision_context.get("count", 1)
                opts = decision_context.get("option_cards")
                lines.append(f"!!! DECISION: {dec_type.upper()} {count} !!!")
                if opts:
                    lines.append(f"Options: {', '.join(opts[:8])}")
                _advice = {
                    "sacrifice": "Choose: sacrifice least valuable permanent for the board state",
                    "exile": "Choose: exile least impactful card",
                    "destroy": "Choose: destroy biggest threat on the board",
                    "return": "Choose: return least important permanent",
                }
                lines.append(_advice[dec_type])
            elif dec_type in (
                "choose_creature",
                "choose_land",
                "choose_enchantment",
                "choose_artifact",
                "choose_permanent",
                "choose",
            ):
                count = decision_context.get("count", 1)
                label = dec_type.replace("choose_", "").upper() or "ITEM"
                opts = decision_context.get("option_cards")
                lines.append(f"!!! DECISION: CHOOSE {label} ({count}) !!!")
                if opts:
                    lines.append(f"Options: {', '.join(opts[:8])}")
                lines.append("Choose: pick the option that best advances your game plan")
            elif dec_type == "actions_available":
                lines.append(
                    f"!!! YOUR PRIORITY \u2014 {decision_context.get('num_actions', '?')} legal actions available !!!"
                )
            else:
                lines.append(f"!!! DECISION: {pending_decision} !!!")
        else:
            lines.append(f"!!! DECISION: {pending_decision} !!!")

        if pending_decision == "Mulligan":
            lines.extend(self._format_mulligan_hand(game_state))
        return lines

    def _format_mulligan_hand(self, game_state: dict[str, Any]) -> list[str]:
        """Format mulligan hand summary lines."""
        import re as _re

        lines: list[str] = []
        my_hand = game_state.get("hand", [])
        if not my_hand:
            lines.append("Waiting for hand...")
            return lines
        lands = [c for c in my_hand if "land" in c.get("type_line", "").lower()]
        creatures = [c for c in my_hand if "creature" in c.get("type_line", "").lower()]
        spells = [c for c in my_hand if c not in lands and c not in creatures]
        cmcs = []
        for c in my_hand:
            cost = c.get("mana_cost", "")
            if cost:
                generic = sum(int(g) for g in _re.findall(r"\{(\d+)\}", cost))
                pips = len(_re.findall(r"\{[WUBRGC]\}", cost))
                cmcs.append(generic + pips)
            else:
                cmcs.append(0)
        avg_cmc = sum(cmcs) / len(cmcs) if cmcs else 0
        land_names = [c.get("name", "?") for c in lands]
        nonland_names = [
            f"{c.get('name', '?')} ({c.get('mana_cost', '')})" for c in my_hand if c not in lands
        ]
        lines.append(
            f"MULLIGAN HAND: {len(lands)} lands, {len(creatures)} creatures, {len(spells)} spells, avg CMC {avg_cmc:.1f}"
        )
        lines.append(f"  Lands: {', '.join(land_names) if land_names else 'NONE'}")
        lines.append(f"  Nonland: {', '.join(nonland_names) if nonland_names else 'NONE'}")
        lines.append("Decide: KEEP or MULLIGAN based on curve, colors, and land count")
        return lines

    def _format_mana_info(
        self, your_cards: list[dict], turn_num: int
    ) -> tuple[list[str], int, dict[str, int]]:
        """Compute mana pool info. Returns (lines, total_mana, mana_pool)."""
        import re

        lines: list[str] = []
        mana_pool = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "Any": 0}
        mana_sources: list[str] = []
        total_mana = 0
        creature_mana_source_count = 0

        for card in your_cards:
            type_line = card.get("type_line", "").lower()
            oracle = card.get("oracle_text", "")
            is_creature = "creature" in type_line
            is_land = "land" in type_line
            has_haste = "haste" in self._remove_reminder_text(oracle).lower()
            is_summoning_sick = (
                is_creature and card.get("turn_entered_battlefield") == turn_num and not has_haste
            )
            has_mana_ability = "add {" in oracle.lower() or "add one mana" in oracle.lower()
            if is_land and not has_mana_ability:
                for basic in ("plains", "island", "swamp", "mountain", "forest"):
                    if basic in type_line:
                        has_mana_ability = True
                        break
            if not card.get("is_tapped"):
                if is_land or (is_creature and has_mana_ability and not is_summoning_sick):
                    total_mana += 1
                    name = card.get("name", "")
                    if is_creature and has_mana_ability and not is_summoning_sick:
                        creature_mana_source_count += 1
                    source_colors: list[str] = []
                    if "Plains" in name or "plains" in type_line or "{W}" in oracle:
                        mana_pool["W"] += 1
                        source_colors.append("W")
                    if "Island" in name or "island" in type_line or "{U}" in oracle:
                        mana_pool["U"] += 1
                        source_colors.append("U")
                    if "Swamp" in name or "swamp" in type_line or "{B}" in oracle:
                        mana_pool["B"] += 1
                        source_colors.append("B")
                    if "Mountain" in name or "mountain" in type_line or "{R}" in oracle:
                        mana_pool["R"] += 1
                        source_colors.append("R")
                    if "Forest" in name or "forest" in type_line or "{G}" in oracle:
                        mana_pool["G"] += 1
                        source_colors.append("G")
                    if "{C}" in oracle:
                        mana_pool["C"] += 1
                        source_colors.append("C")
                    if "any color" in oracle.lower():
                        mana_pool["Any"] += 1
                        source_colors.append("Any")
                    if len(source_colors) > 1:
                        mana_sources.append("/".join(source_colors))
                    elif len(source_colors) == 1:
                        mana_sources.append(source_colors[0])

        mana_bonus_notes: list[str] = []
        for card in your_cards:
            oracle_lower = card.get("oracle_text", "").lower()
            name = card.get("name", "")
            bonus_match = re.search(
                r"whenever you tap a creature for mana,?\s*add an additional \{(\w)\}", oracle_lower
            )
            if bonus_match and creature_mana_source_count > 0:
                bonus_color = bonus_match.group(1).upper()
                bonus_total = creature_mana_source_count
                total_mana += bonus_total
                if bonus_color in mana_pool:
                    mana_pool[bonus_color] += bonus_total
                for _ in range(bonus_total):
                    mana_sources.append(f"+{bonus_color}")
                logger.info(
                    f"Mana bonus from {name}: +{bonus_total} {{{bonus_color}}} ({creature_mana_source_count} creature sources)"
                )
            if "untap" in oracle_lower and (
                "mana value" in oracle_lower or "converted mana cost" in oracle_lower
            ):
                untap_match = re.search(
                    r"(?:mana value|converted mana cost)\s*(\d+)\s*or greater.*untap|cast.*(?:mana value|converted mana cost)\s*(\d+).*untap|untap.*(?:mana value|converted mana cost)\s*(\d+)",
                    oracle_lower,
                )
                if untap_match:
                    threshold = untap_match.group(1) or untap_match.group(2) or untap_match.group(3)
                    mana_bonus_notes.append(
                        f"{name} untaps on MV{threshold}+ cast \u2192 tap again for extra mana"
                    )

        logger.info(f"Mana: {mana_pool} (Total: {total_mana})")
        if mana_sources:
            source_display = " ".join(f"{{{s}}}" if "/" in s else s for s in mana_sources)
            lines.append(f"Mana: {total_mana} (sources: {source_display})")
        else:
            lines.append("Mana: 0")
        for note in mana_bonus_notes:
            lines.append(f"\u26a0\ufe0f {note}")
        return lines, total_mana, mana_pool

    def _format_board_card(
        self,
        card: dict,
        local_seat: int,
        turn_num: int,
        attachments: dict[int, list[dict]],
        name_counts: Counter,
        name_seen: dict[str, int],
        is_local: bool,
        *,
        for_planner: bool = False,
    ) -> list[str]:
        """Format a single battlefield card into display lines.

        Args:
            for_planner: If True, omit full oracle text for permanents that
                have been on the battlefield for more than one turn — the
                ability flags (FLY, RCH, DTH, etc.) already summarize what
                the planner needs. Cards that just entered keep oracle text
                so ETB triggers stay visible.
        """
        lines: list[str] = []
        name = card.get("name", "Unknown")
        type_line = card.get("type_line", "").lower()
        is_creature = "creature" in type_line
        is_land = "land" in type_line

        if name_counts[name] > 1:
            name_seen[name] = name_seen.get(name, 0) + 1
            display_name = f"{name} #{name_seen[name]}"
        else:
            display_name = name

        pt = (
            f" {card.get('power') or 0}/{card.get('toughness') or 0}"
            if is_creature or card.get("power") is not None
            else ""
        )

        flags: list[str] = []
        if not is_creature and not is_land:
            if "equipment" in type_line:
                flags.append("EQUIPMENT")
            elif "artifact" in type_line:
                flags.append("ARTIFACT")
            if "enchantment" in type_line:
                flags.append("ENCHANT")
            if "planeswalker" in type_line:
                flags.append("PW")
        if card.get("is_tapped"):
            flags.append("T")

        oracle_text = self._remove_reminder_text(card.get("oracle_text", "")).lower()
        if "flying" in oracle_text:
            flags.append("FLY")
        if "reach" in oracle_text:
            flags.append("RCH")
        if is_local and "haste" in oracle_text:
            flags.append("HST")
        if "vigilance" in oracle_text:
            flags.append("VIG")
        if "trample" in oracle_text:
            flags.append("TRM")
        if "first strike" in oracle_text:
            flags.append("FS")
        if "deathtouch" in oracle_text:
            flags.append("DTH")
        if is_creature and card.get("turn_entered_battlefield") == turn_num and "haste" not in oracle_text:
            flags.append("SS")
        if self._is_impending(card):
            flags.append("IMPENDING")
        if card.get("is_attacking"):
            flags.append("ATK")
        if card.get("is_blocking"):
            flags.append("BLK")

        inst_id = card.get("instance_id")
        attached = attachments.get(inst_id, [])
        if any("doesn't untap" in (a.get("oracle_text") or "").lower() for a in attached):
            flags.append("LOCKED")

        obj_kind = card.get("object_kind", "")
        if obj_kind == "TOKEN":
            display_name = f"*{display_name}"
        counters = card.get("counters", {})
        counter_str = ""
        if counters:
            cparts = [f"{cc}{ct.replace('CounterType_', '')[:4]}" for ct, cc in counters.items()]
            counter_str = f" ({','.join(cparts)})"

        flag_str = f" [{','.join(flags)}]" if flags else ""
        lines.append(f"  {display_name}{pt}{counter_str}{flag_str}")

        raw_oracle = card.get("oracle_text", "")
        is_basic_land = is_land and not self._land_has_abilities(raw_oracle, type_line)

        if raw_oracle and not is_basic_land:
            stripped = self._remove_reminder_text(raw_oracle).strip()
            keyword_only = all(
                w
                in {
                    "flying",
                    "reach",
                    "haste",
                    "vigilance",
                    "trample",
                    "first",
                    "strike",
                    "double",
                    "deathtouch",
                    "lifelink",
                    "menace",
                    "ward",
                    "hexproof",
                    "indestructible",
                    "defender",
                }
                for w in stripped.lower().replace(",", " ").replace("\n", " ").split()
                if w
            )
            # Planner skips full oracle text on long-resident permanents — the
            # flags already summarize relevant abilities. Recent ETBs keep
            # oracle text so triggered abilities stay visible.
            entered_recently = (turn_num - (card.get("turn_entered_battlefield") or 0)) <= 1
            if for_planner and not entered_recently:
                pass
            elif not keyword_only and len(stripped) > 0:
                # Cap land oracle text to avoid token bloat — non-basic lands
                # with activated abilities (e.g. Evendo, Waking Haven) need
                # their ability conditions visible, but the full text can be
                # verbose after reminder-text removal.
                if is_land and len(stripped) > 300:
                    stripped = stripped[:300] + "..."
                lines.append(f"    {self._clean_oracle_for_prompt(stripped)}")

        if attached:
            for att in attached:
                att_name = att.get("name", "Unknown")
                att_oracle = self._remove_reminder_text(att.get("oracle_text", "")).strip()
                if is_local:
                    att_owner = "OPP" if att.get("owner_seat_id") != local_seat else "YOUR"
                else:
                    att_owner = "YOUR" if att.get("owner_seat_id") == local_seat else "OPP"
                lines.append(f"    >> {att_owner} AURA: {att_name}")
                if att_oracle:
                    lines.append(f"       {self._clean_oracle_for_prompt(att_oracle)}")
        return lines

    # Matches attack-tax permanents (Ghostly Prison, Propaganda, War Tax
    # activations aside): "...can't attack you unless their controller pays
    # {2} for each creature...". Arena oracle text encodes costs as {o2}.
    _ATTACK_TAX_RE = re.compile(
        r"can't attack(?: you| you or planeswalkers you control)? unless"
        r".{0,80}?pays?[^.{]*\{o?(\d+)\}",
        re.IGNORECASE | re.DOTALL,
    )

    def _detect_attack_taxes(self, opp_cards: list[dict]) -> list[tuple[str, int]]:
        """Find opponent permanents taxing each attacker, with cost each."""
        taxes: list[tuple[str, int]] = []
        for card in opp_cards:
            oracle = card.get("oracle_text", "") or ""
            m = self._ATTACK_TAX_RE.search(oracle)
            if m:
                try:
                    taxes.append((card.get("name", "Unknown"), int(m.group(1))))
                except ValueError:
                    continue
        return taxes

    def _attack_tax_lines(self, opp_cards: list[dict], game_state: dict[str, Any] | None) -> list[str]:
        """Warning lines when attacking costs extra mana per creature.

        Field report 2026-07-16: opponent had Ghostly Prison and the coach
        advised casting spells until empty, then attacking with six
        creatures — a {12} tax it never counted. This is calculator work,
        not LLM judgment: state the price and the affordable attacker count.
        """
        taxes = self._detect_attack_taxes(opp_cards)
        if not taxes:
            return []
        per_attacker = sum(cost for _, cost in taxes)
        names = ", ".join(f"{n} (+{{{c}}} per attacker)" for n, c in taxes)
        lines = [f"!! ATTACK TAX: {names} — attacking costs {{{per_attacker}}} PER CREATURE."]
        if game_state is not None:
            try:
                mana = self._available_mana_now(game_state)
                afford = mana // per_attacker if per_attacker else 0
                lines.append(
                    f"   With {mana} mana available you can pay for at most "
                    f"{afford} attacker(s). BUDGET MANA BEFORE CASTING SPELLS "
                    "if you plan to attack this turn."
                )
            except Exception:
                lines.append(
                    "   Reserve mana for the tax before casting spells if you plan to attack this turn."
                )
        return lines

    def _format_attack_combat(
        self,
        your_cards: list[dict],
        opp_cards: list[dict],
        local_player: dict | None,
        opponent_player: dict | None,
        turn_num: int,
        valid_attackers: list[dict],
        game_state: dict[str, Any] | None = None,
    ) -> list[str]:
        """Format the attack-side combat analysis (your turn attacking)."""
        lines: list[str] = []
        lines.extend(self._attack_tax_lines(opp_cards, game_state))
        your_creatures = [
            c
            for c in your_cards
            if "creature" in c.get("type_line", "").lower() and not self._is_impending(c)
        ]
        opp_creatures = [
            c for c in opp_cards if "creature" in c.get("type_line", "").lower() and not self._is_impending(c)
        ]
        opp_blockers = [c for c in opp_creatures if not c.get("is_tapped")]
        opp_block_count = len(opp_blockers)
        opp_life = opponent_player.get("life_total", 20) if opponent_player else 20
        your_attack_power = sum(c.get("power") or 0 for c in valid_attackers)

        if valid_attackers:
            lethal = (
                "LETHAL"
                if (opp_block_count == 0 and your_attack_power >= opp_life)
                else f"{opp_block_count}blk"
            )
            attacker_names = [c.get("name", "?") for c in valid_attackers]
            atk_name_counts = Counter(attacker_names)
            atk_name_seen: dict[str, int] = {}
            deduped_names = []
            for n in attacker_names:
                if atk_name_counts[n] > 1:
                    atk_name_seen[n] = atk_name_seen.get(n, 0) + 1
                    deduped_names.append(f"{n} #{atk_name_seen[n]}")
                else:
                    deduped_names.append(n)
            lines.append(
                f"Atk: {len(valid_attackers)}cr/{your_attack_power}pwr vs {lethal} \u2014 can attack: {', '.join(deduped_names)}"
            )
            if valid_attackers and opp_blockers:
                for atk in valid_attackers:
                    for blk in opp_blockers:
                        trade = self._compute_combat_trade(atk, blk)
                        if trade is None:
                            continue
                        result, atk_dies, blk_dies = trade
                        atk_name = atk.get("name", "?")
                        atk_pow = atk.get("power") or 0
                        atk_tgh = atk.get("toughness") or 0
                        blk_name = blk.get("name", "?")
                        blk_pow = blk.get("power") or 0
                        blk_tgh = blk.get("toughness") or 0
                        if atk_dies and blk_dies:
                            display_result = result
                        elif atk_dies:
                            display_result = f"BAD \u2014 {result}"
                        elif blk_dies:
                            display_result = f"GOOD \u2014 {result}"
                        else:
                            display_result = result
                        lines.append(
                            f"  If {blk_name} {blk_pow}/{blk_tgh} blocks {atk_name} {atk_pow}/{atk_tgh}: {display_result}"
                        )
        else:
            lines.append("Atk: None (T/SS)")

        opp_attack_power = sum(c.get("power") or 0 for c in opp_creatures)
        your_life = local_player.get("life_total", 20) if local_player else 20
        if opp_attack_power > 0:
            non_attackers = [c for c in your_creatures if c not in valid_attackers]
            allout_dmg = self._compute_optimal_blocking_damage(opp_creatures, non_attackers)
            life_after_allout = your_life - allout_dmg
            noatk_dmg = self._compute_optimal_blocking_damage(opp_creatures, your_creatures)
            life_after_noatk = your_life - noatk_dmg
            life_margin = your_life - opp_attack_power
            if life_after_allout <= 0:
                if life_after_noatk > 0:
                    lines.append(
                        f"\u26a0\ufe0f Crackback: opp {opp_attack_power}pwr \u2014 ALL-OUT lethal ({allout_dmg} through vs {your_life} life), but holding all {len(your_creatures)} blockers \u2192 only {noatk_dmg} through \u2192 SAFE at {life_after_noatk} life. Attack selectively!"
                    )
                else:
                    lines.append(
                        f"\u26a0\ufe0f Crackback: opp {opp_attack_power}pwr \u2192 LETHAL even with all {len(your_creatures)} blockers ({noatk_dmg} through vs {your_life} life)! Must race or remove threats!"
                    )
            elif life_margin <= 0:
                if allout_dmg < opp_attack_power and len(non_attackers) > 0:
                    lines.append(
                        f"Crackback: opp {opp_attack_power}pwr, but your {len(non_attackers)} blocker(s) absorb {opp_attack_power - allout_dmg} \u2192 only {allout_dmg} through vs {your_life} life \u2014 {'safe' if life_after_allout > 3 else 'tight'}"
                    )
                else:
                    lines.append(
                        f"Crackback: {opp_attack_power}pwr vs your {your_life} life \u2014 LETHAL if no blockers held!"
                    )
            elif life_margin <= 3:
                lines.append(
                    f"Crackback: {opp_attack_power}pwr vs your {your_life} life \u2014 DANGER (only {life_margin} margin!)"
                )
            else:
                lines.append(f"Crackback: {opp_attack_power}pwr vs your {your_life} life \u2014 safe")

        # Deterministic attack solver — picks the attacker subset that
        # maximizes expected damage through while surviving worst-case
        # crackback. Surface its pick so the LLM can follow it.
        try:
            from arenamcp.combat_solver import optimal_attacks

            opp_next_turn_attackers = [c for c in opp_creatures]
            your_remaining_blockers = [
                c for c in your_creatures if c not in valid_attackers and not c.get("is_tapped")
            ]
            solver_plan = optimal_attacks(
                valid_attackers,
                opp_blockers,
                opp_life,
                your_life,
                opp_next_turn_attackers,
                your_remaining_blockers,
            )
            if solver_plan is not None:
                lines.append(f"Computed optimal attack: {solver_plan.explanation}")
        except Exception as e:
            logger.debug(f"combat solver (attack) failed: {e}")

        return lines

    def _format_block_combat(
        self,
        your_cards: list[dict],
        opp_cards: list[dict],
        local_player: dict | None,
        turn_num: int,
        phase: str,
        _inferred_atk_ids: set[int],
        decision_context: dict[str, Any] | None = None,
    ) -> list[str]:
        """Format the block-side combat analysis (opponent's turn)."""
        lines: list[str] = []
        ctx = decision_context or {}
        attacking = [c for c in opp_cards if c.get("is_attacking")]
        if not attacking and _inferred_atk_ids:
            attacking = [c for c in opp_cards if c.get("instance_id") in _inferred_atk_ids]
        if not attacking:
            # GRE-authoritative attacker ids from the DeclareBlockersReq
            # decision context (log path parity, issue #420).
            ctx_atk_ids = self._attacker_ids_from_decision_context(ctx)
            if ctx_atk_ids:
                attacking = [c for c in opp_cards if int(c.get("instance_id") or 0) in ctx_atk_ids]
        flying_atk = [
            c for c in attacking if "flying" in self._remove_reminder_text(c.get("oracle_text", "")).lower()
        ]
        ground_atk = [c for c in attacking if c not in flying_atk]
        your_creatures = [
            c
            for c in your_cards
            if "creature" in c.get("type_line", "").lower()
            and not c.get("is_tapped")
            and not self._is_impending(c)
        ]
        flyer_blockers = [
            c
            for c in your_creatures
            if any(
                kw in self._remove_reminder_text(c.get("oracle_text", "")).lower()
                for kw in ["flying", "reach"]
            )
        ]

        if not attacking:
            return lines

        fly_dmg = sum(c.get("power") or 0 for c in flying_atk)
        gnd_dmg = sum(c.get("power") or 0 for c in ground_atk)
        total_incoming = fly_dmg + gnd_dmg
        your_life = local_player.get("life_total", 20) if local_player else 20

        # Explicit attacker list so the LLM knows exactly which creatures
        # are attacking (avoids confusion with same-named non-attackers).
        atk_names_raw = [c.get("name", "?") for c in attacking]
        _atk_counts = Counter(atk_names_raw)
        _atk_seen: dict[str, int] = {}
        atk_labels = []
        for c, n in zip(attacking, atk_names_raw, strict=False):
            p = c.get("power") or 0
            t = c.get("toughness") or 0
            if _atk_counts[n] > 1:
                _atk_seen[n] = _atk_seen.get(n, 0) + 1
                atk_labels.append(f"{n} #{_atk_seen[n]} {p}/{t}")
            else:
                atk_labels.append(f"{n} {p}/{t}")
        lines.append(f"Blk: {fly_dmg}fly/{gnd_dmg}gnd dmg | {len(flyer_blockers)}FLY-blk avail")
        lines.append(f"Attackers: {', '.join(atk_labels)}")
        life_after_no_blocks = your_life - total_incoming
        if life_after_no_blocks <= 0:
            lines.append(
                f"\u26a0\ufe0f No blocks \u2192 {total_incoming} dmg \u2192 DEAD (from {your_life} life)! Must block!"
            )
        else:
            lines.append(
                f"No blocks \u2192 take {total_incoming} dmg \u2192 {life_after_no_blocks} life remaining"
            )
        if flying_atk and not flyer_blockers:
            lines.append(f"\u26a0\ufe0f {fly_dmg} UNBLOCKABLE!")
        dth_atk = [
            c
            for c in attacking
            if "deathtouch" in self._remove_reminder_text(c.get("oracle_text", "")).lower()
        ]
        if dth_atk:
            lines.append(
                f"\u26a0\ufe0f DEATHTOUCH: {', '.join(c.get('name', '?') for c in dth_atk)} \u2014 any blocker DIES regardless of toughness!"
            )

        damage_through = self._compute_optimal_blocking_damage(attacking, your_creatures)
        life_after_blocks = your_life - damage_through
        if damage_through < total_incoming:
            if life_after_blocks <= 0:
                lines.append(
                    f"\u26a0\ufe0f Best blocks \u2192 take {damage_through} dmg \u2192 DEAD (from {your_life} life)! Not enough blockers!"
                )
            else:
                lines.append(f"Best blocks \u2192 take {damage_through} dmg \u2192 {life_after_blocks} life")
        else:
            life_after_blocks = life_after_no_blocks

        if your_creatures and attacking:
            for atk in attacking:
                for blk in your_creatures:
                    trade = self._compute_combat_trade(atk, blk)
                    if trade is None:
                        continue
                    result, _atk_dies, _blk_dies = trade
                    atk_name = atk.get("name", "?")
                    atk_pow = atk.get("power") or 0
                    atk_tgh = atk.get("toughness") or 0
                    blk_name = blk.get("name", "?")
                    blk_pow = blk.get("power") or 0
                    blk_tgh = blk.get("toughness") or 0
                    lines.append(
                        f"  If {blk_name} {blk_pow}/{blk_tgh} blocks {atk_name} {atk_pow}/{atk_tgh}: {result}"
                    )

        opp_non_attacking = [
            c
            for c in opp_cards
            if "creature" in c.get("type_line", "").lower()
            and c not in attacking
            and not self._is_impending(c)
        ]
        opp_next_turn_power = sum(c.get("power") or 0 for c in attacking) + sum(
            c.get("power") or 0 for c in opp_non_attacking
        )
        if opp_next_turn_power > 0 and life_after_blocks > 0:
            if opp_next_turn_power >= life_after_blocks:
                lines.append(
                    f"\u26a0\ufe0f Next turn: opp can attack for up to {opp_next_turn_power}pwr \u2014 LETHAL if you're at {life_after_blocks} life after this combat! Preserve blockers!"
                )

        # Deterministic block solver — grounds the LLM in the actual
        # material/life outcome of every legal block assignment rather
        # than letting it guess. The LLM should follow this unless it
        # has a specific reason (e.g. a combat trick in hand).
        try:
            from arenamcp.combat_solver import (
                blocker_allowed_attackers_map,
                optimal_blocks,
            )

            usable_blockers = [c for c in your_creatures if not c.get("is_tapped")]
            # Restrict to the GRE's legal blockers when the decision context
            # names them (creatures that can't block are excluded upstream).
            legal_blocker_ids: set[int] = set()
            if str(ctx.get("type") or "") == "declare_blockers":
                for bid in ctx.get("legal_blocker_ids") or []:
                    try:
                        legal_blocker_ids.add(int(bid))
                    except (TypeError, ValueError):
                        continue
            if legal_blocker_ids:
                gre_blockers = [
                    c for c in usable_blockers if int(c.get("instance_id") or 0) in legal_blocker_ids
                ]
                if gre_blockers:
                    usable_blockers = gre_blockers
            allowed_map = blocker_allowed_attackers_map(ctx.get("raw_blockers") or [])
            solver_plan = optimal_blocks(
                attacking,
                usable_blockers,
                your_life,
                blocker_allowed_attackers=allowed_map or None,
            )
            if solver_plan is not None:
                lines.append(f"Computed optimal blocks: {solver_plan.explanation}")
        except Exception as e:
            logger.debug(f"combat solver (blocks) failed: {e}")

        return lines

    def _check_castability(
        self,
        type_line: str,
        cost: str,
        cmc: int,
        reqs: dict[str, int],
        total_mana: int,
        mana_pool: dict[str, int],
        can_play_land: bool,
    ) -> str:
        """Determine castability status string for a hand card."""
        if "land" in type_line:
            return "LAND" if can_play_land else "HOLD"
        elif total_mana >= cmc:
            color_ok = all(
                mana_pool.get(c, 0) + mana_pool.get("Any", 0) >= reqs[c] for c in "WUBRGC" if reqs[c] > 0
            )
            if color_ok:
                return "OK"
            missing_pips = "".join(
                f"{{{c}}}" * max(0, reqs[c] - mana_pool.get(c, 0) - mana_pool.get("Any", 0))
                for c in "WUBRGC"
                if reqs[c] > 0
            )
            return f"NEED:{missing_pips}" if missing_pips else f"NEED:{max(1, cmc - total_mana)}"
        else:
            missing_pips = "".join(
                f"{{{c}}}" * max(0, reqs[c] - mana_pool.get(c, 0) - mana_pool.get("Any", 0))
                for c in "WUBRGC"
                if reqs[c] > 0
            )
            generic_short = cmc - total_mana
            return f"NEED:{generic_short}+{missing_pips}" if missing_pips else f"NEED:{generic_short}"

    def _analyze_removal(
        self,
        oracle_lower: str,
        opp_creatures: list[dict],
        opp_nonland: list[dict],
        all_creatures: list[dict],
        battlefield: list[dict],
        card_name: str,
        no_target_card_names: set[str],
    ) -> str:
        """Analyze removal capabilities of a card. Mutates no_target_card_names."""
        import re

        removal_info = ""
        damage_match = re.search(r"deals?\s+(\d+)\s+damage", oracle_lower)
        minus_match = re.search(r"gets?\s+(-\d+)/(-\d+)", oracle_lower)
        is_destroy_creature = "destroy target creature" in oracle_lower
        is_exile_creature = "exile target creature" in oracle_lower
        # "destroy target creature or enchantment" / "or planeswalker" — broader than just creature
        is_destroy_creature_or = is_destroy_creature and (
            "or enchantment" in oracle_lower or "or planeswalker" in oracle_lower
        )
        is_exile_creature_or = is_exile_creature and (
            "or enchantment" in oracle_lower or "or planeswalker" in oracle_lower
        )
        is_destroy_permanent = (
            "destroy target permanent" in oracle_lower or "destroy target nonland permanent" in oracle_lower
        )
        is_destroy_art_ench = (
            "destroy target artifact" in oracle_lower
            or "destroy target enchantment" in oracle_lower
            or "naturalize" in oracle_lower
        )
        is_exile_permanent = (
            "exile target permanent" in oracle_lower
            or "exile target nonland permanent" in oracle_lower
            or "exile target artifact" in oracle_lower
            or "exile target enchantment" in oracle_lower
        )
        is_bounce_creature = "return target creature" in oracle_lower or (
            "put target creature" in oracle_lower and "top" in oracle_lower
        )
        is_bounce_permanent = (
            "return target nonland permanent" in oracle_lower or "return target permanent" in oracle_lower
        )

        if not (
            damage_match
            or minus_match
            or is_destroy_creature
            or is_exile_creature
            or is_destroy_permanent
            or is_destroy_art_ench
            or is_exile_permanent
            or is_bounce_creature
            or is_bounce_permanent
        ):
            return removal_info

        if is_bounce_creature or is_bounce_permanent:
            removal_info = " [RM:bounce]"
        elif is_destroy_permanent or is_exile_permanent:
            removal_info = " [RM:perm]"
        elif is_destroy_creature_or or is_exile_creature_or:
            removal_info = " [RM:creat/ench]"
        elif is_destroy_art_ench:
            removal_info = " [RM:art/ench]"
        elif is_destroy_creature or is_exile_creature:
            removal_info = " [RM:creat]"
        elif damage_match:
            removal_info = f" [RM:<={int(damage_match.group(1))}T]"
        elif minus_match:
            removal_info = f" [RM:<={abs(int(minus_match.group(2)))}T]"

        if is_bounce_creature:
            target_pool = all_creatures
        elif is_bounce_permanent:
            target_pool = [c for c in battlefield if "land" not in c.get("type_line", "").lower()]
        elif is_destroy_creature_or or is_exile_creature_or:
            # "destroy target creature or enchantment" — check opponent creatures + enchantments
            target_pool = opp_creatures + [
                c
                for c in opp_nonland
                if "enchantment" in c.get("type_line", "").lower()
                or "planeswalker" in c.get("type_line", "").lower()
            ]
        elif is_destroy_creature or is_exile_creature:
            target_pool = opp_creatures
        elif "nonland" in oracle_lower or is_destroy_permanent or is_exile_permanent:
            target_pool = opp_nonland
        elif is_destroy_art_ench:
            target_pool = [
                c
                for c in opp_nonland
                if any(t in c.get("type_line", "").lower() for t in ["artifact", "enchantment"])
            ]
        else:
            target_pool = opp_creatures

        mv_match = re.search(r"mana value (\d+) or less", oracle_lower)
        if mv_match and target_pool:
            mv_limit = int(mv_match.group(1))
            target_pool = [c for c in target_pool if self._get_cmc(c.get("mana_cost", "")) <= mv_limit]

        if not target_pool:
            removal_info += " [NO TARGETS]"
            no_target_card_names.add(card_name)
        return removal_info

    def _format_hand_cards(
        self,
        game_state: dict[str, Any],
        local_seat: int,
        total_mana: int,
        mana_pool: dict[str, int],
        opp_cards: list[dict],
        battlefield: list[dict],
        is_my_turn: bool,
        phase: str,
        turn_num: int,
        valid_moves: list[str],
    ) -> tuple[list[str], set[str], set[str]]:
        """Format the hand section. Returns (lines, no_target_card_names, uncastable_card_names)."""
        import re

        lines: list[str] = []
        no_target_card_names: set[str] = set()
        uncastable_card_names: set[str] = set()
        hand = game_state.get("hand", [])
        lines.append("")
        lines.append("HAND:")

        opp_creatures = [
            c for c in opp_cards if "creature" in c.get("type_line", "").lower() and not self._is_impending(c)
        ]
        opp_nonland = [c for c in opp_cards if "land" not in c.get("type_line", "").lower()]
        all_creatures = [
            c
            for c in battlefield
            if c.get("power") is not None and "land" not in c.get("type_line", "").lower()
        ]

        if not hand:
            lines.append("  (empty)")
            return lines, no_target_card_names, uncastable_card_names

        local_player = next((p for p in game_state.get("players", []) if p.get("is_local")), None)
        lands_played = local_player.get("lands_played", 0) if local_player else 0
        stack = game_state.get("stack", [])
        can_play_land = (lands_played == 0) and is_my_turn and "Main" in phase and len(stack) == 0
        hand_name_counts = Counter(c.get("name", "Unknown") for c in hand)
        hand_name_seen: dict[str, int] = {}

        for card in hand:
            name = card.get("name", "Unknown")
            cost = card.get("mana_cost", "")
            type_line = card.get("type_line", "").lower()
            oracle_text = card.get("oracle_text", "")
            oracle_lower = oracle_text.lower()
            is_instant = "instant" in type_line or "flash" in oracle_lower
            timing = "I" if is_instant else "S"

            cmc = 0
            reqs = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
            if cost:
                generic = re.findall(r"\{(\d+)\}", cost)
                cmc += sum(int(g) for g in generic)
                for color in "WUBRGC":
                    count = len(re.findall(rf"\{{{color}\}}", cost))
                    reqs[color] += count
                    cmc += count
                hybrid = re.findall(r"\{[^}]+/[^}]+\}", cost)
                cmc += len(hybrid)

            castable = self._check_castability(
                type_line, cost, cmc, reqs, total_mana, mana_pool, can_play_land
            )

            # Track cards the player can't afford so they're filtered from Legal
            if castable.startswith("NEED"):
                uncastable_card_names.add(name)

            # Flag X-cost spells where X would be 0 — usually worthless
            has_x = "{X}" in cost or "{x}" in cost
            if has_x and "land" not in type_line:
                non_x_cost = cmc  # cmc already excludes X (parsed from {digit} and {color})
                x_value = max(0, total_mana - non_x_cost)
                if castable == "OK" and x_value == 0:
                    castable = "OK,X=0"

            removal_info = self._analyze_removal(
                oracle_lower,
                opp_creatures,
                opp_nonland,
                all_creatures,
                battlefield,
                name,
                no_target_card_names,
            )

            # Detect spells that require creatures we don't have
            # Sagas are exempt: Chapter I typically creates tokens or has
            # non-targeted effects, so casting is still valuable even when
            # later chapters need "target creature you control".
            is_saga = "saga" in type_line
            if "land" not in type_line and "creature" not in type_line and not is_saga:
                my_creatures = [
                    c
                    for c in battlefield
                    if c.get("owner_seat_id") == local_seat
                    and c.get("power") is not None
                    and "land" not in c.get("type_line", "").lower()
                ]
                needs_my_creature = (
                    "target creature you control" in oracle_lower
                    or "creature you control fights" in oracle_lower
                )
                if needs_my_creature and not my_creatures:
                    removal_info += " [NO TARGETS]"
                    no_target_card_names.add(name)

            # Auras must enchant a creature. A buff/protective Aura is only
            # worth casting on a creature YOU control; if you control none, the
            # only legal target is an opponent's creature and casting it just
            # hands the enemy a free buff. A debuff/removal Aura (Pacifism-style)
            # is the opposite — it needs an OPPONENT creature. Detect both so we
            # don't recommend an Aura that can only hit the wrong side.
            is_aura = "aura" in type_line and "enchantment" in type_line
            if is_aura and "enchant creature" in oracle_lower and "[NO TARGETS]" not in removal_info:

                def _is_creature(c: dict) -> bool:
                    tl = c.get("type_line", "").lower()
                    return (
                        "creature" in tl or "CardType_Creature" in c.get("card_types", [])
                    ) and "land" not in tl

                my_creatures = [
                    c for c in battlefield if c.get("controller_seat_id") == local_seat and _is_creature(c)
                ]
                enemy_creatures = [
                    c
                    for c in battlefield
                    if c.get("controller_seat_id") not in (None, local_seat) and _is_creature(c)
                ]
                debuff_markers = (
                    "loses all abilities",
                    "can't attack",
                    "can't block",
                    "doesn't untap",
                    "base power and toughness",
                    "is a coward",
                    "can't be blocked by",
                    "as long as enchanted",
                )
                is_debuff_aura = bool(re.search(r"gets? -\d+/-?\d+", oracle_lower)) or any(
                    m in oracle_lower for m in debuff_markers
                )
                relevant = enemy_creatures if is_debuff_aura else my_creatures
                if not relevant:
                    removal_info += " [NO TARGETS]"
                    no_target_card_names.add(name)

            is_basic_land = "land" in type_line and (
                "basic" in type_line or name in ["Plains", "Island", "Swamp", "Mountain", "Forest"]
            )
            oracle_stripped = self._remove_reminder_text(oracle_text) if oracle_text else ""
            show_oracle = bool(oracle_text) and not is_basic_land

            type_tag = ""
            if "creature" not in type_line and "land" not in type_line:
                if "enchantment" in type_line and "aura" in type_line:
                    type_tag = " (AURA)"
                elif "enchantment" in type_line:
                    type_tag = " (ENCHANT)"
                elif "equipment" in type_line:
                    type_tag = " (EQUIP)"
                elif "artifact" in type_line:
                    type_tag = " (ART)"
                elif "planeswalker" in type_line:
                    type_tag = " (PW)"

            if hand_name_counts[name] > 1:
                hand_name_seen[name] = hand_name_seen.get(name, 0) + 1
                display_name = f"{name} #{hand_name_seen[name]}"
            else:
                display_name = name

            lines.append(f"  {display_name}{type_tag} {cost} [{timing},{castable}]{removal_info}")
            if show_oracle:
                lines.append(f"    {self._clean_oracle_for_prompt(oracle_stripped)}")
        return lines, no_target_card_names, uncastable_card_names

    @staticmethod
    def _stack_target_name_map(game_state: dict[str, Any]) -> dict[int, str]:
        """instance_id -> display name across every zone we can see.

        Used to turn a stack object's ``targeting`` instance ids into card
        names. Deliberately conservative: ids we cannot resolve render as
        ``#<id>`` rather than being guessed at as players, because a
        mislabelled target ("targets you" when it targets a creature) is
        strictly worse for the model than an opaque one.
        """
        names: dict[int, str] = {}
        for zone in ("battlefield", "stack", "graveyard", "hand", "exile", "command"):
            for card in game_state.get(zone) or []:
                if not isinstance(card, dict):
                    continue
                try:
                    iid = int(card.get("instance_id"))
                except (TypeError, ValueError):
                    continue
                name = card.get("modified_name") or card.get("name")
                if name:
                    names[iid] = str(name)
        return names

    def _format_stack_section(self, game_state: dict[str, Any], local_seat: int) -> list[str]:
        """Render the ordered stack with controller, targets and chosen modes.

        The GRE stack zone lists objects in the order they were put on the
        stack, so the LAST entry is the top and resolves FIRST. The previous
        one-line ``Stack: A > B`` rendering both dropped targets entirely and
        implied the opposite resolution order, which made it impossible to
        reason about responses, counterspells or timing.

        Emits nothing when the stack is empty (the common case), so the token
        cost is zero for the great majority of decisions.
        """
        stack = game_state.get("stack") or []
        if not stack:
            return []

        target_names = self._stack_target_name_map(game_state)
        lines: list[str] = ["STACK (top resolves first):"]
        # Top of stack = last element in GRE zone order.
        for position, obj in enumerate(reversed(stack), start=1):
            if not isinstance(obj, dict):
                continue
            controller = obj.get("controller_seat_id")
            if controller is None:
                controller = obj.get("owner_seat_id")
            side = "YOU" if controller == local_seat else "OPP"
            name = obj.get("modified_name") or obj.get("name") or "Unknown"

            detail = ""
            targets = obj.get("targeting") or []
            if targets:
                rendered = []
                for tid in targets:
                    try:
                        tid_int = int(tid)
                    except (TypeError, ValueError):
                        continue
                    rendered.append(target_names.get(tid_int, f"#{tid_int}"))
                if rendered:
                    detail += f" -> targets: {', '.join(rendered)}"

            # Chosen modes are not currently captured by the state model; render
            # them if a future GRE/bridge path ever supplies them.
            modes = obj.get("chosen_modes") or obj.get("modes") or []
            if isinstance(modes, str):
                modes = [modes]
            mode_strs = [str(m) for m in modes if m]
            if mode_strs:
                detail += f" [mode: {'; '.join(mode_strs)}]"

            suffix = "  <- resolves next" if position == 1 else ""
            lines.append(f"  {position}. {side} {name}{detail}{suffix}")
        return lines

    def _format_zones_and_events(
        self, game_state: dict[str, Any], local_seat: int, opp_seat: int | None
    ) -> list[str]:
        """Format recent events, revealed cards, stack, graveyard, command zone, and library."""
        lines: list[str] = []
        recent_events = game_state.get("recent_events", [])
        if recent_events:
            window = recent_events[-15:]
            # A spell that finished resolving emits BOTH resolution_start and
            # resolution_complete. Rendering both wastes tokens on "X
            # resolving; X resolved" — keep "resolving" only while the object
            # is still mid-resolution (no matching complete in the window).
            _completed_ids = {
                evt.get("instance_id") for evt in window if evt.get("type") == "resolution_complete"
            }
            event_strs = []
            for evt in window:
                etype = evt.get("type", "")
                if etype == "damage_dealt":
                    event_strs.append(
                        f"{evt.get('source', '?')} dealt {evt.get('amount', 0)} to {evt.get('target', '?')}"
                    )
                elif etype == "zone_transfer":
                    event_strs.append(f"{evt.get('card', '?')} moved zones")
                elif etype == "counter_added":
                    event_strs.append(f"+{evt.get('amount', 1)} counter on {evt.get('card', '?')}")
                elif etype == "counter_removed":
                    event_strs.append(f"-{evt.get('amount', 1)} counter from {evt.get('card', '?')}")
                elif etype == "token_created":
                    event_strs.append(f"Token: {evt.get('card', '?')}")
                elif etype == "card_revealed":
                    event_strs.append(f"Revealed: {evt.get('card', '?')}")
                elif etype == "controller_changed":
                    event_strs.append(f"{evt.get('card', '?')} changed controller")
                # Action history: what actually went on the stack and what came
                # off it. These were emitted by gamestate but silently dropped
                # here, so the prompt had no record of casts/resolutions at all.
                elif etype in ("resolution_start", "resolution_complete"):
                    card = str(evt.get("card") or "?")
                    # An unresolved grp_id renders as "Card#12345" / "Unknown",
                    # which is pure token cost with no strategic signal.
                    if card.startswith("Card#") or card.startswith("Unknown"):
                        continue
                    if etype == "resolution_start":
                        if evt.get("instance_id") in _completed_ids:
                            continue
                        event_strs.append(f"{card} resolving")
                    else:
                        event_strs.append(f"{card} resolved")
            if event_strs:
                lines.append(f"Recent: {'; '.join(event_strs)}")

        revealed = game_state.get("revealed_cards", {})
        if revealed and opp_seat is not None:
            opp_revealed = revealed.get(str(opp_seat), revealed.get(opp_seat, []))
            if opp_revealed:
                lines.append(f"Opp revealed {len(opp_revealed)} card(s) this game")

        lines.extend(self._format_stack_section(game_state, local_seat))

        graveyard = game_state.get("graveyard", [])
        if graveyard:
            your_gy = [c for c in graveyard if c.get("owner_seat_id") == local_seat]
            opp_gy = [c for c in graveyard if c.get("owner_seat_id") != local_seat]
            if your_gy or opp_gy:
                gy_parts = []
                if your_gy:
                    gy_parts.append(
                        f"Y={len(your_gy)} ({', '.join(c.get('name', '?') for c in your_gy[:8])})"
                    )
                if opp_gy:
                    gy_parts.append(f"O={len(opp_gy)} ({', '.join(c.get('name', '?') for c in opp_gy[:8])})")
                lines.append(f"GY: {' '.join(gy_parts)}")

        command = game_state.get("command", [])
        if command:
            your_cmds = [c for c in command if c.get("owner_seat_id") == local_seat]
            opp_cmds = [c for c in command if c.get("owner_seat_id") != local_seat]
            cmd_parts = []
            for c in your_cmds:
                cost_str = f" {c.get('mana_cost', '')}" if c.get("mana_cost") else ""
                cmd_parts.append(f"  YOUR CMD: {c.get('name', 'Unknown')}{cost_str}")
                oracle = self._clean_oracle_for_prompt(c.get("oracle_text", "") or "").replace("\n", " ").strip()
                if oracle:
                    cmd_parts.append(f"    {oracle}")
            for c in opp_cmds:
                cost_str = f" {c.get('mana_cost', '')}" if c.get("mana_cost") else ""
                cmd_parts.append(f"  OPP CMD: {c.get('name', 'Unknown')}{cost_str}")
                oracle = self._clean_oracle_for_prompt(c.get("oracle_text", "") or "").replace("\n", " ").strip()
                if oracle:
                    cmd_parts.append(f"    {oracle}")
            lines.append("COMMAND ZONE:")
            lines.extend(cmd_parts)

        library_summary = game_state.get("library_summary", "")
        if library_summary:
            lines.append("")
            lines.append(library_summary)
        return lines

    def _resolve_raw_legal_actions(self, game_state: dict[str, Any]) -> list[dict[str, Any]]:
        """Pick the freshest raw-action list available.

        Bridge actions are the most authoritative source (they reflect live
        castability and autotap solutions). For non-ActionsAvailable bridge
        requests (target-select, search, casting-time options, etc.) an empty
        bridge action list is itself authoritative — we must NOT fall back
        to stale `legal_actions_raw` from a previous priority window.
        """
        bridge_req = game_state.get("_bridge_request_type")
        bridge_request_class = game_state.get("_bridge_request_class")
        bridge_actions = game_state.get("_bridge_actions")
        is_actions_available_bridge_request = (
            bridge_req in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            or bridge_request_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
        )
        if bridge_req and not is_actions_available_bridge_request:
            return bridge_actions or []
        return bridge_actions or game_state.get("legal_actions_raw") or []

    def _post_filter_uncastable_legal_moves(
        self,
        lines: list[str],
        valid_moves: list[str],
        raw_legal_actions: list[dict[str, Any]],
        no_target_card_names: set[str],
        uncastable_card_names: set[str],
        game_state: dict[str, Any],
    ) -> None:
        """Strip uncastable spells from the Legal: and LegalGRE: lines in-place.

        The GRE may report a spell as legal because it considers potential
        mana abilities, but our mana / target analysis can prove the spell
        actually can't be cast right now. Showing it as legal anyway makes
        the LLM suggest spells the engine will reject. This rewrites both
        lines[1] (the human-readable Legal: line) and any LegalGRE: line in
        place so the LLM only ever sees actionable options.
        """
        cards_to_filter = no_target_card_names | uncastable_card_names
        non_ok_cast_names = {
            m[5:].split("[", 1)[0].strip()
            for m in valid_moves
            if isinstance(m, str) and m.lower().startswith("cast ") and "[ok]" not in m.lower()
        }
        cards_to_filter |= non_ok_cast_names
        if not valid_moves:
            return

        filtered_moves = [
            m
            for m in valid_moves
            if not (
                isinstance(m, str)
                and m.lower().startswith("cast ")
                and ("[ok]" not in m.lower() or any(f"Cast {nt}" in m for nt in cards_to_filter))
            )
        ]
        if filtered_moves == valid_moves:
            return

        if not filtered_moves:
            new_legal = 'NONE — say "pass priority"'
        else:
            new_legal = ", ".join(filtered_moves[:8])
            if len(filtered_moves) > 8:
                new_legal += f"... (+{len(filtered_moves) - 8})"
        lines[1] = f"Legal: {new_legal}"

        if not raw_legal_actions:
            return

        filter_grp_ids: set[int] = set()
        for zone_name in ("hand", "command"):
            for card in game_state.get(zone_name, []):
                if card.get("name") in cards_to_filter:
                    gid = card.get("grp_id")
                    if gid:
                        filter_grp_ids.add(gid)
        filtered_raw = [
            a
            for a in raw_legal_actions
            if not (
                a.get("actionType") == "ActionType_Cast"
                and (a.get("grpId") in filter_grp_ids or not a.get("autoTapSolution"))
            )
        ]
        for i, line in enumerate(lines):
            if isinstance(line, str) and line.startswith("LegalGRE:"):
                lines[i] = "LegalGRE: " + _format_legal_actions_raw_for_prompt(filtered_raw)
                break

    def _format_game_context(
        self,
        game_state: dict[str, Any],
        question: str = "",
        *,
        for_planner: bool = False,
    ) -> str:
        """Format the game state into a COMPACT context for the LLM.

        Orchestrator that delegates to focused helper methods for each section.

        Args:
            for_planner: If True, produce a leaner context for the autopilot
                action planner — drops heavy GRE JSON dumps and trims oracle
                text on long-resident permanents (the flags already summarize
                their relevant abilities). Coach advice path keeps full
                fidelity by default.
        """

        # Determine local player seat and active turn
        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        local_seat = local_player.get("seat_id") if local_player else 1

        turn = game_state.get("turn", {})
        active_seat = turn.get("active_player", 0)
        priority_seat = turn.get("priority_player", 0)
        is_my_turn = active_seat == local_seat
        has_priority = priority_seat == local_seat

        phase = turn.get("phase", "Unknown").replace("Phase_", "")
        step = turn.get("step", "").replace("Step_", "")
        turn_num = turn.get("turn_number", 0)

        # Legal moves
        valid_moves, valid_moves_str = self._format_legal_moves(game_state, local_seat)

        lines = []
        match_num = game_state.get("_match_number")
        match_id = game_state.get("match_id") or ""
        match_tag = ""
        if match_num is not None:
            short_id = match_id[:8] if match_id else "?"
            match_tag = f" [Match #{match_num} id={short_id}]"
        lines.append(
            f"=== NEW GAME ==={match_tag}" if turn_num <= 1 and match_tag else f"=== GAME ==={match_tag}"
        )
        lines.append(f"Legal: {valid_moves_str}")
        raw_legal_actions = self._resolve_raw_legal_actions(game_state)
        lines.extend(_build_bridge_context_lines(game_state, raw_legal_actions, for_planner=for_planner))

        # Post-land planning
        lines.extend(self._format_post_land_planning(game_state, local_seat, valid_moves, is_my_turn, phase))

        # Get player info
        opponent_player = None
        for p in players:
            if not p.get("is_local"):
                opponent_player = p
                break
        opp_seat = opponent_player.get("seat_id") if opponent_player else None

        active_label = "YOUR" if active_seat == local_seat else "OPP"
        priority_label = "You" if priority_seat == local_seat else "Opp"
        is_main_phase = "Main" in phase
        is_your_turn = active_seat == local_seat
        stack = game_state.get("stack", [])
        stack_empty = len(stack) == 0
        can_cast_sorcery = is_your_turn and is_main_phase and stack_empty and has_priority
        is_blocking = "DeclareBlock" in step and not is_your_turn

        # Decision context
        pending_decision = game_state.get("pending_decision")
        if pending_decision:
            lines.extend(self._format_decision_lines(game_state))

        # Turn/phase/priority line
        if pending_decision in ("Mulligan", "Mulligan Bottom"):
            lines.append("YOUR MULLIGAN DECISION")
        else:
            phase_str = f"{phase}/{step}" if step else phase
            lines.append(f"T{turn_num} {active_label} | {phase_str} | Pri:{priority_label}")

        # Timing rules
        if pending_decision not in ("Mulligan", "Mulligan Bottom"):
            if can_cast_sorcery:
                lines.append("Timing: ALL SPELLS")
            elif is_blocking:
                lines.append("ACTION: DECLARE BLOCKERS")
            elif is_your_turn and is_main_phase and not stack_empty:
                lines.append("Timing: ALL SPELLS (after stack resolves)")
            else:
                lines.append("Timing: INSTANTS ONLY")

        # Life totals
        your_life = local_player.get("life_total", "?") if local_player else "?"
        opp_life = opponent_player.get("life_total", "?") if opponent_player else "?"
        damage_taken = game_state.get("damage_taken", {})
        your_dmg = damage_taken.get(str(local_seat), damage_taken.get(local_seat, 0))
        opp_dmg = damage_taken.get(str(opp_seat), damage_taken.get(opp_seat, 0)) if opp_seat else 0
        your_dmg_str = f" (taken {your_dmg})" if your_dmg else ""
        opp_dmg_str = f" (taken {opp_dmg})" if opp_dmg else ""
        lines.append(f"Life: You={your_life}{your_dmg_str} Opp={opp_life}{opp_dmg_str}")

        # Battlefield
        battlefield = game_state.get("battlefield", [])
        your_cards = [
            c
            for c in battlefield
            if c.get("owner_seat_id") == local_seat and c.get("type_line", "").lower() != "ability"
        ]
        opp_cards = [
            c
            for c in battlefield
            if c.get("owner_seat_id") != local_seat and c.get("type_line", "").lower() != "ability"
        ]

        # Mana info
        mana_lines, total_mana, mana_pool = self._format_mana_info(your_cards, turn_num)
        lines.extend(mana_lines)

        # Land drop status. P2-9: lands_played is inferred post-message and
        # lags for seconds after a drop — the CURRENT window's menu is the
        # authority. "Land: AVAILABLE" with no "Play Land:" entry produced
        # play_land hallucinations (Forest #3, 2026-07-05 22:50).
        lands_played = local_player.get("lands_played", 0) if local_player else 0
        has_land_entry = any(
            str(la).strip().lower().startswith(("play land:", "action: playmdfc"))
            for la in (game_state.get("legal_actions") or [])
        )
        if is_your_turn and lands_played == 0 and has_land_entry:
            lines.append("Land: AVAILABLE")
        elif is_your_turn and lands_played == 0:
            lines.append("Land: not playable in this window (no Play Land action)")
        elif is_your_turn:
            lines.append(f"Land: USED ({lands_played})")
        else:
            lines.append("Land: N/A (opp turn)")

        # Build attachment map
        _attachments: dict[int, list[dict]] = {}
        for card in battlefield:
            parent_id = card.get("parent_instance_id")
            if parent_id is not None:
                _attachments.setdefault(parent_id, []).append(card)

        # Battlefield display
        if battlefield:
            lines.append("")
            lines.append("YOUR BOARD:")
            if your_cards:
                your_name_counts = Counter(c.get("name", "Unknown") for c in your_cards)
                your_name_seen: dict[str, int] = {}
                for card in your_cards:
                    lines.extend(
                        self._format_board_card(
                            card,
                            local_seat,
                            turn_num,
                            _attachments,
                            your_name_counts,
                            your_name_seen,
                            is_local=True,
                            for_planner=for_planner,
                        )
                    )
            else:
                lines.append("  (empty)")

            # Pre-compute inferred attackers for DeclareBlock display
            _inferred_atk_ids: set[int] = set()
            _dec_ctx = game_state.get("decision_context") or {}
            _in_block_decision = ("Combat" in phase and not is_your_turn and "DeclareBlock" in step) or str(
                _dec_ctx.get("type") or ""
            ) == "declare_blockers"
            if _in_block_decision:
                has_explicit_atk = any(c.get("is_attacking") for c in opp_cards)
                if not has_explicit_atk:
                    # GRE-authoritative attacker ids from the DeclareBlockersReq
                    # (log path) / bridge blockers payload beat the tapped-
                    # creature heuristic — vigilance attackers stay untapped and
                    # mana-tapped creatures aren't attacking (issue #420).
                    _inferred_atk_ids |= self._attacker_ids_from_decision_context(_dec_ctx)
                if not has_explicit_atk and not _inferred_atk_ids:
                    for c in opp_cards:
                        c_type = c.get("type_line", "").lower()
                        c_oracle = self._remove_reminder_text(c.get("oracle_text", "")).lower()
                        is_ss = c.get("turn_entered_battlefield") == turn_num and "haste" not in c_oracle
                        if c.get("is_tapped") and "creature" in c_type and not is_ss:
                            _inferred_atk_ids.add(c.get("instance_id"))

            lines.append("OPP BOARD:")
            if opp_cards:
                opp_name_counts = Counter(c.get("name", "Unknown") for c in opp_cards)
                opp_name_seen: dict[str, int] = {}
                for card in opp_cards:
                    # Add inferred ATK flag before formatting
                    if card.get("instance_id") in _inferred_atk_ids and not card.get("is_attacking"):
                        card = dict(card)
                        card["is_attacking"] = True
                    lines.extend(
                        self._format_board_card(
                            card,
                            local_seat,
                            turn_num,
                            _attachments,
                            opp_name_counts,
                            opp_name_seen,
                            is_local=False,
                            for_planner=for_planner,
                        )
                    )
            else:
                lines.append("  (empty)")

            # Combat analysis
            if ("Combat" in phase or "Main" in phase) and is_your_turn:
                your_creatures = [
                    c
                    for c in your_cards
                    if "creature" in c.get("type_line", "").lower() and not self._is_impending(c)
                ]
                valid_attackers = [
                    c
                    for c in your_creatures
                    if not c.get("is_tapped")
                    and not (
                        c.get("turn_entered_battlefield") == turn_num
                        and "haste" not in self._remove_reminder_text(c.get("oracle_text", "")).lower()
                    )
                ]
                lines.extend(
                    self._format_attack_combat(
                        your_cards,
                        opp_cards,
                        local_player,
                        opponent_player,
                        turn_num,
                        valid_attackers,
                        game_state=game_state,
                    )
                )
            # A GRE declare_blockers decision is authoritative even when the
            # phase string hasn't caught up (or is missing) — gating the block
            # solver on "Combat" in phase alone silently dropped the
            # "Computed optimal blocks:" line on exactly the decision it
            # exists for. _in_block_decision already encodes this rule for the
            # inferred-attacker flags above; the dispatch must agree with it.
            elif ("Combat" in phase or _in_block_decision) and not is_your_turn:
                lines.extend(
                    self._format_block_combat(
                        your_cards,
                        opp_cards,
                        local_player,
                        turn_num,
                        phase,
                        _inferred_atk_ids,
                        decision_context=game_state.get("decision_context"),
                    )
                )
        else:
            lines.append("")
            lines.append("BOARD: Empty")

        # Recent events and revealed cards
        lines.extend(self._format_zones_and_events(game_state, local_seat, opp_seat))

        # Hand cards
        hand_lines, no_target_card_names, uncastable_card_names = self._format_hand_cards(
            game_state,
            local_seat,
            total_mana,
            mana_pool,
            opp_cards,
            battlefield,
            is_my_turn,
            phase,
            turn_num,
            valid_moves,
        )
        lines.extend(hand_lines)

        # Post-filter: drop spells the GRE thought were legal but our mana /
        # target analysis says aren't actually castable, and rewrite the
        # Legal: line and LegalGRE: raw-action line to match.
        self._post_filter_uncastable_legal_moves(
            lines,
            valid_moves,
            raw_legal_actions,
            no_target_card_names,
            uncastable_card_names,
            game_state,
        )

        return "\n".join(lines)

    def _filter_legal_attacker_names(
        self, game_state: dict[str, Any], legal_attackers: list[str]
    ) -> list[str]:
        """Filter declared attacker names against the GRE-authoritative list.

        The GRE protocol's ``qualifiedAttackers`` already enforces summoning
        sickness, tap state, defender, etc. Trusting it avoids false negatives
        when our local ``turn_entered_battlefield`` is stale (e.g. instance ID
        changes after ETB triggers reset the entered-turn tracker).

        Falls through to the input list if no GRE-authoritative declare-
        attackers context is available — that path only runs as a fallback-
        advice sanity check on hallucinated LLM names, where letting the names
        through unchanged is safer than incorrectly filtering them out.
        """
        if not legal_attackers:
            return []

        decision_ctx = game_state.get("decision_context") or {}
        if str(decision_ctx.get("type", "") or "").lower() == "declare_attackers":
            gre_names = decision_ctx.get("legal_attackers") or []
            if gre_names:
                valid = Counter(gre_names)
                filtered: list[str] = []
                for name in legal_attackers:
                    if valid[name] > 0:
                        filtered.append(name)
                        valid[name] -= 1
                if len(filtered) != len(legal_attackers):
                    logger.info(
                        "Filtered declare attackers vs GRE qualifiedAttackers: %s -> %s",
                        legal_attackers,
                        filtered,
                    )
                return filtered

        return list(legal_attackers)

    @staticmethod
    def _attacker_ids_from_decision_context(
        decision_context: dict[str, Any] | None,
    ) -> set[int]:
        """Attacker instance ids named by a declare_blockers decision context.

        The GRE DeclareBlockersReq is the authoritative statement of who is
        attacking: each blockers[] entry lists the attackerInstanceIds it may
        block. Both the log path (gamestate.py DeclareBlockersReq handler) and
        the bridge enrichment (gre_bridge._apply_bridge_blockers) surface that
        as ``attacker_ids`` + ``raw_blockers`` on decision_context.
        """
        ctx = decision_context or {}
        if str(ctx.get("type") or "") != "declare_blockers":
            return set()
        ids: set[int] = set()
        for aid in ctx.get("attacker_ids") or []:
            try:
                ids.add(int(aid))
            except (TypeError, ValueError):
                continue
        for blk in ctx.get("raw_blockers") or []:
            if not isinstance(blk, dict):
                continue
            for aid in blk.get("attackerInstanceIds") or []:
                try:
                    ids.add(int(aid))
                except (TypeError, ValueError):
                    continue
        return ids

    def _collect_block_decision_attackers(self, game_state: dict[str, Any]) -> list[dict]:
        """Resolve the attacking creatures for the current block decision.

        Prefers battlefield ``is_attacking`` flags (attackState from the log,
        or bridge enrichment); falls back to the decision_context attacker ids
        so the log path stays precise even if the attackState annotation was
        missed (issue #420).
        """
        battlefield = game_state.get("battlefield", []) or []
        local_seat = None
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break
        opp_cards = [c for c in battlefield if c.get("owner_seat_id") != local_seat]
        attacking = [c for c in opp_cards if c.get("is_attacking")]
        if attacking:
            return attacking
        ctx_ids = self._attacker_ids_from_decision_context(game_state.get("decision_context"))
        if ctx_ids:
            return [c for c in opp_cards if int(c.get("instance_id") or 0) in ctx_ids]
        return []

    _COMBAT_KEYWORDS = (
        "flying",
        "deathtouch",
        "trample",
        "first strike",
        "double strike",
        "menace",
        "lifelink",
        "vigilance",
        "indestructible",
    )

    def _combat_keyword_flags(self, card: dict) -> str:
        """Compact ``[FLYING,DEATHTOUCH]``-style suffix for combat listings."""
        oracle = self._remove_reminder_text(card.get("oracle_text", "")).lower()
        found = [kw.upper().replace(" ", "-") for kw in self._COMBAT_KEYWORDS if kw in oracle]
        return f" [{','.join(found)}]" if found else ""

    def _attacker_label_map(self, attackers: list[dict]) -> dict[int, str]:
        """instance_id -> ``Name #N P/T [KEYWORDS]`` labels, deduped by name."""
        names = [c.get("name", "?") for c in attackers]
        counts = Counter(names)
        seen: dict[str, int] = {}
        out: dict[int, str] = {}
        for c, n in zip(attackers, names, strict=False):
            p = c.get("power") or 0
            t = c.get("toughness") or 0
            label = n
            if counts[n] > 1:
                seen[n] = seen.get(n, 0) + 1
                label = f"{n} #{seen[n]}"
            out[int(c.get("instance_id") or 0)] = f"{label} {p}/{t}{self._combat_keyword_flags(c)}"
        return out

    def _format_block_decision_details(
        self, game_state: dict[str, Any], decision_context: dict[str, Any]
    ) -> list[str]:
        """Enumerate attackers (name, P/T, keywords) for a block decision.

        Also lists per-blocker legal-attacker candidates when the GRE says a
        blocker is restricted to a subset of the attackers (covers menace /
        skulk-style restrictions that keyword scans can't see). Log-path
        parity for issue #420: without these lines the LLM saw only blocker
        names and could not name which attacker to block.
        """
        lines: list[str] = []
        attackers = self._collect_block_decision_attackers(game_state)
        if not attackers:
            return lines
        label_by_id = self._attacker_label_map(attackers)
        lines.append(
            "Attackers: " + ", ".join(label_by_id[int(a.get("instance_id") or 0)] for a in attackers)
        )

        # Per-blocker candidate restrictions from the raw GRE blockers payload.
        raw_blockers = decision_context.get("raw_blockers") or []
        blocker_ids = decision_context.get("legal_blocker_ids") or []
        blocker_names = decision_context.get("legal_blockers") or []
        name_by_blocker_id: dict[int, str] = {}
        if len(blocker_ids) == len(blocker_names):
            for bid, bname in zip(blocker_ids, blocker_names, strict=False):
                try:
                    name_by_blocker_id[int(bid)] = bname
                except (TypeError, ValueError):
                    continue
        try:
            from arenamcp.combat_solver import blocker_allowed_attackers_map

            allowed_map = blocker_allowed_attackers_map(raw_blockers)
        except Exception:
            allowed_map = {}
        all_attacker_ids = set(label_by_id)
        for bid, allowed in allowed_map.items():
            if not allowed or allowed >= all_attacker_ids:
                continue  # unrestricted — no extra line needed
            bname = name_by_blocker_id.get(bid, f"Creature {bid}")
            atk_labels = [label_by_id[a] for a in sorted(allowed) if a in label_by_id]
            if atk_labels:
                lines.append(f"  {bname} can ONLY block: {', '.join(atk_labels)}")
        return lines

    def _extract_card_name_words(self, game_state: dict[str, Any]) -> set[str]:
        """Extract all words from card names in the current game state.

        These words are excluded from overuse tracking since they're card names.
        """
        import re

        card_words: set[str] = set()

        # Collect card names from all zones
        for zone in ["battlefield", "hand", "graveyard", "stack", "exile", "command"]:
            for card in game_state.get(zone, []):
                name = card.get("name", "")
                # Extract words from card name
                words = re.findall(r"\b[a-z]+\b", name.lower())
                card_words.update(words)

        return card_words

    # Fields trimmed from raw_json output: large, noisy, or internal. Includes
    # raw_gre_events (megabytes per turn), legal_actions_raw (the bridge action
    # list which is already surfaced via decision_context), and underscore-
    # prefixed fields used for internal bookkeeping. See _format_game_context_raw_json.
    _RAW_JSON_TRIM_FIELDS = frozenset(
        {
            "raw_gre_events",
            "legal_actions_raw",
            "_match_number",
            "_pending_request_raw",
            "annotations",
        }
    )

    def _normalize_game_state_cards(self, game_state: dict[str, Any]) -> None:
        """In-place normalize all card dictionaries in the game state to ensure
        they have a type_line, synthesizing one if type_line is empty but
        card_types or subtypes are present.
        """

        def normalize_card(card: dict[str, Any]) -> None:
            if not isinstance(card, dict):
                return
            type_line = card.get("type_line")
            if not type_line or not str(type_line).strip():
                card_types = card.get("card_types") or []
                subtypes = card.get("subtypes") or []
                if isinstance(card_types, str):
                    card_types = [card_types]
                if isinstance(subtypes, str):
                    subtypes = [subtypes]

                type_parts = []
                if card_types:
                    type_parts.append(" ".join(card_types))
                if subtypes:
                    type_parts.append("—")
                    type_parts.append(" ".join(subtypes))
                card["type_line"] = " ".join(type_parts).strip()

        # Normalize cards in list-based zones at the top level
        for _key, value in game_state.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and "instance_id" in item:
                        normalize_card(item)
            elif isinstance(value, dict):
                # Also normalize cards in nested dictionary lists (like game_state["zones"])
                for _sub_key, sub_val in value.items():
                    if isinstance(sub_val, list):
                        for item in sub_val:
                            if isinstance(item, dict) and "instance_id" in item:
                                normalize_card(item)

    def _build_context(
        self,
        game_state: dict[str, Any],
        question: str = "",
        *,
        for_planner: bool = False,
    ) -> str:
        self._normalize_game_state_cards(game_state)
        """Pick the active prompt variant for the user-message context.

        Reads MTGACOACH_PROMPT_VARIANT once per call. Honors:
          - 'raw_json' -> _format_game_context_raw_json
          - anything else (or unset) -> _format_game_context (compressed)

        Forwards optional kwargs only when they're non-default to preserve
        the calling convention of legacy callers and tests that patch
        _format_game_context with a simple lambda(state).
        """
        kwargs: dict[str, Any] = {}
        if question:
            kwargs["question"] = question
        if for_planner:
            kwargs["for_planner"] = for_planner
        if os.environ.get("MTGACOACH_PROMPT_VARIANT", "default").lower() == "raw_json":
            return self._format_game_context_raw_json(game_state, **kwargs)
        return self._format_game_context(game_state, **kwargs)

    def _format_game_context_raw_json(
        self,
        game_state: dict[str, Any],
        question: str = "",
        *,
        for_planner: bool = False,
    ) -> str:
        """Ablation variant: emit the game_state dict as JSON, no compression.

        Tests Gemini's claim that the structured-English formatting in
        _format_game_context is obsolete for small models like gemma4:e2b.
        Strips obviously-noisy fields (raw_gre_events, internal markers)
        but does NO derivation — the model gets the same dict the rest of
        the coach pipeline sees. Round-trippable, content-faithful, and
        directly comparable to the compressed builder when both are run
        on the same game_state.

        for_planner is accepted for API parity with the compressed builder
        but currently has no effect here.
        """
        cleaned = {k: v for k, v in game_state.items() if k not in self._RAW_JSON_TRIM_FIELDS}
        body = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), default=str)
        suffix = f"\n\nThe player asks: {question}" if question else ""
        return f"Game state (JSON):\n{body}{suffix}"

    @staticmethod
    def _plan_framing_instruction(plan_block: str, *, our_turn: bool, plan_changed: bool) -> str:
        """Return the GAME PLAN prompt suffix, gated by when to recite it aloud.

        The plan stays in the prompt as context either way (so advice is
        plan-aware), but it's only *spoken* on the opponent's turn or when the
        win strategy just changed. On our own turn with an unchanged plan it's
        silent background and the model gives just the concrete play. Returns ""
        when there is no plan yet.
        """
        if not plan_block:
            return ""
        recite = (not our_turn) or plan_changed
        if recite:
            return (
                "\n\n" + plan_block + "\n\nLead with the concrete recommended move FIRST, then briefly "
                "name the plan and how this move advances it."
            )
        return (
            "\n\n" + plan_block + "\n\nUse this game plan as SILENT background only. Do NOT name, recite, "
            "or summarize the plan or win condition in your answer. Give ONLY the "
            "concrete next play and its immediate tactical reason."
        )

    def get_advice(
        self,
        game_state: dict[str, Any],
        question: str | None = None,
        trigger: str | None = None,
        style: str | None = None,
        threat: dict[str, Any] | None = None,
    ) -> str:
        """Get coaching advice for the current game state.

        Args:
            game_state: Dict from get_game_state() MCP tool
            question: Optional user question to answer
            trigger: Optional trigger name (e.g., "combat_attackers", "low_life")
            style: Advice style ("concise" or "verbose")

        Returns:
            Advice string from the LLM
        """

        total_start = time.perf_counter()

        # Build context. _build_context honors MTGACOACH_PROMPT_VARIANT
        # (default | raw_json) — see _format_game_context_raw_json for the
        # compression-ablation rationale.
        context_start = time.perf_counter()
        context = self._build_context(game_state)
        context_time = (time.perf_counter() - context_start) * 1000

        # Get card name words to exclude from overuse tracking
        card_words = self._extract_card_name_words(game_state)

        # Check for overused words to avoid (excluding card names)
        blacklisted = self._word_tracker.get_blacklisted(exclude_words=card_words)

        # Build dynamic system prompt
        system_prompt = self._system_prompt

        if blacklisted:
            avoid_list = ", ".join(blacklisted)
            system_prompt += (
                f"\n\nIMPORTANT: Avoid using these overused words: {avoid_list}. Use different phrasing."
            )
            logger.debug(f"Blacklisted words: {blacklisted}")

        # PHASE 2: Inject decision-specific guidance when a decision is pending
        decision_context = game_state.get("decision_context")
        if decision_context:
            dec_type = decision_context.get("type", "unknown")
            decision_guidance = DECISION_PROMPTS.get(dec_type)
            if decision_guidance:
                system_prompt += f"\n\n{decision_guidance}"
                logger.debug(f"Injected decision prompt for type: {dec_type}")

        # Build user message
        # Priority: explicit arg > object property > default.
        # Accept both new names (quick/chatty) and legacy (concise/verbose).
        selected_style = style if style else getattr(self, "advice_style", "quick")
        raw_key = selected_style.lower()
        if raw_key in ("chatty", "verbose"):
            style_key = "chatty"
        elif raw_key in ("quick", "concise"):
            style_key = "quick"
        else:
            style_key = raw_key
        is_verbose = style_key == "chatty"  # retained name for legacy code below

        if question:
            if is_verbose:
                user_message = (
                    f"{context}\n\nThe player asks: {question}\nProvide a thorough answer with reasoning."
                )
            else:
                user_message = f"{context}\n\nThe player asks: {question}"
        elif trigger:
            if is_verbose:
                trigger_descriptions = {
                    "new_turn": "Your turn just started (Main 1). What is the best play and why? Consider alternatives.",
                    "opponent_turn": (
                        "Opponent's turn just started. Analyze their board, strategy, and game plan. "
                        "What threats should we prepare for? "
                        "What should we do on our next turn to counter them? "
                        "Explain your reasoning."
                    ),
                    "land_played": "A land was just played. What is the best next play? Explain why.",
                    "spell_resolved": "A spell just resolved. What is the best next play? Explain why.",
                    "priority_gained": "You have priority. Should you respond or pass? Explain your reasoning.",
                    "combat_attackers": "Combat: Declare attackers. Which creatures should attack and why? Default: attack with ALL eligible creatures unless you have a specific reason to hold one back (e.g., need a blocker to survive crackback). Explain the combat math.",
                    "combat_blockers": 'Combat: Opponent is attacking. How should you block and why? Name the attacker each blocker blocks ("Block [attacker] with [blocker]") and explain the trade-offs.',
                    "low_life": "Your life is dangerously low! What's the survival plan? Explain the reasoning.",
                    "opponent_low_life": "Opponent's life is low — can you finish them? Explain the line.",
                    "stack_spell": "Something was just cast. Should you respond or let it resolve? Explain why.",
                    "stack_spell_yours": "Your spell is on the stack. Pass priority or hold? Explain your reasoning.",
                    "stack_spell_opponent": "Opponent just cast something. Should you respond or let it resolve? Explain why.",
                    "user_request": "Give detailed strategic advice for this moment with reasoning.",
                    "decision_required": "Decision required (scry, discard, target, mulligan, etc). What should the player choose and why?",
                    "threat_detected": "ALERT: A dangerous card just hit the battlefield! Explain the threat and how to deal with it.",
                    "losing_badly": "The board state looks very bad. Assess honestly: can we come back, or should we concede and save time?",
                }
            else:
                trigger_descriptions = {
                    "new_turn": "Your turn just started (Main 1). What is the ONE best play right now?",
                    "opponent_turn": (
                        "Opponent's turn just started. Briefly analyze their board and strategy. "
                        "What is their game plan? What threats should we prepare for? "
                        "What should we do on our next turn to counter them? "
                        "Keep it to 2-3 sentences focused on opponent's strategy and your plan."
                    ),
                    "land_played": "A land was just played. What is the ONE next play?",
                    "spell_resolved": "A spell just resolved. What is the ONE next play?",
                    "priority_gained": "You have priority. Respond or pass?",
                    "combat_attackers": "Combat: Declare attackers. Which creatures should attack? Default: attack with ALL eligible creatures unless you have a specific reason to hold one back (e.g., need a blocker to survive crackback).",
                    "combat_blockers": 'Combat: Opponent is attacking. How should you block? Name the attacker each blocker blocks ("Block [attacker] with [blocker]").',
                    "low_life": "Your life is dangerously low! What's the survival plan?",
                    "opponent_low_life": "Opponent's life is low — can you finish them?",
                    "stack_spell": "Something was just cast. Respond or let it resolve?",
                    "stack_spell_yours": "Your spell is on the stack. Pass priority or hold?",
                    "stack_spell_opponent": "Opponent just cast something. Respond or let it resolve?",
                    "user_request": "Give quick strategic advice for this moment.",
                    "decision_required": "Decision required (scry, discard, target, mulligan, etc). What should the player choose?",
                    "threat_detected": "ALERT: A dangerous card just hit the battlefield!",
                    "losing_badly": "Board looks dire. Can we come back or should we concede?",
                }
            if trigger == "threat_detected" and threat:
                trigger_desc = self._build_threat_trigger_description(
                    game_state,
                    threat,
                    is_verbose=is_verbose,
                )
            else:
                trigger_desc = trigger_descriptions.get(trigger, f"Trigger: {trigger}")
            user_message = f"{context}\n\n{trigger_desc}"
        else:
            if is_verbose:
                user_message = f"{context}\n\nWhat's the best play right now? Explain your reasoning."
            else:
                user_message = f"{context}\n\nWhat's the best play right now?"

        # OPTIMIZATION: Log prompt size with token estimate
        prompt_chars = len(system_prompt) + len(user_message)
        prompt_tokens_est = self._estimate_tokens(system_prompt + user_message)
        context_lines = context.count("\n") + 1
        logger.info(
            f"[PROMPT] {context_lines} lines, {prompt_chars} chars, ~{prompt_tokens_est} tokens | context: {context_time:.1f}ms"
        )

        # Log backend diagnostics
        backend_info = self.get_backend_info()
        logger.info(
            f"[BACKEND] {backend_info['backend_name']} | model={backend_info['model']} | style={style_key}"
        )

        # style_key and is_verbose were already computed above for trigger descriptions

        # ── QUICK prompt ──────────────────────────────────────────────────
        # Single sentence, imperative, speakable in under 5 seconds.
        _quick_prompt = DEFAULT_SYSTEM_PROMPT.replace(
            "Keep responses concise (2-3 sentences max) since they'll be spoken aloud.\n"
            "Focus ONLY on the final strategic recommendation.\n"
            'Do NOT show your thinking process, "reasoning", or "corrections".\n'
            "Do NOT use internal monologue tags like [plan] or [thought].\n"
            'Do NOT second-guess yourself in the text (e.g., "Wait, I need to check...").\n'
            "Be authoritative and decisive. Start your response immediately with the command.",
            "QUICK MODE: respond in ONE short imperative sentence, under 15 words. "
            "Just the action — no reasoning, no alternatives, no hedging. "
            'Examples: "Play Forest." "Cast Lightning Bolt on the dragon." '
            '"Attack with all creatures." "Pass priority." '
            "If the play truly requires context, use 2 short sentences max — "
            "but prefer one. Never exceed 20 words total.",
        ).replace(
            "Output directly as the coach. No preamble, no meta-commentary.",
            "Output directly as the coach. No preamble. No meta-commentary. One sentence.",
        )

        # ── CHATTY prompt ─────────────────────────────────────────────────
        # Multiple sentences, explain the WHY, mention alternatives/tradeoffs,
        # feel conversational. Still capped so TTS doesn't run forever.
        _chatty_prompt = DEFAULT_SYSTEM_PROMPT.replace(
            "Keep responses concise (2-3 sentences max) since they'll be spoken aloud.\n"
            "Focus ONLY on the final strategic recommendation.\n"
            'Do NOT show your thinking process, "reasoning", or "corrections".\n'
            "Do NOT use internal monologue tags like [plan] or [thought].\n"
            'Do NOT second-guess yourself in the text (e.g., "Wait, I need to check...").\n'
            "Be authoritative and decisive. Start your response immediately with the command.",
            "CHATTY MODE: give a conversational, natural-sounding recommendation "
            "of 3 to 5 sentences. Lead with the recommended play, then explain "
            "WHY it's right: the game state reasoning, combat math, or the "
            "tradeoff vs the most obvious alternative. Mention any relevant "
            "threat you're playing around. Speak like a friend watching over "
            "your shoulder — warm but focused. Cap it at ~80 words so speech "
            "stays under ~25 seconds. Still no internal monologue tags or "
            "self-correction — just deliver the reasoning cleanly.",
        ).replace(
            "Output directly as the coach. No preamble, no meta-commentary.",
            "Output directly as the coach. No preamble or meta-commentary — "
            "just lead with the play and explain the thinking.",
        )

        prompts = {
            "quick": _quick_prompt,
            "chatty": _chatty_prompt,
            # Legacy aliases still work
            "concise": _quick_prompt,
            "verbose": _chatty_prompt,
            "normal": DEFAULT_SYSTEM_PROMPT,
            "explain": DEFAULT_SYSTEM_PROMPT.replace(
                "Keep responses concise (2-3 sentences max)",
                "Explain your reasoning clearly but briefly.",
            )
            + "\nInclude a short explanation of WHY this is the best line.",
            "pirate": "You are a ruthless pirate captain coaching a swabby! Speak like a pirate! Yarr! Keep it short!",
        }

        effective_system_prompt = prompts.get(style_key, _quick_prompt)

        # Inject deck strategy if available — instruct model to reference it
        if self._deck_strategy:
            effective_system_prompt += (
                f"\n\nDECK STRATEGY:\n{self._deck_strategy}"
                "\n\nALWAYS consider this strategy when advising. Prioritize plays that:"
                "\n- Set up or execute the combos/synergies listed above"
                "\n- Advance the deck's win condition"
                "\n- Follow the ideal play pattern for the current game phase"
                "\nBriefly explain WHY a play matters for the deck's plan "
                "(e.g. 'Cast X — triggers Kodama for a free land, setting up combo next turn')."
            )

        # Persistent GAME PLAN: refresh on our own active turn, then frame the
        # advice as the next STEP in that plan. Fully guarded — a plan failure
        # must never break advice generation.
        #
        # Reciting the hierarchical plan/win aloud on EVERY turn is repetitive.
        # Speak it only on the opponent's turn (less to do — strategy recap is
        # useful) or when the win strategy just changed; on our own turn with an
        # unchanged plan, keep it as silent background and just give the play.
        # (When autopilot drives, advice goes through the planner, which narrates
        # the plan there.)
        try:
            mgr = self._ensure_game_plan_mgr()
            if mgr is not None:
                # Only reform on our own active turn. If the local seat is
                # unknown, treat the turn as ours (conservative — still refreshes).
                local_seat = None
                for p in game_state.get("players", []) or []:
                    if p.get("is_local"):
                        local_seat = p.get("seat_id")
                        break
                active_player = (game_state.get("turn", {}) or {}).get("active_player")
                our_turn = local_seat is None or active_player == local_seat
                intro_before = mgr.coach_intro()
                if our_turn:
                    mgr.maybe_reform(game_state)
                intro_after = mgr.coach_intro()
                plan_changed = bool(intro_after) and intro_after != intro_before
                effective_system_prompt += self._plan_framing_instruction(
                    mgr.plan_text() or "", our_turn=our_turn, plan_changed=plan_changed
                )
        except Exception as e:
            logger.debug(f"Game-plan injection failed (non-fatal): {e}")

        # Re-inject blacklisted words and decision guidance into effective prompt
        if blacklisted:
            avoid_list = ", ".join(blacklisted)
            effective_system_prompt += (
                f"\n\nIMPORTANT: Avoid using these overused words: {avoid_list}. Use different phrasing."
            )

        if decision_context:
            dec_type = decision_context.get("type", "unknown")
            decision_guidance = DECISION_PROMPTS.get(dec_type)
            if decision_guidance:
                effective_system_prompt += f"\n\n{decision_guidance}"

        # RAG: Inject relevant MTG rules for this situation
        try:
            if self._rules_db is None:
                from arenamcp.rules_db import RulesDB

                self._rules_db = RulesDB()
            rules = self._rules_db.get_rules_for_situation(game_state, trigger, limit=5)
            if rules:
                rules_lines = [f"- Rule {r['number']}: {r['text']}" for r in rules]
                effective_system_prompt += (
                    "\n\nRELEVANT MTG RULES (official — these override any conflicting assumptions):\n"
                    + "\n".join(rules_lines)
                )
                logger.debug(f"Injected {len(rules)} rules: {[r['number'] for r in rules]}")
        except Exception as e:
            logger.warning(f"Rules RAG error (non-fatal): {e}")

        # Get response with timeout to prevent hanging on slow models.
        # IMPORTANT: The external timeout MUST be longer than the backend's
        # internal timeout (timeout_s) so the backend releases its lock first.
        # If the external timeout fires first, the backend thread still holds
        # the lock, causing cascading lock-busy failures on subsequent calls
        # which triggers unnecessary restarts.
        backend_timeout = getattr(self._backend, "timeout_s", 12.0)
        is_local = _is_local_backend(self._backend)
        if is_local:
            api_timeout = max(backend_timeout + 5, 45)  # Local models need more time
        else:
            api_timeout = max(backend_timeout + 5, 15)
        api_start = time.perf_counter()
        import concurrent.futures

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        complete_kwargs = (
            {"request_timeout_s": api_timeout} if isinstance(self._backend, ProxyBackend) else {}
        )
        future = executor.submit(
            self._backend.complete,
            effective_system_prompt,
            user_message,
            1000,
            **complete_kwargs,
        )
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            is_local = _is_local_backend(self._backend)
            hint = " — try a smaller model or use a cloud backend" if is_local else ""
            logger.warning(
                f"LLM API call timed out after {api_timeout}s (model may be too slow for real-time coaching){hint}"
            )
            # Return error string (not empty) to avoid triggering the
            # consecutive-empty-response restart counter in standalone.py
            response = f"{BACKEND_ERROR_PREFIX} LLM timed out after {api_timeout}s"
        # shutdown(wait=False) only abandons the thread; the SDK-level
        # request_timeout_s above is what actually unblocks it. Without
        # that, every hung backend call leaks a thread forever.
        executor.shutdown(wait=False)
        api_time = (time.perf_counter() - api_start) * 1000

        if trigger == "threat_detected" and threat and (not response or is_backend_error_text(response)):
            response = self._build_threat_fallback(game_state, threat)

        # Prepend a short plan framing for longer styles only (the "quick" style
        # has a very tight word cap — prepending would blow it). Fully guarded.
        try:
            if (
                style_key in ("normal", "chatty", "explain")
                and response
                and not is_backend_error_text(response)
                and not response.lstrip().lower().startswith("plan")
            ):
                mgr = self._game_plan_mgr
                intro = mgr.coach_intro() if mgr is not None else ""
                if intro:
                    response = f"{intro}. {response}"
        except Exception as e:
            logger.debug(f"Game-plan intro prepend failed (non-fatal): {e}")

        # POST-PROCESSING: Validate and fix common LLM issues (especially for smaller models)
        response = self._postprocess_advice(response, game_state, style=style_key)

        if trigger == "threat_detected" and threat:
            threat_name = str(threat.get("name", "") or "").strip()
            if threat_name and threat_name.lower() not in response.lower():
                response = f"{threat_name} is the key threat. {response}"

        self._word_tracker.record(response, exclude_words=card_words)

        total_time = (time.perf_counter() - total_start) * 1000
        logger.info(
            f"[TIMING] API call: {api_time:.0f}ms, total: {total_time:.0f}ms, response: {len(response)} chars"
        )

        return response

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
