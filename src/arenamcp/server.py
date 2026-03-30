"""FastMCP server exposing MTGA game state and card information.

This module provides the MCP server that bridges live MTGA games to Claude,
implementing the Calculator + Coach pattern: deterministic code tracks state
while the LLM provides strategic analysis.
"""

import copy
import logging
import threading
import time
from collections import deque
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

from arenamcp.coach import CoachEngine, GameStateTrigger, create_backend

from arenamcp.gamestate import (
    GameState, create_game_state_handler,
    save_match_state, load_match_state,
    validate_log_identity,
)
from arenamcp.parser import LogParser
from arenamcp.scryfall import ScryfallCache
from arenamcp.draftstats import DraftStatsCache
from arenamcp.draftstate import DraftState, create_draft_handler
from arenamcp.draft_eval import evaluate_pack, format_pick_recommendation
from arenamcp.sealed_eval import analyze_sealed_pool, format_sealed_recommendation, format_sealed_detailed
from arenamcp.mtgadb import MTGADatabase
from arenamcp.card_db import (
    FallbackCardDatabase,
    get_card_database,
    ScryfallAdapter,
)
from arenamcp.watcher import MTGALogWatcher
# Defer voice/audio imports — sounddevice initializes PortAudio on import
# which can hang if an audio device/driver is misbehaving.
VoiceInput = None  # type: ignore[assignment,misc]
VoiceOutput = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Initialize FastMCP server with STDIO transport
mcp = FastMCP("mtga")

# Module-level state instances
game_state: GameState = GameState()
draft_state: DraftState = DraftState()
parser: LogParser = LogParser()
watcher: Optional[MTGALogWatcher] = None


# Lazy-loaded caches (avoid blocking startup with bulk downloads)
_draft_stats: Optional[DraftStatsCache] = None

# Lazy-loaded voice components
_voice_input: Optional[VoiceInput] = None
_voice_output: Optional[VoiceOutput] = None

# Lazy-loaded new modules
_edhrec = None
_mtggoldfish = None
_deck_builder = None
_synergy_graph = None

# Advice queue for proactive coaching (Phase 9 integration)
_pending_advice: deque[dict[str, Any]] = deque(maxlen=10)

# Background coaching state
_coaching_thread: Optional[threading.Thread] = None
_coaching_enabled: bool = False
_coaching_backend: Optional[str] = None
_coaching_model: Optional[str] = None
_coaching_auto_speak: bool = False

# Background draft helper state
_draft_helper_thread: Optional[threading.Thread] = None
_draft_helper_enabled: bool = False
_draft_helper_last_pack: int = 0
_draft_helper_last_pick: int = 0


def _get_card_db() -> FallbackCardDatabase:
    """Get the unified card database (lazy loading, thread-safe)."""
    return get_card_database()


def _get_scryfall() -> ScryfallCache:
    """Get or initialize the Scryfall cache (lazy loading).

    Deprecated: prefer _get_card_db() for card lookups. This accessor
    is retained for call sites that need the raw ScryfallCache (e.g.
    draft_eval.evaluate_pack which type-hints ScryfallCache).
    """
    db = _get_card_db()
    for src in db.sources:
        if isinstance(src, ScryfallAdapter):
            return src._cache  # type: ignore[return-value]
    # Fallback: create a NullCardDatabase-backed stub that quacks like ScryfallCache
    return _NullScryfallCompat()


class _NullScryfallCompat:
    """Minimal ScryfallCache-compatible stub for when Scryfall is unavailable."""

    def get_card_by_arena_id(self, arena_id: int):  # noqa: ARG002
        return None

    def get_card_by_name(self, name: str):  # noqa: ARG002
        return None


def _get_draft_stats() -> DraftStatsCache:
    """Get or initialize the draft stats cache (lazy loading)."""
    global _draft_stats
    if _draft_stats is None:
        logger.info("Initializing 17lands draft stats cache...")
        _draft_stats = DraftStatsCache()
    return _draft_stats


def _get_mtgadb() -> MTGADatabase:
    """Get or initialize the MTGA local database (lazy loading).

    Deprecated: prefer _get_card_db() for card lookups. This accessor
    is retained for call sites that need the raw MTGADatabase (e.g.
    draft_eval.evaluate_pack which type-hints MTGADatabase).
    """
    db = _get_card_db()
    raw = db.get_raw_mtgadb()
    if raw is not None:
        return raw
    # Not available -- return a fresh (non-connected) instance
    return MTGADatabase()


def _get_voice_input(mode: Literal["ptt", "vox"] = "ptt") -> VoiceInput:
    """Get or initialize voice input (lazy loading).

    Args:
        mode: Voice input mode - 'ptt' for push-to-talk, 'vox' for voice activation.

    Returns:
        VoiceInput instance configured for the requested mode.
    """
    global _voice_input
    # Recreate if mode changed
    if _voice_input is None or _voice_input.mode != mode:
        logger.info(f"Initializing voice input in {mode} mode...")
        _voice_input = VoiceInput(mode=mode)
    return _voice_input


def _get_voice_output() -> VoiceOutput:
    """Get or initialize voice output (lazy loading)."""
    global _voice_output
    if _voice_output is None:
        logger.info("Initializing voice output (TTS)...")
        _voice_output = VoiceOutput()
    return _voice_output


# Match state tracking
_last_saved_turn: int = -1


def _get_edhrec():
    """Get or initialize EDHREC client (lazy loading)."""
    global _edhrec
    if _edhrec is None:
        from arenamcp.edhrec import EDHRECClient
        logger.info("Initializing EDHREC client...")
        _edhrec = EDHRECClient()
    return _edhrec


def _get_mtggoldfish():
    """Get or initialize MTGGoldfish client (lazy loading)."""
    global _mtggoldfish
    if _mtggoldfish is None:
        from arenamcp.mtggoldfish import MTGGoldfishClient
        logger.info("Initializing MTGGoldfish client...")
        _mtggoldfish = MTGGoldfishClient()
    return _mtggoldfish


def _get_deck_builder():
    """Get or initialize DeckBuilderV2 (lazy loading)."""
    global _deck_builder
    if _deck_builder is None:
        from arenamcp.deck_builder import DeckBuilderV2
        logger.info("Initializing DeckBuilderV2...")
        _deck_builder = DeckBuilderV2(
            draft_stats=_get_draft_stats(),
            enrich_fn=enrich_with_oracle_text,
        )
    return _deck_builder


def _get_synergy_graph():
    """Get or initialize SynergyGraph (lazy loading)."""
    global _synergy_graph
    if _synergy_graph is None:
        from arenamcp.synergy import get_synergy_graph
        _synergy_graph = get_synergy_graph()
    return _synergy_graph


def _save_match_state_if_needed() -> None:
    """Save match state on turn changes."""
    global _last_saved_turn
    current_turn = game_state.turn_info.turn_number
    if current_turn != _last_saved_turn and game_state.match_id:
        _last_saved_turn = current_turn
        offset = watcher.file_position if watcher else 0
        log_path = str(watcher.log_path) if watcher else None
        save_match_state(game_state, log_offset=offset, log_path=log_path)


def _background_coaching_loop(
    coach: CoachEngine,
    trigger_detector: GameStateTrigger,
    auto_speak: bool
) -> None:
    """Background loop that monitors game state and generates proactive advice.

    Args:
        coach: CoachEngine instance to generate advice
        trigger_detector: GameStateTrigger to detect state changes
        auto_speak: Whether to automatically speak advice via TTS
    """
    global _coaching_enabled

    logger.info("Background coaching loop started")
    prev_state: dict[str, Any] = {}

    while _coaching_enabled:
        try:
            # Get current game state
            curr_state = get_game_state()

            # Check for triggers (skip on first iteration when prev_state empty, unless vital state exists)
            triggers = []
            if prev_state:
                triggers = trigger_detector.check_triggers(prev_state, curr_state)
            elif curr_state.get("pending_decision"):
                 # Force trigger on first run if we are stuck in a decision
                 logger.info("Detected pending decision on startup - forcing trigger")
                 triggers = ["decision_required"]

            if triggers:
                for trigger in triggers:
                    logger.debug(f"Trigger fired: {trigger}")
                    try:
                        advice = coach.get_advice(curr_state, trigger=trigger)
                        queue_advice(advice, trigger)

                        if auto_speak:
                            try:
                                voice = _get_voice_output()
                                voice.speak(advice, blocking=True)
                            except Exception as e:
                                logger.error(f"TTS error: {e}")
                    except Exception as e:
                        logger.error(f"Error getting advice for {trigger}: {e}")

            prev_state = curr_state

        except Exception as e:
            logger.error(f"Error in coaching loop: {e}")

        # Poll interval
        time.sleep(1.5)

    logger.info("Background coaching loop stopped")


