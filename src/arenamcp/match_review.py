"""Post-match structured review: deterministic, verifiable findings.

The prose post-match analysis is narrative — and demonstrably willing to
invent events (it reported a "concede recommendation" that was never
given, match 9d7d486b). This module extracts only findings that can be
proven from the match's own artifacts: the coach log slice, the advice
history, and the match packet. Every finding carries the evidence lines
it was derived from, so the filed issue is directly actionable.

Categories map to who fixes what:
- card_db      → enrichment gaps (unresolved Card#IDs leaking into prompts)
- autopilot    → MANUAL REQUIRED pauses, matcher dead-ends, rejected submits
- planner      → validator drops of actions that WERE legal, zero-action plans
- advice       → repetition, win-probability calibration misses
- platform     → environment noise (e.g. screenshot attempts on Wayland)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

REVIEW_DIR = Path.home() / ".arenamcp" / "match_reviews"
CALIBRATION_LOG = Path.home() / ".arenamcp" / "win_prob_calibration.jsonl"

_SEVERITIES = ("low", "medium", "high")


@dataclass
class Finding:
    category: str
    title: str
    detail: str
    severity: str = "medium"  # low | medium | high
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Log slicing
# ---------------------------------------------------------------------------

_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def read_log_slice(log_path: Path, since: datetime) -> str:
    """Return log lines at/after `since`. Lines without a leading timestamp
    (tracebacks, continuations) inherit the inclusion of the previous line."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    out: list[str] = []
    including = False
    for line in text.splitlines():
        m = _LOG_TS_RE.match(line)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                including = ts >= since
            except ValueError:
                pass
        if including:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Detectors — each returns a list of Findings with evidence
# ---------------------------------------------------------------------------

_CARD_ID_RE = re.compile(r"Card#(\d+)")


def detect_unresolved_cards(log_slice: str, advice_history: list[dict]) -> list[Finding]:
    counts: dict[str, int] = {}
    for m in _CARD_ID_RE.finditer(log_slice):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    for entry in advice_history or []:
        for blob in (entry.get("advice") or "", entry.get("game_context") or ""):
            for m in _CARD_ID_RE.finditer(blob):
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    if not counts:
        return []
    total = sum(counts.values())
    ids = sorted(counts, key=counts.get, reverse=True)
    return [Finding(
        category="card_db",
        title=f"{len(ids)} unresolved card grpIds leaked into prompts {total}x",
        detail=(
            "These grpIds have no name in the local card DB, so prompts and "
            "the post-match analysis see opaque Card#IDs — the model invents "
            "properties for them. Refresh the card database. "
            f"grpIds: {', '.join(ids[:12])}"
        ),
        severity="high" if len(ids) >= 3 else "medium",
        evidence=[f"Card#{i} x{counts[i]}" for i in ids[:12]],
    )]


_MANUAL_RE = re.compile(r"MANUAL REQUIRED: (.+)$", re.MULTILINE)


def detect_manual_required(log_slice: str) -> list[Finding]:
    reasons: dict[str, list[str]] = {}
    for m in _MANUAL_RE.finditer(log_slice):
        text = m.group(1).strip()
        # Group by the stable hint (strip card names in parens)
        key = re.sub(r"\([^)]*\)", "(…)", text)
        reasons.setdefault(key, []).append(text)
    out = []
    for key, hits in reasons.items():
        out.append(Finding(
            category="autopilot",
            title=f"MANUAL REQUIRED x{len(hits)}: {key[:90]}",
            detail=(
                "Autopilot paused and required manual input. Each distinct "
                "reason here is either a bridge gap or a planner gap."
            ),
            severity="high",
            evidence=hits[:5],
        ))
    return out


_DROP_RE = re.compile(
    r"Dropping illegal planner action: (\S+) \(([^)]*)\).*?not in \[([^\]]*)\]"
)


