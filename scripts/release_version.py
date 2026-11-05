"""Allocate master versions and preserve them when promoting to stable."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PLATFORMS = ("osx-arm64", "win-x64", "linux-x64")


def master_version(run_number: int) -> str:
    if run_number < 1:
        raise ValueError("The release run number must be positive")
    return f"4.0.{run_number}"


def tag_version(tag: str) -> str:
    if not re.fullmatch(r"v4\.0\.(?:0|[1-9][0-9]*)", tag):
        raise ValueError("Use a v4.0.x tag matching a published master build")
    return tag[1:]


def promotion_version(tag: str, commit: str, manifests: dict[str, dict]) -> str:
    version = tag_version(tag)
    for platform in PLATFORMS:
        expected = {"lane": "master", "platform": platform, "version": version, "commit": commit}
        actual = manifests.get(platform, {})
        if any(actual.get(key) != value for key, value in expected.items()):
            raise ValueError(f"{platform} has not published master {version} at {commit}")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    master = commands.add_parser("master")
    master.add_argument("run_number", type=int)
    tag = commands.add_parser("tag")
    tag.add_argument("tag")
    promote = commands.add_parser("promote")
    promote.add_argument("tag")
    promote.add_argument("commit")
    promote.add_argument("manifest_dir", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "master":
            version = master_version(args.run_number)
        elif args.command == "tag":
            version = tag_version(args.tag)
        else:
            manifests = {
                rid: json.loads((args.manifest_dir / f"published.master-{rid}.json").read_text())
                for rid in PLATFORMS
            }
            version = promotion_version(args.tag, args.commit, manifests)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(version)


if __name__ == "__main__":
    main()
