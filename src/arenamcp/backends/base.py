"""Base protocol for LLM backends."""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class LLMBackend(Protocol):
    """Protocol for LLM backends that can provide coaching advice."""

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Get a completion from the LLM."""
        ...

    def list_models(self) -> list[str]:
        """List available models (optional)."""
        return []
