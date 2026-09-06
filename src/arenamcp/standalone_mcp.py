"""MCP client for the standalone coach.

Extracted from arenamcp.standalone (pure move, no behavior change).
Re-exported from arenamcp.standalone for backwards compatibility."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


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

    def get_draft_picked_ids(self) -> list[int]:
        """Return raw grpIds of cards picked during the current draft."""
        return list(self._server.draft_state.picked_cards)

    def get_card_info(self, arena_id: int) -> dict[str, Any]:
        """Call get_card_info MCP tool."""
        return self._server.get_card_info(arena_id)

    def start_draft_helper(self, set_code: str | None = None) -> dict[str, Any]:
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
