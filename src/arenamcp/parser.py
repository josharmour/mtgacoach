"""MTGA log parser with multi-line JSON accumulation and event routing.

This module provides parsing of MTGA Player.log content, accumulating
multi-line JSON blocks and routing events to registered handlers.
"""

import json
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Event type patterns found in MTGA logs
EVENT_PATTERNS = [
    re.compile(r'GreToClientEvent'),
    re.compile(r'MatchCreated'),
    re.compile(r'MatchGameRoomStateChangedEvent'),
    re.compile(r'ClientToMatchServiceMessage'),
    re.compile(r'MulliganReq'),
    re.compile(r'MulliganResp'),
    re.compile(r'GameStateMessage'),
    # Draft-related events
    re.compile(r'Draft\.Notify'),
    re.compile(r'Draft\.MakeHumanDraftPick'),
    re.compile(r'Event_PlayerDraftMakePick'),
    re.compile(r'BotDraft_DraftPick'),
    re.compile(r'DraftPack'),
    re.compile(r'DraftStatus'),
    re.compile(r'CardsInPack'),
    re.compile(r'EventName'),
    # Sealed pool events
    re.compile(r'CardPool'),
    re.compile(r'InternalEventName'),
]


class LogParser:
    """Parser that accumulates multi-line JSON from MTGA log stream.

    The MTGA log format has JSON payloads spanning multiple lines:

        [UnityCrossThreadLogger]12:34:56.789 GreToClientEvent
        {
          "greToClientEvent": {
            "gameStateMessage": {...}
          }
        }

    This parser uses brace-depth tracking to accumulate complete JSON blocks,
    then parses and routes them to registered event handlers.
    """

    def __init__(
        self,
        on_event: Optional[Callable[[str, dict], None]] = None
    ) -> None:
        """Initialize the parser.

        Args:
            on_event: Default callback for all events. Called with (event_type, payload).
        """
        self._on_event = on_event
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._default_handler: Optional[Callable[[str, dict], None]] = None

        # JSON accumulation state
        self._buffer: list[str] = []
        self._brace_depth: int = 0
        self._in_json: bool = False
        self._current_event_type: Optional[str] = None
        self._pending_line: str = ""  # Incomplete line from previous chunk
        self._last_event_hint: Optional[str] = None  # Event type from previous line

    def register_handler(
        self,
        event_type: str,
        handler: Callable[[dict], None]
    ) -> None:
        """Register a handler for a specific event type.

        Args:
            event_type: Event type string (e.g., 'GreToClientEvent').
            handler: Callback receiving the parsed JSON payload dict.
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.debug(f"Registered handler for {event_type}")

    def set_default_handler(
        self,
        handler: Callable[[str, dict], None]
    ) -> None:
        """Set handler for events with no registered type-specific handler.

        Args:
            handler: Callback receiving (event_type, payload) for unhandled events.
        """
        self._default_handler = handler

    def process_chunk(self, text: str) -> None:
        """Process a chunk of log text.

        Handles partial lines at chunk boundaries by buffering incomplete lines.

        Args:
            text: Raw text chunk from log file.
        """
        # Prepend any pending partial line from previous chunk
        if self._pending_line:
            text = self._pending_line + text
            self._pending_line = ""

        # Split into lines, keeping track of whether last line is complete
        lines = text.split('\n')

        # If text doesn't end with newline, last "line" is incomplete
        if text and not text.endswith('\n'):
            self._pending_line = lines[-1]
            lines = lines[:-1]

        for line in lines:
            self._process_line(line)

    def _process_line(self, line: str) -> None:
        """Process a single log line.

        Args:
            line: A complete line from the log (without newline).
        """
        if not self._in_json:
            # Check if this line starts a JSON block
            brace_idx = line.find('{')
            if brace_idx != -1:
                # Detect event type from content before the brace
                prefix = line[:brace_idx]
                detected = self._detect_event_type(prefix)

                # If we found an event type on this line, use it
                # Otherwise, use the hint from a previous line
                if detected != "Unknown":
                    self._current_event_type = detected
                elif self._last_event_hint:
                    self._current_event_type = self._last_event_hint
                else:
                    self._current_event_type = "Unknown"

                # Clear the hint since we've consumed it
                self._last_event_hint = None

                # Start accumulating JSON from the brace
                json_start = line[brace_idx:]
                self._start_json_block(json_start)
            else:
                # No JSON on this line, but check for event type hint
                detected = self._detect_event_type(line)
                if detected != "Unknown":
                    self._last_event_hint = detected
        else:
            # Continue accumulating JSON
            self._accumulate_json(line)

    def _detect_event_type(self, text: str) -> str:
        """Detect event type from log line text.

        Args:
            text: Text to search for event type patterns.

        Returns:
            Detected event type string, or 'Unknown' if not recognized.
        """
        for pattern in EVENT_PATTERNS:
            if pattern.search(text):
                return pattern.pattern
        return "Unknown"

    def _start_json_block(self, json_start: str) -> None:
        """Start accumulating a new JSON block.

        Args:
            json_start: The beginning of the JSON (starting with '{').
        """
        self._in_json = True
        self._buffer = [json_start]
        self._brace_depth = self._count_brace_delta(json_start)

        # Check if JSON completes on same line
        if self._brace_depth == 0:
            self._complete_json_block()

    def _accumulate_json(self, line: str) -> None:
        """Accumulate a line into the current JSON block.

        Args:
            line: Line to add to the JSON buffer.
        """
        self._buffer.append(line)
        self._brace_depth += self._count_brace_delta(line)

        # Sanity check - negative depth indicates corruption
        if self._brace_depth < 0:
            logger.warning("Negative brace depth detected, resetting parser state")
            self._reset_json_state()
            return

        if self._brace_depth == 0:
            self._complete_json_block()

    def _count_brace_delta(self, text: str) -> int:
        """Count net brace depth change in text.

        Note: This is a simple counter that doesn't account for braces
        inside strings. MTGA logs are well-formed enough that this works.

        Args:
            text: Text to count braces in.

        Returns:
            Net change in brace depth (opens - closes).
        """
        return text.count('{') - text.count('}')

    def _complete_json_block(self) -> None:
        """Parse completed JSON block and emit event."""
        json_text = '\n'.join(self._buffer)
        event_type = self._current_event_type or "Unknown"

        try:
            payload = json.loads(json_text)
            self._emit_event(event_type, payload)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON ({event_type}): {e}")
            logger.debug(f"Malformed JSON content: {json_text[:500]}...")

        self._reset_json_state()

    def _reset_json_state(self) -> None:
        """Reset JSON accumulation state."""
        self._buffer = []
        self._brace_depth = 0
        self._in_json = False
        self._current_event_type = None

    def _emit_event(self, event_type: str, payload: dict) -> None:
        """Emit parsed event to handlers.

        Args:
            event_type: Type of event (e.g., 'GreToClientEvent').
            payload: Parsed JSON payload dict.
        """
        logger.debug(f"Emitting event: {event_type}")

        # Call generic on_event callback if set
        if self._on_event:
            try:
                self._on_event(event_type, payload)
            except Exception as e:
                logger.error(f"on_event callback error: {e}")

        # Call type-specific handlers
        handlers = self._handlers.get(event_type, [])
        if handlers:
            for handler in handlers:
                try:
                    handler(payload)
                except Exception as e:
                    logger.error(f"Handler error for {event_type}: {e}")

        # ALWAYS call default handler (draft handler) regardless of whether
        # type-specific handlers ran. Draft events can be wrapped inside
        # GreToClientEvent or other registered event types, so the default
        # handler must inspect every payload for draft-related content.
        if self._default_handler:
            try:
                self._default_handler(event_type, payload)
            except Exception as e:
                logger.error(f"Default handler error: {e}")