def start_background_coaching(
    backend: str = "auto",
    model: Optional[str] = None,
    auto_speak: bool = False
) -> None:
    """Start background game monitoring with proactive coaching.

    Creates a daemon thread that polls game state and generates advice
    when triggers fire.

    Args:
        backend: LLM mode to use ("online", "local", or "auto")
        model: Optional model override (uses mode default if not specified)
        auto_speak: If True, automatically speak advice via TTS
    """
    global _coaching_thread, _coaching_enabled, _coaching_backend, _coaching_model, _coaching_auto_speak

    if _coaching_enabled:
        raise RuntimeError("Background coaching already running")

    # Create coach components
    llm_backend = create_backend(backend, model=model)
    coach = CoachEngine(backend=llm_backend)
    trigger_detector = GameStateTrigger()

    # Store config
    _coaching_backend = backend
    _coaching_model = model
    _coaching_auto_speak = auto_speak
    _coaching_enabled = True

    # Start daemon thread
    _coaching_thread = threading.Thread(
        target=_background_coaching_loop,
        args=(coach, trigger_detector, auto_speak),
        daemon=True,
        name="coaching-loop"
    )
    _coaching_thread.start()
    logger.info(f"Started background coaching with {backend} mode (model={model}), auto_speak={auto_speak}")


def stop_background_coaching() -> None:
    """Stop background coaching if running."""
    global _coaching_thread, _coaching_enabled, _coaching_backend, _coaching_model, _coaching_auto_speak

    if not _coaching_enabled:
        raise RuntimeError("Background coaching not running")

    _coaching_enabled = False

    if _coaching_thread is not None:
        _coaching_thread.join(timeout=5.0)
        _coaching_thread = None

    _coaching_backend = None
    _coaching_model = None
    _coaching_auto_speak = False
    logger.info("Stopped background coaching")


# Draft evaluation now uses shared module: arenamcp.draft_eval


def _draft_helper_loop() -> None:
    """Background loop that monitors draft state and speaks recommendations.

    Polls draft state rapidly and speaks top 2 picks with reasons.
    Uses shared draft_eval module for evaluation logic.
    """
    global _draft_helper_enabled, _draft_helper_last_pack, _draft_helper_last_pick

    logger.info("Draft helper loop started")

    # Pre-load caches for speed
    scryfall = _get_scryfall()
    draft_stats_cache = _get_draft_stats()
    mtgadb = _get_mtgadb()
    voice = _get_voice_output()

    while _draft_helper_enabled:
        try:
            # Check if pack changed
            if (draft_state.is_active and
                draft_state.cards_in_pack and
                (draft_state.pack_number != _draft_helper_last_pack or
                 draft_state.pick_number != _draft_helper_last_pick)):

                _draft_helper_last_pack = draft_state.pack_number
                _draft_helper_last_pick = draft_state.pick_number

                # Use shared evaluation logic
                evaluations = evaluate_pack(
                    cards_in_pack=draft_state.cards_in_pack,
                    picked_cards=draft_state.picked_cards,
                    set_code=draft_state.set_code,
                    scryfall=scryfall,
                    draft_stats=draft_stats_cache,
                    mtgadb=mtgadb,
                )

                # Format and speak recommendation
                advice = format_pick_recommendation(
                    evaluations,
                    _draft_helper_last_pack,
                    _draft_helper_last_pick,
                )

                try:
                    voice.speak(advice, blocking=False)
                    logger.info(f"Draft advice: {advice}")
                except Exception as e:
                    logger.error(f"TTS error: {e}")

        except Exception as e:
            logger.error(f"Draft helper error: {e}")

        # Fast polling - 300ms
        time.sleep(0.3)

    logger.info("Draft helper loop stopped")


def start_draft_helper(set_code: Optional[str] = None) -> None:
    """Start background draft helper that auto-speaks pick recommendations.

    Args:
        set_code: Optional set code override (e.g., "MH3", "BLB"). If provided,
                  uses this instead of auto-detection for 17lands lookups.
    """
    global _draft_helper_thread, _draft_helper_enabled
    global _draft_helper_last_pack, _draft_helper_last_pick

    if _draft_helper_enabled:
        raise RuntimeError("Draft helper already running")

    # Auto-start watcher
    if watcher is None:
        start_watching()

    # Set manual set code if provided
    if set_code:
        draft_state.set_code = set_code.upper()
        logger.info(f"Using manual set code: {draft_state.set_code}")

    # Reset tracking
    _draft_helper_last_pack = draft_state.pack_number
    _draft_helper_last_pick = draft_state.pick_number
    _draft_helper_enabled = True

    # Start daemon thread
    _draft_helper_thread = threading.Thread(
        target=_draft_helper_loop,
        daemon=True,
        name="draft-helper"
    )
    _draft_helper_thread.start()
    logger.info("Started draft helper")


def stop_draft_helper() -> None:
    """Stop background draft helper."""
    global _draft_helper_thread, _draft_helper_enabled

    if not _draft_helper_enabled:
        raise RuntimeError("Draft helper not running")

    _draft_helper_enabled = False

    if _draft_helper_thread is not None:
        _draft_helper_thread.join(timeout=2.0)
        _draft_helper_thread = None

    logger.info("Stopped draft helper")


def _handle_match_created(payload: dict) -> None:
    """Handle MatchCreated / MatchGameRoomStateChangedEvent.

    The MatchCreated message contains systemSeatId which definitively
    identifies the local player's seat.

    MatchGameRoomStateChangedEvent also fires at match end with result data.
    We capture the result into ``last_game_result`` so the coaching loop can
    detect it even if the WinTheGame/LossOfGame annotation was missed (e.g.
    concede).
    """
    event_payload = (
        payload.get("matchGameRoomStateChangedEvent")
        or payload.get("matchCreated")
        or payload.get("clientToMatchServiceMessage", {}).get("payload")
        or payload.get("payload")
        or payload
    )
    room_info = (
        event_payload.get("gameRoomInfo")
        or event_payload.get("matchGameRoomInfo")
        or payload.get("matchGameRoomInfo", {})
    )
    game_room_config = (
        room_info.get("gameRoomConfig")
        or event_payload.get("gameRoomConfig")
        or payload.get("gameRoomConfig", {})
    )
    state_type = room_info.get("stateType") or event_payload.get("stateType")

    participants = list(game_room_config.get("reservedPlayers", []))
    for player in room_info.get("players", []):
        if isinstance(player, dict):
            participants.append(player)

    from arenamcp.log_utils import get_local_player_id
    local_user_id = get_local_player_id()

    def _get_participant_field(field: str) -> Optional[Any]:
        if not local_user_id:
            return None
        for participant in participants:
            if participant.get("userId") == local_user_id:
                return participant.get(field)
        return None

    seat_id = (
        event_payload.get("systemSeatId")
        or event_payload.get("systemSeatNumber")
        or event_payload.get("localPlayerSeatId")
        or payload.get("systemSeatId")
        or payload.get("systemSeatNumber")
        or payload.get("localPlayerSeatId")
        or _get_participant_field("systemSeatId")
    )
    team_id = _get_participant_field("teamId")

    if seat_id is None and not local_user_id:
        from arenamcp.log_utils import detect_local_seat
        seat_id = detect_local_seat()

    if team_id is None and seat_id is not None:
        for participant in participants:
            if participant.get("systemSeatId") == seat_id:
                team_id = participant.get("teamId")
                break

    if seat_id is not None:
        # Use System (2) priority for match messages. This will be ignored if
        # the user already manually forced a seat via F8.
        game_state.set_local_seat_id(seat_id, source=2)
        logger.info(f"Captured local seat ID {seat_id} from match message")

    # ── Match ID tracking ──
    match_id = (
        event_payload.get("matchId") or
        room_info.get("gameRoomConfig", {}).get("matchId") or
        game_room_config.get("matchId") or
        payload.get("matchId")
    )

    if match_id:
        if game_state.match_id != match_id:
            if state_type == "MatchGameRoomStateType_MatchCompleted":
                logger.info(
                    "Completed match event for unseen match %s. Recording metadata without reset.",
                    match_id,
                )
            else:
                logger.info(f"New match detected (ID: {match_id}). Resetting game state.")
                game_state.reset()
            game_state.match_id = match_id

            # Since we reset state, notify draft helper effectively if needed
            # (though draft state is separate)

    # ── Match result detection (from finalMatchResult) ──
    # MTGA sends this in MatchGameRoomStateChangedEvent when the match ends.
    results = (
        room_info.get("finalMatchResult", {}).get("resultList", [])
        or event_payload.get("finalMatchResult", {}).get("resultList", [])
    )
    if results and not game_state.last_game_result:
        # IntermissionReq may have already wiped seat/team state via reset().
        our_seat = seat_id or game_state.local_seat_id
        if our_seat is None and game_state._pre_reset_snapshot:
            our_seat = game_state._pre_reset_snapshot.get("local_seat_id")

        our_team = team_id
        if our_team is None:
            our_team = game_state.get_local_team_id()
        if our_team is None and game_state._pre_reset_snapshot:
            for player in game_state._pre_reset_snapshot.get("players", []):
                if player.get("seat_id") == our_seat:
                    our_team = player.get("team_id")
                    break

        ordered_results = sorted(
            results,
            key=lambda row: 0 if row.get("scope") == "MatchScope_Game" else 1,
        )
        for result_row in ordered_results:
            scope = result_row.get("scope", "")
            if scope not in ("MatchScope_Game", "MatchScope_Match", ""):
                continue

            resolved: Optional[str] = None
            winning_team_id = result_row.get("winningTeamId")
            row_seat_id = result_row.get("seatId")
            result_str = str(result_row.get("result", "") or "")

            if "Draw" in result_str:
                resolved = "draw"
            elif winning_team_id is not None and our_team is not None:
                resolved = "win" if winning_team_id == our_team else "loss"
            elif row_seat_id is not None and our_seat is not None:
                if "Win" in result_str:
                    resolved = "win" if row_seat_id == our_seat else "loss"
                elif "Loss" in result_str:
                    resolved = "loss" if row_seat_id == our_seat else "win"

            if resolved:
                game_state.last_game_result = resolved
                logger.info(
                    "Match result from finalMatchResult: %s (scope=%s, seat=%s, team=%s)",
                    resolved,
                    scope or "unknown",
                    our_seat,
                    our_team,
                )
                if not game_state.game_ended_event.is_set():
                    game_state.game_ended_event.set()
                    logger.info("Game-end event set from finalMatchResult")
                break
        else:
            logger.warning(
                "finalMatchResult received but could not resolve local outcome "
                "(seat=%s, team=%s, results=%s)",
                our_seat,
                our_team,
                results,
            )


