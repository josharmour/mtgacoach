"""Standalone MTGA Coach - Lightweight MCP client with voice I/O.

This app runs the ArenaMCP server and connects to it as an MCP client,
using an LLM (via cli-api-proxy, Claude CLI, or Gemini CLI) for coaching
advice with voice support.

Usage:
    python -m arenamcp.standalone --backend proxy
    python -m arenamcp.standalone --provider gemini-2.5-pro
    python -m arenamcp.standalone --draft --set MH3

The MCP server handles all game state tracking; this client just:
- Polls MCP tools for state changes
- Passes state to local LLM for advice
- Handles voice I/O (PTT/VOX input, TTS output)
"""

# Load .env before other imports
def _load_dotenv():
    """Load environment variables from .env file if it exists."""
    import os
    from pathlib import Path
    for env_path in [Path(".env"), Path(__file__).parent.parent.parent / ".env"]:
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ.setdefault(key.strip(), value.strip())
            break

_load_dotenv()

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from arenamcp.settings import get_settings

# Configure logging
LOG_DIR = Path.home() / ".arenamcp"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "standalone.log"

file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)
root_logger.addHandler(file_handler)

# Suppress noisy third-party loggers
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Console handler disabled when using TUI to prevent log bleed-through
# Logs still go to file (standalone.log)
# console_handler = logging.StreamHandler()
# console_handler.setLevel(logging.INFO)
# console_handler.setFormatter(logging.Formatter('%(message)s'))
# root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the Windows clipboard.

    Tries pyperclip first, falls back to Windows clip command.
    Returns True if successful, False otherwise.
    """
    # Try pyperclip first (if installed)
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pyperclip failed: {e}")

    # Fallback: Windows clip command
    try:
        process = subprocess.Popen(
            ['clip'],
            stdin=subprocess.PIPE,
            shell=True
        )
        process.communicate(input=text.encode('utf-8'))
        return process.returncode == 0
    except Exception as e:
        logger.debug(f"clip command failed: {e}")
        return False


# Import dependencies
try:
    import keyboard
except ImportError:
    keyboard = None
    logger.warning("keyboard module not available - hotkeys disabled")


class UIAdapter:
    """Interface for UI feedback (CLI or TUI)."""
    def log(self, message: str) -> None: pass
    def advice(self, text: str, seat_info: str) -> None: pass
    def status(self, key: str, value: str) -> None: pass
    def error(self, message: str) -> None: pass
    def speak(self, text: str) -> None: pass
    def subtask(self, status: str) -> None: pass

class CLIAdapter(UIAdapter):
    """Default adapter for CLI output."""
    def log(self, message: str) -> None:
        print(message)
    def advice(self, text: str, seat_info: str) -> None:
        print(f"\n[COACH|{seat_info}] {text}\n")
    def status(self, key: str, value: str) -> None:
        print(f"[{key}] {value}")
    def error(self, message: str) -> None:
        print(f"ERROR: {message}")
    def speak(self, text: str) -> None: pass
    def subtask(self, status: str) -> None:
        print(f"  ⟳ {status}", end="\r")

class MCPClient:
    """Simple in-process MCP client that calls server tools directly.

    Since the MCP server runs in-process, we import and call tools directly
    rather than going through STDIO transport.
    """

    def __init__(self):
        """Initialize MCP client by importing server module."""
        # Import server module - this starts the log watcher
        from arenamcp import server
        self._server = server

        # Ensure watcher is running
        server.start_watching()
        logger.info("MCP server initialized")

    def get_game_state(self) -> dict[str, Any]:
        """Call get_game_state MCP tool."""
        return self._server.get_game_state()

    def clear_pending_combat_steps(self) -> None:
        """Clear pending combat steps after trigger processing."""
        self._server.clear_pending_combat_steps()

    def poll_log(self) -> None:
        """Manually poll for new log content (backup for missed watchdog events)."""
        self._server.poll_log()

    def get_draft_pack(self) -> dict[str, Any]:
        """Call get_draft_pack MCP tool."""
        return self._server.get_draft_pack()

    def get_card_info(self, arena_id: int) -> dict[str, Any]:
        """Call get_card_info MCP tool."""
        return self._server.get_card_info(arena_id)

    def start_draft_helper(self, set_code: Optional[str] = None) -> dict[str, Any]:
        """Start the built-in draft helper."""
        return self._server.start_draft_helper_tool(set_code)

    def stop_draft_helper(self) -> dict[str, Any]:
        """Stop the draft helper."""
        return self._server.stop_draft_helper_tool()

    def get_draft_helper_status(self) -> dict[str, Any]:
        """Get draft helper status."""
        return self._server.get_draft_helper_status()

    def evaluate_draft_pack(self) -> dict[str, Any]:
        """Evaluate draft pack with composite scoring (colors, synergy, WR)."""
        return self._server.evaluate_draft_pack_for_standalone()

    def get_sealed_pool(self) -> dict[str, Any]:
        """Get sealed pool analysis."""
        return self._server.get_sealed_pool()

    def analyze_draft_pool(self) -> dict[str, Any]:
        """Analyze drafted cards for deck building."""
        return self._server.analyze_draft_pool()


class ConsoleAdapter(UIAdapter):
    """Fallback for CLI mode."""
    def log(self, message: str) -> None: print(message, end='')
    def advice(self, text: str, seat_info: str) -> None: print(f"\n[COACH|{seat_info}] {text}\n")
    def status(self, key: str, value: str) -> None: pass
    def error(self, message: str) -> None: print(f"ERROR: {message}")
    def speak(self, text: str) -> None: pass
    def subtask(self, status: str) -> None: pass


class StandaloneCoach:
    """Standalone coaching app using MCP client + local LLM."""

    def __init__(
        self,
        backend: str = "proxy",
        model: Optional[str] = None,
        voice_mode: str = "ptt",
        draft_mode: bool = False,
        set_code: Optional[str] = None,
        ui_adapter: Optional[UIAdapter] = None,
        register_hotkeys: bool = True,
        autopilot: bool = False,
        dry_run: bool = False,
        afk: bool = False,
    ):
        self._register_keyboard = register_hotkeys

        # Load settings
        self.settings = get_settings()

        # Resolve configuration (Args > Settings > Defaults)
        self._backend_name = backend or self.settings.get("backend", "proxy")
        self._model_name = model or self.settings.get("model")
        self._voice_mode = voice_mode or self.settings.get("voice_mode", "ptt")

        self.draft_mode = draft_mode
        self.set_code = set_code.upper() if set_code else None

        # State
        self.advice_style = "verbose"
        self._advice_frequency = self.settings.get("advice_frequency", "start_of_turn")

        # TTS always enabled
        self._auto_speak = True

        self.ui = ui_adapter or CLIAdapter()

        # Save validated configuration back to settings (ensure consistency)
        self.settings.set("backend", self._backend_name, save=False)
        if self._model_name:
            self.settings.set("model", self._model_name, save=False)
        self.settings.set("voice_mode", self._voice_mode, save=False)
        self.settings.set("advice_frequency", self._advice_frequency, save=True)

        self._start_time = datetime.now()
        self._running = False
        self._restart_requested = False
        self._deck_analyzed = False
        self._mcp: Optional[MCPClient] = None

        # Voice components
        self._voice_input = None
        self._voice_output = None

        # LLM backend
        self._coach = None
        self._trigger = None

        # Threads
        self._coaching_thread: Optional[threading.Thread] = None
        self._voice_thread: Optional[threading.Thread] = None

        # Background win plan
        self._win_plan_turn = 0       # Last turn a win plan was launched
        self._thinking_model = None   # Cached thinking model ID (lazy-init)
        self._pending_win_plan: Optional[str] = None    # Stored viable plan text
        self._pending_win_plan_turns: int = 0            # N in "win-in-N"
        self._pending_win_plan_turn: int = 0             # Game turn when plan was generated

        # Post-match analysis
        self._saved_advice_history: list[dict] = []
        self._last_match_result: Optional[str] = None
        self._last_match_final_state: Optional[dict] = None

    def speak_advice(self, text: str, blocking: bool = True) -> None:
        """Speak advice using local Kokoro TTS."""
        if not text:
            return

        # Filter out passive calls from TTS (User Request)
        # We silence: Wait, Pass, No actions
        clean_text = text.lower().strip(" .!")
        silence_triggers = [
            "wait",
            "pass",
            "pass priority",
            "no actions",
            "wait for opponent",
            "opponent has priority"
        ]

        # Check if text starts with or is substantially just these phrases
        # We use a simple heuristic: if it contains no active verbs (Cast, Attack, Block, Play),
        # and matches a silence trigger, we skip it.
        is_passive = any(trigger in clean_text for trigger in silence_triggers)
        has_action = any(verb in clean_text for verb in ["cast", "play", "attack", "block", "activate", "kill", "destroy"])

        if is_passive and not has_action and len(text) < 60:
            return

        # Use local Kokoro TTS
        if self._voice_output:
            try:
                self._voice_output.speak(text, blocking=blocking)
            except Exception as e:
                logger.error(f"Kokoro TTS error: {e}")

    @property
    def backend_name(self) -> str:
        return self._backend_name
    
    @backend_name.setter
    def backend_name(self, value: str):
        self._backend_name = value
        self.settings.set("backend", value)
        
    @property
    def model_name(self) -> Optional[str]:
        return self._model_name
    
    @model_name.setter
    def model_name(self, value: Optional[str]):
        self._model_name = value
        if value:
            self.settings.set("model", value)
            
    @property
    def voice_mode(self) -> str:
        return self._voice_mode
    
    @voice_mode.setter
    def voice_mode(self, value: str):
        self._voice_mode = value
        self.settings.set("voice_mode", value)
        if hasattr(self, "_voice_input") and self._voice_input:
            # Propagate to input handler if running
            # (Note: VoiceInput might need restart to change mode fully, preventing hot-swap here)
            pass

    @property
    def advice_frequency(self) -> str:
        return self._advice_frequency

    @advice_frequency.setter
    def advice_frequency(self, value: str):
        self._advice_frequency = value
        self.settings.set("advice_frequency", value)

    def _init_mcp(self) -> None:
        """Initialize MCP client connection."""
        logger.info("Initializing MCP server...")
        self._mcp = MCPClient()

        # Pre-initialize card databases to avoid lazy-load delays during gameplay
        logger.info("Pre-loading card databases...")
        try:
            from arenamcp.mtgjson import get_mtgjson
            from arenamcp.server import _get_scryfall, _get_mtgadb
            get_mtgjson()  # Load MTGJSON (primary source, updated daily)
            _get_mtgadb()  # Load MTGA local database
            _get_scryfall()  # Load Scryfall cache (fallback)
        except Exception as e:
            logger.warning(f"Failed to pre-load databases: {e}")

    def _init_llm(self) -> None:
        """Initialize LLM backend for coaching."""
        if self.draft_mode:
            return  # Draft mode uses MCP's built-in draft helper

        from arenamcp.coach import CoachEngine, GameStateTrigger, create_backend

        # Pass UI subtask callback for real-time progress display
        progress_cb = self.ui.subtask if self.ui else None
        llm_backend = create_backend(self.backend_name, model=self.model_name, progress_callback=progress_cb)
        actual_model = getattr(llm_backend, 'model', 'unknown')
        logger.info(f"Created {self.backend_name} backend with model: {actual_model}")
        self._coach = CoachEngine(backend=llm_backend)
        self._trigger = GameStateTrigger()

    def _init_voice(self) -> None:
        """Initialize voice I/O components."""
        logger.info(f"_init_voice called, backend_name={self.backend_name}")

        from arenamcp.tts import VoiceOutput

        # Initialize local TTS
        logger.info("Initializing TTS...")
        self._voice_output = VoiceOutput()

        voice_id, voice_desc = self._voice_output.current_voice
        logger.info(f"TTS voice: {voice_desc}")
        self.ui.status("VOICE", f"TTS Voice: {voice_desc}")

        # Initialize local STT (Whisper via VoiceInput) only if PTT/VOX mode
        if self._voice_mode in ("ptt", "vox"):
            from arenamcp.voice import VoiceInput
            logger.info(f"Initializing voice input ({self.voice_mode})...")
            self._voice_input = VoiceInput(mode=self.voice_mode)
        else:
            logger.info(f"Voice input disabled (mode={self._voice_mode})")

    def _coaching_loop(self) -> None:
        """Poll MCP for game state and provide coaching, with auto-draft detection."""
        logger.info("Coaching loop started")
        prev_state: dict[str, Any] = {}
        seat_announced = False

        last_advice_turn = 0
        last_advice_phase = ""
        # Critical triggers that always fire regardless of frequency setting
        # Combat triggers removed - too noisy for "start_of_turn" mode
        # decision_required added - scry, discard, target choices need immediate advice
        CRITICAL_PRIORITY = {"stack_spell", "stack_spell_yours", "stack_spell_opponent", "low_life", "opponent_low_life", "decision_required", "threat_detected"}

        # Match ID tracking — reset coaching state when match changes
        last_match_id = None

        # Draft/Sealed detection state
        in_draft_mode = False
        in_sealed_mode = False
        sealed_analyzed = False
        last_draft_pack = 0
        last_draft_pick = 0
        last_inactive_log = 0

        while self._running:
            try:
                # Poll for new log content (watchdog backup - Windows often misses events)
                self._mcp.poll_log()

                # Check for active draft/sealed first
                draft_pack = self._mcp.get_draft_pack()

                if draft_pack.get("is_active"):
                    is_sealed = draft_pack.get("is_sealed", False)

                    if is_sealed:
                        # SEALED MODE
                        if not in_sealed_mode:
                            in_sealed_mode = True
                            in_draft_mode = False
                            self.draft_mode = True
                            set_code = draft_pack.get("set_code", "???")
                            if not self.set_code:
                                self.set_code = set_code
                            self.ui.status("SEALED", f"Detected sealed event: {set_code}")
                            self.ui.log("[SEALED] Waiting for pool to be opened...\n")
                            logger.info(f"Auto-detected sealed: {set_code}")

                        # Check if pool is ready for analysis
                        if not sealed_analyzed:
                            sealed_result = self._mcp.get_sealed_pool()
                            pool_size = sealed_result.get("pool_size", 0)

                            if pool_size > 0:
                                sealed_analyzed = True
                                self.ui.log(f"\n[SEALED] Pool opened ({pool_size} cards)")
                                self.ui.log(sealed_result.get("detailed_text", ""))
                                self.ui.log("")

                                # Speak the recommendation
                                advice = sealed_result.get("spoken_advice", "")
                                if advice:
                                    logger.info(f"SEALED ADVICE: {advice}")
                                    self.speak_advice(advice)

                        time.sleep(2.0)  # Slower polling for sealed
                        continue

                    else:
                        # DRAFT MODE
                        pack_num = draft_pack.get("pack_number", 0)
                        pick_num = draft_pack.get("pick_number", 0)
                        cards = draft_pack.get("cards", [])

                        # New pack detected
                        if cards and (pack_num != last_draft_pack or pick_num != last_draft_pick):
                            if not in_draft_mode:
                                in_draft_mode = True
                                in_sealed_mode = False
                                self.draft_mode = True
                                set_code = draft_pack.get("set_code", "???")
                                if not self.set_code:
                                    self.set_code = set_code
                                self.ui.status("DRAFT", f"Detected draft: {set_code}")
                                self.ui.log("[DRAFT] Auto-switching to draft advice mode\n")
                                logger.info(f"Auto-detected draft: {set_code}")

                            last_draft_pack = pack_num
                            last_draft_pick = pick_num

                            # Use composite evaluation (WR + on-color + synergy + card type)
                            eval_result = self._mcp.evaluate_draft_pack()
                            if eval_result.get("is_active") and eval_result.get("evaluations"):
                                advice = eval_result["spoken_advice"]
                                picked = eval_result.get("picked_count", 0)

                                # Log detailed scores for the top picks
                                top_evals = eval_result["evaluations"]
                                detail_parts = []
                                for e in top_evals[:3]:
                                    wr = f"{e['gih_wr']*100:.0f}%" if e.get("gih_wr") else "N/A"
                                    reasons = ", ".join(e.get("all_reasons", []))
                                    detail_parts.append(f"  {e['name']}: score={e['score']:.0f} WR={wr} [{reasons}]")
                                detail_log = "\n".join(detail_parts)
                                self.ui.log(f"\n[DRAFT P{pack_num}P{pick_num}] ({picked} picked)\n{detail_log}\n")
                                logger.info(f"DRAFT: P{pack_num}P{pick_num} - {advice}")
                                self.speak_advice(advice)
                            elif eval_result.get("is_active"):
                                self.ui.log(f"\n[DRAFT P{pack_num}P{pick_num}] No evaluated picks\n")
                                logger.warning(f"Draft eval returned no evaluations for P{pack_num}P{pick_num}")

                        time.sleep(1.0)  # Faster polling during draft
                        continue

                else:
                    # Inactive draft/sealed
                    if time.time() - last_inactive_log > 10.0:
                        logger.info("Draft inactive (waiting for event entry...)")
                        last_inactive_log = time.time()

                # Not in draft/sealed - regular game coaching
                if in_draft_mode or in_sealed_mode:
                    mode_name = "Sealed" if in_sealed_mode else "Draft"
                    was_draft = in_draft_mode
                    in_draft_mode = False
                    in_sealed_mode = False
                    sealed_analyzed = False
                    self.draft_mode = False
                    self.ui.log(f"\n[{mode_name.upper()}] {mode_name} complete, switching to game coaching\n")
                    logger.info(f"{mode_name} ended, resuming game coaching")
                    last_draft_pack = 0
                    last_draft_pick = 0

                    # Analyze drafted pool and suggest a deck build
                    if was_draft:
                        try:
                            pool_result = self._mcp.analyze_draft_pool()
                            pool_size = pool_result.get("pool_size", 0)
                            if pool_size > 0:
                                detailed = pool_result.get("detailed_text", "")
                                spoken = pool_result.get("spoken_advice", "")
                                if detailed:
                                    self.ui.log(f"\n{detailed}\n")
                                if spoken:
                                    logger.info(f"Draft deck suggestion: {spoken}")
                                    self.speak_advice(spoken)
                            else:
                                logger.warning("No picked cards found for post-draft analysis")
                        except Exception as e:
                            logger.error(f"Post-draft deck analysis failed: {e}")

                curr_state = self._mcp.get_game_state()
                turn = curr_state.get("turn", {})
                turn_num = turn.get("turn_number", 0)
                phase = turn.get("phase", "")
                curr_match_id = curr_state.get("match_id")

                # Detect new match via match_id change (most reliable signal)
                if curr_match_id and curr_match_id != last_match_id:
                    if last_match_id is not None:
                        logger.info(f"New match detected via match_id change ({last_match_id} -> {curr_match_id}), resetting coaching state")

                        # Save match data for post-match analysis BEFORE clearing
                        if self._advice_history and not self._saved_advice_history:
                            self._saved_advice_history = list(self._advice_history)
                            self._last_match_result = self._detect_match_result()
                            self._last_match_final_state = dict(prev_state) if prev_state else None
                            threading.Thread(
                                target=self._post_match_analysis_worker,
                                daemon=True,
                            ).start()

                        prev_state = {}
                        last_advice_turn = 0
                        last_advice_phase = ""
                        seat_announced = False
                        self._advice_history = []
                        self._deck_analyzed = False
                        if self._coach:
                            self._coach.clear_deck_strategy()
                    last_match_id = curr_match_id

                # Debug: Log if turn_num is 0 (every 30 seconds)
                if turn_num == 0:
                    if not hasattr(self, '_last_turn0_log'):
                        self._last_turn0_log = 0
                    if time.time() - self._last_turn0_log > 30:
                        logger.debug(f"turn_num=0, players={len(curr_state.get('players', []))}, battlefield={len(curr_state.get('battlefield', []))}")
                        self._last_turn0_log = time.time()

                # Detect new game (turn number decreased) — fallback for same-match restarts
                if turn_num > 0 and turn_num < last_advice_turn:
                    logger.info(f"New game detected in coaching loop (turn {last_advice_turn} -> {turn_num}), resetting advice tracking")

                    # Save match data for post-match analysis BEFORE clearing
                    if self._advice_history and not self._saved_advice_history:
                        self._saved_advice_history = list(self._advice_history)
                        self._last_match_result = self._detect_match_result()
                        self._last_match_final_state = dict(prev_state) if prev_state else None
                        threading.Thread(
                            target=self._post_match_analysis_worker,
                            daemon=True,
                        ).start()

                    prev_state = {}
                    last_advice_turn = 0
                    last_advice_phase = ""
                    seat_announced = False  # Re-announce seat for new game
                    # Clear advice history for new match
                    self._advice_history = []
                    self._deck_analyzed = False
                    if self._coach:
                        self._coach.clear_deck_strategy()
                    logger.info("Cleared advice history for new match")

                # Announce seat detection when game starts
                if not seat_announced and turn_num > 0:
                    players = curr_state.get("players", [])
                    for p in players:
                        if p.get("is_local"):
                            seat_id = p.get("seat_id")
                            self.ui.status("GAME", f"Detected as Seat {seat_id} - press F8 if this is wrong")
                            logger.info(f"Game detected, local seat = {seat_id}")
                            seat_announced = True
                            break

                # Deck strategy analysis (once per match)
                if not self._deck_analyzed and self._coach and turn_num > 0:
                    deck_cards = curr_state.get("deck_cards", [])
                    if deck_cards:
                        self._deck_analyzed = True
                        logger.info(f"Starting deck analysis for {len(deck_cards)} cards")

                        def _analyze_deck_bg(coach, mcp, card_ids, ui, backend_name, model_name):
                            try:
                                # Enrich grpIds to (name, type, oracle_text) tuples
                                enriched = []
                                for grp_id in card_ids:
                                    try:
                                        info = mcp.get_card_info(grp_id)
                                        name = info.get("name", f"Unknown({grp_id})")
                                        card_type = info.get("type_line", "")
                                        oracle = info.get("oracle_text", "")
                                        enriched.append((name, card_type, oracle))
                                    except Exception:
                                        enriched.append((f"Unknown({grp_id})", "", ""))

                                # Use a SEPARATE backend instance so deck analysis
                                # doesn't hold the advice backend's lock
                                from arenamcp.coach import create_backend
                                deck_backend = create_backend(backend_name, model=model_name)
                                strategy = coach.analyze_deck(enriched, backend=deck_backend)
                                if hasattr(deck_backend, 'close'):
                                    deck_backend.close()
                                if strategy:
                                    # Extract first line as archetype for status bar
                                    first_line = strategy.split("\n")[0].strip()
                                    ui.status("DECK", first_line[:60])
                                    logger.info(f"Deck strategy stored: {len(strategy)} chars")
                                    # Don't TTS the deck — it races with the first
                                    # turn advice and they talk over each other.
                            except Exception as e:
                                logger.error(f"Background deck analysis failed: {e}")

                        t = threading.Thread(
                            target=_analyze_deck_bg,
                            args=(self._coach, self._mcp, deck_cards, self.ui,
                                  self._backend_name, self.model_name),
                            daemon=True,
                        )
                        t.start()

                # FORCE CHECK: Always check triggers if trigger detector exists.
                # prev_state starts as {} (falsy) but check_triggers handles empty
                # prev_state gracefully via .get() defaults — this allows mulligan
                # triggers to fire on the very first poll cycle.
                if self._trigger:
                    # Auto-detect draft mode
                    try:
                        draft_state = self._mcp.get_draft_pack()
                        is_draft_active = draft_state.get("is_active", False)
                        
                        if is_draft_active and not self.draft_mode:
                            logger.info("Auto-detected draft - enabling draft mode")
                            self.draft_mode = True
                            self.ui.status("MODE", "Draft")
                        elif not is_draft_active and self.draft_mode:
                            logger.info("Draft ended - disabling draft mode")
                            self.draft_mode = False
                            self.ui.status("MODE", "Game")
                    except Exception as e:
                        logger.debug(f"Draft detection error: {e}")

                    triggers = self._trigger.check_triggers(prev_state, curr_state)
                    
                    # Debug: Log trigger results
                    if triggers:
                        logger.info(f"Triggers detected: {triggers}")
                    else:
                        # Log why no triggers (every 30 seconds to avoid spam)
                        if not hasattr(self, '_last_no_trigger_log'):
                            self._last_no_trigger_log = 0
                        if time.time() - self._last_no_trigger_log > 30:
                            local_s = curr_state.get("turn", {}).get("active_player", 0)
                            priority = curr_state.get("turn", {}).get("priority_player", 0)
                            logger.debug(f"No triggers: turn={turn_num}, active={local_s}, priority={priority}, phase={phase}")
                            self._last_no_trigger_log = time.time()

                    # Clear pending combat steps after checking (they're now processed)
                    self._mcp.clear_pending_combat_steps()

                    # Determine if it's our turn (for filtering turn-specific triggers)
                    local_seat = None
                    for p in curr_state.get("players", []):
                        if p.get("is_local"):
                            local_seat = p.get("seat_id")
                            break
                    active_seat = curr_state.get("turn", {}).get("active_player", 0)
                    is_my_turn = (active_seat == local_seat) if local_seat else False

                    # Sort triggers by priority to ensure we handle the most critical one only
                    # Priority order: Decision > Stack > Action > Combat > Turn > Priority
                    trigger_priorities = {
                        "decision_required": 11,
                        "stack_spell": 10,
                        "stack_spell_yours": 10,
                        "stack_spell_opponent": 10,
                        "low_life": 9,
                        "opponent_low_life": 8,
                        "land_played": 7,      # After land drop, what's next?
                        "spell_resolved": 7,   # After spell resolves, what's next?
                        "combat_attackers": 6,
                        "combat_blockers": 6,
                        "new_turn": 5,
                        "priority_gained": 1
                    }

                    triggers.sort(key=lambda x: trigger_priorities.get(x, 0), reverse=True)

                    for trigger in triggers:
                        # CRITICAL: Filter turn-specific triggers based on whose turn it is
                        # "new_turn" advice only makes sense on YOUR turn (play lands, cast spells)
                        # On opponent's turn, rename to "opponent_turn" for strategy analysis
                        # "combat_attackers" only on YOUR turn (you declare attackers)
                        # "combat_blockers" only on OPPONENT's turn (you declare blockers)
                        if trigger == "new_turn" and not is_my_turn:
                            trigger = "opponent_turn"
                            logger.info(f"Opponent's turn started (turn {turn_num})")
                        if trigger == "combat_attackers" and not is_my_turn:
                            logger.debug(f"Suppressing combat_attackers trigger (opponent's turn)")
                            continue
                        if trigger == "combat_blockers" and is_my_turn:
                            logger.debug(f"Suppressing combat_blockers trigger (my turn, not blocking)")
                            continue
                        # Critical triggers always fire (stack spells, low life)
                        # BUT: "Action Required" is just generic main-phase priority,
                        # not a real decision (Mulligan/Scry/Discard/Target). Suppress
                        # it if we already advised this turn+phase to avoid duplicates.
                        is_critical = trigger in CRITICAL_PRIORITY
                        if trigger == "decision_required":
                            pending = curr_state.get("pending_decision")
                            if pending == "Action Required" and turn_num == last_advice_turn and phase == last_advice_phase:
                                logger.info(f"Suppressing decision_required: 'Action Required' already advised this turn+phase")
                                continue

                        # New turn triggers once per turn
                        is_new_turn = trigger == "new_turn" and turn_num > last_advice_turn

                        # Opponent turn triggers once per opponent turn
                        is_opponent_turn = trigger == "opponent_turn" and turn_num > last_advice_turn
                        
                        # DELAY BUFFER: For new_turn triggers, wait briefly for Hand zone to update
                        # This prevents "missing draw" bugs where we advise before the drawn card arrives
                        if is_new_turn:
                            # Reset seen threats on new game (turn 1)
                            if turn_num == 1 and hasattr(self._trigger, '_seen_threats'):
                                self._trigger._seen_threats.clear()
                                logger.info("New game detected - cleared seen threats")
                            time.sleep(0.4)  # 400ms to allow Draw Step zone update
                            # Force a log poll to ensure we have latest updates
                            try:
                                self._mcp.poll_log()
                            except Exception:
                                pass
                            # Re-fetch game state to get updated hand
                            try:
                                curr_state = self._mcp.get_game_state()
                            except Exception as e:
                                logger.debug(f"Failed to re-fetch state after new_turn delay: {e}")

                            # Clear stale pending win plan if game has advanced
                            if self._pending_win_plan and turn_num > self._pending_win_plan_turn + 1:
                                logger.info(f"Clearing stale win plan (plan turn {self._pending_win_plan_turn}, now {turn_num})")
                                self._pending_win_plan = None
                                self.ui.status("WIN-PLAN", "")

                            # Spawn background win plan worker (non-blocking)
                            # Skip when autopilot is active — it handles its own strategy
                            if is_my_turn and turn_num > self._win_plan_turn:
                                self._win_plan_turn = turn_num
                                threading.Thread(
                                    target=self._win_plan_worker,
                                    args=(curr_state,),
                                    daemon=True,
                                ).start()

                        # DELAY BUFFER: For mulligan decisions, wait for hand zone to populate.
                        # SubmitDeckReq arrives before the GameStateMessage with hand cards.
                        if trigger == "decision_required" and curr_state.get("pending_decision") == "Mulligan":
                            time.sleep(0.5)  # 500ms to allow hand zone update
                            try:
                                self._mcp.poll_log()
                            except Exception:
                                pass
                            try:
                                curr_state = self._mcp.get_game_state()
                            except Exception as e:
                                logger.debug(f"Failed to re-fetch state after mulligan delay: {e}")

                        # DELAY BUFFER: For spell_resolved, wait briefly for ETB triggers to resolve.
                        # When spells like Sheltered by Ghosts resolve, the exile/removal happens
                        # via a subsequent ETB trigger that needs another game state diff.
                        if trigger == "spell_resolved":
                            time.sleep(0.4)  # 400ms to allow ETB triggers to resolve
                            try:
                                self._mcp.poll_log()
                            except Exception:
                                pass
                            try:
                                curr_state = self._mcp.get_game_state()
                            except Exception as e:
                                logger.debug(f"Failed to re-fetch state after spell_resolved delay: {e}")

                        # Check if there's a pending decision (scry, discard, target, etc.)
                        # If so, suppress step-by-step "what's next" triggers until decision resolves
                        pending_decision = curr_state.get("pending_decision")
                        has_pending_decision = pending_decision is not None

                        # Step-by-step triggers: land_played, spell_resolved, and combat
                        # BUT suppress if there's a pending decision - wait for it to resolve first
                        is_step_by_step = (
                            trigger in ("land_played", "spell_resolved", "combat_attackers", "combat_blockers")
                            and not has_pending_decision
                        )

                        # Log when we're waiting for a decision
                        if trigger in ("land_played", "spell_resolved") and has_pending_decision:
                            logger.debug(f"Suppressing {trigger} - waiting for decision: {pending_decision}")

                        # Combat and priority triggers only in "every_priority" mode
                        is_frequent = (
                            self.advice_frequency == "every_priority" and
                            trigger in ("priority_gained", "combat_attackers", "combat_blockers") and
                            (turn_num > last_advice_turn or phase != last_advice_phase)
                        )

                        # Additional check: Don't spam priority triggers if we just advised on new_turn
                        # unless distinct phase
                        if trigger == "priority_gained" and is_new_turn:
                            continue

                        # DECISION PRIORITY: If there's a decision required, skip non-critical triggers
                        # in the same batch to ensure the decision is the primary focus.
                        if "decision_required" in triggers and trigger != "decision_required" and not is_critical:
                            continue

                        should_advise = is_critical or is_new_turn or is_opponent_turn or is_step_by_step or is_frequent

                        if not should_advise:
                            continue

                        logger.info(f"TRIGGER: {trigger}")

                        # NOISE SUPPRESSION: Skip advice when something is on the stack
                        # and player can't respond. Prevents confusing "let it resolve" advice
                        # when an ETB trigger (yours or opponent's) is just passing through.
                        # NOTE: "new_turn" is excluded — when the player's turn starts, they
                        # always need advice even if a stale ability is still on the stack.
                        stack = curr_state.get("stack", [])
                        if stack and trigger in ("land_played", "spell_resolved", "priority_gained"):
                            has_instants = self._trigger._has_castable_instants(curr_state)
                            if not has_instants:
                                logger.info(f"Quiet: {trigger} (stack active, no responses)")
                                continue

                        # NOISE SUPPRESSION: Skip LLM call when player has no meaningful options.
                        # Saves ~3-5s API call + TTS for obvious "pass priority" situations.
                        QUIET_TRIGGERS = {"stack_spell_yours", "stack_spell_opponent", "priority_gained", "spell_resolved", "opponent_turn"}
                        if trigger in QUIET_TRIGGERS:
                            has_instants = self._trigger._has_castable_instants(curr_state)
                            stack = curr_state.get("stack", [])

                            # Own spell on stack with no instants to respond → auto-pass
                            if trigger == "stack_spell_yours" and not has_instants:
                                logger.info(f"Quiet: {trigger} (own spell, no responses)")
                                continue

                            # Opponent spell/ability on stack with no instant-speed responses → quiet
                            # This covers both opponent's turn AND your turn (e.g. opponent ETB triggers)
                            if trigger == "stack_spell_opponent" and not has_instants:
                                logger.info(f"Quiet: {trigger} (no instant-speed responses)")
                                continue

                            # Opponent's action or priority with no instant-speed options → quiet
                            if not is_my_turn and not has_instants:
                                logger.info(f"Quiet: {trigger} (opp turn, no instants)")
                                continue

                            # Spell resolved but nothing castable in hand and not my main phase
                            if trigger == "spell_resolved" and not has_instants and not is_my_turn:
                                logger.info(f"Quiet: {trigger} (resolved, no options)")
                                continue

                        # THREAT DETECTION: Direct speaking for instant response (no LLM needed)
                        if trigger == "threat_detected" and hasattr(self._trigger, '_last_threat'):
                            threat = self._trigger._last_threat
                            advice = f"Warning! {threat['name']}. {threat['warning']}"
                            logger.info(f"THREAT ALERT: {advice}")
                            self._record_advice(advice, trigger, game_state=curr_state)
                            last_advice_turn = turn_num
                            last_advice_phase = phase

                            # Speak immediately and display
                            self.ui.advice(advice, "THREAT")
                            self.speak_advice(advice)
                            continue  # Don't send to LLM

                        if self._coach:
                            # Snapshot turn state BEFORE the (slow) LLM call
                            pre_advice_turn = turn_num
                            pre_advice_active = active_seat
                            pre_advice_phase = phase
                            pre_advice_step = turn.get("step", "")

                            advice = self._coach.get_advice(
                                curr_state,
                                trigger=trigger,
                                style=self.advice_style
                            )
                            logger.info(f"ADVICE: {advice}")

                            # STALENESS CHECK: Re-poll game state after the LLM call.
                            # If the turn, active player, phase, or combat step changed
                            # while waiting for the API response (~5s), the advice is
                            # stale and would confuse the player (e.g. "attack!" after
                            # combat is over).
                            fresh_state = self._mcp.get_game_state()
                            fresh_turn = fresh_state.get("turn", {})
                            fresh_turn_num = fresh_turn.get("turn_number", 0)
                            fresh_active = fresh_turn.get("active_player", 0)
                            fresh_phase = fresh_turn.get("phase", "")
                            fresh_step = fresh_turn.get("step", "")

                            # For opponent_turn analysis, allow turn to advance by 1
                            # (opponent's turn → your turn is expected progression)
                            if trigger == "opponent_turn":
                                is_stale = fresh_turn_num > pre_advice_turn + 1
                            else:
                                is_stale = (
                                    fresh_turn_num != pre_advice_turn
                                    or fresh_active != pre_advice_active
                                    or fresh_phase != pre_advice_phase
                                    or fresh_step != pre_advice_step
                                )
                            if is_stale:
                                stale_label = "[STALE - discarded]"
                                logger.info(
                                    f"Discarding stale advice: turn {pre_advice_turn}->{fresh_turn_num}, "
                                    f"active {pre_advice_active}->{fresh_active}, phase {pre_advice_phase}->{fresh_phase}, "
                                    f"step {pre_advice_step}->{fresh_step}"
                                )
                                self._record_advice(
                                    f"{stale_label} {advice}", trigger, game_state=curr_state
                                )
                                last_advice_turn = turn_num
                                last_advice_phase = phase
                                continue

                            last_advice_turn = turn_num
                            last_advice_phase = phase

                            # Build seat info for display
                            local_seat = None
                            for p in curr_state.get("players", []):
                                if p.get("is_local"):
                                    local_seat = p.get("seat_id")
                                    break

                            battlefield = curr_state.get("battlefield", [])
                            your_cards = [c for c in battlefield if c.get("owner_seat_id") == local_seat]
                            untapped_lands = sum(1 for c in your_cards
                                                 if "land" in c.get("type_line", "").lower()
                                                 and not c.get("is_tapped"))
                            seat_info = f"Seat {local_seat}|{untapped_lands} mana|{self.backend_name}" if local_seat else "Seat ?"

                            # Skip empty responses (e.g. from timeout)
                            if not advice or not advice.strip():
                                logger.warning("Empty advice response (model timeout?), skipping")
                                continue

                            # Don't speak error/fallback messages aloud
                            if advice.startswith("Error") or "didn't catch that" in advice:
                                logger.warning(f"Suppressing error advice from TTS: {advice[:80]}")
                                self.ui.error(advice)
                            else:
                                self._record_advice(advice, trigger, game_state=curr_state)
                                self.ui.advice(advice, seat_info)
                                # Non-blocking TTS: lets the loop poll for new
                                # game states (e.g. Select Targets) immediately.
                                # New advice will interrupt stale speech.
                                self.speak_advice(advice, blocking=False)

                prev_state = curr_state

            except Exception as e:
                logger.error(f"Coaching loop error: {e}")
                logger.debug(traceback.format_exc())
                self._record_error(str(e), "coaching_loop")

            time.sleep(1.5)

        logger.info("Coaching loop stopped")

    def _voice_loop(self) -> None:
        """Handle voice input for questions (PTT mode with Whisper + Kokoro)."""
        if not self._voice_input:
            return

        logger.info(f"Voice loop started ({self.voice_mode})")
        if self.voice_mode == "ptt":
            self.ui.log("\n[MIC] Press F4 to ask (tap for quick advice)\n")
        else:
            self.ui.log("\n[MIC] Voice activation enabled\n")

        self._voice_input.start()

        while self._running:
            try:
                text = self._voice_input.wait_for_speech(timeout=2.0)

                if not self._voice_input._result_ready.is_set():
                    continue

                if self._coach and self._mcp:
                    # Force a log poll to get freshest state before advice
                    self._mcp.poll_log()
                    game_state = self._mcp.get_game_state()

                    # Get current seat and mana for display
                    local_seat = None
                    for p in game_state.get("players", []):
                        if p.get("is_local"):
                            local_seat = p.get("seat_id")
                            break

                    # Count untapped lands for mana display
                    battlefield = game_state.get("battlefield", [])
                    your_cards = [c for c in battlefield if c.get("owner_seat_id") == local_seat]
                    untapped_lands = sum(1 for c in your_cards
                                         if "land" in c.get("type_line", "").lower()
                                         and not c.get("is_tapped"))

                    seat_info = f"Seat {local_seat}|{untapped_lands} mana|{self.backend_name}" if local_seat else "Seat ?"

                    # Check if we can use direct audio with Gemini
                    audio_data = self._voice_input.get_last_audio()
                    use_direct_audio = (
                        audio_data is not None and
                        len(audio_data) > 0 and
                        hasattr(self._coach._backend, 'complete_with_audio')
                    )

                    if use_direct_audio:
                        # Direct audio to Gemini - skip local transcription
                        logger.info(f"AUDIO INPUT: {len(audio_data)} samples -> Gemini")
                        self.ui.log("\n[AUDIO] Sending to Gemini...")
                        context = self._coach._format_game_context(game_state)

                        # FORCE specific answer mode
                        user_message = (
                            f"{context}\n\n"
                            "IMPORTANT: The user just asked a specific question via audio (attached). "
                            "Do NOT give generic gameplay advice. "
                            "Listen to the audio and answer EXACTLY what they asked. "
                            "If they asked about a specific card, interaction, or rule, explain it in detail. "
                            "Ignore your usual brevity constraints if needed to answer fully."
                        )
                        advice = self._coach._backend.complete_with_audio(
                            self._coach._system_prompt,
                            user_message,
                            audio_data
                        )
                    elif text and text.strip():
                        logger.info(f"QUESTION: {text}")
                        self.ui.log(f"\n[YOU] {text}")
                        advice = self._coach.get_advice(game_state, question=text)
                    else:
                        logger.info("QUICK ADVICE (F4 tap)")
                        self.ui.log("\n[QUICK] Analyzing...")
                        advice = self._coach.get_advice(game_state, trigger="user_request")

                    logger.info(f"RESPONSE: {advice}")
                    self.ui.advice(advice, seat_info)
                    self.speak_advice(advice)

                    # Record for debug history with the same game state
                    trigger = "voice_audio" if use_direct_audio else ("voice_question" if text else "voice_quick")
                    self._record_advice(advice, trigger, game_state=game_state)

            except Exception as e:
                if self._running:
                    logger.error(f"Voice loop error: {e}")
                    self._record_error(str(e), "voice_loop")

        self._voice_input.stop()
        logger.info("Voice loop stopped")

    def _on_mute_hotkey(self) -> None:
        """F5 - Toggle TTS mute."""
        if self._voice_output:
            muted = self._voice_output.toggle_mute()
            self.ui.status("VOICE", f"{'MUTED' if muted else 'UNMUTED'} (saved)")
        else:
            self.ui.status("VOICE", "TTS not enabled")

    def _on_voice_hotkey(self) -> None:
        """F6 - Change TTS voice."""
        if self._voice_output:
            voice_id, desc = self._voice_output.next_voice()
            self.ui.status("VOICE", f"Changed to: {desc} (saved)")
            try:
                self._voice_output.speak("Voice changed.", blocking=False)
            except Exception:
                pass
        else:
            self.ui.status("VOICE", "TTS not enabled")


    def _on_speed_hotkey(self) -> None:
        """F8 - Cycle TTS speed."""
        if self._voice_output:
            speed = self._voice_output.cycle_speed()
            self.ui.status("SPEED", f"{speed}x")
            try:
                self._voice_output.speak("Speed changed.", blocking=False)
            except Exception:
                pass
        else:
            self.ui.status("SPEED", "TTS not enabled")

    def save_bug_report(self, reason: str = "User Request") -> Optional["Path"]:
        """Save comprehensive bug report and return path."""
        bug_dir = LOG_DIR / "bug_reports"
        bug_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bug_file = bug_dir / f"bug_{timestamp}.json"

        try:
            # Collect comprehensive debug info
            report = self._collect_debug_info()
            report["reason"] = reason

            with open(bug_file, "w") as f:
                json.dump(report, f, indent=2, default=str)

            # Make path clickable and copy a shareable link to clipboard
            file_url = f"file:///{str(bug_file).replace(chr(92), '/')}"
            clickable = f"\x1b]8;;{file_url}\x1b\\{bug_file}\x1b]8;;\x1b\\"

            clipboard_success = copy_to_clipboard(file_url)

            if clipboard_success:
                self.ui.log(f"\n[BUG] Saved: {clickable}")
                self.ui.log(f"[BUG] Link copied to clipboard: {file_url}\n")
            else:
                self.ui.log(f"\n[BUG] Saved: {clickable}")
                self.ui.log(f"[BUG] Link: {file_url}\n")

            return bug_file
        except Exception as e:
            self.ui.error(f"\n[BUG] Failed: {e}\n")
            logger.exception("Bug report failed")
            return None

    def _on_bug_report_hotkey(self) -> None:
        """F7 - Save comprehensive bug report and copy path to clipboard."""
        bug_path = self.save_bug_report("Hotkey F7")
        if bug_path:
            self.ui.log("[BUG] Path copied to clipboard. Paste it anywhere.")

    def take_screenshot_analysis(self) -> None:
        """Capture screen and request visual analysis (e.g. Mulligan)."""
        # Check if backend supports vision
        backend_lower = self.backend_name.lower()
        vision_capable = backend_lower in ("claude-code", "gemini-cli", "proxy")

        if not vision_capable:
            self.ui.log("[red]Current backend does not support image analysis.[/]")
            return
             
        try:
            from PIL import ImageGrab
            import io
            
            # Capture primary screen
            img = ImageGrab.grab()
            
            # Resize if huge to save bandwidth/latency
            img.thumbnail((1920, 1080)) 
            
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            png_bytes = buf.getvalue()

            # Save to temp file for CLI backends
            import tempfile
            tmp_file = None
            file_url = None
            if backend_lower in ("claude-code", "gemini-cli"):
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
                tmp.write(png_bytes)
                tmp.close()
                tmp_file = tmp.name
                file_url = f"file:///{tmp_file.replace(chr(92), '/')}"

            self.ui.log("[yellow]Analyzing screenshot...[/]")
            
            # Context
            try:
                game_state = self.get_game_state()
            except:
                game_state = {}

            system_prompt = """You are an expert Magic: The Gathering Arena coach. Look at this screenshot and give immediate, actionable advice based on what you see.

