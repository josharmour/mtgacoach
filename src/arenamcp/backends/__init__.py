"""Pluggable LLM backends for MTG game coaching.

This package provides the LLMBackend protocol and the ProxyBackend
implementation, which handles both online (mtgacoach.com) and local
(Ollama/LM Studio) modes via OpenAI-compatible endpoints.
"""

from arenamcp.backends.base import LLMBackend
from arenamcp.backends.proxy import ProxyBackend

__all__ = [
    "LLMBackend",
    "ProxyBackend",
]
