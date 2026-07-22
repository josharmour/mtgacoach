"""Shared pytest configuration.

This module is imported by pytest before any test module, which makes it
the one reliable place to redirect arenamcp's file logging BEFORE
``arenamcp.standalone`` (and friends) call ``configure_logging()`` at
import time.

Without this, every pytest run appends test-fixture noise (fake bridge
request types, sub-second verification timeouts, scripted planner
failures) to the LIVE ~/.arenamcp/standalone.log — which has previously
been misdiagnosed as real autopilot failures.
"""

import os
import tempfile

import pytest

# Redirect logging to a temporary log file for pytest runs
os.environ.setdefault(
    "ARENAMCP_LOG_FILE",
    os.path.join(tempfile.gettempdir(), "arenamcp-pytest.log"),
)

# Force Qt offscreen platform plugin for headless test environments
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp_args():
    """Arguments passed to QApplication when created for testing."""
    return ["-platform", "offscreen"]


@pytest.fixture(scope="session")
def qapp(qapp_args):
    """Session-scoped QApplication instance configured for headless testing.

    Ensures a single QApplication instance exists for tests importing PySide6 widgets.
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 is not installed")

    app = QApplication.instance()
    if app is None:
        app = QApplication(qapp_args)
    yield app