def start_watching() -> None:
    """Start watching the MTGA log file for game events.

    Creates and starts the watcher if not already running.
    Watcher feeds log chunks to the parser, which updates game_state and draft_state.
    Checks for saved match state to resume from if available.
    """
    global watcher
    if watcher is not None:
        logger.debug("Watcher already running")
        return

    # Wire up the game state event handler
    handler = create_game_state_handler(game_state)
    parser.register_handler("GreToClientEvent", handler)

    # Wire up match creation handler to capture local seat ID
    parser.register_handler("MatchCreated", _handle_match_created)
    parser.register_handler("MatchGameRoomStateChangedEvent", _handle_match_created)
    parser.register_handler("ClientToMatchServiceMessage", _handle_match_created)

    # Wire up the draft handler as default to catch all draft events
    draft_handler = create_draft_handler(draft_state)
    parser.set_default_handler(draft_handler)

    # Check for saved match state to resume from
    resume_offset = None
    saved_state = load_match_state()
    if saved_state:
        # Validate log identity before trusting the saved offset
        # Use the watcher's resolved log path for comparison
        import os
        from arenamcp.watcher import _normalize_log_path, DEFAULT_LOG_PATH
        raw_log_path = os.environ.get("MTGA_LOG_PATH", DEFAULT_LOG_PATH)
        current_log_path = str(_normalize_log_path(raw_log_path))

        resume_decision = validate_log_identity(saved_state, current_log_path)
        logger.info(f"Resume decision: {resume_decision}")

        if resume_decision in ("resume_same_session", "resume_no_identity"):
            resume_offset = saved_state.get("log_offset")
            if saved_state.get("local_seat_id") is not None:
                game_state.set_local_seat_id(saved_state["local_seat_id"], source=2)
            if saved_state.get("match_id"):
                game_state.match_id = saved_state["match_id"]
            logger.info(
                f"Resuming match {saved_state.get('match_id')} "
                f"from offset {resume_offset} ({resume_decision})"
            )
        elif resume_decision == "resume_append_mode_ambiguous":
            # Appendlog mode: allow resume but log a warning
            resume_offset = saved_state.get("log_offset")
            if saved_state.get("local_seat_id") is not None:
                game_state.set_local_seat_id(saved_state["local_seat_id"], source=2)
            if saved_state.get("match_id"):
                game_state.match_id = saved_state["match_id"]
            logger.warning(
                f"Resuming match {saved_state.get('match_id')} "
                f"from offset {resume_offset} — appendlog mode suspected, "
                "offset may be stale"
            )
        else:
            # fresh_log_after_restart, resume_invalid_path, etc.
            logger.info(
                f"Discarding saved resume state: {resume_decision} "
                f"(match={saved_state.get('match_id')}, "
                f"offset={saved_state.get('log_offset')})"
            )

    # Create and start the watcher
    watcher = MTGALogWatcher(
        callback=parser.process_chunk,
        resume_offset=resume_offset,
    )
    watcher.start()
    logger.info("Started MTGA log watcher")


def stop_watching() -> None:
    """Stop watching the MTGA log file."""
    global watcher
    if watcher is None:
        return
    watcher.stop()
    watcher = None
    logger.info("Stopped MTGA log watcher")


def poll_log() -> None:
    """Manually poll for new log content.

    Call this periodically as a backup when watchdog misses file events.
    """
    if watcher is not None:
        watcher.poll()


# Track card enrichment failures for bug reports
_enrichment_failures: list[dict[str, Any]] = []


def get_enrichment_failures() -> list[dict[str, Any]]:
    """Return recent card enrichment failures for bug reports."""
    return list(_enrichment_failures[-50:])


def enrich_with_oracle_text(grp_id: int) -> dict[str, Any]:
    """Look up card data and return enriched dict.

    Delegates to the unified FallbackCardDatabase which tries sources in order:
    1. MTGJSON by arena_id (most complete, updated daily)
    2. MTGA local DB (has newest cards, tokens, digital-only)
    3. Scryfall (API fallback)

    The FallbackCardDatabase also performs cross-source enrichment: if a source
    returns a card without oracle_text, it checks remaining sources by name.

    Args:
        grp_id: MTGA arena_id for the card

    Returns:
        Dict with name, oracle_text, type_line, mana_cost if found,
        or minimal dict with just grp_id if lookup fails (graceful degradation).
    """
    if grp_id == 0:
        return {"grp_id": 0, "name": "Unknown", "oracle_text": "", "type_line": "", "mana_cost": ""}

    card_db = _get_card_db()
    card = card_db.get_card_by_arena_id(grp_id)

    if card is not None:
        return {
            "grp_id": grp_id,
            "name": card.name or f"Unknown ({grp_id})",
            "oracle_text": card.oracle_text or "",
            "type_line": card.type_line or "",
            "mana_cost": card.mana_cost or "",
        }

    # Check if it's an ABILITY (e.g. on stack) via MTGA database
    ability_text = card_db.get_ability_text(grp_id)
    if ability_text:
        return {
            "grp_id": grp_id,
            "name": f"Ability (ID: {grp_id})",
            "oracle_text": ability_text,
            "type_line": "Ability",
            "mana_cost": "",
        }

    # All lookups failed -- record for diagnostics
    from datetime import datetime
    _enrichment_failures.append({
        "timestamp": datetime.now().isoformat(),
        "grp_id": grp_id,
        "source": "all_lookups_failed",
    })
    if len(_enrichment_failures) > 100:
        _enrichment_failures[:] = _enrichment_failures[-50:]
    return {
        "grp_id": grp_id,
        "name": f"Unknown (ID: {grp_id})",
        "oracle_text": "",
        "type_line": "",
        "mana_cost": "",
    }


def _serialize_game_object(obj) -> dict[str, Any]:
    """Serialize a GameObject with oracle text enrichment.

    For abilities on the stack (objects with parent_instance_id), resolves the
    source card to provide context about what triggered or activated.
    """
    enriched = enrich_with_oracle_text(obj.grp_id)

    # Resolve source card for abilities (triggered/activated abilities on stack)
    source_card = None
    if obj.parent_instance_id is not None:
        parent_obj = game_state.game_objects.get(obj.parent_instance_id)
        if parent_obj:
            source_enriched = enrich_with_oracle_text(parent_obj.grp_id)
            source_card = {
                "instance_id": parent_obj.instance_id,
                "grp_id": parent_obj.grp_id,
                "name": source_enriched["name"],
                "oracle_text": source_enriched["oracle_text"],
            }
            # If the ability name is unknown, label it with the source
            if enriched["name"].startswith("Unknown"):
                enriched["name"] = f"Ability of {source_enriched['name']}"

    result = {
        "instance_id": obj.instance_id,
        "grp_id": obj.grp_id,
        "name": enriched.get("name") or f"Unknown ({obj.grp_id})",
        "oracle_text": enriched.get("oracle_text") or "",
        "type_line": enriched.get("type_line") or "",
        "mana_cost": enriched.get("mana_cost") or "",
        "owner_seat_id": obj.owner_seat_id,
        "controller_seat_id": obj.controller_seat_id,
        "power": obj.power,
        "toughness": obj.toughness,
        "is_tapped": obj.is_tapped,
        "turn_entered_battlefield": obj.turn_entered_battlefield,
    }

    if source_card:
        result["source_card"] = source_card

    return result


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Best-effort integer coercion for mixed GRE / JSON payloads."""
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _copy_list(value: Any) -> list[Any]:
    """Return a shallow-copied list for scalar-or-list GRE fields."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _normalize_object_kind(raw_kind: Any, fallback_kind: str = "UNKNOWN") -> str:
    kind = str(raw_kind or fallback_kind or "UNKNOWN")
    return kind.upper()


