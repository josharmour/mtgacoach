"""Post-match self-evaluation for the autonomous real-match harness.

After each MTGA game the *playing* model reviews its own performance and writes a
structured critique to JSONL. These critiques, together with the per-decision
trajectories recorded by :class:`arenamcp.trajectory_recorder.TrajectoryRecorder`,
are the two inputs the training pipeline (``tools/training/build_dataset.py``)
consumes to actually improve the model over many games: trajectories supply the
concrete state→action pairs, while these evaluations supply a model-graded signal
about *which* games (and which kinds of plays) went well or badly.

Design goals (mirrors the trajectory recorder):
- **Dependency-light:** stdlib only (json, logging, datetime, re).
- **Safe:** :meth:`MatchEvaluator.evaluate` NEVER raises into the match loop. On
  any error it logs and returns ``None``. The harness must keep playing matches
  whether or not evaluation succeeds.
- **Reuse:** constructed with the SAME LLM client the harness already built for
  the autopilot — it does not open a new backend connection.

The prompt style mirrors ``coach.POST_MATCH_ANALYSIS_PROMPT`` (an expert MTG
coach reviewing the bot's just-played game) but asks for STRICT JSON so the
output is machine-consumable for training.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("arenamcp.match_evaluator")

# Default output path, alongside the eval/trajectory data.
_REPO = Path(__file__).resolve().parents[2]
DEFAULT_EVAL_PATH = _REPO / "tools/eval/data/match_evaluations.jsonl"

# Cap on how many decisions get summarised into the review prompt. Keeps the
# prompt bounded for long games while retaining the most informative plays.
_MAX_DECISIONS = 36

EVAL_SYSTEM_PROMPT = (
    "You are an expert Magic: The Gathering coach reviewing a game that an AI bot "
    "just played on its own. You are grading the BOT'S OWN decisions to produce a "
    "critique that will be used to train a better model. Be specific and honest — "
    "reference actual turns and cards from the decision log, not generic platitudes.\n\n"
    "Respond with STRICT JSON only (no prose, no code fences) in exactly this shape:\n"
    "{\n"
    '  "summary": "one or two sentences on how the game went and how it was decided",\n'
    '  "key_mistakes": ["specific misplay 1", "specific misplay 2"],\n'
    '  "improvements": ["concrete actionable rule the bot should follow next time"],\n'
    '  "self_score": 3\n'
    "}\n"
    "self_score is an integer 1-5 (1 = played terribly, 5 = played near-optimally). "
    "Use [] for key_mistakes/improvements if there are genuinely none. "
    "Only reference card names that appear in the decision log; do not invent cards."
)


def _summarize_decisions(decisions: List[Dict[str, Any]]) -> str:
    """Render a compact turn-by-turn list of the bot's key plays.

    Caps to the most informative ``_MAX_DECISIONS`` records: when there are many,
    keep the opening and the endgame (where games are usually decided) and drop
    the middle, marking the gap.
    """
    recs = list(decisions or [])
    if len(recs) > _MAX_DECISIONS:
        head = _MAX_DECISIONS * 2 // 3
        tail = _MAX_DECISIONS - head
        kept = recs[:head] + [{"_gap": len(recs) - _MAX_DECISIONS}] + recs[-tail:]
    else:
        kept = recs

    lines: List[str] = []
    for rec in kept:
        if "_gap" in rec:
            lines.append(f"... ({rec['_gap']} routine decisions omitted) ...")
            continue
        turn = rec.get("turn", "?")
        phase = rec.get("phase", "") or ""
        rtype = rec.get("request_type", "") or ""
        action = rec.get("planned_action", "pass")
        prefix = f"T{turn}"
        if phase:
            prefix += f"/{phase}"
        meta = f" [{rtype}]" if rtype else ""
        lines.append(f"{prefix}{meta}: {action}")
    return "\n".join(lines) if lines else "(no decisions recorded)"


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Tolerantly parse a JSON object out of an LLM response.

    Strips ```json fences and surrounding prose, then parses the first balanced
    ``{...}`` block. Returns ``None`` if nothing parseable is found.
    """
    if not text:
        return None
    cleaned = text.strip()
    # Drop code fences.
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Fall back to the first balanced {...} span.
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        c = cleaned[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[start : i + 1])
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    return None
    return None


class MatchEvaluator:
    """Have the playing model critique its own just-finished game."""

    def __init__(
        self,
        client: Any,
        out_path: Optional[Path] = None,
        *,
        model_label: str = "",
    ) -> None:
        """
        Args:
            client: the LLM client the harness already built for the autopilot
                (e.g. a :class:`ProxyBackend`). Must expose
                ``complete(system_prompt, user_message, max_tokens=..., temperature=...)``.
            out_path: JSONL file to append evaluations to.
            model_label: human label for the model (for the ``model`` field).
        """
        self.client = client
        self.out_path = Path(out_path) if out_path else DEFAULT_EVAL_PATH
        self.model_label = model_label or getattr(client, "model", "") or ""
        try:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # pragma: no cover - filesystem edge case
            logger.warning("Could not create eval dir %s: %s", self.out_path.parent, e)

    def evaluate(
        self,
        match_id: str,
        decisions: List[Dict[str, Any]],
        result: Optional[str],
        winner: Optional[str],
        deck_name: Optional[str] = None,
    ) -> Optional[dict]:
        """Critique one match and append a JSONL record. Never raises.

        Returns the written record dict, or ``None`` on any failure / no data.
        """
        try:
            n_decisions = len(decisions or [])
            if n_decisions == 0:
                logger.info("Match eval skipped: no decisions for match %s", match_id)
                return None

            user_message = (
                f"DECK: {deck_name or 'unknown'}\n"
                f"RESULT: {result or 'unknown'} "
                f"(bot {'WON' if winner == 'local' else 'LOST' if winner else 'DREW/UNKNOWN'})\n"
                f"DECISIONS ({n_decisions} total, the bot's own plays in order):\n"
                f"{_summarize_decisions(decisions)}\n\n"
                "Review the bot's play above and return the STRICT JSON critique."
            )

            raw = ""
            try:
                raw = self.client.complete(
                    EVAL_SYSTEM_PROMPT,
                    user_message,
                    max_tokens=600,
                    temperature=0.2,
                )
            except Exception as e:
                logger.warning("Match eval LLM call failed for %s: %s", match_id, e)
                return None

            parsed = _extract_json(raw or "")

            record: Dict[str, Any] = {
                "match_id": match_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "result": result,
                "winner": winner,
                "deck_name": deck_name,
                "n_decisions": n_decisions,
                "model": self.model_label,
            }
            if parsed is not None:
                record["summary"] = parsed.get("summary", "")
                km = parsed.get("key_mistakes", [])
                imp = parsed.get("improvements", [])
                record["key_mistakes"] = km if isinstance(km, list) else [str(km)]
                record["improvements"] = imp if isinstance(imp, list) else [str(imp)]
                record["self_score"] = parsed.get("self_score")
            else:
                # Unparseable — keep the raw text so it isn't lost for training.
                record["summary"] = ""
                record["key_mistakes"] = []
                record["improvements"] = []
                record["self_score"] = None
                record["raw"] = (raw or "")[:4000]

            try:
                with open(self.out_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning("Could not write match eval for %s: %s", match_id, e)
                return None

            return record
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Match eval failed for %s (ignored): %s", match_id, e)
            return None
