"""FastMCP server exposing MTGA game state and card information.

This module provides the MCP server that bridges live MTGA games to Claude,
implementing the Calculator + Coach pattern: deterministic code tracks state
while the LLM provides strategic analysis.
"""

import logging
import threading
import time
from collections import deque
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

from arenamcp.coach import CoachEngine, GameStateTrigger, create_backend

from arenamcp.gamestate import (
    GameState, GameObjectKind, ZoneType, create_game_state_handler,
    save_match_state, load_match_state, mark_match_ended,
)
from arenamcp.parser import LogParser
from arenamcp.scryfall import ScryfallCache
from arenamcp.draftstats import DraftStatsCache
from arenamcp.draftstate import DraftState, create_draft_handler
from arenamcp.draft_eval import evaluate_pack, format_pick_recommendation
from arenamcp.sealed_eval import analyze_sealed_pool, format_sealed_recommendation, format_sealed_detailed
from arenamcp.mtgadb import MTGADatabase
from arenamcp.mtgjson import MTGJSONDatabase, get_mtgjson
from arenamcp.watcher import MTGALogWatcher
try:
    from arenamcp.voice import VoiceInput
except ImportError:
    VoiceInput = None  # type: ignore[assignment,misc]
try:
    from arenamcp.tts import VoiceOutput
except ImportError:
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
_scryfall: Optional[ScryfallCache] = None
_draft_stats: Optional[DraftStatsCache] = None
_mtgadb: Optional[MTGADatabase] = None

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


def _get_scryfall() -> ScryfallCache:
    """Get or initialize the Scryfall cache (lazy loading)."""
    global _scryfall
    if _scryfall is None:
        logger.info("Initializing Scryfall cache...")
        _scryfall = ScryfallCache()
    return _scryfall


def _get_draft_stats() -> DraftStatsCache:
    """Get or initialize the draft stats cache (lazy loading)."""
    global _draft_stats
    if _draft_stats is None:
        logger.info("Initializing 17lands draft stats cache...")
        _draft_stats = DraftStatsCache()
    return _draft_stats


