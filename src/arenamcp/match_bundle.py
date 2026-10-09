"""Match bundles: one redacted, gzipped record per finished match.

At game end the coach gathers everything a reviewer needs to replay the
match offline — the match packet, the coach-log slice for the match, the
Player.log GRE traffic for the match, bug reports saved during it, the final
game-state snapshot and a whitelisted settings subset — redacts it, saves a
local copy under ``~/.arenamcp/match_bundles/`` and uploads it to
mtgacoach.com in a background thread. Nothing here may ever affect play:
every entry point swallows its own errors and the upload runs off the
coaching loop.

Privacy: the only identifier that leaves the machine is ``install_id``.
License keys, API keys, tokens, emails, home-directory paths, Arena account
ids and both players' names are scrubbed by :func:`redact`, which runs on
the serialized JSON text as a final pass so a field nobody thought of cannot
slip through.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import platform
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from arenamcp.logging_config import LOG_DIR, LOG_FILE

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BUNDLE_DIR = LOG_DIR / "match_bundles"
MAX_BUNDLES = 100
UPLOAD_PATH = "/api/match-bundle"
UPLOAD_TIMEOUT_S = 30
RETRY_DELAYS_S = (5.0, 30.0, 120.0)

# Raw caps (bytes of serialized JSON) before gzip, and the gzip hard cap.
COACH_LOG_CAP = 3 * 1024 * 1024
PLAYER_LOG_CAP = 6 * 1024 * 1024
BUG_REPORT_CAP = 1024 * 1024
MAX_BUG_REPORTS = 5
FINAL_STATE_CAP = 1024 * 1024
GZIP_CAP = 8 * 1024 * 1024
MAX_ADVICE_ENTRIES = 50
# How far behind the watcher's offset the Player.log slicer looks for the
# match's first room event (the watcher has consumed it by the time the
# snapshot shows the match id).
PLAYER_LOG_LOOKBACK = 256 * 1024

# Settings keys that may leave the machine (booleans / enums only).
SETTINGS_WHITELIST = (
    "autopilot_enabled",
    "auto_queue_enabled",
    "auto_concede",
    "oops_emote",
    "draft_commentary",
    "auto_deck_strategy",
    "auto_post_match_analysis",
    "conversation_mode",
    "conversation_verbosity",
    "voice_mode",
    "game_device",
    "language",
    "mode",
    "model",
    "desktop_theme",
)

_MATCH_END_TOKENS = ("MatchGameRoomStateType_MatchCompleted",)

# Match ids already bundled in this process (the server re-surfaces finished
# matches; a second bundle for the same match is pure noise).
_bundled_match_ids: set[str] = set()
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Match context (captured at match start)
# ---------------------------------------------------------------------------


@dataclass
class MatchBundleContext:
    """Byte offsets and timestamps captured when a match starts."""

    match_id: str
    # Game within a Bo3 match (1-based). Each game gets its own bundle; the
    # Arena match id stays in ``match_id`` so the Player.log slicer can find it.
    game_number: int = 1
    started_at: float = field(default_factory=time.time)
    coach_log_path: Path | None = None
    coach_log_start: int = 0
    player_log_path: Path | None = None
    player_log_start: int = 0
    # Bug reports saved during the match (paths), when the engine tells us.
    bug_reports: list[Path] = field(default_factory=list)

    @property
    def bundle_id(self) -> str:
        """The id this bundle is saved, deduplicated and uploaded under.

        Game 1 keeps the bare match id; later games of a Bo3 get ``-g<n>`` so
        the server (which dedupes per id) and the local directory keep them
        apart.
        """
        return self.match_id if self.game_number <= 1 else f"{self.match_id}-g{self.game_number}"


def begin_match(
    match_id: str,
    *,
    coach_log_path: Path | None = None,
    player_log_path: Path | None = None,
    player_log_offset: int | None = None,
    game_number: int = 1,
) -> MatchBundleContext:
    """Record where the logs stand when game *game_number* of *match_id* starts."""
    coach_path = coach_log_path if coach_log_path is not None else LOG_FILE
    ctx = MatchBundleContext(
        match_id=str(match_id),
        game_number=max(1, int(game_number or 1)),
        coach_log_path=coach_path,
        player_log_path=player_log_path,
    )
    with contextlib.suppress(OSError):
        ctx.coach_log_start = coach_path.stat().st_size if coach_path else 0
    if player_log_path is not None:
        if player_log_offset is None:
            with contextlib.suppress(OSError):
                player_log_offset = player_log_path.stat().st_size
        ctx.player_log_start = max(0, int(player_log_offset or 0))
    return ctx


# ---------------------------------------------------------------------------
# Log slicing (byte ranges only — never the whole file)
# ---------------------------------------------------------------------------

_COACH_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| ")
_COACH_DEBUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| DEBUG")


def _read_range(path: Path, start: int, end: int | None) -> str:
    start = max(0, int(start or 0))
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read() if end is None else fh.read(max(0, int(end) - start))
    return data.decode("utf-8", errors="replace")


def slice_coach_log(path: Path | None, start: int, end: int | None = None) -> list[str]:
    """INFO-and-above lines of the coach log between two byte offsets.

    Traceback / continuation lines (no timestamp prefix) stay attached to the
    record they belong to; DEBUG records are dropped with their continuations.
    """
    if path is None or not Path(path).exists():
        return []
    try:
        text = _read_range(Path(path), start, end)
    except OSError:
        return []
    lines: list[str] = []
    keeping = False
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if _COACH_TS_RE.match(line):
            keeping = not _COACH_DEBUG_RE.match(line)
            if keeping:
                lines.append(line)
        elif keeping and line.strip():
            lines.append(line)
    # The first record may start mid-line when the offset was mid-write.
    if lines and not _COACH_TS_RE.match(lines[0]):
        lines = lines[1:]
    return lines


_PLAYER_HEADER_RE = re.compile(
    r"^\[UnityCrossThreadLogger\](?P<ts>.*?)"
    r": (?:Match to (?P<to_id>\S+): (?P<kind_in>\w+)|(?P<from_id>\S+) to Match: (?P<kind_out>\w+))\s*$"
)


def _brace_delta(text: str, state: list[bool]) -> int:
    """Net brace depth change of *text*, string-aware; state = [in_string, escape]."""
    delta = 0
    in_string, escape = state
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch == "{":
                delta += 1
            elif ch == "}":
                delta -= 1
    state[0], state[1] = in_string, escape
    return delta


def _match_events(text: str) -> list[dict[str, Any]]:
    """Every ``Match to <id>`` / ``<id> to Match`` header plus its JSON block."""
    events: list[dict[str, Any]] = []
    header: dict[str, Any] | None = None
    block: list[str] = []
    depth = 0
    state = [False, False]
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if block:
            block.append(line)
            depth += _brace_delta(line, state)
            if depth <= 0:
                events.append({**(header or {}), "json": "\n".join(block)})
                block, header, depth = [], None, 0
            elif len(block) > 20000:  # corrupt block; the parser gives up here too
                block, header, depth = [], None, 0
            continue
        m = _PLAYER_HEADER_RE.match(line)
        if m:
            header = {
                "header_ts": (m.group("ts") or "").strip(),
                "direction": "in" if m.group("to_id") else "out",
                "kind": m.group("kind_in") or m.group("kind_out") or "",
            }
            continue
        if header is not None and line.lstrip().startswith("{"):
            state = [False, False]
            depth = _brace_delta(line, state)
            block = [line]
            if depth <= 0:
                events.append({**header, "json": line})
                block, header, depth = [], None, 0
        elif header is not None and line.strip():
            header = None  # something else came between header and JSON
    return events


def slice_player_log(
    path: Path | None,
    start: int,
    end: int | None = None,
    match_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """GRE traffic (header + raw JSON) for one match from Player.log.

    Reads only ``[start - lookback, end]``; keeps every ``Match to`` /
    ``to Match`` block, trims to the first block that mentions *match_id*
    and stops after the match-completed room event. Returns the events and
    a small ``{start_marker_found, end_marker_found}`` info dict.
    """
    info = {"start_marker_found": False, "end_marker_found": False}
    if path is None or not Path(path).exists():
        return [], info
    try:
        text = _read_range(Path(path), max(0, int(start or 0) - PLAYER_LOG_LOOKBACK), end)
    except OSError:
        return [], info
    events = _match_events(text)
    if match_id:
        needle = str(match_id).lower()
        first = next((i for i, e in enumerate(events) if needle in e["json"].lower()), None)
        if first is not None:
            events = events[first:]
            info["start_marker_found"] = True
    for i, e in enumerate(events):
        if any(tok in e["json"] for tok in _MATCH_END_TOKENS):
            events = events[: i + 1]
            info["end_marker_found"] = True
            break
    return events, info


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bmc_[A-Za-z0-9]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
# Home directories: /Users/<name>, /home/<name>, C:\Users\<name> (a JSON-encoded
# path doubles the backslashes, so one-or-more are accepted).
_HOME_RE = re.compile(r"(?:/Users|/home|[A-Za-z]:\\+Users)[\\/]+[^\\/\s\"']+")
_MATCH_TO_RE = re.compile(r"(?<=Match to )[A-Za-z0-9_-]{8,}")
_TO_MATCH_RE = re.compile(r"[A-Za-z0-9_-]{8,}(?= to Match)")
# Inside serialized JSON the name ends at the next unescaped quote.
_RECORDED_MATCH_RE = re.compile(r"(Recorded match: \w+ vs )(?:[^\"\\]|\\.)*")
_SECRET_KEY_RE = re.compile(r"(?i)(license_key|api_key|_key$|^key$|^token$|secret|password|_url$)")
# ``*_token`` / ``*_tokens`` keys hold a credential when the value is a string
# (``access_token``) and a count when it is a number (``prompt_tokens``).
_TOKEN_KEY_RE = re.compile(r"(?i)(^|_)tokens?$")
# Player identity fields inside the raw Player.log JSON text that the bundle
# keeps as strings (``"playerName":"armour"``); the same keys as dict keys are
# handled by the value walk.
_PLAYER_FIELDS = ("playerName", "screenName", "userId", "sessionId", "clientId", "machineId")
_PLAYER_FIELD_RE = re.compile(r'"(?P<k>' + "|".join(_PLAYER_FIELDS) + r')"\s*:\s*"(?:[^"\\]|\\.)*"')
_BLOB_LIMIT = 1_000_000


def _scrub_text(text: str) -> str:
    for rx in _SECRET_RES:
        text = rx.sub("[redacted]", text)
    text = _HOME_RE.sub("~", text)
    text = _MATCH_TO_RE.sub("[account]", text)
    text = _TO_MATCH_RE.sub("[account]", text)
    text = _RECORDED_MATCH_RE.sub(r"\1opponent", text)
    return text


def _is_secret_key(key: str, value: Any) -> bool:
    if key == "install_id":
        return False
    if _SECRET_KEY_RE.search(key):
        return True
    return bool(_TOKEN_KEY_RE.search(key)) and isinstance(value, str)


def _scrub_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _is_secret_key(k, v):
                out[k] = "[redacted]" if v not in (None, "", False) else v
            else:
                out[k] = _scrub_keys(v)
        return out
    if isinstance(obj, list):
        return [_scrub_keys(v) for v in obj]
    if isinstance(obj, str) and len(obj) > _BLOB_LIMIT:
        return f"[dropped {len(obj)} chars]"
    return obj


def _name_patterns(names: dict[str, str] | None) -> list[tuple[re.Pattern, str]]:
    """Word-bounded patterns for every player name / account id, longest first.

    Both the raw spelling and its JSON-escaped form are covered: the
    Player.log events are kept as raw JSON text inside strings, so a name with
    a quote or backslash appears escaped there. A name is never replaced where
    it is a JSON key (``"hand": [...]``), and a name that is itself a JSON
    literal or a bare number (``null``, ``2024``) only where it is quoted, so
    the embedded JSON stays parseable.
    """
    patterns: list[tuple[re.Pattern, str]] = []
    for name, label in sorted((names or {}).items(), key=lambda kv: -len(kv[0])):
        if not isinstance(name, str) or len(name) < 4:
            continue
        literal_like = name.isdigit() or name.lower() in ("null", "true", "false")
        for spelling in {name, json.dumps(name, ensure_ascii=False)[1:-1]}:
            escaped = re.escape(spelling)
            if literal_like:
                patterns.append((re.compile(r'(?<=")' + escaped + r'(?=")'), label))
            else:
                patterns.append(
                    (
                        re.compile(r"(?<![A-Za-z0-9_])" + escaped + r'(?![A-Za-z0-9_])(?!\\?"\s*:)'),
                        label,
                    )
                )
    return patterns


def _scrub_string(text: str, name_patterns: list[tuple[re.Pattern, str]]) -> str:
    text = _PLAYER_FIELD_RE.sub(lambda m: f'"{m.group("k")}": "[{m.group("k")}]"', text)
    for rx, label in name_patterns:
        text = rx.sub(label, text)
    return _scrub_text(text)


def _scrub_values(obj: Any, name_patterns: list[tuple[re.Pattern, str]]) -> Any:
    """Apply the identity / name / secret scrubs to string values only.

    Keys and non-strings are never touched, so a player called ``hand``,
    ``null`` or ``2024`` cannot rewrite the document's structure.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k in _PLAYER_FIELDS and isinstance(v, str) and v:
                out[k] = f"[{k}]"
            else:
                out[k] = _scrub_values(v, name_patterns)
        return out
    if isinstance(obj, list):
        return [_scrub_values(v, name_patterns) for v in obj]
    if isinstance(obj, str):
        return _scrub_string(obj, name_patterns)
    return obj


