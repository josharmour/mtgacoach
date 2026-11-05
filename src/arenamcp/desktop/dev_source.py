"""Read a source revision without requiring Git on the client computer."""

import os
import re
from pathlib import Path


def source_revision(checkout: str, *, short: bool = True) -> str:
    try:
        metadata = Path(os.environ.get("MTGACOACH_DEV_GIT_DIR") or Path(checkout) / ".git")
        if metadata.is_file():
            pointer = metadata.read_text().strip()
            if not pointer.startswith("gitdir: "):
                return "unknown commit"
            metadata = metadata.parent / pointer.removeprefix("gitdir: ")
        head = (metadata / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head.removeprefix("ref: ")
            if not ref.startswith("refs/") or ".." in ref or "\\" in ref:
                return "unknown commit"
            common_file = metadata / "commondir"
            common = metadata / common_file.read_text().strip() if common_file.is_file() else metadata
            for root in (metadata, common):
                loose = root / ref
                if loose.is_file():
                    head = loose.read_text().strip()
                    break
            else:
                head = next(
                    (
                        line.split()[0]
                        for line in (common / "packed-refs").read_text().splitlines()
                        if line.endswith(" " + ref)
                    ),
                    "",
                )
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
            return "unknown commit"
        return head[:7] if short else head
    except (OSError, ValueError):
        return "unknown commit"
