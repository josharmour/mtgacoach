"""Compatibility entrypoint for the Windows Dev launcher installer."""

import sys
from pathlib import Path

from install_windows_dev_launcher import main

if __name__ == "__main__":
    if "--checkout" not in sys.argv:
        sys.argv.extend(["--checkout", str(Path(__file__).resolve().parents[1])])
    main()