def collect_player_names(
    player_log_events: list[dict[str, Any]],
    local_seat_id: int | None,
) -> dict[str, str]:
    """Map every player name / account id seen in the GRE room events to a seat label."""
    names: dict[str, str] = {}
    for event in player_log_events:
        js = event.get("json") or ""
        if "reservedPlayers" not in js and '"players"' not in js:
            continue
        try:
            payload = json.loads(js)
        except Exception:
            continue
        for players in _iter_player_lists(payload):
            for p in players:
                if not isinstance(p, dict):
                    continue
                seat = p.get("systemSeatId")
                label = "you" if local_seat_id is not None and seat == local_seat_id else "opponent"
                for key in ("playerName", "screenName"):
                    val = p.get(key)
                    if isinstance(val, str) and val.strip():
                        names[val] = label
                for key in ("userId", "sessionId", "clientId"):
                    val = p.get(key)
                    if isinstance(val, str) and val.strip():
                        names[val] = "[account]"
    return names


def _iter_player_lists(payload: Any):
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k in ("reservedPlayers", "players") and isinstance(v, list):
                yield v
            else:
                yield from _iter_player_lists(v)
    elif isinstance(payload, list):
        for v in payload:
            yield from _iter_player_lists(v)


def redact(bundle: Any, names: dict[str, str] | None = None) -> Any:
    """Return a redacted copy of a JSON-able object.

    Key-based scrubbing first, then — on string values only — the player
    identity fields, every known player name / id and the secret / email /
    home-path regexes. A final pass of the secret regexes over the serialized
    text is the safety net for a field nobody thought of; it can only replace
    inside strings and keys, never structure, and if it ever produced
    unparseable text the value-level copy is returned instead.
    """
    cleaned = _scrub_values(_scrub_keys(bundle), _name_patterns(names))
    text = json.dumps(cleaned, ensure_ascii=False, default=str)
    try:
        return json.loads(_scrub_text(text))
    except ValueError:
        return json.loads(text)


