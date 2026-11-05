"""Stamp the release package version into the wheel before building it."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def stamp_version(path: Path, version: str) -> None:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError(f"Invalid release version: {version!r}")
    data, count = re.subn(
        rb'(?m)^__version__ = "[^"\r\n]+"',
        f'__version__ = "{version}"'.encode(),
        path.read_bytes(),
    )
    if count != 1:
        raise ValueError("Expected exactly one source version declaration")
    path.write_bytes(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    stamp_version(Path(__file__).resolve().parents[1] / "src/arenamcp/__init__.py", args.version)
