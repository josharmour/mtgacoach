"""Opt-in, bounded parser diagnostics; never persist raw model/request bodies."""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
MALFORMED_ACTION_DIR = Path.home() / ".arenamcp" / "debug"
MALFORMED_ACTION_KEEP = 20
MALFORMED_ACTION_MAX_BYTES = 16_384
_WINDOW_CHARS = 1024
_PREFIX = "action_json_diagnostic_"
_WRITE_LOCK = threading.Lock()
_REQUESTS = frozenset(
    {
        "ActionsAvailable",
        "DeclareAttackers",
        "DeclareBlockers",
        "AssignDamage",
        "SelectTargets",
        "CastingTimeOptions",
        "SelectN",
        "Search",
        "OptionalAction",
        "NumericInput",
        "Group",
        "Mulligan",
        "PayCosts",
    }
)
_ERRORS = {
    "Expecting value": "expected_value",
    "Expecting property name enclosed in double quotes": "expected_quoted_key",
    "Expecting ':' delimiter": "expected_colon",
    "Expecting ',' delimiter": "expected_comma",
    "Extra data": "extra_data",
    "Unterminated string starting at": "unterminated_string",
    "Invalid \\escape": "invalid_escape",
}


def _integer(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _syntax_window(text: str, start: int) -> dict:
    """Preserve only JSON delimiters/whitespace and offsets, not literal content.

    This is a syntax map, NOT reparsable JSON. All letters, digits, Unicode,
    credentials, names, paths and arbitrary prose become x, including escape
    letters; quotes and backslashes remain visible to diagnose #245 escaping.
    Masking every non-structural character avoids fragile secret-pattern lists
    on malformed or unterminated strings. Work is bounded to one small window.
    """
    fragment = text[start : start + _WINDOW_CHARS]
    return {
        "start": start,
        "end": start + len(fragment),
        "truncated": start != 0 or len(fragment) != len(text),
        "shape": "".join(char if char in '{}[]:,"\\ \t\r\n' else "x" for char in fragment),
    }


def record_parse_failure(
    response: str,
    *,
    parse_input: str,
    error: json.JSONDecodeError,
    recovery: str,
    legal_action_count: int,
    request_type: str | None = None,
    request_id: tuple = (),
    origin_logger: logging.Logger | None = None,
) -> Path | None:
    """Record safe error/recovery metadata; capture files only with explicit opt-in.

    ARENAMCP_ACTION_JSON_DIAGNOSTICS is off by default. `metadata` stores only
    allowlisted scalar metadata; `syntax` additionally stores masked windows.
    Both require DEBUG enabled in the originating planner and this logger,
    respecting logger.disabled, effective levels and logging.disable(). This
    function never changes logging settings and has no raw-body capture mode.
    """
    kind = _ERRORS.get(error.msg, "invalid_json")
    recovery = recovery if recovery in {"action_payload", "pick", "none"} else "none"
    origin_logger = origin_logger or logger
    origin_logger.log(
        logging.ERROR if recovery == "none" else logging.WARNING,
        "Action JSON parse failure: kind=%s offset=%s recovery=%s input_chars=%d",
        kind,
        _integer(error.pos),
        recovery,
        len(parse_input),
    )
    mode = os.getenv("ARENAMCP_ACTION_JSON_DIAGNOSTICS", "off").strip().lower()
    if (
        mode not in {"metadata", "syntax"}
        or not origin_logger.isEnabledFor(logging.DEBUG)
        or not logger.isEnabledFor(logging.DEBUG)
    ):
        return None
    path = None
    try:
        payload = {
            "version": 1,
            "capture_mode": mode,
            "error": {
                "kind": kind,
                "position": _integer(error.pos),
                "line": _integer(error.lineno),
                "column": _integer(error.colno),
            },
            "recovery": recovery,
            "legality_still_required": recovery != "none",
            "response_chars": len(response),
            "parse_input_chars": len(parse_input),
            "normalization_changed": response != parse_input,
            "legal_action_count": _integer(legal_action_count),
            "request_type": request_type if request_type in _REQUESTS else "other",
            "request_id": list(request_id)
            if len(request_id) == 2 and all(_integer(v) is not None for v in request_id)
            else [],
        }
        if mode == "syntax":
            payload["response_prefix"] = _syntax_window(response, 0)
            start = max(0, min(error.pos, len(parse_input)) - _WINDOW_CHARS // 2)
            payload["parse_error_window"] = _syntax_window(parse_input, start)
        encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        if len(encoded) > MALFORMED_ACTION_MAX_BYTES:
            payload.pop("response_prefix", None)
            payload.pop("parse_error_window", None)
            payload["syntax_omitted"] = "byte_cap"
            encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        if len(encoded) > MALFORMED_ACTION_MAX_BYTES:
            return None
        with _WRITE_LOCK:
            MALFORMED_ACTION_DIR.mkdir(parents=True, exist_ok=True)
            # mkstemp-backed files are private (0600) and unique even in one tick.
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=MALFORMED_ACTION_DIR,
                prefix=f"{_PREFIX}{time.time_ns()}_",
                suffix=".json",
                delete=False,
            ) as stream:
                path = Path(stream.name)
                stream.write(encoded)
            for stale in sorted(MALFORMED_ACTION_DIR.glob(f"{_PREFIX}*.json"))[:-MALFORMED_ACTION_KEEP]:
                stale.unlink(missing_ok=True)
        return path
    except (OSError, TypeError, ValueError):
        # Do not print exception text: paths or payload values may be private.
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.debug("Could not save bounded action JSON diagnostic")
        return None