def settings_subset(settings: Any) -> dict[str, Any]:
    """The whitelisted, non-identifying subset of the settings."""
    getter = getattr(settings, "get", None)
    data = settings if isinstance(settings, dict) else None
    out: dict[str, Any] = {}
    for key in SETTINGS_WHITELIST:
        if data is not None and key not in data:
            continue
        try:
            val = data.get(key) if data is not None else (getter(key) if callable(getter) else None)
        except Exception:
            val = None
        if isinstance(val, (bool, int, float, str)) or val is None:
            out[key] = val
    return out


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def _json_size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def _truncate_lines(lines: list[str], cap: int, key: Callable[[str], int] = len) -> tuple[list[str], int]:
    """Drop oldest entries until the section fits *cap* bytes; returns (kept, dropped)."""
    total = sum(key(x) + 2 for x in lines)
    dropped = 0
    while lines and total > cap:
        total -= key(lines[0]) + 2
        lines = lines[1:]
        dropped += 1
    return lines, dropped


def _log_section(lines: list[str], dropped: int) -> dict[str, Any]:
    out = list(lines)
    if dropped:
        out.insert(0, f"[BUNDLE TRUNCATED: dropped {dropped} older lines]")
    return {"lines": out, "truncated": dropped}


def _events_section(events: list[dict[str, Any]], dropped: int, info: dict[str, Any]) -> dict[str, Any]:
    return {"events": list(events), "truncated": dropped, **info}


