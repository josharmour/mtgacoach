from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

GITHUB_REPO = "josharmour/mtgacoach"
GITHUB_ISSUES_NEW_URL = f"https://github.com/{GITHUB_REPO}/issues/new"


def build_issue_payload(
    report_data: dict[str, Any],
    report_path: Path,
    user_message: str = "",
) -> tuple[str, str]:
    """Build a concise GitHub issue title/body from a saved bug report."""
    timestamp = str(report_data.get("timestamp", report_path.stem))
    version = str(report_data.get("version", "unknown"))
    config = report_data.get("config", {}) or {}
    voice = report_data.get("voice", {}) or {}
    errors = report_data.get("errors", []) or []
    recent_logs = report_data.get("recent_logs", []) or []
    game_state = report_data.get("game_state", {}) or {}
    turn = game_state.get("turn", {}) if isinstance(game_state, dict) else {}
    match_context = report_data.get("match_context", {}) or {}
    reporter = report_data.get("reporter", {}) or {}
    settings = report_data.get("settings", {}) or {}
    post_match_feedback = report_data.get("post_match_feedback", {}) or {}
    install_id = reporter.get("install_id") or settings.get("install_id")

    note = user_message.strip()
    title_suffix = note if note else timestamp.replace("T", " ").split(".")[0]
    title = f"Desktop bug report: {title_suffix}"
    if len(title) > 78:
        title = title[:75].rstrip() + "..."

    excerpt = {
        "config": config,
        "reporter": {"install_id": install_id},
        "voice": voice,
        "match_context": match_context,
        "bridge_state": report_data.get("bridge_state"),
        "autopilot": report_data.get("autopilot"),
        "replay": report_data.get("replay"),
        "recent_errors": errors[-5:],
    }
    excerpt_json = json.dumps(excerpt, indent=2, default=str)
    if len(excerpt_json) > 24000:
        excerpt_json = excerpt_json[:23900] + "\n... truncated ..."

    log_tail = "".join(str(line) for line in recent_logs[-25:]).strip()
    if len(log_tail) > 8000:
        log_tail = log_tail[-8000:]

    lines = [
        "## Summary",
        "",
        f"- Version: `{version}`",
        f"- Timestamp: `{timestamp}`",
        f"- Local report: `{report_path}`",
    ]
    if install_id:
        lines.append(f"- Install ID: `{install_id}`")
    if note:
        lines.append(f"- Reporter note: {note}")
    if post_match_feedback.get("source"):
        lines.append(f"- Feedback source: `{post_match_feedback.get('source')}`")

    lines.extend(
        [
            "",
            "## Runtime",
            "",
            f"- Backend: `{config.get('backend', 'unknown')}`",
            f"- Model: `{config.get('model') or 'default'}`",
            f"- Advice style: `{config.get('advice_style', 'unknown')}`",
            f"- Voice: `{voice.get('tts_voice')}`",
            f"- Auto speak: `{config.get('auto_speak')}`",
            "",
            "## Game Snapshot",
            "",
            f"- Match ID: `{match_context.get('match_id')}`",
            f"- Turn: `{turn.get('turn_number')}`",
            f"- Phase: `{turn.get('phase')}`",
            f"- Pending decision: `{game_state.get('pending_decision')}`",
            "",
            "## Recent Errors",
            "",
        ]
    )

    if errors:
        for entry in errors[-5:]:
            lines.append(f"- `{entry.get('timestamp', '?')}` {entry.get('context', '')}: {entry.get('error', '')}")
    else:
        lines.append("- No recent recorded errors.")

    if post_match_feedback:
        lines.extend(["", "## Coaching Feedback", ""])
        match_result = str(post_match_feedback.get("match_result") or "").strip()
        if match_result:
            lines.append(f"- Match result: `{match_result}`")
        user_feedback = str(post_match_feedback.get("user_feedback") or "").strip()
        if user_feedback:
            lines.append(f"- User feedback: {user_feedback}")
        analysis = str(post_match_feedback.get("analysis") or "").strip()
        if analysis:
            trimmed_analysis = analysis
            if len(trimmed_analysis) > 4000:
                trimmed_analysis = trimmed_analysis[:3900].rstrip() + "\n... truncated ..."
            lines.extend(
                [
                    "",
                    "<details>",
                    "<summary>Post-match analysis attached</summary>",
                    "",
                    trimmed_analysis,
                    "",
                    "</details>",
                ]
            )

    lines.extend(
        [
            "",
            "<details>",
            "<summary>Debug Excerpt</summary>",
            "",
            "```json",
            excerpt_json,
            "```",
            "</details>",
        ]
    )

    if log_tail:
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Recent Log Tail</summary>",
                "",
                "```text",
                log_tail,
                "```",
                "</details>",
            ]
        )

    lines.extend(
        [
            "",
            "---",
            "The full local JSON report was saved on the reporter's machine. Ask them for that file if deeper forensics are needed.",
        ]
    )

    return title, "\n".join(lines)


def build_issue_url(title: str, body: str, max_body_chars: int = 6000) -> str:
    """Build a prefilled GitHub issue URL.

    Browser URL lengths are limited, so keep the fallback body compact.
    """
    trimmed_body = body
    if len(trimmed_body) > max_body_chars:
        trimmed_body = trimmed_body[: max_body_chars - 32].rstrip() + "\n\n... browser draft truncated ..."
    query = urlencode({"title": title, "body": trimmed_body})
    return f"{GITHUB_ISSUES_NEW_URL}?{query}"