def detect_validator_dropped_legal(log_slice: str) -> list[Finding]:
    """Planner validator dropped an action whose name IS in the legal list —
    a string-matching bug in the validator, not an illegal action."""
    out = []
    seen = set()
    for m in _DROP_RE.finditer(log_slice):
        _atype, name, legal_blob = m.group(1), m.group(2).strip(), m.group(3)
        legal = [s.strip().strip("'\"") for s in legal_blob.split(",")]
        if not name:
            continue
        if any(name.lower() == l.lower() for l in legal) and name not in seen:
            seen.add(name)
            out.append(Finding(
                category="planner",
                title=f"Validator dropped '{name}' although it IS in the legal list",
                detail=(
                    "The legality validator rejected a planner action whose "
                    "name appears verbatim in the legal-actions list — a "
                    "matching bug that delays execution until a fallback "
                    "recovers it."
                ),
                severity="high",
                evidence=[m.group(0)[:300]],
            ))
    return out


_NOMATCH_RE = re.compile(r"Could not match (\w+) '([^']*)' among 0 (\w+) actions")


def detect_matcher_dead_ends(log_slice: str) -> list[Finding]:
    counts: dict[str, list[str]] = {}
    for m in _NOMATCH_RE.finditer(log_slice):
        key = f"{m.group(1)} '{m.group(2)}'"
        counts.setdefault(key, []).append(m.group(0))
    out = []
    for key, hits in counts.items():
        if len(hits) >= 2:
            out.append(Finding(
                category="autopilot",
                title=f"Matcher dead-end x{len(hits)}: {key}",
                detail=(
                    "The planner repeatedly produced an action the GRE "
                    "matcher could not map to any bridge action — usually a "
                    "request family not yet on the typed pipeline (e.g. "
                    "CastingTimeOption) or a hallucinated card name."
                ),
                severity="medium",
                evidence=hits[:4],
            ))
    return out


def detect_rejected_decisions(packet: Optional[dict]) -> list[Finding]:
    out = []
    for dec in (packet or {}).get("decisions") or []:
        outcome = dec.get("outcome")
        if outcome in ("REJECTED", "ROLLED_BACK"):
            pd = dec.get("pending_decision") or {}
            out.append(Finding(
                category="autopilot",
                title=f"{pd.get('request_type', '?')} submission {outcome}",
                detail=(
                    "A typed-pipeline submission was not accepted by MTGA. "
                    f"Chosen: {dec.get('chosen_options')}; request_id "
                    f"{pd.get('request_id')}."
                ),
                severity="high",
                evidence=[json.dumps(pd)[:400]],
            ))
    return out


def _norm_advice(text: str) -> str:
    return re.sub(r"[^a-z ]", "", (text or "").lower())[:40]


def detect_advice_repetition(advice_history: list[dict]) -> list[Finding]:
    counts: dict[str, int] = {}
    sample: dict[str, str] = {}
    for e in advice_history or []:
        key = _norm_advice(e.get("advice") or "")
        if len(key) < 12:
            continue
        counts[key] = counts.get(key, 0) + 1
        sample.setdefault(key, (e.get("advice") or "")[:120])
    out = []
    for key, n in counts.items():
        if n >= 4:
            out.append(Finding(
                category="advice",
                title=f"Near-identical advice repeated {n}x",
                detail=(
                    "The coach verbalized essentially the same line many "
                    "times in one match; repeated advice should be deduped "
                    "or summarized per turn."
                ),
                severity="low",
                evidence=[sample[key]],
            ))
    return out


_WINPROB_RE = re.compile(r"\[WIN-PROB\] WIN: (\d+)%")