def _load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def collect_bug_reports(bug_dir: Path | None, since: float, limit: int = MAX_BUG_REPORTS) -> list[Path]:
    """Bug report JSON files saved since *since* (epoch), newest *limit*."""
    if bug_dir is None or not Path(bug_dir).is_dir():
        return []
    found: list[tuple[float, Path]] = []
    for path in Path(bug_dir).glob("bug_*.json"):
        with contextlib.suppress(OSError):
            mtime = path.stat().st_mtime
            if mtime >= since - 5:
                found.append((mtime, path))
    found.sort()
    return [p for _, p in found[-limit:]]


_BUG_REPORT_HEAVY_KEYS = ("bepinex_log", "recent_logs", "llm_context", "advice_history", "game_state")


def _bug_report_entry(path: Path) -> dict[str, Any] | None:
    report = _load_json_file(path)
    if report is None:
        return None
    report = dict(report)
    report.pop("screenshots", None)
    report.pop("mtga_log", None)
    raw_settings = report.pop("settings", None)
    report["settings_subset"] = settings_subset(raw_settings if isinstance(raw_settings, dict) else {})
    dropped: list[str] = []
    for key in _BUG_REPORT_HEAVY_KEYS:
        if _json_size(report) <= BUG_REPORT_CAP:
            break
        if key in report:
            report.pop(key)
            dropped.append(key)
    if dropped:
        report["dropped_fields"] = dropped
    return {"file": Path(path).name, "report": report}