DETECT THE SITUATION AND RESPOND:
- If showing a hand before game starts (7 cards, "Keep/Mulligan" buttons): Start with "KEEP" or "MULLIGAN", then brief reason.
- If showing a Scry prompt (card with TOP/BOTTOM options): Say "TOP" or "BOTTOM" with one-sentence reasoning.
- If showing Surveil: Say "GRAVEYARD" or "LIBRARY" based on card value and graveyard synergies.
- If showing a modal spell or choice (multiple options highlighted): Recommend which option and why.
- If showing target selection (arrows, glowing borders): Say which target to pick.
- If showing combat with attackers/blockers: Give combat math and optimal blocks/attacks.
- If showing the game board during your turn: Recommend the best play sequence.
- If opponent is acting: Note if you should respond or let it resolve.

BE DECISIVE. Start with your recommendation immediately. Keep it to 1-2 sentences spoken aloud."""

            # We can optionally format game state into the prompt if available
            ctx = ""
            if game_state:
                turn_num = game_state.get('turn', {}).get('turn_number', '?')
                phase = game_state.get('turn', {}).get('phase', '')
                life_you = 20
                life_opp = 20
                for p in game_state.get('players', []):
                    if p.get('is_local'):
                        life_you = p.get('life_total', 20)
                    else:
                        life_opp = p.get('life_total', 20)
                ctx = f" Turn {turn_num}. Life: You {life_you}, Opp {life_opp}."

            user_msg = f"What should I do here?{ctx}"

            if backend_lower == "proxy":
                # Proxy backend: send image as base64 via OpenAI multimodal API
                from arenamcp.coach import ProxyBackend
                vision_backend = ProxyBackend(model=self.model_name)
                self.ui.log("[yellow]Analyzing screenshot via proxy...[/]")
                advice = vision_backend.complete_with_image(system_prompt, user_msg, png_bytes)

                if advice and not advice.startswith("Error"):
                    self.ui.advice(advice, "Visual Analysis")
                    if self._auto_speak:
                        self.speak_advice(advice, blocking=False)
                else:
                    self.ui.log(f"[red]Vision analysis failed: {advice or 'empty response'}[/]")

            elif file_url:
                # CLI backends that take file paths
                from arenamcp.coach import create_backend, ClaudeCodeBackend
                raw_path = tmp_file or ""

                if backend_lower == "gemini-cli":
                    vision_backend = create_backend("gemini-cli", model=self.model_name)
                    vision_backend.timeout_s = float(os.environ.get("VISION_TIMEOUT_S", "20"))
                    vision_msg = (
                        f"{system_prompt}\n\n"
                        f"Image to analyze: {raw_path}\n"
                        f"{user_msg}"
                    )
                    advice = vision_backend.complete(system_prompt, vision_msg)
                else:
                    temp_dir = os.path.dirname(tmp_file) if tmp_file else None
                    add_dirs = [temp_dir] if temp_dir else []
                    vision_backend = ClaudeCodeBackend(
                        model=os.environ.get("VISION_BACKEND_MODEL", "sonnet"),
                        add_dirs=add_dirs,
                        tools=["Read"],
                        permission_mode="dontAsk",
                    )
                    vision_backend.timeout_s = float(os.environ.get("VISION_TIMEOUT_S", "15"))
                    vision_msg = (
                        f"Image path: {raw_path}\n"
                        f"Image URL: {file_url}\n"
                        f"{user_msg}\n"
                        "Use the Read tool to open the image file."
                    )
                    advice = vision_backend.complete(system_prompt, vision_msg)

                if hasattr(vision_backend, 'close'):
                    vision_backend.close()

                if advice and not advice.startswith("Error"):
                    self.ui.advice(advice, "Visual Analysis")
                    if self._auto_speak:
                        self.speak_advice(advice, blocking=False)
                else:
                    self.ui.log(f"[red]Vision analysis failed: {advice or 'empty response'}[/]")
            else:
                self.ui.log("[red]Current backend does not support image analysis.[/]")

        except ImportError:
            self.ui.log("[red]Missing 'Pillow' library. Install with: pip install Pillow[/]")
        except Exception as e:
            self.ui.log(f"[red]Screenshot error: {e}[/]")
            import traceback
            print(f"Screenshot Error: {e}")

    def _collect_debug_info(self) -> dict:
        """Collect comprehensive debug information for bug reports."""
        import platform
        from arenamcp import __version__

        report = {
            "timestamp": datetime.now().isoformat(),
            "version": __version__,

            # System info
            "system": {
                "platform": platform.platform(),
                "python_version": platform.python_version(),
                "machine": platform.machine(),
                "packages": self._get_package_versions(),
            },

            # Coach configuration
            "config": {
                "backend": self.backend_name,
                "model": self.model_name,
                "voice_mode": self.voice_mode,
                "advice_style": self.advice_style,
                "advice_frequency": self.advice_frequency,
                "draft_mode": self.draft_mode,
                "set_code": self.set_code,
                "auto_speak": self._auto_speak,
            },

            # Settings from disk
            "settings": dict(self.settings._data) if self.settings else {},

            # MTGA log file status
            "mtga_log": self._get_mtga_log_status(),

            # Current game state
            "game_state": self._mcp.get_game_state() if self._mcp else {},

            # Match context
            "match_context": self._get_match_context(),

            # Draft state if active
            "draft_state": self._mcp.get_draft_pack() if self._mcp else {},

            # Voice state
            "voice": {
                "tts_enabled": self._voice_output is not None,
                "tts_muted": self._voice_output._muted if self._voice_output else None,
                "tts_voice": self._voice_output.current_voice if self._voice_output else None,
                "stt_enabled": self._voice_input is not None,
            },

            # Recent advice history
            "advice_history": list(self._advice_history) if hasattr(self, '_advice_history') else [],

            # LLM context (what the coach sees)
            "llm_context": self._get_llm_context(),

            # Recent log entries (last 100 lines)
            "recent_logs": self._get_recent_logs(100),

            # Card enrichment failures (oracle text lookups that failed)
            "enrichment_failures": self._get_enrichment_failures(),

            # Error state
            "errors": list(self._recent_errors) if hasattr(self, '_recent_errors') else [],

            # Uptime
            "uptime_seconds": (datetime.now() - self._start_time).total_seconds() if hasattr(self, '_start_time') else None,
        }

        return report

    @staticmethod
    def _get_package_versions() -> dict[str, str]:
        """Get versions of key installed packages."""
        packages = [
            "textual", "openai", "mcp", "watchdog", "requests",
            "faster_whisper", "kokoro", "anthropic", "google.genai",
        ]
        versions: dict[str, str] = {}
        for pkg in packages:
            try:
                mod = __import__(pkg)
                versions[pkg] = getattr(mod, "__version__", "installed")
            except ImportError:
                versions[pkg] = "not installed"
        return versions

    def _get_mtga_log_status(self) -> dict:
        """Get MTGA Player.log file status."""
        import os
        # Use the same path logic as watcher.py: LOCALAPPDATA (AppData\Local)
        # -> parent (AppData) -> LocalLow sibling
        _local_appdata = os.environ.get("LOCALAPPDATA", "")
        if _local_appdata:
            default_path = str(
                Path(os.path.dirname(_local_appdata)) / "LocalLow"
                / "Wizards Of The Coast" / "MTGA" / "Player.log"
            )
        else:
            default_path = ""
        log_path = os.environ.get("MTGA_LOG_PATH", default_path)
        result: dict = {"path": log_path}
        try:
            p = Path(log_path)
            result["exists"] = p.exists()
            if p.exists():
                stat = p.stat()
                result["size_bytes"] = stat.st_size
                result["last_modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        except Exception as e:
            result["error"] = str(e)
        return result

    def _get_match_context(self) -> dict:
        """Get match-level context: match ID, opponent cards, recent events."""
        ctx: dict = {}
        try:
            if not self._mcp:
                return ctx
            from arenamcp.server import game_state
            ctx["match_id"] = getattr(game_state, "match_id", None)
            ctx["local_seat_id"] = getattr(game_state, "local_seat_id", None)
            ctx["seat_source"] = game_state.get_seat_source_name() if hasattr(game_state, "get_seat_source_name") else None
            # Opponent played cards
            try:
                from arenamcp.server import get_opponent_played_cards
                opp = get_opponent_played_cards()
                ctx["opponent_played_cards"] = opp if opp else []
            except Exception:
                ctx["opponent_played_cards"] = []
            # Recent game events
            snapshot = game_state.get_snapshot() if hasattr(game_state, "get_snapshot") else {}
            ctx["recent_events"] = snapshot.get("recent_events", [])[-20:]
            ctx["damage_taken"] = snapshot.get("damage_taken", {})
        except Exception as e:
            ctx["error"] = str(e)
        return ctx

    def _get_llm_context(self) -> dict:
        """Get the LLM context from the most recent advice (what the LLM actually saw).

        IMPORTANT: This captures the game state that was sent to the LLM during the last
        advice generation, NOT the current game state. This prevents timing bugs where
        the game state changes between advice generation and bug report generation.
        """
        context = {
            "system_prompt": None,
            "formatted_game_state": None,
        }

        try:
            if self._coach:
                context["system_prompt"] = getattr(self._coach, '_system_prompt', None)
                context["deck_strategy"] = getattr(self._coach, '_deck_strategy', None)

                # Use the game_context from the last advice_history entry instead of
                # regenerating it, to avoid timing issues where game state has changed
                if hasattr(self, '_advice_history') and self._advice_history:
                    context["formatted_game_state"] = self._advice_history[-1].get("game_context")
                # Fallback: if no advice history, generate from current state
                elif self._mcp and hasattr(self._coach, '_format_game_context'):
                    game_state = self._mcp.get_game_state()
                    context["formatted_game_state"] = self._coach._format_game_context(game_state)
        except Exception as e:
            context["error"] = str(e)

        return context

    def _get_enrichment_failures(self) -> list:
        """Get card enrichment (oracle text lookup) failures for bug reports."""
        try:
            from arenamcp.server import get_enrichment_failures
            return get_enrichment_failures()
        except Exception:
            return []

    def _get_recent_logs(self, num_lines: int = 100) -> list:
        """Get recent log entries from standalone.log."""
        try:
            if LOG_FILE.exists():
                with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                    # Read last N lines efficiently
                    lines = f.readlines()
                    return lines[-num_lines:]
        except Exception as e:
            return [f"Error reading logs: {e}"]
        return []

    def _record_advice(self, advice: str, trigger: str, game_context: str = None, game_state: dict = None) -> None:
        """Record advice for debug history with full game state.

        Args:
            advice: The advice text that was given
            trigger: What triggered this advice
            game_context: Pre-formatted context string (optional)
            game_state: Game state dict to use for context (optional, avoids re-polling)
        """
        if not hasattr(self, '_advice_history'):
            self._advice_history = []

        # Use provided game_state, or fetch fresh if needed
        # IMPORTANT: If game_state is provided, use it to avoid timing issues
        # where the game state changes between advice generation and recording
        if game_context is None and self._coach:
            try:
                if game_state is None and self._mcp:
                    game_state = self._mcp.get_game_state()
                if game_state and hasattr(self._coach, '_format_game_context'):
                    game_context = self._coach._format_game_context(game_state)
            except Exception:
                pass

        # Extract key game state fields for bug reports (turn, life, board snapshot)
        game_snapshot = None
        if game_state:
            try:
                turn = game_state.get("turn", {})
                players = game_state.get("players", [])
                game_snapshot = {
                    "turn_number": turn.get("turn_number"),
                    "phase": turn.get("phase"),
                    "active_player": turn.get("active_player"),
                    "players": [
                        {"seat_id": p.get("seat_id"), "life_total": p.get("life_total")}
                        for p in players
                    ],
                    "battlefield_count": len(game_state.get("battlefield", [])),
                    "hand_count": len(game_state.get("hand", [])),
                }
            except Exception:
                pass

        entry = {
            "timestamp": datetime.now().isoformat(),
            "trigger": trigger,
            "advice": advice,
            "game_context": game_context[:8000] if game_context else None,
            "game_snapshot": game_snapshot,
        }
        self._advice_history.append(entry)

        # Keep only last 50 entries (enough for post-match analysis)
        if len(self._advice_history) > 50:
            self._advice_history = self._advice_history[-50:]
        
        # Also record to match recording for post-match analysis
        try:
            from arenamcp.match_validator import get_current_recording
            current = get_current_recording()
            if current:
                # Extract turn/phase from game state
                turn_info = game_state.get("turn", {}) if game_state else {}
                parsed_turn = turn_info.get("turn_number", 0)
                parsed_phase = turn_info.get("phase", "")
                current.add_advice_event(
                    trigger=trigger,
                    advice=advice,
                    game_context=game_context or "",
                    parsed_turn=parsed_turn,
                    parsed_phase=parsed_phase
                )
        except Exception:
            pass  # Don't fail if recording isn't active

    def _record_error(self, error: str, context: str = None) -> None:
        """Record error for debug history."""
        if not hasattr(self, '_recent_errors'):
            self._recent_errors = []

        entry = {
            "timestamp": datetime.now().isoformat(),
            "error": error,
            "context": context,
        }
        self._recent_errors.append(entry)

        # Keep only last 10 errors
        if len(self._recent_errors) > 10:
            self._recent_errors = self._recent_errors[-10:]

    def _detect_match_result(self) -> str:
        """Detect win/loss from recent game events.

        Returns "win", "loss", or "unknown".
        """
        try:
            game_state = self._mcp.get_game_state()
            recent = game_state.get("recent_events", [])
            for event in reversed(recent):
                if event.get("type") == "game_end":
                    return event.get("result", "unknown")
        except Exception as e:
            logger.debug(f"Could not detect match result: {e}")
        return "unknown"

    def _post_match_analysis_worker(self) -> None:
        """Background worker: generate post-match strategic analysis.

        Spawned when a match ends. Uses a dedicated backend to avoid
        lock contention with real-time coaching.
        """
        try:
            advice_history = self._saved_advice_history
            match_result = self._last_match_result or "unknown"
            final_state = self._last_match_final_state

            if not advice_history:
                logger.info("No advice history for post-match analysis")
                return

            if not self._coach:
                logger.warning("No coach engine for post-match analysis")
                return

            logger.info(
                f"Post-match analysis started: {len(advice_history)} entries, result={match_result}"
            )
            self.ui.status("ANALYSIS", "Generating post-match analysis...")

            # Compute match duration from advice snapshots
            turns = [
                e.get("game_snapshot", {}).get("turn_number", 0)
                for e in advice_history
                if e.get("game_snapshot")
            ]
            match_duration = max(turns) if turns else 0

            # Extract final life totals
            final_life = {}
            if final_state:
                for p in final_state.get("players", []):
                    final_life[p.get("seat_id")] = p.get("life_total")

            # Extract opponent played cards (names from battlefield/graveyard)
            opponent_cards = []
            if final_state:
                opp_cards = final_state.get("opponent_played_cards", [])
                for card in opp_cards:
                    name = card.get("name") if isinstance(card, dict) else str(card)
                    if name:
                        opponent_cards.append(name)

            # Get deck strategy
            deck_strategy = getattr(self._coach, '_deck_strategy', "") or ""

            # Create dedicated backend (avoid lock contention with coaching)
            from arenamcp.coach import create_backend
            try:
                analysis_backend = create_backend(self._backend_name, model=self._model_name)
            except Exception as e:
                logger.error(f"Failed to create analysis backend: {e}")
                self.ui.status("ANALYSIS", "")
                return

            analysis = self._coach.generate_post_match_analysis(
                advice_history=advice_history,
                match_result=match_result,
                match_duration_turns=match_duration,
                deck_strategy=deck_strategy,
                final_life_totals=final_life,
                opponent_played_cards=opponent_cards,
                backend=analysis_backend,
            )

            if hasattr(analysis_backend, 'close'):
                analysis_backend.close()

            if not analysis:
                logger.warning("Post-match analysis returned empty")
                self.ui.status("ANALYSIS", "")
                return

            # Split off the SPOKEN: summary for TTS
            spoken_summary = ""
            display_analysis = analysis
            if "SPOKEN:" in analysis:
                parts = analysis.rsplit("SPOKEN:", 1)
                display_analysis = parts[0].strip()
                spoken_summary = parts[1].strip()

            # Display in TUI
            result_label = "VICTORY" if match_result == "win" else "DEFEAT" if match_result == "loss" else "MATCH COMPLETE"
            self.ui.log("")
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log(f"[bold green]  POST-MATCH ANALYSIS — {result_label}[/]")
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log(display_analysis)
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log("")

            self.ui.status("ANALYSIS", f"Match analysis complete ({match_result})")

            # Speak the short summary via TTS
            if spoken_summary:
                self.speak_advice(spoken_summary, blocking=False)

            logger.info(f"Post-match analysis complete: {len(analysis)} chars")

        except Exception as e:
            logger.error(f"Post-match analysis error: {e}", exc_info=True)
            self.ui.status("ANALYSIS", "")
        finally:
            # Clear saved data
            self._saved_advice_history = []
            self._last_match_result = None
            self._last_match_final_state = None

    def trigger_match_analysis(self) -> None:
        """Manually trigger post-match analysis (from Analyze Match button).

        Uses current advice history (does not require match end).
        """
        if not self._advice_history:
            self.ui.log("[yellow]No advice history to analyze.[/]")
            return

        if self._saved_advice_history:
            self.ui.log("[yellow]Analysis already in progress...[/]")
            return

        self._saved_advice_history = list(self._advice_history)
        self._last_match_result = self._detect_match_result()
        try:
            game_state = self._mcp.get_game_state()
            self._last_match_final_state = game_state
        except Exception:
            self._last_match_final_state = None

        threading.Thread(
            target=self._post_match_analysis_worker,
            daemon=True,
        ).start()

    def _on_swap_seat_hotkey(self) -> None:
        """F8 - Swap local seat (fix wrong player detection)."""
        if not self._mcp:
            return

        try:
            from arenamcp.server import game_state
            # Get current state
            players = list(game_state.players.keys())
            current = game_state.local_seat_id

            if len(players) >= 2:
                # Swap to the other seat
                new_seat = [s for s in players if s != current][0] if current else players[0]
                # Use source=3 (User) to lock it
                game_state.set_local_seat_id(new_seat, source=3)
                self.ui.status("SEAT", f"Swapped to Seat {new_seat} (LOCKED - won't auto-change)")
                logger.info(f"Manual seat swap: {current} -> {new_seat} (locked by User)")
            else:
                self.ui.status("SEAT", f"Only {len(players)} player(s) detected, cannot swap")
        except Exception as e:
            self.ui.error(f"Seat swap failed: {e}")
            logger.error(f"Seat swap error: {e}")

    def _compute_library_summary(self, game_state: dict) -> str:
        """Compute remaining library by subtracting visible cards from deck_cards.

        Returns a compact summary like "~28 cards: 2x Mountain, 1x Lightning Bolt, ..."
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
                except Exception:
                    card_info_cache[grp_id] = {"name": f"Unknown({grp_id})"}
            name = card_info_cache[grp_id].get("name", f"Unknown({grp_id})")
            name_counts[name] = name_counts.get(name, 0) + 1

        # Sort by count descending, cap at top 15 unique cards
        sorted_cards = sorted(name_counts.items(), key=lambda x: -x[1])[:15]
        total = len(remaining)
        shown = sum(count for _, count in sorted_cards)

        # Build detailed summary with oracle text for non-basic lands
        # so the LLM doesn't hallucinate card abilities
        lines = [f"~{total} cards remaining in library:"]
        # Reverse map: name -> grp_id (for info lookup)
        name_to_grp: dict[str, int] = {}
        for grp_id, info in card_info_cache.items():
            name = info.get("name", "")
            if name not in name_to_grp:
                name_to_grp[name] = grp_id

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
                # Truncate long oracle text
                if len(oracle) > 120:
                    oracle = oracle[:117] + "..."
                detail = f"  {count}x {name}"
                if mana:
                    detail += f" {mana}"
                if oracle:
                    detail += f" — {oracle}"
                lines.append(detail)

        if shown < total:
            lines.append(f"  ... and {total - shown} more")

        return "\n".join(lines)

    def _on_win_plan_hotkey(self, turns: int) -> None:
        """Handle win-in-N-turns hotkey press (keys 2-8)."""
        if not self._coach or not self._mcp:
            return

        # 5-second cooldown to prevent spam
        now = time.time()
        last = getattr(self, '_last_win_plan_time', 0.0)
        if now - last < 5.0:
            self.ui.status("WIN-PLAN", "Cooldown — wait a few seconds")
            return
        self._last_win_plan_time = now

        def _do():
            try:
                # Ensure latest log data is processed
                self._mcp.poll_log()
                game_state = self._mcp.get_game_state()

                turn_num = game_state.get("turn", {}).get("turn_number", 0)
                if turn_num <= 0:
                    self.ui.status("WIN-PLAN", "No active game")
                    logger.info("Win plan: no active game (turn=0)")
                    return

                self.ui.status("WIN-PLAN", f"Planning win in {turns} turns...")
                self.ui.log(f"\n[bold cyan]--- WIN-IN-{turns} PLAN (generating...) ---[/]")
                logger.info(f"Win plan: requesting {turns}-turn plan")

                library_summary = self._compute_library_summary(game_state)
                plan = self._coach.get_win_plan(game_state, turns, library_summary)

                logger.info(f"Win plan: got response, {len(plan)} chars")
                if plan:
                    self.ui.advice(plan, f"WIN-IN-{turns}")
                    self._record_advice(plan, f"win_in_{turns}", game_state=game_state)
                    self.speak_advice(plan, blocking=False)
                else:
                    self.ui.status("WIN-PLAN", "No plan generated (timeout or error)")
                    self.ui.log("[yellow]Win plan returned empty — API may have timed out[/]")
            except Exception as e:
                logger.error(f"Win plan error: {e}", exc_info=True)
                self.ui.error(f"Win plan failed: {e}")

        threading.Thread(target=_do, daemon=True).start()

    def _win_plan_worker(self, game_state: dict) -> None:
        """Background worker: compute win-in-2 and win-in-3 plans using a thinking model.

        Spawned automatically at the start of each of your turns. Uses a separate
        thinking-enabled backend so it doesn't interfere with real-time coaching.

        Parses VIABLE: YES/NO from the LLM response. Only stores viable plans
        and plays a sound alert. The plan is read aloud only on Ctrl+0 press.
        """
        import concurrent.futures

        try:
            # Lazy-init thinking model
            if self._thinking_model is None:
                from arenamcp.coach import pick_thinking_model
                self._thinking_model = pick_thinking_model()
                if self._thinking_model is None:
                    logger.info("No thinking model available — win plan worker disabled")
                    # Sentinel to avoid retrying every turn
                    self._thinking_model = ""
                    return
            if self._thinking_model == "":
                return  # Previously determined unavailable

            logger.info(f"Win plan worker started (thinking model: {self._thinking_model})")

            from arenamcp.coach import ProxyBackend
            thinking_backend = ProxyBackend(
                model=self._thinking_model, enable_thinking=True
            )

            library_summary = self._compute_library_summary(game_state)
            turn_num = game_state.get("turn", {}).get("turn_number", 0)

            # Submit win-in-2 and win-in-3 concurrently (no win-in-4)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            futures_ordered = []
            for n in (2, 3):
                f = executor.submit(
                    self._coach.get_win_plan,
                    game_state, n, library_summary,
                    backend=thinking_backend,
                )
                futures_ordered.append((n, f))

            # Process in order (prefer shortest viable plan: 2-turn over 3-turn)
            for n, future in futures_ordered:
                try:
                    plan = future.result()
                except Exception as e:
                    logger.warning(f"Win-in-{n} future failed: {e}")
                    continue

                if not plan or plan.startswith("Error"):
                    continue

                # Parse viability from first line
                first_line = plan.split("\n", 1)[0].strip()
                is_viable = first_line.upper().startswith("VIABLE: YES") or first_line.upper().startswith("VIABLE:YES")

                # Strip the VIABLE: line from the plan text
                if first_line.upper().startswith("VIABLE:"):
                    plan = plan.split("\n", 1)[1].strip() if "\n" in plan else ""

                if not is_viable:
                    logger.info(f"Win-in-{n} plan not viable, skipping")
                    continue

                # Staleness check: don't store if game has advanced >2 turns
                current_turn = 0
                try:
                    current_state = self._mcp.get_game_state()
                    current_turn = current_state.get("turn", {}).get("turn_number", 0)
                except Exception:
                    pass
                if current_turn and current_turn - turn_num > 2:
                    logger.info(f"Win plan stale (started turn {turn_num}, now {current_turn})")
                    break

                logger.info(f"VIABLE win-in-{n} plan found ({len(plan)} chars)")

                # Store pending plan (no text output, no TTS — wait for Ctrl+0)
                self._pending_win_plan = plan
                self._pending_win_plan_turns = n
                self._pending_win_plan_turn = turn_num
                self._record_advice(plan, f"win_in_{n}", game_state=game_state)

                # Play ascending two-tone alert
                try:
                    from arenamcp.voice import play_beep
                    play_beep(frequency=1047, duration=0.12, volume=0.4)  # C6
                    time.sleep(0.08)
                    play_beep(frequency=1319, duration=0.12, volume=0.4)  # E6
                except Exception as e:
                    logger.debug(f"Win plan beep failed: {e}")

                self.ui.status("WIN-PLAN", f"WIN IN {n} FOUND — Ctrl+0 to hear")
                break  # First viable result wins

            executor.shutdown(wait=False)

            if hasattr(thinking_backend, 'close'):
                thinking_backend.close()

        except Exception as e:
            logger.error(f"Win plan worker error: {e}", exc_info=True)

    def _on_read_win_plan(self) -> None:
        """Numpad 0 — Read the pending win plan aloud via TTS."""
        plan = self._pending_win_plan
        if not plan:
            self.ui.status("WIN-PLAN", "No win plan available")
            return
        turns = self._pending_win_plan_turns
        self.ui.advice(plan, f"WIN-IN-{turns}")
        self.speak_advice(plan, blocking=False)
        # Clear pending state after reading
        self._pending_win_plan = None
        self.ui.status("WIN-PLAN", "")

    def _on_restart_hotkey(self) -> None:
        """F9 - Restart the coach."""
        self.ui.status("RESTART", "Restarting coach...")
        logger.info("F9 restart requested")
        self._restart_requested = True
        self._running = False

    def set_backend(self, provider: str, model: Optional[str] = None) -> None:
        """Explicitly set the backend provider."""
        if self.draft_mode:
            self.ui.status("PROVIDER", "Not available in draft mode")
            return

        try:
            from arenamcp.coach import CoachEngine, create_backend

            # Update backend
            self.backend_name = provider
            self.model_name = model
            progress_cb = self.ui.subtask if self.ui else None
            llm_backend = create_backend(provider, model=model, progress_callback=progress_cb)
            self._coach = CoachEngine(backend=llm_backend)

            actual_model = getattr(llm_backend, 'model', 'default')

            # Reconfigure voice input if needed
            if self._voice_input:
                enable_transcription = True
                self._voice_input.transcription_enabled = enable_transcription
                logger.info(f"Voice transcription enabled: {enable_transcription}")

            self.ui.status("PROVIDER", f"Switched to {provider.upper()} ({actual_model})")
            logger.info(f"Switched to {provider} backend, model: {actual_model}")
        except Exception as e:
            self.ui.error(f"Failed to set provider {provider}: {e}")
            logger.error(f"Set provider error: {e}")
            import traceback
            logger.debug(traceback.format_exc())


    def _on_model_cycle_hotkey(self) -> None:
        """F12 - Cycle through all available provider/model combinations."""
        if self.draft_mode:
            return

        # Unified logic: Cycle through the same list as the TUI dropdown
        # Format: (provider, model_name)
        # Build combo list dynamically from proxy
        if not hasattr(self, '_combo_list'):
            from arenamcp.coach import fetch_proxy_models
            combos = []
            for _, val in fetch_proxy_models():
                # val is "proxy/model-id"
                parts = val.split("/", 1)
                if len(parts) == 2:
                    combos.append((parts[0], parts[1]))
            self._combo_list = combos

        combo_list = self._combo_list

        # Find current index
        current_idx = -1
        for i, (p, m) in enumerate(combo_list):
            if p == self.backend_name and m == self.model_name:
                current_idx = i
                break
        
        # Next index
        next_idx = (current_idx + 1) % len(combo_list)
        new_provider, new_model = combo_list[next_idx]
        
        # Switch
        self.set_backend(new_provider, new_model)


    def _on_style_toggle_hotkey(self) -> None:
        """F2 - Toggle advice style (Concise/Verbose)."""
        self.advice_style = "verbose" if self.advice_style == "concise" else "concise"
        self.ui.status("STYLE", self.advice_style.upper())
        self.ui.log(f"\n[STYLE] Changed to {self.advice_style.upper()}\n")

    def _on_frequency_toggle_hotkey(self) -> None:
        """F3 - Toggle advice frequency."""
        self.advice_frequency = "every_priority" if self.advice_frequency == "start_of_turn" else "start_of_turn"
        label = "EVERY PRIORITY" if self.advice_frequency == "every_priority" else "START OF TURN"
        self.ui.status("FREQ", label)
        self.ui.log(f"\n[FREQ] Changed to {label}\n")

    def _on_voice_cycle_hotkey(self) -> None:
        """F6 - Cycle TTS voice."""
        if self._voice_output:
            try:
                voice_id, desc = self._voice_output.next_voice(step=2)
                self.ui.status("VOICE_ID", desc)
                self.ui.log(f"\n[VOICE] Changed to: {desc}\n")
                self.ui.speak("Voice changed.")
            except Exception as e:
                self.ui.log(f"Error changing voice: {e}")

    def _reinit_coach(self):
        """Reinitialize the coach backend with current settings."""
        try:
            from arenamcp.coach import CoachEngine, create_backend
            llm_backend = create_backend(self.backend_name, model=self.model_name)
            self._coach = CoachEngine(backend=llm_backend)
            
            # Get actual model name for display if it was auto-selected
            actual_model = getattr(llm_backend, 'model', self.model_name or 'default')
            self.model_name = actual_model # Sync back
            
            # Configure voice input based on backend
            if self._voice_input:
                enable_transcription = True
                self._voice_input.transcription_enabled = enable_transcription
                logger.info(f"Voice transcription enabled: {enable_transcription} (Backend: {self.backend_name})")
            
            logger.info(f"Re-initialized {self.backend_name} backend, model: {actual_model}")
        except Exception as e:
            self.ui.log(f"\nbackend init failed: {e}\n")
            logger.error(f"Backend init error: {e}")

    def _register_hotkeys(self) -> None:
        """Register hotkeys."""
        if not self._register_keyboard:
            logger.info("Skipping global keyboard hotkey registration (TUI/Active Mode)")
            return

        if not keyboard:
            return
        try:
            keyboard.on_press_key("f2", lambda _: self._on_style_toggle_hotkey(), suppress=False)
            keyboard.on_press_key("f3", lambda _: self._on_frequency_toggle_hotkey(), suppress=False)
            keyboard.on_press_key("f5", lambda _: self._on_mute_hotkey(), suppress=False)
            keyboard.on_press_key("f6", lambda _: self._on_voice_cycle_hotkey(), suppress=False)
            keyboard.on_press_key("f7", lambda _: self._on_bug_report_hotkey(), suppress=False)
            keyboard.on_press_key("f8", lambda _: self._on_swap_seat_hotkey(), suppress=False)
            keyboard.on_press_key("f10", lambda _: self.run_speed_test(), suppress=False)
            keyboard.on_press_key("f12", lambda _: self._on_model_cycle_hotkey(), suppress=False)
            keyboard.add_hotkey("ctrl+0", lambda: self._on_read_win_plan(), suppress=False)
            logger.info("Hotkeys registered")
        except Exception as e:
            logger.warning(f"Hotkey registration failed: {e}")

    def _unregister_hotkeys(self) -> None:
        """Unregister hotkeys."""
        if keyboard:
            try:
                keyboard.unhook_all()
            except (ValueError, KeyError, Exception):
                pass  # Already unhooked or error

    def start(self) -> None:
        """Start the standalone coach."""
        logger.info(f"start() called: backend_name={self.backend_name}, model={self.model_name}, draft={self.draft_mode}")
        if self._running:
            logger.info("Already running, returning early")
            return

        self._running = True

        # Initialize components
        self._init_mcp()
        self._init_voice()

        # Track actual model name for display
        actual_model = self.model_name

        if self.draft_mode:
            # Use MCP's built-in draft helper
            logger.info("Starting MCP draft helper...")
            result = self._mcp.start_draft_helper(self.set_code)
            logger.info(f"Draft helper: {result}")
        else:
            # Initialize LLM for coaching
            self._init_llm()
            # Get actual model name from backend
            if self._coach and hasattr(self._coach, '_backend'):
                actual_model = getattr(self._coach._backend, 'model', self.model_name)

            # Start coaching and voice threads
            logger.info(f"Starting threads for backend: {self.backend_name}")
            logger.info("Starting PTT voice loop + coaching loop")
            self._coaching_thread = threading.Thread(
                target=self._coaching_loop, daemon=True, name="coaching"
            )
            self._coaching_thread.start()

            # Only launch voice thread if PTT/VOX is wanted
            if self._voice_mode in ("ptt", "vox"):
                self._voice_thread = threading.Thread(
                    target=self._voice_loop, daemon=True, name="voice"
                )
                self._voice_thread.start()

        self._register_hotkeys()

        # Print status
        self.ui.log("\n" + "="*50)
        if self.draft_mode:
            self.ui.log("MTGA DRAFT HELPER")
            self.ui.log("="*50)
            self.ui.log(f"Set: {self.set_code or 'auto-detect'}")
            self.ui.log("Using MCP server's draft evaluation")
        else:
            self.ui.log("MTGA STANDALONE COACH")
            self.ui.log("="*50)
            self.ui.status("BACKEND", f"{self.backend_name} ({actual_model or 'default'})")
            self.ui.status("VOICE", f"PTT (F4) + Kokoro")
        self.ui.log("-"*50)
        self.ui.log("F5=mute F6=voice F7=bug F8=seat F9=restart F10=speed F12=model Num1=land")
        self.ui.log("="*50)
        self.ui.log("\nWaiting for MTGA...")
        self.ui.log("F8=swap seat if wrong | F9=restart coach\n")

    def stop(self) -> None:
        """Stop the coach and clean up all resources.

        This method ensures proper termination of all threads and resources:
        1. Signals threads to stop via _running flag
        2. Stops voice input/output
        3. Stops MCP server watcher
        4. Waits for threads to terminate
        """
        if not self._running:
            return

        logger.info("Stopping coach - beginning cleanup...")
        self._running = False

        # 1. Unregister hotkeys first to prevent new events
        self._unregister_hotkeys()

        # 2. Stop voice input immediately (releases PTT hotkey, stops VOX stream)
        if self._voice_input:
            try:
                logger.debug("Stopping voice input...")
                self._voice_input.stop()
            except Exception as e:
                logger.debug(f"Voice input stop error (non-fatal): {e}")
            self._voice_input = None

        # 3. Stop voice output (TTS) - interrupts any playing audio
        if self._voice_output:
            try:
                logger.debug("Stopping voice output...")
                self._voice_output.stop()
            except Exception as e:
                logger.debug(f"Voice output stop error (non-fatal): {e}")
            self._voice_output = None

        # 4. Stop draft helper if active
        if self.draft_mode and self._mcp:
            try:
                logger.debug("Stopping draft helper...")
                self._mcp.stop_draft_helper()
            except Exception as e:
                logger.debug(f"Draft helper stop error (non-fatal): {e}")

        # 6. Stop MCP server's log watcher
        if self._mcp:
            try:
                logger.debug("Stopping MCP watcher...")
                from arenamcp.server import stop_watching
                stop_watching()
            except Exception as e:
                logger.debug(f"Watcher stop error (non-fatal): {e}")

        # 7. Wait for daemon threads to finish (with timeout)
        # These should exit quickly since _running is False
        if self._coaching_thread and self._coaching_thread.is_alive():
            logger.debug("Waiting for coaching thread...")
            self._coaching_thread.join(timeout=2.0)
            if self._coaching_thread.is_alive():
                logger.warning("Coaching thread did not terminate cleanly")
        self._coaching_thread = None

        if self._voice_thread and self._voice_thread.is_alive():
            logger.debug("Waiting for voice thread...")
            self._voice_thread.join(timeout=2.0)
            if self._voice_thread.is_alive():
                logger.warning("Voice thread did not terminate cleanly")
        self._voice_thread = None

        # 8. Clear references to allow garbage collection
        if self._coach and hasattr(self._coach, "_backend"):
            backend = self._coach._backend
            close_fn = getattr(backend, "close", None)
            if callable(close_fn):
                try:
                    logger.debug("Closing LLM backend...")
                    close_fn()
                except Exception as e:
                    logger.debug(f"Backend close error (non-fatal): {e}")

        self._mcp = None
        self._coach = None
        self._trigger = None

        logger.info("Coach stopped - cleanup complete")
        self.ui.log(f"\nStopped. Log: {LOG_FILE}")

    def run_speed_test(self):
        """Run latency test against all providers."""
        if not self.ui:
            return

        self.ui.log("\n[bold yellow]Running API Speed Test (3 passes)...[/]")
        
        # Define test cases: (Provider, Model Class, Model Name)
        from arenamcp.coach import GeminiCliBackend, ClaudeCodeBackend, ProxyBackend

        tests = [
            ("Claude Code Haiku", ClaudeCodeBackend, "haiku"),
            ("Claude Code Sonnet", ClaudeCodeBackend, "sonnet"),
            ("Gemini CLI Flash", GeminiCliBackend, "gemini-2.0-flash"),
            ("Proxy Default", ProxyBackend, None),
        ]
        
        import time

        for name, backend_cls, model_id in tests:
            try:
                self.ui.log(f"Testing {name}...")
                latencies = []
                
                # Init backend once
                backend = backend_cls(model=model_id)

                # Warmup / 3 passes
                for i in range(3):
                    start_req = time.perf_counter()
                    response = backend.complete("You are a helpful assistant.", "Say 'ok' and nothing else.")
                    req_ms = (time.perf_counter() - start_req) * 1000
                    
                    if response.startswith("Error"):
                        raise Exception(response)
                        
                    latencies.append(req_ms)
                    # Small delay between requests
                    time.sleep(0.1)

                avg_ms = sum(latencies) / len(latencies)
                min_ms = min(latencies)
                max_ms = max(latencies)
                
                self.ui.log(f"[green]PASS {name}: Avg {avg_ms:.0f}ms (Range: {min_ms:.0f}-{max_ms:.0f}ms)[/]")
                    
            except Exception as e:
                self.ui.log(f"[red]FAIL {name}: {e}[/]")

        self.ui.log("[bold yellow]Speed Test Complete.[/]\n")

    def run_forever(self) -> None:
        """Run until interrupted."""
        self.start()

        def signal_handler(sig, frame):
            print("\n\nShutting down...")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        while self._running:
            time.sleep(1)


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="MTGA Standalone Coach - MCP client with voice I/O",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m arenamcp.standalone --backend proxy
  python -m arenamcp.standalone --provider gemini-2.5-pro
  python -m arenamcp.standalone --backend claude-code
  python -m arenamcp.standalone --draft --set MH3