def _serialize_snapshot_obj(obj: dict[str, Any]) -> dict[str, Any]:
    """Serialize a published-snapshot object with oracle text enrichment."""
    grp_id = int(obj.get("grp_id", 0) or 0)
    enriched = enrich_with_oracle_text(grp_id)
    result = {
        "instance_id": obj.get("instance_id"),
        "grp_id": grp_id,
        "name": enriched.get("name") or f"Unknown ({grp_id})",
        "oracle_text": enriched.get("oracle_text") or "",
        "type_line": enriched.get("type_line") or "",
        "mana_cost": enriched.get("mana_cost") or "",
        "owner_seat_id": obj.get("owner_seat_id"),
        "controller_seat_id": obj.get("controller_seat_id"),
        "power": obj.get("power"),
        "toughness": obj.get("toughness"),
        "is_tapped": obj.get("is_tapped", False),
        "turn_entered_battlefield": obj.get("turn_entered_battlefield", -1),
        "is_attacking": obj.get("is_attacking", False),
        "is_blocking": obj.get("is_blocking", False),
        "card_types": obj.get("card_types", []),
        "subtypes": obj.get("subtypes", []),
        "object_kind": obj.get("object_kind", "UNKNOWN"),
        "counters": obj.get("counters", {}),
    }
    if obj.get("parent_instance_id") is not None:
        result["parent_instance_id"] = obj.get("parent_instance_id")
    # ── Phase 1 turbo-charge fields (only include when set) ──
    for key in ("modified_power", "modified_toughness", "modified_cost",
                "modified_colors", "modified_types", "modified_name",
                "granted_abilities", "removed_abilities",
                "damaged_this_turn", "crewed_this_turn", "saddled_this_turn",
                "is_phased_out", "class_level", "copied_from_grp_id",
                "targeting", "color_production"):
        val = obj.get(key)
        if val:  # Only include truthy values
            result[key] = copy.deepcopy(val)
    return result


