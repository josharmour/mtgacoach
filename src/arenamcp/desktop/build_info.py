"""One version in release titles; source identity for local development."""

import os


def window_title(version: str) -> str:
    label = os.environ.get("MTGACOACH_BUILD_LABEL", "").strip()
    if os.environ.get("MTGACOACH_DEV_CHECKOUT") or label.startswith("dev source"):
        return "MTGA Coach — " + (label.replace("dev source", "Dev", 1) or "Dev")
    # The host's channel label is metadata; never repeat the release version.
    parts = [part for part in label.split() if part != version]
    return f"MTGA Coach {version}" + (" · " + " ".join(parts) if parts else "")
