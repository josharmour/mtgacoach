"""Stall corpus: every autopilot dead-end becomes a replayable fixture.

(fable-improvements.md item 5.) When a typed decision is rejected,
exhausted, or surfaced as MANUAL REQUIRED, the full PendingDecision plus
the planner's answer and the outcome are dumped to
``~/.arenamcp/stall_corpus/``. ``tools/eval/replay_stalls.py`` replays the
corpus offline; curated fixtures are promoted into
``tests/fixtures/stalls/`` and run in CI forever.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from arenamcp.decisions import PendingDecision, decision_to_dict

logger = logging.getLogger(__name__)

CORPUS_DIR = Path.home() / ".arenamcp" / "stall_corpus"
# Keep the corpus bounded; oldest fixtures rotate out.
MAX_FIXTURES = 200


def record_stall(
    decision: PendingDecision,
    option_ids: Optional[list[str]],
    outcome: str,
    context: Optional[dict[str, Any]] = None,
    *,
    corpus_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Append one stall fixture. Never raises; returns the path or None."""
    try:
        target_dir = corpus_dir or CORPUS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_type = "".join(
            c for c in decision.request_type if c.isalnum()
        ) or "Unknown"
        path = target_dir / f"stall_{ts}_{safe_type}_{int(time.time() * 1000) % 100000}.json"
        fixture = {
            "pending_decision": decision_to_dict(decision),
            "planner_answer": {"option_ids": option_ids or []},
            "outcome": outcome,
            "context": context or {},
        }
        path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
        _rotate(target_dir)
        logger.info(f"Stall fixture recorded: {path.name} ({outcome})")
        return path
    except Exception as e:
        logger.debug(f"record_stall failed (ignored): {e}")
        return None


def _rotate(target_dir: Path) -> None:
    fixtures = sorted(target_dir.glob("stall_*.json"))
    excess = len(fixtures) - MAX_FIXTURES
    for old in fixtures[:excess]:
        try:
            old.unlink()
        except OSError:
            pass


def load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