def _get_mtgadb() -> MTGADatabase:
    """Get or initialize the MTGA local database (lazy loading)."""
    global _mtgadb
    if _mtgadb is None:
        logger.info("Initializing MTGA local database...")
        _mtgadb = MTGADatabase()
    return _mtgadb


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
        save_match_state(game_state, log_offset=offset)


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
    backend: str = "claude-code",
    model: Optional[str] = None,
    auto_speak: bool = False
) -> None:
    """Start background game monitoring with proactive coaching.

    Creates a daemon thread that polls game state and generates advice
    when triggers fire.

    Args:
        backend: LLM backend to use ("claude-code", "gemini-cli", "ollama")
        model: Optional model override (uses backend default if not specified)
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
    logger.info(f"Started background coaching with {backend} backend (model={model}), auto_speak={auto_speak}")


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
    """Handle MatchCreated events to capture local seat ID.

    The MatchCreated message contains systemSeatId which definitively
    identifies the local player's seat.
    """
    # Try to extract match ID to detect new matches
    match_id = (
        payload.get("matchId") or 
        payload.get("matchGameRoomInfo", {}).get("gameRoomConfig", {}).get("matchId") or
        payload.get("gameRoomConfig", {}).get("matchId")
    )

    if match_id:
        if game_state.match_id != match_id:
            logger.info(f"New match detected (ID: {match_id}). Resetting game state.")
            game_state.reset()
            game_state.match_id = match_id
            
            # Since we reset state, notify draft helper effectively if needed
            # (though draft state is separate)
    
    # Try various known locations for seat ID in match messages
    seat_id = (
        payload.get("systemSeatId") or
        payload.get("systemSeatNumber") or
        payload.get("localPlayerSeatId")
    )

    # Also check nested structures
    if seat_id is None:
        match_info = payload.get("matchGameRoomInfo", {})
        seat_id = match_info.get("systemSeatId")

    if seat_id is None:
        game_room_config = payload.get("gameRoomConfig", {})
        reserved_players = game_room_config.get("reservedPlayers", [])
        
        # We need to know WHICH player is us. scanning reservedPlayers blindly is wrong if both are listed.
        # Use log_utils to find our local userId from AuthenticateResponse
        from arenamcp.log_utils import get_local_player_id
        local_user_id = get_local_player_id()
        
        if local_user_id:
            logger.info(f"Using Local User ID: {local_user_id}")
            for rp in reserved_players:
                if rp.get("userId") == local_user_id:
                    seat_id = rp.get("systemSeatId")
                    logger.info(f"Found matching seat {seat_id} for user {local_user_id}")
                    break
        
        # Fallback if ID lookup failed (or logic is different): pick the first one with a userId?
        # No, that's dangerous. Let's try log_utils full detection if still None.
        if seat_id is None and not local_user_id:
             # Try legacy heuristic: First one with a userId (often works if logs are filtered to client?)
             # But Player.log contains opponent metadata too.
             # Better to use the robust scanner.
             from arenamcp.log_utils import detect_local_seat
             seat_id = detect_local_seat()

    if seat_id is not None:
        # Use System (2) priority for match messages
        # This will automatically be ignored if User (3) has already set the seat
        game_state.set_local_seat_id(seat_id, source=2)
        logger.info(f"Captured local seat ID {seat_id} from match message")


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
        resume_offset = saved_state.get("log_offset")
        if saved_state.get("local_seat_id") is not None:
            game_state.set_local_seat_id(saved_state["local_seat_id"], source=2)
        if saved_state.get("match_id"):
            game_state.match_id = saved_state["match_id"]
        logger.info(
            f"Resuming match {saved_state.get('match_id')} "
            f"from offset {resume_offset}"
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

    Uses multiple sources with fallback chain:
    1. MTGJSON by arena_id (if available)
    2. MTGA local DB for name -> MTGJSON by name (for new sets without arena_id mapping)
    3. Scryfall as last resort

    Args:
        grp_id: MTGA arena_id for the card

    Returns:
        Dict with name, oracle_text, type_line, mana_cost if found,
        or minimal dict with just grp_id if lookup fails (graceful degradation).
    """
    if grp_id == 0:
        return {"grp_id": 0, "name": "Unknown", "oracle_text": "", "type_line": "", "mana_cost": ""}

    mtgjson = get_mtgjson()

    # Try MTGJSON by arena_id first (most complete, updated daily)
    if mtgjson.available:
        mtgjson_card = mtgjson.get_card(grp_id)
        if mtgjson_card:
            return {
                "grp_id": grp_id,
                "name": mtgjson_card.name or f"Unknown ({grp_id})",
                "oracle_text": mtgjson_card.oracle_text or "",
                "type_line": mtgjson_card.type_line or "",
                "mana_cost": mtgjson_card.mana_cost or "",
            }

    # Try MTGA local DB for name, then look up oracle text by name from MTGJSON
    mtgadb = _get_mtgadb()
    if mtgadb.available:
        mtga_card = mtgadb.get_card(grp_id)
        if mtga_card:
            # Got simple card data from MTGA DB.
            # Ideally we enrich it with Scryfall/MTGJSON for formatted mana costs and clean text.

            # Try MTGJSON by name
            if mtgjson.available:
                mtgjson_card = mtgjson.get_card_by_name(mtga_card.name)
                if mtgjson_card:
                    return {
                        "grp_id": grp_id,
                        "name": mtga_card.name or f"Unknown ({grp_id})",
                        "oracle_text": mtgjson_card.oracle_text or "",
                        "type_line": mtgjson_card.type_line or "",
                        "mana_cost": mtgjson_card.mana_cost or "",
                    }

            # Try Scryfall for oracle text - first by arena_id, then by name
            scryfall = _get_scryfall()
            scryfall_card = scryfall.get_card_by_arena_id(grp_id)

            # If arena_id lookup fails (new sets), try by name
            if scryfall_card is None:
                scryfall_card = scryfall.get_card_by_name(mtga_card.name)

            if scryfall_card:
                return {
                    "grp_id": grp_id,
                    "name": mtga_card.name or f"Unknown ({grp_id})",
                    "oracle_text": scryfall_card.oracle_text or "",
                    "type_line": scryfall_card.type_line or "",
                    "mana_cost": scryfall_card.mana_cost or "",
                }
            else:
                # MTGA DB Fallback: Use the text we resolved from AbilityIds
                # This handles digital-only cards (Alchemy) or new sets not yet in Scryfall
                return {
                    "grp_id": grp_id,
                    "name": mtga_card.name or f"Unknown ({grp_id})",
                    "oracle_text": mtga_card.oracle_text or "",
                    "type_line": mtga_card.types or "",
                    "mana_cost": "", # TODO: Parse OldSchoolManaText if needed
                }
        
        # If not found as a card, check if it's an ABILITY (e.g. on stack)
        ability_text = mtgadb.get_ability_text(grp_id)
        if ability_text:
            return {
                "grp_id": grp_id,
                "name": f"Ability (ID: {grp_id})",
                "oracle_text": ability_text,
                "type_line": "Ability",
                "mana_cost": "",
            }

    # Last resort: Scryfall only (legacy path)
    scryfall = _get_scryfall()
    card = scryfall.get_card_by_arena_id(grp_id)

    if card is None:
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

    return {
        "grp_id": grp_id,
        "name": card.name or f"Unknown ({grp_id})",
        "oracle_text": card.oracle_text or "",
        "type_line": card.type_line or "",
        "mana_cost": card.mana_cost or "",
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

    # Serialize turn info
    turn = {
        "turn_number": game_state.turn_info.turn_number,
        "active_player": game_state.turn_info.active_player,
        "priority_player": game_state.turn_info.priority_player,
        "phase": game_state.turn_info.phase,
        "step": game_state.turn_info.step,
        "pending_combat_steps": game_state.get_pending_combat_steps(),
    }

    # Serialize players
    players = []
    for player in game_state.players.values():
        players.append({
            "seat_id": player.seat_id,
            "life_total": player.life_total,
            "mana_pool": player.mana_pool,
            "is_local": player.seat_id == game_state.local_seat_id,
            "lands_played": player.lands_played,
        })

    # Serialize zones with oracle text enrichment
    battlefield = [
        _serialize_game_object(obj) for obj in game_state.battlefield
        if obj.object_kind != GameObjectKind.ABILITY
    ]
    hand = [_serialize_game_object(obj) for obj in game_state.hand]
    graveyard = [_serialize_game_object(obj) for obj in game_state.graveyard]
    stack = [_serialize_game_object(obj) for obj in game_state.stack]
    exile = [_serialize_game_object(obj) for obj in game_state.get_objects_in_zone(ZoneType.EXILE)]
    command = [_serialize_game_object(obj) for obj in game_state.command]

    # Save match state on turn changes for recovery
    _save_match_state_if_needed()

    return {
        "match_id": game_state.match_id,
        "turn": turn,
        "players": players,
        "battlefield": battlefield,
        "hand": hand,
        "graveyard": graveyard,
        "stack": stack,
        "exile": exile,
        "command": command,
        "pending_decision": game_state.pending_decision if game_state.decision_seat_id == game_state.local_seat_id else None,
        "decision_context": game_state.decision_context if game_state.decision_seat_id == game_state.local_seat_id else None,
        "deck_cards": game_state.deck_cards,
        "damage_taken": dict(game_state.damage_taken),
    }


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
    
    Tries MTGA's local database first (for newest cards), then Scryfall.

    Args:
        arena_id: The MTGA arena ID (grp_id) of the card

    Returns:
        Dict with name, oracle_text, type_line, mana_cost, cmc, colors, scryfall_uri
        or {"error": "Card not found"} if the card isn't in any database.
    """
    # Try MTGA local database first (has newest cards like Final Fantasy crossover)
    mtgadb = _get_mtgadb()
    if mtgadb.available:
        mtga_card = mtgadb.get_card(arena_id)
        if mtga_card:
            return {
                "name": mtga_card.name,
                "oracle_text": mtga_card.oracle_text or "",
                "type_line": mtga_card.types or "",
                "mana_cost": "",  # MTGA DB doesn't store mana cost
                "cmc": 0,
                "colors": mtga_card.colors.split(",") if mtga_card.colors else [],
                "scryfall_uri": None,
            }
    
    # Fall back to Scryfall
    scryfall = _get_scryfall()
    card = scryfall.get_card_by_arena_id(arena_id)

    if card is None:
        return {"error": f"Card not found for arena_id {arena_id}"}

    return {
        "name": card.name or f"Unknown ({arena_id})",
        "oracle_text": card.oracle_text or "",
        "type_line": card.type_line or "",
        "mana_cost": card.mana_cost or "",
        "cmc": card.cmc or 0,
        "colors": card.colors or [],
        "scryfall_uri": card.scryfall_uri,
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

    evaluations = evaluate_pack(
        cards_in_pack=draft_state.cards_in_pack,
        picked_cards=draft_state.picked_cards,
        set_code=draft_state.set_code,
        scryfall=scryfall,
        draft_stats=draft_stats_cache,
        mtgadb=mtgadb,
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
    backend: str = "claude-code",
    model: Optional[str] = None,
    auto_speak: bool = False
) -> dict[str, Any]:
    """Start background game monitoring with proactive coaching.

    Begins a background loop that monitors game state changes and generates
    coaching advice when triggers fire (new turn, combat, low life, etc.).
    Advice is queued and can be retrieved with get_pending_advice().

    Args:
        backend: LLM backend to use for advice generation.
            Options: "claude-code" (default), "gemini-cli", "ollama"
        model: Optional model name to use. Defaults vary by backend:
            - claude-code: sonnet
            - gemini-cli: gemini-2.0-flash
            - ollama: llama3.2
        auto_speak: If True, automatically speak advice via TTS when generated.
            Default False - retrieve advice manually with get_pending_advice().

    Returns:
        Dict with:
        - started: True if coaching started successfully
        - backend: The LLM backend being used
        - model: The model being used (or None for default)
        - auto_speak: Whether auto-speak is enabled

        or {"error": message} if already running or backend invalid.
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
        - backend: Name of LLM backend being used (or None if not running)
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
    mcp.run()
