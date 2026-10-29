"""Run an isolated worker without executing Coach's application package initializer.

Invoked by file path with a real Python interpreter, including frozen desktop builds.
Only the two fixed worker entrypoints are accepted. No dynamic user module execution.
"""

import runpy
import sys
import types
from pathlib import Path


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"worker", "local_voice_worker"}:
        raise SystemExit("Expected worker or local_voice_worker")
    package = types.ModuleType("arenamcp")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules["arenamcp"] = package
    runpy.run_module("arenamcp.commentary." + sys.argv[1], run_name="__main__")


if __name__ == "__main__":
    main()
