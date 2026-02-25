"""Quality scoring metrics for model benchmark responses.

Scores each response on multiple dimensions and produces a composite
quality score for comparison across models.
"""

import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

from arenamcp.model_benchmark.scenarios import EvalScenario

logger = logging.getLogger(__name__)


@dataclass
class QualityScore:
    """Multi-dimensional quality score for a single model response.

    All component scores are 0.0-1.0. The composite score is a weighted average.
    """

    # Did the model recommend one of the correct actions?
    action_correctness: float = 0.0

    # Did the model avoid recommending incorrect/illegal actions?
    rule_compliance: float = 1.0

    # Did the model mention required keywords (card names, mechanics)?
    completeness: float = 0.0

    # Did the model avoid hallucinated/forbidden content?
    hallucination_penalty: float = 0.0

    # Response brevity: penalize excessively long or short responses
    conciseness: float = 0.0

    # Composite weighted score (computed)
    composite: float = 0.0

    # Per-check details for debugging
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# Default weights for composite score computation
DEFAULT_WEIGHTS = {
    "action_correctness": 0.40,
    "rule_compliance": 0.25,
    "completeness": 0.15,
    "hallucination_penalty": 0.10,
    "conciseness": 0.10,
}


def _normalize_text(text: str) -> str:
    """Lowercase and strip extra whitespace for matching."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _phrase_in_text(phrase: str, text: str) -> bool:
    """Check if a phrase appears in text (case-insensitive, whitespace-normalized)."""
    return _normalize_text(phrase) in _normalize_text(text)


def score_response(
    scenario: EvalScenario,
    response: str,
    weights: Optional[dict[str, float]] = None,
) -> QualityScore:
    """Score a model's response against a scenario's expected behavior.

    Args:
        scenario: The evaluation scenario with correct/incorrect actions.
        response: The raw text response from the model.
        weights: Optional custom weights for composite calculation.

    Returns:
        QualityScore with all dimensions filled in.
    """
    w = weights or DEFAULT_WEIGHTS
    score = QualityScore()
    details: dict = {}

    if not response or not response.strip():
        score.details = {"error": "empty_response"}
        return score

    resp_norm = _normalize_text(response)

    # --- Action correctness ---
    # Check if ANY correct action phrase appears in the response
    matched_correct = []
    for action in scenario.correct_actions:
        if _phrase_in_text(action, response):
            matched_correct.append(action)

    if matched_correct:
        score.action_correctness = 1.0
    elif scenario.correct_actions:
        # Partial credit: check if the response mentions key card names
        # from correct actions (e.g., says "Lightning Bolt" even if not
        # the exact phrasing "Cast Lightning Bolt")
        partial_matches = 0
        for action in scenario.correct_actions:
            # Extract card-name-like tokens (capitalized multi-word phrases)
            words = action.split()
            # Skip action verbs to get the card/target name
            name_words = [w for w in words if w[0:1].isupper() and w.lower() not in {
                "cast", "play", "attack", "block", "activate", "pass", "keep",
                "bottom", "mulligan", "with", "the", "your", "all",
            }] if words else []
            name_phrase = " ".join(name_words).lower()
            if name_phrase and name_phrase in resp_norm:
                partial_matches += 1

        if partial_matches > 0:
            score.action_correctness = 0.5
    details["matched_correct"] = matched_correct

    # --- Rule compliance ---
    # Check if any incorrect/illegal actions appear
    matched_incorrect = []
    for action in scenario.incorrect_actions:
        if _phrase_in_text(action, response):
            matched_incorrect.append(action)

    if matched_incorrect:
        # Scale penalty by number of violations
        penalty = min(1.0, len(matched_incorrect) * 0.5)
        score.rule_compliance = max(0.0, 1.0 - penalty)
    details["matched_incorrect"] = matched_incorrect

    # --- Completeness ---
    # Check required mentions
    if scenario.must_mention:
        found = [kw for kw in scenario.must_mention if _phrase_in_text(kw, response)]
        score.completeness = len(found) / len(scenario.must_mention)
        details["must_mention_found"] = found
        details["must_mention_missing"] = [
            kw for kw in scenario.must_mention if kw not in found
        ]
    else:
        # No requirements — full marks
        score.completeness = 1.0

    # --- Hallucination penalty ---
    # Check for forbidden content
    if scenario.must_not_mention:
        violations = [
            kw for kw in scenario.must_not_mention if _phrase_in_text(kw, response)
        ]
        if violations:
            penalty = min(1.0, len(violations) * 0.5)
            score.hallucination_penalty = penalty
        details["hallucination_violations"] = violations
    # hallucination_penalty is inverted: 0 = no hallucinations (good), 1 = severe

    # --- Conciseness ---
    word_count = len(response.split())
    # Ideal range: 10-60 words for concise coaching advice
    if 10 <= word_count <= 60:
        score.conciseness = 1.0
    elif word_count < 10:
        score.conciseness = max(0.2, word_count / 10)
    else:
        # Gradual penalty above 60 words, flooring at 0.2
        score.conciseness = max(0.2, 1.0 - (word_count - 60) / 200)
    details["word_count"] = word_count

    # --- Composite ---
    # hallucination_penalty is a cost, so we invert it for the composite
    hallucination_quality = 1.0 - score.hallucination_penalty

    score.composite = (
        w.get("action_correctness", 0.4) * score.action_correctness
        + w.get("rule_compliance", 0.25) * score.rule_compliance
        + w.get("completeness", 0.15) * score.completeness
        + w.get("hallucination_penalty", 0.10) * hallucination_quality
        + w.get("conciseness", 0.10) * score.conciseness
    )

    score.details = details
    return score
