"""Pluggable LLM backends for MTG game coaching.

This package provides the LLMBackend protocol and the ProxyBackend
implementation, an OpenAI-compatible client for the api.mtgacoach.com
gateway.
"""

from arenamcp.backends.base import LLMBackend
from arenamcp.backends.proxy import ProxyBackend

__all__ = [
    "LLMBackend",
    "ProxyBackend",
]
