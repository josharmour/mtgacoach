"""ArenaMCP: Real-time MCP server for MTGA game analysis."""

from typing import Optional

from arenamcp.watcher import MTGALogWatcher
from arenamcp.parser import LogParser
from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.scryfall import ScryfallCache, ScryfallCard
from arenamcp.draftstats import DraftStatsCache, DraftStats
from arenamcp.draftstate import DraftState, create_draft_handler
from arenamcp.mtgadb import MTGADatabase, MTGACard
from arenamcp.draft_eval import evaluate_pack, format_pick_recommendation, CardEvaluation
from arenamcp.server import mcp, start_watching, stop_watching
try:
    from arenamcp.voice import VoiceInput
except ImportError:
    pass
try:
    from arenamcp.tts import VoiceOutput, KokoroTTS
except ImportError:
    pass
from arenamcp.coach import (
    CoachEngine,
    GameStateTrigger,
    create_backend,
    create_ollama_fallback,
    ClaudeCodeBackend,
    GeminiCliBackend,
    CodexCliBackend,
)
# Optional modules — these have extra dependencies that may not be installed.
# They are lazily imported so the core package works without them.
try:
    from arenamcp.action_planner import ActionPlanner, ActionPlan, GameAction, ActionType
    from arenamcp.screen_mapper import ScreenMapper, ScreenCoord, FixedCoordinates
    from arenamcp.input_controller import InputController, ClickResult
    from arenamcp.autopilot import AutopilotEngine, AutopilotConfig, AutopilotState
except ImportError:
    pass

try:
    from arenamcp.synergy import SynergyGraph, get_synergy_graph
except ImportError:
    pass

try:
    from arenamcp.deck_builder import DeckBuilderV2, DeckSuggestion, CardRating
except ImportError:
    pass

try:
    from arenamcp.edhrec import EDHRECClient
except ImportError:
    pass

try:
    from arenamcp.mtggoldfish import MTGGoldfishClient
except ImportError:
    pass

from arenamcp.gamestate import save_match_state, load_match_state, mark_match_ended

__version__ = "0.4.1"


def create_log_pipeline(
    log_path: Optional[str] = None,
    backfill: bool = True
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
    watcher = MTGALogWatcher(
        callback=parser.process_chunk,
        log_path=log_path,
        backfill=backfill
    )
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
    "create_ollama_fallback",
    "ClaudeCodeBackend",
    "GeminiCliBackend",
    "CodexCliBackend",
    "save_match_state",
    "load_match_state",
    "mark_match_ended",
]

# Extend __all__ with optional modules that were successfully imported
for _name in [
    "ActionPlanner", "ActionPlan", "GameAction", "ActionType",
    "ScreenMapper", "ScreenCoord", "FixedCoordinates",
    "InputController", "ClickResult",
    "AutopilotEngine", "AutopilotConfig", "AutopilotState",
    "SynergyGraph", "get_synergy_graph",
    "DeckBuilderV2", "DeckSuggestion", "CardRating",
    "EDHRECClient", "MTGGoldfishClient",
]:
    if _name in globals():
        __all__.append(_name)