def _app_info() -> dict[str, str]:
    try:
        from arenamcp import __version__

        version = str(__version__)
    except Exception:
        version = "unknown"
    return {
        "version": version,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
    }


def build_bundle(
    ctx: MatchBundleContext,
    *,
    install_id: str,
    result: str = "unknown",
    final_state: dict | None = None,
    packet: dict | None = None,
    packet_path: Path | str | None = None,
    advice_history: list[dict] | None = None,
    settings: Any = None,
    config: dict | None = None,
    bug_reports: list[Path] | None = None,
    bug_dir: Path | None = None,
    coach_log_end: int | None = None,
    player_log_end: int | None = None,
) -> dict[str, Any]:
    """Assemble and redact the bundle dict for one match."""
    if packet is None and packet_path:
        packet = _load_json_file(Path(packet_path))
    coach_lines = slice_coach_log(ctx.coach_log_path, ctx.coach_log_start, coach_log_end)
    coach_lines, coach_dropped = _truncate_lines(coach_lines, COACH_LOG_CAP)
    events, info = slice_player_log(ctx.player_log_path, ctx.player_log_start, player_log_end, ctx.match_id)
    events, events_dropped = _truncate_lines(events, PLAYER_LOG_CAP, key=lambda e: len(e["json"]) + 64)
    if events_dropped:
        info["start_marker_found"] = False

    reports = list(bug_reports or ctx.bug_reports or [])
    if not reports:
        reports = collect_bug_reports(
            bug_dir if bug_dir is not None else LOG_DIR / "bug_reports", ctx.started_at
        )
    report_entries = [e for e in (_bug_report_entry(p) for p in reports[-MAX_BUG_REPORTS:]) if e]

    state = dict(final_state) if isinstance(final_state, dict) else None
    if state is not None and _json_size(state) > FINAL_STATE_CAP:
        for key in ("raw_gre_events", "legal_actions_raw", "action_history", "recent_events"):
            state.pop(key, None)
            if _json_size(state) <= FINAL_STATE_CAP:
                break
        state["truncated"] = True

    history = [h for h in (advice_history or []) if isinstance(h, dict)][-MAX_ADVICE_ENTRIES:]
    packet_dict = dict(packet) if isinstance(packet, dict) else None
    if packet_dict is not None:
        packet_dict["opponent_name"] = "opponent" if packet_dict.get("opponent_name") else None
        if packet_dict.get("replay_path"):
            packet_dict["replay_path"] = Path(str(packet_dict["replay_path"])).name

    names = collect_player_names(events, (state or {}).get("local_seat_id"))
    for raw_name in ((state or {}).get("opponent_name"), (packet or {}).get("opponent_name")):
        if isinstance(raw_name, str) and raw_name.strip():
            names.setdefault(raw_name, "opponent")
    for entry in report_entries:
        gs = (entry["report"].get("game_state") or {}) if isinstance(entry["report"], dict) else {}
        opp = gs.get("opponent_name") if isinstance(gs, dict) else None
        if isinstance(opp, str) and opp.strip():
            names.setdefault(opp, "opponent")

    bundle = {
        "schema": SCHEMA_VERSION,
        "install_id": str(install_id or ""),
        "match_id": ctx.bundle_id,
        "arena_match_id": ctx.match_id,
        "game_number": ctx.game_number,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": ctx.started_at,
        "result": result or "unknown",
        "app": _app_info(),
        "settings_subset": settings_subset(settings if settings is not None else {}),
        "config": dict(config or {}),
        "packet": packet_dict,
        "final_state": state,
        "coach_log": _log_section(coach_lines, coach_dropped),
        "player_log": _events_section(events, events_dropped, info),
        "bug_reports": report_entries,
        "advice_history": history,
        # The shipped match_review detectors are NOT run here: the post-match
        # path already runs them (and appends to win_prob_calibration.jsonl);
        # the offline reviewer recomputes them from the slice.
    }
    return redact(bundle, names)


