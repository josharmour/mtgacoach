"""Evaluation scenario definitions for model benchmarking.

Each scenario represents a game state snapshot with known-correct advice,
allowing automated quality scoring across models.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCENARIOS_DIR = Path(__file__).parent / "scenario_data"


@dataclass
class EvalScenario:
    """A single evaluation scenario with game state and expected behavior.

    Attributes:
        id: Unique identifier (e.g., "lethal_on_board_01")
        name: Human-readable name
        category: Scenario category for grouping (e.g., "combat", "sequencing", "removal")
        difficulty: Difficulty tier: "basic", "intermediate", "advanced"
        game_state: Dict matching the format from get_game_state() MCP tool
        trigger: The coaching trigger type (e.g., "new_turn", "combat_attackers")
        correct_actions: List of acceptable correct actions (any match = correct).
            Matched case-insensitively against the model response.
        incorrect_actions: Actions that are definitively wrong (presence = penalty).
            Matched case-insensitively against the model response.
        must_mention: Keywords/phrases the response MUST contain for full credit.
            E.g., card names, key mechanics.
        must_not_mention: Keywords/phrases that should NOT appear (hallucinated cards, etc.)
        reasoning_notes: Human explanation of why the correct action is right.
            Not used in scoring — for documentation only.
        tags: Freeform tags for filtering (e.g., ["mana_efficiency", "aggro"])
        weight: Scenario importance weight for aggregate scoring (default 1.0)
    """

    id: str
    name: str
    category: str
    difficulty: str
    game_state: dict
    trigger: str
    correct_actions: list[str]
    incorrect_actions: list[str] = field(default_factory=list)
    must_mention: list[str] = field(default_factory=list)
    must_not_mention: list[str] = field(default_factory=list)
    reasoning_notes: str = ""
    tags: list[str] = field(default_factory=list)
    weight: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EvalScenario":
        # Handle forward-compat: ignore unknown keys
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


def load_scenarios(
    path: Optional[Path] = None,
    categories: Optional[list[str]] = None,
    difficulties: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> list[EvalScenario]:
    """Load evaluation scenarios from a JSON file.

    Args:
        path: Path to scenarios JSON. Defaults to built-in scenario_data/scenarios.json.
        categories: Filter to these categories only.
        difficulties: Filter to these difficulty levels only.
        tags: Filter to scenarios that have ANY of these tags.

    Returns:
        List of EvalScenario objects.
    """
    if path is None:
        path = SCENARIOS_DIR / "scenarios.json"

    if not path.exists():
        logger.warning(f"Scenarios file not found: {path}")
        return []

    with open(path) as f:
        data = json.load(f)

    scenarios_data = data if isinstance(data, list) else data.get("scenarios", [])
    scenarios = [EvalScenario.from_dict(s) for s in scenarios_data]

    # Apply filters
    if categories:
        cat_set = {c.lower() for c in categories}
        scenarios = [s for s in scenarios if s.category.lower() in cat_set]

    if difficulties:
        diff_set = {d.lower() for d in difficulties}
        scenarios = [s for s in scenarios if s.difficulty.lower() in diff_set]

    if tags:
        tag_set = {t.lower() for t in tags}
        scenarios = [
            s for s in scenarios if tag_set & {t.lower() for t in s.tags}
        ]

    return scenarios


def save_scenarios(scenarios: list[EvalScenario], path: Path) -> None:
    """Save evaluation scenarios to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            {"scenarios": [s.to_dict() for s in scenarios]},
            f,
            indent=2,
        )
    logger.info(f"Saved {len(scenarios)} scenarios to {path}")