Environment variables:
  CLAUDE_CODE_CMD    Optional: path to claude.exe for claude-code backend
  GEMINI_CLI_CMD     Optional: path to gemini CLI (default: gemini)
        """
    )

    parser.add_argument("--backend", "-b", choices=["claude-code", "gemini-cli", "proxy", "ollama"],
                        default=None, help="LLM backend (default: proxy)")
    parser.add_argument("--model", "-m", help="Model name override")
    parser.add_argument("--provider", help="Default proxy model (e.g., gemini-2.5-pro, claude-sonnet-4-5-20250929)")
    parser.add_argument("--voice", "-v", choices=["ptt", "vox"], default=None,
                        help="Voice input: ptt (F4) or vox (auto)")
    parser.add_argument("--draft", action="store_true",
                        help="Draft helper mode (no LLM needed)")
    parser.add_argument("--set", "-s", dest="set_code",
                        help="Set code for draft (e.g., MH3, BLB)")
    parser.add_argument("--autopilot", action="store_true",
                        help="Enable autopilot mode (AI plays via mouse clicks)")
    parser.add_argument("--afk", action="store_true",
                        help="Start in AFK mode (auto-pass all priority without LLM)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Autopilot dry run: plan actions but log instead of clicking")
    parser.add_argument("--show-log", action="store_true",
                        help="Show log file and exit")
    parser.add_argument("--language", "-l", default=None,
                        help="Language code for voice (e.g., en, nl, es, fr, de, ja)")
    parser.add_argument("--cli", action="store_true",
                        help="Run in legacy CLI mode (default is TUI)")

    args = parser.parse_args()

    # --provider shortcut: set proxy model and backend
    if args.provider:
        settings = get_settings()
        settings.set("model", args.provider)
        settings.set("backend", "proxy")
        args.model = args.provider
        args.backend = "proxy"

    # --language: persist to settings
    if args.language:
        settings = get_settings()
        settings.set("language", args.language)

    # Launch TUI unless CLI mode requested or show-log
    if not args.cli and not args.show_log:
        try:
            from arenamcp.tui import run_tui
            run_tui(args)
            return
        except ImportError as e:
            print(f"Failed to load TUI (install 'textual'): {e}")
            print("Falling back to CLI mode...")


    if args.show_log:
        print(f"Log: {LOG_FILE}")
        if LOG_FILE.exists():
            with open(LOG_FILE) as f:
                for line in f.readlines()[-30:]:
                    print(line, end='')
        return

    logger.info(f"Starting: backend={args.backend}, draft={args.draft}")

    while True:
        coach = StandaloneCoach(
            backend=args.backend,
            model=args.model,
            voice_mode=args.voice,
            draft_mode=args.draft,
            set_code=args.set_code,
            autopilot=args.autopilot,
            dry_run=args.dry_run,
            afk=getattr(args, 'afk', False),
        )

        try:
            coach.run_forever()
        except KeyboardInterrupt:
            coach.stop()
            break  # Exit on Ctrl+C
        except Exception as e:
            logger.error(f"Fatal: {e}")
            logger.debug(traceback.format_exc())
            print(f"\nError: {e}\nSee: {LOG_FILE}")
            sys.exit(1)

        # Check if restart was requested (F9)
        if coach._restart_requested:
            print("\n" + "="*50)
            print("RESTARTING...")
            print("="*50 + "\n")
            logger.info("Restarting coach...")
            continue
        else:
            break  # Normal exit


if __name__ == "__main__":
    main()
