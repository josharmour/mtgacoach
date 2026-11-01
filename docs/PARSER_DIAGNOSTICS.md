# Malformed action diagnostics

Malformed-action parsing logs only an error category, normalized-input offset,
recovery outcome (`none`, `action_payload`, or `pick`), and character count.
Recovered payloads still go through the existing request-legality checks.

File capture is **off by default**. `ARENAMCP_ACTION_JSON_DIAGNOSTICS` supports:

- `off` (also missing, empty, or unrecognized values): no files or rotation.
- `metadata`: allowlisted error positions, lengths, recovery outcome, legal-menu
  count, known request type and numeric request IDs. No request/decision context,
  match/player identity, legal-action labels, exception text, or response body.
- `syntax`: the same metadata plus two masked windows of at most 1,024 characters:
  the original-response prefix and normalized input around the parse error.
  Only JSON delimiters, quotes, backslashes and ASCII whitespace survive; every
  other character becomes `x`. These maps diagnose broken quote/escape boundaries
  and are deliberately **not replayable JSON or raw model output**.

Both capture modes also require DEBUG to be enabled for the originating planner
and diagnostic loggers. Effective log levels, disabled loggers, and Python's
global `logging.disable()` take precedence. Capture never changes logging levels,
UI settings, or environment configuration. There is no raw/full-body mode.

Files use the `action_json_diagnostic_*.json` family under the debug directory.
Each file is capped at 16 KiB; at most 20 are retained (320 KiB total for this
family). Oversized syntax is omitted; oversized metadata is not written. Writes
are serialized within the process and use private temporary files (0600 on
POSIX). Retention touches only this family. File failures do not block recovery
or print sensitive paths. Disabling capture preserves existing evidence files.

This replaces the proposed unbounded capture in `091bd189`; that excluded commit
is not a dependency. The historical #214(b)/#245 prefixes remain regression
fixtures, while the unavailable complete historical answers and quote-escaping
cause remain unqualified. These diagnostics do not close those evidence gaps.
