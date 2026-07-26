"""mtgacoach: Real-time AI coaching for MTGA via mtgacoach.com."""

from typing import Optional

from arenamcp.card_db import (
    CardDatabase,
    CardInfo,
    FallbackCardDatabase,
    create_card_database,
    get_card_database,
)
from arenamcp.draft_eval import CardEvaluation, evaluate_pack, format_pick_recommendation
from arenamcp.draftstate import DraftState, create_draft_handler
from arenamcp.draftstats import DraftStats, DraftStatsCache
from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.mtgadb import MTGACard, MTGADatabase
from arenamcp.parser import LogParser
from arenamcp.scryfall import ScryfallCache, ScryfallCard
from arenamcp.watcher import MTGALogWatcher

try:
    from arenamcp.server import mcp, start_watching, stop_watching
except ImportError:
    mcp = None  # type: ignore[assignment]
    start_watching = None  # type: ignore[assignment]
    stop_watching = None  # type: ignore[assignment]
# Voice/TTS imports deferred — sounddevice initializes PortAudio on import
# which can hang if an audio device/driver is misbehaving.
# These are imported lazily on first use instead.
import contextlib

from arenamcp.coach import (
    CoachEngine,
    GameStateTrigger,
    create_backend,
    create_local_fallback,
    get_available_modes,
    get_models_for_mode,
)

# Optional modules — these have extra dependencies that may not be installed.
# They are lazily imported so the core package works without them.
try:
    from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine, AutopilotState
    from arenamcp.input_controller import ClickResult, InputController
    from arenamcp.screen_mapper import FixedCoordinates, ScreenCoord, ScreenMapper
except ImportError:
    pass

with contextlib.suppress(ImportError):
    from arenamcp.synergy import SynergyGraph, get_synergy_graph

with contextlib.suppress(ImportError):
    from arenamcp.deck_builder import CardRating, DeckBuilderV2, DeckSuggestion

with contextlib.suppress(ImportError):
    from arenamcp.edhrec import EDHRECClient

with contextlib.suppress(ImportError):
    from arenamcp.mtggoldfish import MTGGoldfishClient

from arenamcp.gamestate import load_match_state, mark_match_ended, save_match_state

__version__ = "2.7.4"


def create_log_pipeline(
    log_path: str | None = None, backfill: bool = True
) -> tuple[MTGALogWatcher, LogParser]:
    """Create a connected watcher -> parser pipeline.

    Convenience factory that wires the log watcher to feed the parser.

    Args:
        log_path: Path to MTGA Player.log. Defaults to MTGA_LOG_PATH env var
                 or standard Windows location.
        backfill: If True, parse existing log content from the last match
                 start when the watcher starts. Enables catching up on
                 in-progress games. Defaults to True.

    Returns:
        Tuple of (watcher, parser). Start the watcher to begin processing.
        Register handlers on the parser before starting the watcher.

    Example:
        watcher, parser = create_log_pipeline()
        parser.register_handler('GreToClientEvent', handle_game_event)
        with watcher:
            # Events flow through pipeline
            time.sleep(10)
    """
    parser = LogParser()
    watcher = MTGALogWatcher(callback=parser.process_chunk, log_path=log_path, backfill=backfill)
    return watcher, parser


__all__ = [
    "__version__",
    "MTGALogWatcher",
    "LogParser",
    "create_log_pipeline",
    "GameState",
    "create_game_state_handler",
    "ScryfallCache",
    "ScryfallCard",
    "DraftStatsCache",
    "DraftStats",
    "DraftState",
    "create_draft_handler",
    "MTGADatabase",
    "MTGACard",
    "CardInfo",
    "CardDatabase",
    "FallbackCardDatabase",
    "get_card_database",
    "create_card_database",
    "evaluate_pack",
    "format_pick_recommendation",
    "CardEvaluation",
    "mcp",
    "start_watching",
    "stop_watching",
    "VoiceInput",
    "VoiceOutput",
    "KokoroTTS",
    "CoachEngine",
    "GameStateTrigger",
    "create_backend",
    "create_local_fallback",
    "get_available_modes",
    "get_models_for_mode",
    "save_match_state",
    "load_match_state",
    "mark_match_ended",
]

# Extend __all__ with optional modules that were successfully imported
for _name in [
    "ActionPlanner",
    "ActionPlan",
    "GameAction",
    "ActionType",
    "ScreenMapper",
    "ScreenCoord",
    "FixedCoordinates",
    "InputController",
    "ClickResult",
    "AutopilotEngine",
    "AutopilotConfig",
    "AutopilotState",
    "SynergyGraph",
    "get_synergy_graph",
    "DeckBuilderV2",
    "DeckSuggestion",
    "CardRating",
    "EDHRECClient",
    "MTGGoldfishClient",
]:
    if _name in globals():
        __all__.append(_name)
