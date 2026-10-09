"""Bounded excerpts of large logs for bug reports.

Bug reports used to carry 100 lines of standalone.log (about two seconds at
match verbosity) and only the *path* of MTGA's Player.log — which Arena
rotates on every relaunch, so the evidence for the 2026-10-09 07:38
sideboarding glitch was gone by the time it was investigated. These helpers
read only the tail of a file (never the whole 50 MB log) and keep the lines
that explain what the client was doing: scene changes, exceptions with
their first stack frames, connection events and match-room transitions.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

_PLAYER_LOG_MARKERS = re.compile(
    r"Client\.SceneChange|Exception|Client\.TcpConnection|FrontDoorConnection\.Close|"
    r"MatchGameRoomStateChangedEvent|Reconnect|ConcedeReq|EnterSideboardingReq|SubmitDeckReq|"
    r"\[StartupProfiling\] Startup\.|probe loaded|EventClaimPrize|EventJoin|EventPayEntry|"
    r"GREMessageType_ConnectResp|StateType_MatchCompleted|Disconnect"
)
_UNITY_TIMESTAMP = re.compile(r"^\[UnityCrossThreadLogger\](\d+/\d+/\d+ \d+:\d+:\d+ [AP]M)")
_STACK_FRAME = re.compile(r"^\s+at \S")


def tail_lines(path: str | Path, count: int, *, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    """The last ``count`` lines of ``path``, reading at most ``max_bytes`` from its end."""
    file = Path(path)
    try:
        size = file.stat().st_size
        with file.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if size > max_bytes and lines:
        lines = lines[1:]  # the first line is probably cut mid-way
    return lines[-count:]


def player_log_excerpt(
    path: str | Path | None,
    *,
    max_bytes: int = 3 * 1024 * 1024,
    tail: int = 60,
    events: int = 160,
    frames: int = 8,
) -> dict[str, Any]:
    """Status plus two bounded views of Player.log: its raw tail and its notable events.

    ``events`` keeps, in order, every line matching :data:`_PLAYER_LOG_MARKERS`
    prefixed with the nearest preceding Unity timestamp, and up to ``frames``
    stack-frame lines after each exception. ``tail`` is the raw end of the
    file. Missing or unreadable files report that instead of raising.
    """
    result: dict[str, Any] = {"path": str(path or "")}
    if not path:
        result["exists"] = False
        return result
    file = Path(path)
    try:
        stat = file.stat()
    except OSError as error:
        result.update(exists=False, error=str(error))
        return result
    result.update(
        exists=True,
        size_bytes=stat.st_size,
        last_modified=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        truncated_to_bytes=min(stat.st_size, max_bytes),
    )
    lines = tail_lines(file, 10**9, max_bytes=max_bytes)
    if not lines:
        result["tail"] = []
        result["events"] = []
        return result
    kept: list[str] = []
    stamp = ""
    pending_frames = 0  # stack frames still wanted after an exception line
    gap = 0  # non-frame lines seen since that exception ("Parameter name: …" sits between)
    for raw in lines:
        line = raw.rstrip("\n")
        found = _UNITY_TIMESTAMP.match(line)
        if found:
            stamp = found.group(1)
        if pending_frames and _STACK_FRAME.match(line):
            kept.append("    " + line.strip()[:220])
            pending_frames -= 1
            continue
        if pending_frames:
            gap += 1
            if not line.strip() or gap > 3 or found:
                pending_frames = 0
        if _PLAYER_LOG_MARKERS.search(line) and not line.startswith("{"):
            kept.append(f"[{stamp or '?'}] {line.strip()[:300]}")
            if "Exception" in line:
                pending_frames, gap = frames, 0
    result["events"] = kept[-events:]
    result["tail"] = [line.rstrip("\n")[:400] for line in lines[-tail:]]
    return result