def _build_snapshot_object_lookup(zones: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index current snapshot objects by instance id for bridge overlay fallbacks."""
    lookup: dict[int, dict[str, Any]] = {}
    for zone_name in ("battlefield", "my_hand", "graveyard", "stack", "exile", "command"):
        for obj in zones.get(zone_name, []) or []:
            instance_id = _coerce_int(obj.get("instance_id"))
            if instance_id is not None:
                lookup[instance_id] = obj
    return lookup


def _serialize_bridge_card(
    card: dict[str, Any],
    snapshot_lookup: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Normalize a bridge card payload to the public server schema."""
    instance_id = _coerce_int(card.get("instance_id"))
    fallback = snapshot_lookup.get(instance_id, {}) if instance_id is not None else {}

    fallback_grp_id = _coerce_int(fallback.get("grp_id"), 0) or 0
    grp_id = _coerce_int(card.get("grp_id"), fallback_grp_id) or 0
    enriched = enrich_with_oracle_text(grp_id)

    result = {
        "instance_id": instance_id,
        "grp_id": grp_id,
        "name": enriched.get("name") or fallback.get("name") or f"Unknown ({grp_id})",
        "oracle_text": enriched.get("oracle_text") or fallback.get("oracle_text") or "",
        "type_line": enriched.get("type_line") or fallback.get("type_line") or "",
        "mana_cost": enriched.get("mana_cost") or fallback.get("mana_cost") or "",
        "owner_seat_id": _coerce_int(card.get("owner_id"), fallback.get("owner_seat_id")),
        "controller_seat_id": _coerce_int(card.get("controller_id"), fallback.get("controller_seat_id")),
        "power": _coerce_int(card.get("power"), fallback.get("power")),
        "toughness": _coerce_int(card.get("toughness"), fallback.get("toughness")),
        "is_tapped": bool(card.get("is_tapped", fallback.get("is_tapped", False))),
        "turn_entered_battlefield": _coerce_int(fallback.get("turn_entered_battlefield"), -1),
        "is_attacking": bool(card.get("is_attacking", fallback.get("is_attacking", False))),
        "is_blocking": bool(card.get("is_blocking", fallback.get("is_blocking", False))),
        "card_types": _copy_list(card.get("card_types") or fallback.get("card_types")),
        "subtypes": _copy_list(card.get("subtypes") or fallback.get("subtypes")),
        "object_kind": _normalize_object_kind(card.get("object_type"), fallback.get("object_kind", "UNKNOWN")),
        "counters": dict(card.get("counters") or fallback.get("counters") or {}),
    }

    if card.get("parent_instance_id") is not None or fallback.get("parent_instance_id") is not None:
        result["parent_instance_id"] = _coerce_int(
            card.get("parent_instance_id"),
            fallback.get("parent_instance_id"),
        )

    targeting = _copy_list(card.get("target_ids") or fallback.get("targeting"))
    if targeting:
        result["targeting"] = [value for value in targeting if value is not None]

    for key in ("modified_power", "modified_toughness", "modified_cost",
                "modified_colors", "modified_types", "modified_name",
                "granted_abilities", "removed_abilities",
                "damaged_this_turn", "crewed_this_turn", "saddled_this_turn",
                "is_phased_out", "class_level", "copied_from_grp_id",
                "color_production"):
        fallback_value = fallback.get(key)
        if fallback_value:
            result[key] = copy.deepcopy(fallback_value)

    if card.get("loyalty") is not None:
        result["loyalty"] = _coerce_int(card.get("loyalty"))
    if card.get("defense") is not None:
        result["defense"] = _coerce_int(card.get("defense"))
    if card.get("damage") is not None:
        result["damage"] = _coerce_int(card.get("damage"))
    if card.get("attack_target_id") is not None:
        result["attack_target_id"] = _coerce_int(card.get("attack_target_id"))

    colors = _copy_list(card.get("colors") or fallback.get("colors"))
    if colors:
        result["colors"] = colors

    color_production = _copy_list(card.get("color_production"))
    if color_production:
        result["color_production"] = color_production

    attached_to_id = _coerce_int(card.get("attached_to_id"))
    if attached_to_id:
        result["attached_to_id"] = attached_to_id

    attached_with_ids = _copy_list(card.get("attached_with_ids"))
    if attached_with_ids:
        result["attached_with_ids"] = [value for value in attached_with_ids if value is not None]

    visibility = card.get("visibility")
    if visibility:
        result["visibility"] = visibility
    if card.get("summoning_sickness"):
        result["summoning_sickness"] = True
    if card.get("revealed_to_opponent"):
        result["revealed_to_opponent"] = True
    if card.get("face_down"):
        result["face_down"] = True

    return result


def _normalize_bridge_turn(bridge_turn: dict[str, Any], fallback_turn: dict[str, Any]) -> dict[str, Any]:
    """Map the bridge turn payload into the public server turn schema."""
    turn = {
        "turn_number": _coerce_int(bridge_turn.get("turn_number"), fallback_turn.get("turn_number", 0)) or 0,
        "active_player": _coerce_int(bridge_turn.get("active_player"), fallback_turn.get("active_player", 0)) or 0,
        "priority_player": _coerce_int(
            bridge_turn.get("deciding_player", bridge_turn.get("priority_player")),
            fallback_turn.get("priority_player", 0),
        ) or 0,
        "phase": bridge_turn.get("phase") or fallback_turn.get("phase", ""),
        "step": bridge_turn.get("step") or fallback_turn.get("step", ""),
        "pending_combat_steps": list(fallback_turn.get("pending_combat_steps", [])),
    }
    stage = bridge_turn.get("stage")
    if stage:
        turn["stage"] = stage
    return turn


def _normalize_bridge_players(
    bridge_players: list[dict[str, Any]],
    snapshot_players: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge bridge player data over the published snapshot players list."""
    snapshot_by_seat = {
        _coerce_int(player.get("seat_id")): dict(player)
        for player in snapshot_players
        if _coerce_int(player.get("seat_id")) is not None
    }

    normalized: list[dict[str, Any]] = []
    for player in bridge_players:
        seat_id = _coerce_int(player.get("seat_id"))
        merged = dict(snapshot_by_seat.get(seat_id, {}))
        if seat_id is not None:
            merged["seat_id"] = seat_id
        if player.get("life_total") is not None:
            merged["life_total"] = _coerce_int(player.get("life_total"), merged.get("life_total", 0)) or 0
        if "is_local" in player:
            merged["is_local"] = bool(player.get("is_local"))

        mana_pool = player.get("mana_pool")
        if isinstance(mana_pool, dict):
            merged["mana_pool"] = dict(mana_pool)
        elif "mana_pool" not in merged:
            merged["mana_pool"] = {}

        for key in ("status", "mulligan_count", "timeout_count", "team_id", "lands_played"):
            if player.get(key) is not None:
                merged[key] = copy.deepcopy(player.get(key))

        if player.get("commander_ids"):
            merged["commander_ids"] = list(player.get("commander_ids", []))
        if player.get("dungeon"):
            merged["dungeon"] = copy.deepcopy(player.get("dungeon"))
        if player.get("designations"):
            merged["designations"] = list(player.get("designations", []))

        normalized.append(merged)

    return normalized


def _derive_seat_ids(
    bridge_state: dict[str, Any],
    players: list[dict[str, Any]],
    fallback_local_seat_id: Optional[int],
    fallback_opponent_seat_id: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    """Derive local/opponent seat ids from bridge state with snapshot fallback."""
    local_seat_id = _coerce_int(bridge_state.get("local_seat_id"), fallback_local_seat_id)
    opponent_seat_id = _coerce_int(bridge_state.get("opponent_seat_id"), fallback_opponent_seat_id)

    if local_seat_id is None:
        for player in players:
            if player.get("is_local"):
                local_seat_id = _coerce_int(player.get("seat_id"))
                if local_seat_id is not None:
                    break

    if opponent_seat_id is None:
        for player in players:
            seat_id = _coerce_int(player.get("seat_id"))
            if seat_id is not None and seat_id != local_seat_id:
                opponent_seat_id = seat_id
                break

    return local_seat_id, opponent_seat_id


def _normalize_bridge_timer_state(bridge_timer_state: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Map bridge player timer payloads to the snapshot timer_state schema."""
    normalized: dict[int, dict[str, Any]] = {}
    player_timers = bridge_timer_state.get("player_timers")
    if not isinstance(player_timers, dict):
        return normalized

    for seat_key, timers in player_timers.items():
        seat_id = _coerce_int(seat_key)
        if seat_id is None:
            continue
        timer_entries = [entry for entry in _copy_list(timers) if isinstance(entry, dict)]
        if not timer_entries:
            continue

        chosen = next((entry for entry in timer_entries if entry.get("running")), timer_entries[0])
        duration_sec = _coerce_int(chosen.get("duration_sec"), 0) or 0
        elapsed_sec = _coerce_int(chosen.get("elapsed_sec"), 0) or 0
        remaining_ms = max(0, (duration_sec - elapsed_sec) * 1000)

        normalized[seat_id] = {
            "time_remaining_ms": remaining_ms,
            "timer_type": chosen.get("type") or chosen.get("timer_type") or "",
            "behavior": chosen.get("behavior") or "",
            "is_ticking": bool(chosen.get("running", chosen.get("is_ticking", False))),
        }

    return normalized


def _build_public_zones(
    *,
    battlefield: list[dict[str, Any]],
    hand: list[dict[str, Any]],
    graveyard: list[dict[str, Any]],
    stack: list[dict[str, Any]],
    exile: list[dict[str, Any]],
    command: list[dict[str, Any]],
    opponent_hand_count: Any,
    library_count: Any,
    local_graveyard: Optional[list[dict[str, Any]]] = None,
    opponent_graveyard: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build the nested zones payload used by older consumers."""
    zones = {
        "battlefield": battlefield,
        "my_hand": hand,
        "hand": hand,
        "graveyard": graveyard,
        "stack": stack,
        "exile": exile,
        "command": command,
        "opponent_hand_count": opponent_hand_count,
        "library_count": library_count,
    }
    if local_graveyard is not None:
        zones["local_graveyard"] = local_graveyard
    if opponent_graveyard is not None:
        zones["opponent_graveyard"] = opponent_graveyard
    return zones


def _get_bridge_overlay(
    fallback_turn: dict[str, Any],
    fallback_players: list[dict[str, Any]],
    fallback_zones: dict[str, Any],
    fallback_local_seat_id: Optional[int],
    fallback_opponent_seat_id: Optional[int],
) -> dict[str, Any]:
    """Return bridge-authoritative visible state that can safely overlay the snapshot."""
    try:
        from arenamcp.gre_bridge import get_bridge
    except Exception:
        return {}

    bridge = get_bridge()
    bridge_state = bridge.get_game_state()

    overlay = {
        "bridge_connected": bool(getattr(bridge, "connected", False)),
    }

    if not bridge_state:
        return overlay

    snapshot_lookup = _build_snapshot_object_lookup(fallback_zones)
    overlay["bridge_connected"] = True

    bridge_players = [
        player for player in _copy_list(bridge_state.get("players"))
        if isinstance(player, dict)
    ]
    normalized_players = _normalize_bridge_players(bridge_players, fallback_players) if bridge_players else []
    local_seat_id, opponent_seat_id = _derive_seat_ids(
        bridge_state,
        normalized_players or fallback_players,
        fallback_local_seat_id,
        fallback_opponent_seat_id,
    )

    bridge_zones = bridge_state.get("zones") if isinstance(bridge_state.get("zones"), dict) else {}

    def _bridge_zone_cards(name: str) -> list[dict[str, Any]]:
        zone = bridge_zones.get(name, {})
        if not isinstance(zone, dict):
            return []
        cards = zone.get("cards")
        return [
            _serialize_bridge_card(card, snapshot_lookup)
            for card in _copy_list(cards)
            if isinstance(card, dict)
        ]

    battlefield = [
        card for card in _bridge_zone_cards("battlefield")
        if card.get("object_kind") != "ABILITY"
    ]
    hand = _bridge_zone_cards("local_hand")
    local_graveyard = _bridge_zone_cards("local_graveyard")
    opponent_graveyard = _bridge_zone_cards("opponent_graveyard")
    graveyard = local_graveyard + opponent_graveyard
    stack = _bridge_zone_cards("stack")
    exile = _bridge_zone_cards("exile")
    command = _bridge_zone_cards("command")

    timer_state = {}
    bridge_timer_state = bridge.get_timer_state()
    if bridge_timer_state:
        timer_state = _normalize_bridge_timer_state(bridge_timer_state)

    opponent_hand_zone = bridge_zones.get("opponent_hand", {})
    local_library_zone = bridge_zones.get("local_library", {})
    opponent_hand_count = (
        opponent_hand_zone.get("total_count")
        if isinstance(opponent_hand_zone, dict) and opponent_hand_zone.get("total_count") is not None
        else fallback_zones.get("opponent_hand_count", 0)
    )
    library_count = (
        local_library_zone.get("total_count")
        if isinstance(local_library_zone, dict) and local_library_zone.get("total_count") is not None
        else fallback_zones.get("library_count", "?")
    )

    overlay.update(
        {
            "turn": _normalize_bridge_turn(
                bridge_state.get("turn") if isinstance(bridge_state.get("turn"), dict) else {},
                fallback_turn,
            ),
            "players": normalized_players or fallback_players,
            "battlefield": battlefield,
            "hand": hand,
            "graveyard": graveyard,
            "stack": stack,
            "exile": exile,
            "command": command,
            "local_seat_id": local_seat_id,
            "opponent_seat_id": opponent_seat_id,
            "zones": _build_public_zones(
                battlefield=battlefield,
                hand=hand,
                graveyard=graveyard,
                stack=stack,
                exile=exile,
                command=command,
                opponent_hand_count=opponent_hand_count,
                library_count=library_count,
                local_graveyard=local_graveyard,
                opponent_graveyard=opponent_graveyard,
            ),
        }
    )
    if timer_state:
        overlay["timer_state"] = timer_state
    pending_interaction = bridge_state.get("pending_interaction")
    if pending_interaction:
        overlay["bridge_pending_interaction"] = pending_interaction

    return overlay


# MCP Tools

@mcp.tool()
def get_game_state() -> dict[str, Any]:
    """Get the complete current game state snapshot.

    Returns the full board state including turn info, player life totals,
    and all cards in each zone (battlefield, hand, graveyard, stack, exile).
    Each card includes oracle text for strategic analysis.

    Use this to understand the current game situation before providing advice.
    Call periodically during a game to track state changes.

    Returns:
        Dict with structure:
        - turn: {turn_number, active_player, priority_player, phase, step}
        - players: [{seat_id, life_total, mana_pool, is_local}]
        - battlefield: [card objects with oracle text]
        - hand: [card objects - local player only]
        - graveyard: [card objects]
        - stack: [card objects]
        - exile: [card objects]
    """
    # Auto-start watcher if not running
    if watcher is None:
        start_watching()

    # Ensure local player is detected before serializing
    game_state.ensure_local_seat_id()

    # Read from published immutable snapshot to avoid mixed-frame reads
    snap = game_state.get_published_snapshot(deep_copy=False)
    turn_info = snap.get("turn_info", {})
    zones = snap.get("zones", {})
    players = copy.deepcopy(snap.get("players", []))

    turn = {
        "turn_number": turn_info.get("turn_number", 0),
        "active_player": turn_info.get("active_player", 0),
        "priority_player": turn_info.get("priority_player", 0),
        "phase": turn_info.get("phase", ""),
        "step": turn_info.get("step", ""),
        "pending_combat_steps": list(snap.get("pending_combat_steps", [])),
    }

    battlefield = [
        _serialize_snapshot_obj(o)
        for o in zones.get("battlefield", [])
        if o.get("object_kind") != "ABILITY"
    ]
    hand = [_serialize_snapshot_obj(o) for o in zones.get("my_hand", [])]
    graveyard = [_serialize_snapshot_obj(o) for o in zones.get("graveyard", [])]
    stack = [_serialize_snapshot_obj(o) for o in zones.get("stack", [])]
    exile = [_serialize_snapshot_obj(o) for o in zones.get("exile", [])]
    command = [_serialize_snapshot_obj(o) for o in zones.get("command", [])]

    # Save match state on turn changes for recovery
    _save_match_state_if_needed()

    local_seat_id = snap.get("local_seat_id")
    opponent_seat_id = snap.get("opponent_seat_id")
    decision_seat_id = snap.get("decision_seat_id")
    decision_context = snap.get("decision_context")
    if decision_context is not None:
        decision_context = copy.deepcopy(decision_context)
    legal_actions_raw = copy.deepcopy(snap.get("legal_actions_raw", []))
    recent_events = copy.deepcopy(snap.get("recent_events", []))
    raw_gre_events = copy.deepcopy(snap.get("raw_gre_events", []))

    response = {
        "match_id": snap.get("match_id"),
        "turn": turn,
        "players": players,
        "battlefield": battlefield,
        "hand": hand,
        "graveyard": graveyard,
        "stack": stack,
        "exile": exile,
        "command": command,
        "local_seat_id": local_seat_id,
        "opponent_seat_id": opponent_seat_id,
        "zones": _build_public_zones(
            battlefield=battlefield,
            hand=hand,
            graveyard=graveyard,
            stack=stack,
            exile=exile,
            command=command,
            opponent_hand_count=zones.get("opponent_hand_count", 0),
            library_count=zones.get("library_count", "?"),
        ),
        "pending_decision": None,
        "decision_context": None,
        "decision_seat_id": decision_seat_id,
        "last_cleared_decision": snap.get("last_cleared_decision"),
        "legal_actions": list(snap.get("legal_actions", [])),
        "legal_actions_raw": legal_actions_raw,
        "recent_events": recent_events,
        "raw_gre_events": raw_gre_events,
        "raw_gre_event_count": snap.get("raw_gre_event_count", len(raw_gre_events)),
        "deck_cards": list(snap.get("deck_cards", [])),
        "damage_taken": dict(snap.get("damage_taken", {})),
        # ── Phase 1 turbo-charge fields ──
        "designations": copy.deepcopy(snap.get("designations", {})),
        "dungeon_status": copy.deepcopy(snap.get("dungeon_status", {})),
        "timer_state": copy.deepcopy(snap.get("timer_state", {})),
        "action_history": copy.deepcopy(snap.get("action_history", [])),
        "sideboard_cards": copy.deepcopy(snap.get("sideboard_cards", [])),
        # ── Bridge decision detection fields ──
        "bridge_connected": snap.get("_bridge_connected", False),
        "bridge_request_type": snap.get("_bridge_request_type"),
        "_bridge_connected": snap.get("_bridge_connected", False),
        "_bridge_request_type": snap.get("_bridge_request_type"),
        "_bridge_request_class": snap.get("_bridge_request_class"),
        "_bridge_actions": copy.deepcopy(snap.get("_bridge_actions")),
        "_bridge_can_pass": snap.get("_bridge_can_pass"),
        "_bridge_can_cancel": snap.get("_bridge_can_cancel"),
        "_bridge_allow_undo": snap.get("_bridge_allow_undo"),
        "_bridge_request_payload": copy.deepcopy(snap.get("_bridge_request_payload")),
    }

    bridge_overlay = _get_bridge_overlay(
        fallback_turn=turn,
        fallback_players=players,
        fallback_zones=zones,
        fallback_local_seat_id=local_seat_id,
        fallback_opponent_seat_id=opponent_seat_id,
    )
    if bridge_overlay:
        response.update(bridge_overlay)
        response["_bridge_connected"] = response["bridge_connected"]

    effective_local_seat_id = response.get("local_seat_id")
    if decision_seat_id is None and snap.get("pending_decision"):
        decision_seat_id = effective_local_seat_id
    if decision_seat_id == effective_local_seat_id:
        response["pending_decision"] = snap.get("pending_decision")
        response["decision_context"] = decision_context

    return response

def clear_pending_combat_steps() -> None:
    """Clear pending combat steps after they've been processed.

    Call this after checking triggers to prevent duplicate combat triggers.
    """
    game_state.clear_pending_combat_steps()


@mcp.tool()
def get_card_info(arena_id: int) -> dict[str, Any]:
    """Look up detailed card information by MTGA arena ID.

    Use this to get oracle text, mana cost, and other card details
    when you need to understand what a specific card does.

    Uses the unified FallbackCardDatabase which tries MTGJSON, MTGA local DB,
    and Scryfall in order, enriching results across sources as needed.

    Args:
        arena_id: The MTGA arena ID (grp_id) of the card

    Returns:
        Dict with name, oracle_text, type_line, mana_cost, cmc, colors, scryfall_uri
        or {"error": "Card not found"} if the card isn't in any database.
    """
    card_db = _get_card_db()
    card = card_db.get_card_by_arena_id(arena_id)

    if card is None:
        return {"error": f"Card not found for arena_id {arena_id}"}

    return {
        "name": card.name or f"Unknown ({arena_id})",
        "oracle_text": card.oracle_text or "",
        "type_line": card.type_line or "",
        "mana_cost": card.mana_cost or "",
        "cmc": card.cmc or 0,
        "colors": card.colors or [],
        "scryfall_uri": card.scryfall_uri or None,
    }


@mcp.tool()
def get_opponent_played_cards() -> list[dict[str, Any]]:
    """Get all cards the opponent has revealed this game.

    Tracks cards as they move from library to other zones (hand, battlefield,
    graveyard, etc.), building a picture of opponent's deck composition.

    Use this to:
    - Understand what cards opponent has access to
    - Predict what they might play based on revealed cards
    - Identify deck archetype from card patterns

    Returns:
        List of card dicts with grp_id, name, oracle_text, type_line, mana_cost.
        Empty list if no opponent or no cards revealed yet.
    """
    # Auto-start watcher if not running
    if watcher is None:
        start_watching()

    grp_ids = game_state.get_opponent_played_cards()

    cards = []
    for grp_id in grp_ids:
        enriched = enrich_with_oracle_text(grp_id)
        # Filter out ability objects (type_line == "Ability") — they aren't real cards
        if enriched.get("type_line", "").lower() == "ability":
            continue
        cards.append(enriched)

    return cards


@mcp.tool()
def get_draft_rating(card_name: str, set_code: str) -> dict[str, Any]:
    """Get 17lands draft statistics for a card.

    Provides win rate and pick order data from 17lands.com to help evaluate
    cards during draft. Data is from Premier Draft format.

    Args:
        card_name: The card name (case-insensitive)
        set_code: The set code (e.g., 'DSK', 'BLB', 'MKM')

    Returns:
        Dict with:
        - name: Card name
        - set_code: Set code
        - gih_wr: Games in Hand Win Rate (0.0-1.0, e.g., 0.55 = 55%)
        - alsa: Average Last Seen At (pick position)
        - iwd: Improvement When Drawn
        - games_in_hand: Sample size for statistics

        or {"error": "Card not found"} if not found.
    """
    draft_stats = _get_draft_stats()
    stats = draft_stats.get_draft_rating(card_name, set_code)

    if stats is None:
        return {"error": f"Card '{card_name}' not found in {set_code} draft data"}

    return {
        "name": stats.name,
        "set_code": stats.set_code,
        "gih_wr": stats.gih_wr,
        "alsa": stats.alsa,
        "iwd": stats.iwd,
        "games_in_hand": stats.games_in_hand,
    }


@mcp.tool()
def get_draft_pack() -> dict[str, Any]:
    """Get the current draft pack contents with card details and 17lands ratings.

    Reads the MTGA log to determine what cards are currently in the draft pack.
    Returns card names, types, and 17lands statistics for each card.

    Use this during a draft to see what cards are available and their ratings.

    Returns:
        Dict with:
        - is_active: Whether a draft is currently in progress
        - event_name: The draft event name (e.g., "PremierDraft_MH3")
        - set_code: The set being drafted (e.g., "MH3")
        - pack_number: Current pack number (1-3)
        - pick_number: Current pick within the pack (1-15)
        - cards: List of card dicts with:
          - grp_id: Arena card ID
          - name: Card name
          - type_line: Card type
          - mana_cost: Mana cost
          - gih_wr: Games in Hand Win Rate (0.0-1.0)
          - alsa: Average Last Seen At
          - iwd: Improvement When Drawn
        - picked_cards: List of cards already picked this draft

        or {"is_active": False, "message": "..."} if no draft in progress.
    """
    # Auto-start watcher if not running
    if watcher is None:
        start_watching()

    if not draft_state.is_active or not draft_state.cards_in_pack:
        return {
            "is_active": False,
            "message": "No active draft pack detected. Make sure you're in a draft and a pack is open."
        }

    # Get draft stats cache
    draft_stats_cache = _get_draft_stats()

    # Build card list with details and ratings
    cards = []
    for grp_id in draft_state.cards_in_pack:
        # Use robust enrichment (MTGJSON -> MTGADb -> Scryfall)
        card_info = enrich_with_oracle_text(grp_id)

        # Get 17lands stats if we have a set code and valid name
        if draft_state.set_code and "Unknown" not in card_info["name"]:
            stats = draft_stats_cache.get_draft_rating(
                card_info["name"], draft_state.set_code
            )
            if stats:
                card_info["gih_wr"] = stats.gih_wr
                card_info["alsa"] = stats.alsa
                card_info["iwd"] = stats.iwd

        cards.append(card_info)

    # Sort by GIH win rate (highest first)
    cards.sort(key=lambda c: c.get("gih_wr") or 0, reverse=True)

    # Get picked card details
    picked = []
    for grp_id in draft_state.picked_cards:
        enriched = enrich_with_oracle_text(grp_id)
        picked.append(enriched)

    return {
        "is_active": True,
        "is_sealed": draft_state.is_sealed,
        "event_name": draft_state.event_name,
        "set_code": draft_state.set_code,
        "pack_number": draft_state.pack_number,
        "pick_number": draft_state.pick_number,
        "picks_per_pack": draft_state.picks_per_pack,
        "cards": cards,
        "picked_cards": picked,
    }


def evaluate_draft_pack_for_standalone() -> dict[str, Any]:
    """Evaluate current draft pack using composite scoring (colors, synergy, win rate).

    Uses evaluate_pack() which factors in:
    - 17lands GIH win rate
    - On-color bonus based on already-picked cards
    - Synergy with picked cards (tribal, mechanic, name references)
    - Card type/mechanic value (removal, card draw, evasion, etc.)

    Returns:
        Dict with pack_number, pick_number, spoken_advice, and evaluations list,
        or {"is_active": False} if no draft in progress.
    """
    if not draft_state.is_active or draft_state.is_sealed:
        return {"is_active": False}

    if not draft_state.cards_in_pack:
        return {"is_active": True, "cards": []}

    _scryfall = _get_scryfall()
    _draft_stats = _get_draft_stats()
    _mtga = _get_mtgadb()

    # Ensure synergy graph is built (auto-builds on first draft if missing)
    try:
        from arenamcp.synergy import ensure_synergy_graph
        ensure_synergy_graph(_scryfall)
    except Exception as e:
        logger.debug(f"Synergy graph init: {e}")

    evaluations = evaluate_pack(
        cards_in_pack=draft_state.cards_in_pack,
        picked_cards=draft_state.picked_cards,
        set_code=draft_state.set_code,
        scryfall=_scryfall,
        draft_stats=_draft_stats,
        mtgadb=_mtga,
    )

    advice = format_pick_recommendation(
        evaluations,
        draft_state.pack_number,
        draft_state.pick_number,
        num_recommendations=draft_state.picks_per_pack,
    )

    return {
        "is_active": True,
        "pack_number": draft_state.pack_number,
        "pick_number": draft_state.pick_number,
        "picks_per_pack": draft_state.picks_per_pack,
        "spoken_advice": advice,
        "evaluations": [
            {
                "name": e.name,
                "score": e.score,
                "gih_wr": e.gih_wr,
                "reason": e.reason,
                "all_reasons": e.all_reasons,
            }
            for e in evaluations[:5]  # Top 5 for display
        ],
        "picked_count": len(draft_state.picked_cards),
    }


def get_sealed_pool() -> dict[str, Any]:
    """Get sealed pool analysis with deck building recommendations.

    Analyzes the sealed pool using 17lands win rate data to suggest
    the best color combinations and cards to build around.

    Returns:
        Dict with:
        - is_sealed: True if this is a sealed event
        - set_code: The set code
        - pool_size: Number of cards in pool
        - analysis: Detailed analysis dict with:
          - recommended_colors: Best color pair (e.g., "White/Green")
          - recommended_wr: Average win rate of recommended build
          - playable_count: Number of playable cards in recommended colors
          - creature_count: Number of creatures in recommended colors
          - top_cards: List of best cards to build around
          - splash_candidates: High WR off-color cards to consider
          - alternatives: Other viable color pairs
        - spoken_advice: Short spoken recommendation
        - detailed_text: Full text breakdown for display

        or {"is_sealed": False} if not in a sealed event.
    """
    if not draft_state.is_active or not draft_state.is_sealed:
        return {"is_sealed": False, "message": "No active sealed event detected."}

    # In sealed, picked_cards contains the full pool after opening packs
    pool_grp_ids = draft_state.picked_cards
    if not pool_grp_ids:
        return {"is_sealed": True, "message": "Sealed pool not yet opened. Open your packs first."}

    # Get card details with 17lands ratings
    draft_stats_cache = _get_draft_stats()
    pool_cards = []

    for grp_id in pool_grp_ids:
        card_info = enrich_with_oracle_text(grp_id)
        card_info["grp_id"] = grp_id

        # Parse colors from mana cost
        mana_cost = card_info.get("mana_cost", "")
        colors = []
        for color in ["W", "U", "B", "R", "G"]:
            if f"{{{color}}}" in mana_cost:
                colors.append(color)
        card_info["colors"] = colors

        # Get 17lands stats
        if draft_state.set_code and card_info.get("name"):
            stats = draft_stats_cache.get_draft_rating(
                card_info["name"], draft_state.set_code
            )
            if stats:
                card_info["gih_wr"] = stats.gih_wr
                card_info["alsa"] = stats.alsa
                card_info["iwd"] = stats.iwd

        pool_cards.append(card_info)

    # Run analysis
    analysis = analyze_sealed_pool(pool_cards, draft_state.set_code, draft_stats_cache)

    # Format recommendations
    spoken = format_sealed_recommendation(analysis)
    detailed = format_sealed_detailed(analysis)

    # Build response
    rec = analysis.recommended_build
    color_names = {
        "W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"
    }
    rec_colors = "/".join(color_names.get(c, c) for c in rec.colors) if rec.colors else "Unknown"

    return {
        "is_sealed": True,
        "set_code": draft_state.set_code,
        "pool_size": len(pool_cards),
        "analysis": {
            "recommended_colors": rec_colors,
            "recommended_wr": rec.avg_win_rate,
            "playable_count": rec.playable_count,
            "creature_count": rec.creature_count,
            "top_cards": [{"name": c.get("name"), "gih_wr": c.get("gih_wr")} for c in rec.best_cards],
            "splash_candidates": [{"name": c.get("name"), "colors": c.get("colors"), "gih_wr": c.get("gih_wr")} for c in analysis.splash_candidates],
            "alternatives": [
                {
                    "colors": "/".join(color_names.get(c, c) for c in ca.colors),
                    "playable_count": ca.playable_count,
                    "avg_wr": ca.avg_win_rate,
                }
                for ca in analysis.color_analyses[1:4]  # Top 3 alternatives
            ],
        },
        "spoken_advice": spoken,
        "detailed_text": detailed,
    }


def analyze_draft_pool() -> dict[str, Any]:
    """Analyze drafted cards and recommend a deck build.

    Uses the same color-pair analysis as sealed pools to suggest
    the best build from the cards picked during a draft.

    Returns:
        Dict with pool_size, spoken_advice, detailed_text,
        or {"pool_size": 0} if no picked cards.
    """
    pool_grp_ids = draft_state.picked_cards
    if not pool_grp_ids:
        return {"pool_size": 0, "message": "No picked cards to analyze."}

    draft_stats_cache = _get_draft_stats()
    pool_cards = []

    for grp_id in pool_grp_ids:
        card_info = enrich_with_oracle_text(grp_id)
        card_info["grp_id"] = grp_id

        # Parse colors from mana cost
        mana_cost = card_info.get("mana_cost", "")
        colors = []
        for color in ["W", "U", "B", "R", "G"]:
            if f"{{{color}}}" in mana_cost:
                colors.append(color)
        card_info["colors"] = colors

        # Get 17lands stats
        if draft_state.set_code and card_info.get("name"):
            stats = draft_stats_cache.get_draft_rating(
                card_info["name"], draft_state.set_code
            )
            if stats:
                card_info["gih_wr"] = stats.gih_wr
                card_info["alsa"] = stats.alsa
                card_info["iwd"] = stats.iwd

        pool_cards.append(card_info)

    analysis = analyze_sealed_pool(pool_cards, draft_state.set_code, draft_stats_cache)
    spoken = format_sealed_recommendation(analysis)
    detailed = format_sealed_detailed(analysis)

    return {
        "pool_size": len(pool_cards),
        "set_code": draft_state.set_code,
        "spoken_advice": spoken,
        "detailed_text": detailed,
    }


@mcp.tool()
def reset_game_state() -> dict[str, Any]:
    """Reset game state tracking for a new game.

    Clears local player detection and forces re-inference from hand zones.
    Use this if player detection is wrong or when starting a new game.

    Returns:
        Dict with:
        - reset: True
        - message: Confirmation message
    """
    game_state.reset_local_player()
    return {
        "reset": True,
        "message": "Game state reset. Local player will be re-inferred from next hand zone update."
    }


@mcp.tool()
def listen_for_voice(
    mode: Literal["ptt", "vox"] = "ptt",
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Listen for voice input and return transcription.

    Blocks until voice is captured and transcribed. In PTT mode, waits for
    the user to press and release F4. In VOX mode, waits for voice activity.

    Use this to get spoken commands or questions from the user during gameplay.

    Args:
        mode: Voice input mode. 'ptt' (default) for push-to-talk with F4 key,
             'vox' for voice-activated detection.
        timeout: Maximum time to wait in seconds. None (default) waits forever.

    Returns:
        Dict with:
        - transcription: The transcribed text from speech
        - mode: The voice mode used ('ptt' or 'vox')

        or {"error": message} if timeout or failure occurs.
    """
    try:
        voice_input = _get_voice_input(mode)
        voice_input.start()
        transcription = voice_input.wait_for_speech(timeout=timeout)
        voice_input.stop()

        if not transcription:
            return {"error": "No speech detected or timeout", "mode": mode}

        return {"transcription": transcription, "mode": mode}
    except Exception as e:
        logger.exception("Voice input error")
        return {"error": str(e), "mode": mode}


@mcp.tool()
def speak_advice(text: str) -> dict[str, Any]:
    """Speak text using text-to-speech synthesis.

    Synthesizes the provided text and plays it through the audio output.
    Blocks until playback is complete.

    Use this to give spoken coaching advice to the player during games.

    Args:
        text: The text to synthesize and speak.

    Returns:
        Dict with:
        - spoken: True if speech completed successfully
        - text: The text that was spoken

        or {"error": message} if TTS fails (e.g., missing model files).
    """
    try:
        voice_output = _get_voice_output()
        voice_output.speak(text, blocking=True)
        return {"spoken": True, "text": text}
    except FileNotFoundError as e:
        # Missing Kokoro model files - provide helpful error
        return {"error": f"TTS model not found: {e}", "spoken": False}
    except Exception as e:
        logger.exception("TTS error")
        return {"error": str(e), "spoken": False}


def queue_advice(advice: str, trigger: str) -> None:
    """Queue proactive coaching advice for later retrieval.

    Internal function called by background monitoring (Phase 9) when
    game state triggers fire. Advice is queued for retrieval by
    get_pending_advice().

    Args:
        advice: The coaching advice text.
        trigger: Description of what triggered this advice.
    """
    _pending_advice.append({
        "advice": advice,
        "trigger": trigger,
        "timestamp": time.time(),
    })
    logger.debug(f"Queued advice: {trigger}")


@mcp.tool()
def get_pending_advice() -> dict[str, Any]:
    """Get all pending proactive coaching advice.

    Returns and clears all advice that has been queued by the background
    game state monitor. Each advice item includes the trigger that caused it.

    Use this to poll for proactive coaching suggestions during gameplay.

    Returns:
        Dict with:
        - advice_items: List of advice dicts, each with:
          - advice: The coaching advice text
          - trigger: What triggered this advice
          - timestamp: When the advice was generated (Unix timestamp)
        - count: Number of advice items returned
    """
    items = []
    while _pending_advice:
        try:
            items.append(_pending_advice.popleft())
        except IndexError:
            break  # Queue emptied by another thread

    return {"advice_items": items, "count": len(items)}


@mcp.tool()
def clear_pending_advice() -> dict[str, Any]:
    """Clear all pending proactive coaching advice.

    Removes all queued advice without returning it. Use this to reset
    the advice queue, for example when starting a new game.

    Returns:
        Dict with:
        - cleared: True
        - count: Number of items that were cleared
    """
    count = len(_pending_advice)
    _pending_advice.clear()
    return {"cleared": True, "count": count}


@mcp.tool()
def start_coaching(
    backend: str = "auto",
    model: Optional[str] = None,
    auto_speak: bool = False
) -> dict[str, Any]:
    """Start background game monitoring with proactive coaching.

    Begins a background loop that monitors game state changes and generates
    coaching advice when triggers fire (new turn, combat, low life, etc.).
    Advice is queued and can be retrieved with get_pending_advice().

    Args:
        backend: LLM mode to use for advice generation.
            Options: "online", "local", or "auto" (default)
        model: Optional model name to use. Defaults vary by mode.
        auto_speak: If True, automatically speak advice via TTS when generated.
            Default False - retrieve advice manually with get_pending_advice().

    Returns:
        Dict with:
        - started: True if coaching started successfully
        - backend: The LLM mode being used
        - model: The model being used (or None for default)
        - auto_speak: Whether auto-speak is enabled

        or {"error": message} if already running or mode invalid.
    """
    try:
        start_background_coaching(backend=backend, model=model, auto_speak=auto_speak)
        return {"started": True, "backend": backend, "model": model, "auto_speak": auto_speak}
    except RuntimeError as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def stop_coaching() -> dict[str, Any]:
    """Stop background game monitoring.

    Stops the coaching loop if running. Any pending advice remains in the queue.

    Returns:
        Dict with:
        - stopped: True if coaching was stopped

        or {"error": "not running"} if coaching wasn't active.
    """
    try:
        stop_background_coaching()
        return {"stopped": True}
    except RuntimeError:
        return {"error": "not running"}


@mcp.tool()
def get_coaching_status() -> dict[str, Any]:
    """Check if background coaching is active and its configuration.

    Use this to see if coaching is running and what settings are being used.

    Returns:
        Dict with:
        - active: True if background coaching is running
        - backend: LLM mode being used (or None if not running)
        - model: Model name being used (or None for default/not running)
        - auto_speak: Whether advice is automatically spoken (or False if not running)
    """
    return {
        "active": _coaching_enabled,
        "backend": _coaching_backend,
        "model": _coaching_model,
        "auto_speak": _coaching_auto_speak,
    }


@mcp.tool()
def start_draft_helper_tool(set_code: Optional[str] = None) -> dict[str, Any]:
    """Start background draft helper that auto-speaks pick recommendations.

    Monitors draft state in real-time and automatically speaks the top pick
    (by 17lands GIH win rate) whenever a new pack is detected. No LLM needed -
    pure data lookup for maximum speed.

    Use this at the start of a draft for hands-free pick recommendations.

    Args:
        set_code: Set code for 17lands lookups (e.g., "MH3", "BLB", "DSK").
                  Required for accurate win rate data.

    Returns:
        Dict with:
        - started: True if helper started successfully
        - message: Status message

        or {"error": message} if already running.
    """
    try:
        start_draft_helper(set_code=set_code)
        return {
            "started": True,
            "set_code": set_code.upper() if set_code else None,
            "message": f"Draft helper started for {set_code.upper() if set_code else 'unknown set'}. Will auto-speak top pick for each pack."
        }
    except RuntimeError as e:
        return {"error": str(e)}


@mcp.tool()
def stop_draft_helper_tool() -> dict[str, Any]:
    """Stop background draft helper.

    Stops the automatic pick recommendation announcements.

    Returns:
        Dict with:
        - stopped: True if helper was stopped

        or {"error": message} if not running.
    """
    try:
        stop_draft_helper()
        return {"stopped": True}
    except RuntimeError:
        return {"error": "Draft helper not running"}


@mcp.tool()
def get_draft_helper_status() -> dict[str, Any]:
    """Check if draft helper is active.

    Returns:
        Dict with:
        - active: True if draft helper is running
        - current_pack: Current pack number being tracked
        - current_pick: Current pick number being tracked
    """
    return {
        "active": _draft_helper_enabled,
        "current_pack": _draft_helper_last_pack,
        "current_pick": _draft_helper_last_pick,
    }


@mcp.tool()
def build_deck(top_n: int = 3) -> dict[str, Any]:
    """Build deck suggestions from the current draft pool.

    Analyzes picked cards using 17lands GIHWR data and archetype constraints
    (Aggro/Midrange/Control) to suggest optimal deck configurations with
    maindeck, sideboard, and land base.

    Args:
        top_n: Number of deck suggestions to return (default 3).

    Returns:
        Dict with:
        - suggestions: List of deck suggestions, each with:
          - archetype: "Aggro", "Midrange", or "Control"
          - color_pair_name: e.g., "Dimir", "Boros"
          - maindeck: {card_name: count}
          - sideboard: {card_name: count}
          - lands: {land_name: count}
          - score: Overall deck quality score
          - avg_gihwr: Average Games In Hand Win Rate
        - pool_size: Number of cards in draft pool

        or {"error": message} if no draft pool available.
    """
    if not draft_state.picked_cards:
        return {"error": "No draft pool available. Pick cards first."}

    builder = _get_deck_builder()
    set_code = draft_state.set_code or ""

    suggestions = builder.suggest_deck(
        drafted_grp_ids=draft_state.picked_cards,
        set_code=set_code,
        top_n=top_n,
    )

    if not suggestions:
        return {"error": f"Could not build decks from pool. Check set_code ({set_code})."}

    return {
        "suggestions": [
            {
                "archetype": s.archetype,
                "main_colors": s.main_colors,
                "color_pair_name": s.color_pair_name,
                "maindeck": s.maindeck,
                "sideboard": s.sideboard,
                "lands": s.lands,
                "avg_gihwr": round(s.avg_gihwr, 4),
                "penalty": round(s.penalty, 4),
                "score": round(s.score, 4),
            }
            for s in suggestions
        ],
        "pool_size": len(draft_state.picked_cards),
        "set_code": set_code,
    }


@mcp.tool()
def get_metagame(format_name: str) -> dict[str, Any]:
    """Get metagame breakdown for a constructed format from MTGGoldfish.

    Scrapes current metagame data showing top deck archetypes, their
    meta share, and color identity.

    Args:
        format_name: Format to query (standard, modern, pioneer, legacy, pauper).

    Returns:
        Dict with:
        - format: The format queried
        - decks: List of deck dicts with name, meta_share, url, colors
        - count: Number of decks returned

        or {"error": message} on failure.
    """
    try:
        client = _get_mtggoldfish()
        decks = client.get_metagame(format_name)
        return {
            "format": format_name.lower(),
            "decks": decks,
            "count": len(decks),
        }
    except Exception as e:
        logger.error(f"Metagame lookup error: {e}")
        return {"error": str(e)}


@mcp.tool()
def get_commander_info(commander_name: str) -> dict[str, Any]:
    """Get EDHREC data for a commander (top cards, themes, salt score).

    Fetches detailed commander page data from EDHREC including most-played
    cards, deck themes, and community salt score.

    Args:
        commander_name: Name of the commander card.

    Returns:
        Dict with:
        - commander: Commander name
        - url: EDHREC page URL
        - cards: List of top cards with synergy/inclusion rates
        - themes: List of deck themes
        - meta: Rank, total decks, salt score

        or {"error": message} on failure.
    """
    try:
        client = _get_edhrec()
        return client.get_commander_page(commander_name)
    except Exception as e:
        logger.error(f"EDHREC lookup error: {e}")
        return {"commander": commander_name, "error": str(e)}


# Entry point for running as module
if __name__ == "__main__":
    from arenamcp.logging_config import configure_logging

    configure_logging(console=False)
    mcp.run()
