"""Centralized logging configuration for ArenaMCP.

All entry points (standalone.py, server.py, etc.) should call
``configure_logging()`` once at startup to ensure a consistent format
and destination.

Log level precedence:
    ARENAMCP_LOG_LEVEL env var  ->  default (INFO)

Log file location:
    ~/.arenamcp/standalone.log   (always enabled)
    Console output               (opt-in via *console* parameter)
"""

import logging
import os
from pathlib import Path

# Shared constants
LOG_DIR = Path.home() / ".arenamcp"
LOG_FILE = LOG_DIR / "standalone.log"

# Shared format used by every handler
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that are excessively noisy at INFO level
_NOISY_LOGGERS = ("google", "httpx", "httpcore")

_configured = False


def configure_logging(*, console: bool = False) -> None:
    """Set up logging for the entire process.

    Safe to call multiple times -- subsequent calls are no-ops so that
    both ``standalone.py`` and ``server.py`` can call it without conflict.

    Args:
        console: If True, also attach a StreamHandler for console output.
    """
    global _configured
    if _configured:
        return
    _configured = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level_name = os.getenv("ARENAMCP_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # File handler -- always present
    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any pre-existing handlers to avoid duplicates
    for h in root.handlers[:]:
        root.removeHandler(h)

    root.addHandler(file_handler)

    # Optional console handler (useful for CLI tools, disabled when using TUI)
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
        root.addHandler(console_handler)

    # Suppress noisy third-party loggers
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