def detect_win_prob_misses(log_slice: str, match_result: str) -> list[Finding]:
    probs = [int(m.group(1)) for m in _WINPROB_RE.finditer(log_slice)]
    if not probs:
        return []
    # Append every estimate to the calibration log (signal for tuning).
    try:
        CALIBRATION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CALIBRATION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "result": match_result,
                "estimates": probs,
            }) + "\n")
    except OSError:
        pass
    last = probs[-1]
    if (match_result == "win" and last <= 20) or (match_result == "loss" and last >= 80):
        return [Finding(
            category="advice",
            title=f"Win-probability miss: last estimate {last}% but result was {match_result}",
            detail=(
                "The final win-probability estimate was on the wrong side by "
                "a wide margin. All estimates from this match were appended "
                f"to {CALIBRATION_LOG.name} for calibration."
            ),
            severity="medium",
            evidence=[f"estimates: {probs}"],
        )]
    return []


def detect_platform_noise(log_slice: str) -> list[Finding]:
    hits = re.findall(r"All screenshot methods failed.*", log_slice)
    if len(hits) >= 2:
        return [Finding(
            category="platform",
            title=f"Vision fallback attempted screenshots {len(hits)}x on a platform where capture fails",
            detail=(
                "Screen capture failed every time (Wayland/no input backend). "
                "The vision fallback should be disabled on this platform "
                "instead of burning cycles per decision."
            ),
            severity="low",
            evidence=hits[:3],
        )]
    return []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_match_review(
    *,
    advice_history: list[dict],
    match_result: str,
    log_slice: str = "",
    packet: Optional[dict] = None,
) -> list[Finding]:
    findings: list[Finding] = []
    detectors = (
        lambda: detect_unresolved_cards(log_slice, advice_history),
        lambda: detect_manual_required(log_slice),
        lambda: detect_validator_dropped_legal(log_slice),
        lambda: detect_matcher_dead_ends(log_slice),
        lambda: detect_rejected_decisions(packet),
        lambda: detect_advice_repetition(advice_history),
        lambda: detect_win_prob_misses(log_slice, match_result),
        lambda: detect_platform_noise(log_slice),
    )
    for det in detectors:
        try:
            findings.extend(det())
        except Exception as e:
            logger.debug(f"match-review detector failed: {e}")
    return findings


def save_review(match_id: str, match_result: str, findings: list[Finding]) -> Optional[Path]:
    try:
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = REVIEW_DIR / f"review_{ts}_{(match_id or 'unknown')[:8]}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "match_id": match_id,
                "result": match_result,
                "created": datetime.now().isoformat(),
                "findings": [f.to_dict() for f in findings],
            }, f, indent=2)
        return path
    except OSError as e:
        logger.warning(f"match-review save failed: {e}")
        return None


def should_file_issue(findings: list[Finding]) -> bool:
    """File only when something medium/high surfaced — low-only reviews
    stay local so the tracker doesn't fill with repetition notes."""
    return any(f.severity in ("medium", "high") for f in findings)


def build_issue(
    match_id: str,
    match_result: str,
    findings: list[Finding],
    version: str = "",
) -> tuple[str, str]:
    n = len(findings)
    high = sum(1 for f in findings if f.severity == "high")
    title = (
        f"[match-review] {match_result} {(match_id or 'unknown')[:8]}: "
        f"{n} finding{'s' if n != 1 else ''}"
        + (f" ({high} high)" if high else "")
    )
    lines = [
        "## Match Review (deterministic)",
        "",
        f"- Match: `{match_id}`",
        f"- Result: `{match_result}`",
        f"- Version: `{version}`",
        f"- Findings: {n}",
        "",
        "Every finding below was mechanically detected from the match's log",
        "slice, advice history, or match packet — evidence included. No LLM",
        "claims, no speculation.",
        "",
    ]
    order = {"high": 0, "medium": 1, "low": 2}
    for f in sorted(findings, key=lambda f: order.get(f.severity, 3)):
        lines.append(f"### [{f.severity.upper()}] {f.category}: {f.title}")
        lines.append("")
        lines.append(f.detail)
        if f.evidence:
            lines.append("")
            lines.append("```")
            lines.extend(e[:300] for e in f.evidence)
            lines.append("```")
        lines.append("")
    return title, "\n".join(lines)
