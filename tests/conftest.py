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

os.environ.setdefault(
    "ARENAMCP_LOG_FILE",
    os.path.join(tempfile.gettempdir(), "arenamcp-pytest.log"),
)