def _shrink(bundle: dict[str, Any]) -> bool:
    """Halve the two log sections (oldest first). False when nothing is left to drop."""
    changed = False
    coach = bundle.get("coach_log") or {}
    lines = [ln for ln in coach.get("lines", []) if not ln.startswith("[BUNDLE TRUNCATED")]
    if lines:
        drop = max(1, len(lines) // 2)
        bundle["coach_log"] = _log_section(lines[drop:], int(coach.get("truncated", 0)) + drop)
        changed = True
    plog = bundle.get("player_log") or {}
    events = plog.get("events", [])
    if events:
        drop = max(1, len(events) // 2)
        bundle["player_log"] = {
            **plog,
            "events": events[drop:],
            "truncated": int(plog.get("truncated", 0)) + drop,
            "start_marker_found": False,
        }
        changed = True
    return changed


def encode_bundle(bundle: dict[str, Any], gzip_cap: int = GZIP_CAP) -> bytes:
    """Gzip the bundle, shrinking the log sections until it fits *gzip_cap*."""
    while True:
        data = gzip.compress(
            json.dumps(bundle, ensure_ascii=False, default=str).encode("utf-8"), compresslevel=6
        )
        if len(data) <= gzip_cap or not _shrink(bundle):
            return data


def save_bundle(
    data: bytes, match_id: str, bundle_dir: Path | None = None, max_bundles: int = MAX_BUNDLES
) -> Path:
    target_dir = Path(bundle_dir) if bundle_dir is not None else BUNDLE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(match_id))[:80] or "match"
    path = target_dir / f"{safe_id}.json.gz"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    _rotate(target_dir, max_bundles)
    return path


def _rotate(target_dir: Path, max_bundles: int) -> None:
    try:
        files = sorted(target_dir.glob("*.json.gz"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - max_bundles)]:
            with contextlib.suppress(OSError):
                old.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _website_base() -> str:
    try:
        from arenamcp.subscription import WEBSITE_BASE

        return str(WEBSITE_BASE).rstrip("/")
    except Exception:
        return "https://mtgacoach.com"


def _default_opener(url: str, data: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read(512).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        with contextlib.suppress(Exception):
            body = e.read(512).decode("utf-8", errors="replace")
        return int(e.code), body


def _consents(should_upload: Callable[[], bool]) -> bool:
    try:
        return bool(should_upload())
    except Exception:
        return False


def upload_bundle(
    data: bytes,
    *,
    match_id: str,
    license_key: str,
    install_id: str,
    base_url: str | None = None,
    opener: Callable[..., tuple[int, str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retry_delays: tuple[float, ...] = RETRY_DELAYS_S,
    should_upload: Callable[[], bool] | None = None,
) -> bool:
    """POST the gzip bundle; retries on network errors / 5xx / 429. Never raises.

    *should_upload* is consulted immediately before every attempt (building
    the bundle and the retry back-off both take long enough for the user to
    turn sharing off in the meantime); a False answer ends the upload.
    """
    url = (base_url or _website_base()).rstrip("/") + UPLOAD_PATH
    headers = {
        "Content-Type": "application/gzip",
        "Authorization": f"Bearer {license_key}",
        "X-MTGACoach-Match-ID": str(match_id),
    }
    try:
        from arenamcp.client_metadata import get_client_headers

        headers.update(get_client_headers())
    except Exception:
        headers.setdefault("User-Agent", "mtgacoach/unknown (unknown)")
    headers["X-MTGACoach-Install-ID"] = str(install_id)
    send = opener or _default_opener
    attempts = len(retry_delays) + 1
    for attempt in range(attempts):
        if should_upload is not None and not _consents(should_upload):
            logger.info(f"[BUNDLE] skipped: sharing turned off before upload of {match_id}")
            return False
        try:
            status, body = send(url, data, headers, UPLOAD_TIMEOUT_S)
        except Exception as e:  # URLError, timeouts, anything
            status, body = 0, f"{type(e).__name__}: {e}"
        if status in (200, 201, 202, 204, 409):
            logger.info(f"[BUNDLE] uploaded {match_id} (HTTP {status}, {len(data) // 1024} KB)")
            return True
        retryable = status in (0, 429) or status >= 500
        if not retryable or attempt == attempts - 1:
            logger.warning(f"[BUNDLE] upload failed ({'HTTP ' + str(status) if status else body[:120]})")
            return False
        delay = retry_delays[attempt]
        logger.info(
            f"[BUNDLE] upload attempt {attempt + 1} failed ({status or body[:80]}); retrying in {delay:g}s"
        )
        try:
            sleep(delay)
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Orchestration (called from the coaching loop at game end)
# ---------------------------------------------------------------------------


def _setting(settings: Any, key: str, default: Any = None) -> Any:
    try:
        if isinstance(settings, dict):
            return settings.get(key, default)
        return settings.get(key, default)
    except Exception:
        return default


def _should_skip(
    ctx: MatchBundleContext, settings: Any, packet, advice_history, has_events: bool
) -> str | None:
    if _setting(settings, "share_match_logs", True) is False:
        return "sharing off"
    if not str(_setting(settings, "license_key", "") or "").strip():
        return "no license key"
    with _lock:
        if ctx.bundle_id in _bundled_match_ids:
            return f"already bundled {ctx.bundle_id}"
    decisions = (packet or {}).get("decisions") if isinstance(packet, dict) else None
    if not decisions and not advice_history and not has_events:
        return "nothing recorded for this match"
    return None


def _player_log_has_events(ctx: MatchBundleContext) -> bool:
    path = ctx.player_log_path
    if path is None:
        return False
    try:
        return Path(path).stat().st_size > ctx.player_log_start
    except OSError:
        return False


def process_match(
    ctx: MatchBundleContext,
    *,
    settings: Any,
    result: str = "unknown",
    final_state: dict | None = None,
    packet_path: Path | str | None = None,
    advice_history: list[dict] | None = None,
    config: dict | None = None,
    bug_dir: Path | None = None,
    bundle_dir: Path | None = None,
    base_url: str | None = None,
    opener: Callable[..., tuple[int, str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    upload: bool = True,
) -> Path | None:
    """Build, save and upload the bundle for *ctx* synchronously. Never raises."""
    try:
        packet = _load_json_file(Path(packet_path)) if packet_path else None
        skip = _should_skip(ctx, settings, packet, advice_history, _player_log_has_events(ctx))
        if skip:
            logger.info(f"[BUNDLE] skipped: {skip}")
            return None
        with _lock:
            _bundled_match_ids.add(ctx.bundle_id)
        install_id = str(_setting(settings, "install_id", "") or "")
        bundle = build_bundle(
            ctx,
            install_id=install_id,
            result=result,
            final_state=final_state,
            packet=packet,
            advice_history=advice_history,
            settings=settings,
            config=config,
            bug_dir=bug_dir,
        )
        data = encode_bundle(bundle)
        path = save_bundle(data, ctx.bundle_id, bundle_dir)
        logger.info(f"[BUNDLE] saved {path} ({len(data) // 1024} KB)")
        if upload:
            upload_bundle(
                data,
                match_id=ctx.bundle_id,
                license_key=str(_setting(settings, "license_key", "") or ""),
                install_id=install_id,
                base_url=base_url,
                opener=opener,
                sleep=sleep,
                should_upload=lambda: _setting(settings, "share_match_logs", True) is not False,
            )
        return path
    except Exception as e:
        logger.warning(f"[BUNDLE] failed: {e}")
        return None


def schedule_upload(ctx: MatchBundleContext, **kwargs: Any) -> threading.Thread | None:
    """Run :func:`process_match` on a daemon thread; the loop never waits."""
    if _setting(kwargs.get("settings"), "share_match_logs", True) is False:
        logger.info("[BUNDLE] skipped: sharing off")
        return None
    try:
        thread = threading.Thread(
            target=process_match,
            args=(ctx,),
            kwargs=kwargs,
            name=f"match-bundle-{ctx.bundle_id[:8]}",
            daemon=True,
        )
        thread.start()
        return thread
    except Exception as e:
        logger.warning(f"[BUNDLE] could not start upload thread: {e}")
        return None
