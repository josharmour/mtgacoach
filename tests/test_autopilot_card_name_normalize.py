"""Regression test for the activate_ability "Ability:" prefix bug (issue #186).

The bridge type+name match path used strict lowercase equality between
the planner's card_name and the bridge's grpId-resolved card name. When
the LLM kept the leading legal-action label ("Ability: Promising Vein")
instead of stripping it to just "Promising Vein", the equality failed,
no bridge action matched, and autopilot flipped to MANUAL REQUIRED.

These tests pin the normalization helper plus the looser comparison.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock


def _load_autopilot():
    try:
        return importlib.import_module("arenamcp.autopilot")
    except ImportError as e:
        if "PIL" in str(e):
            sys.modules["PIL"] = MagicMock()
            sys.modules["PIL.ImageGrab"] = MagicMock()
            return importlib.import_module("arenamcp.autopilot")
        raise


def test_strips_ability_prefix():
    autopilot = _load_autopilot()
    assert autopilot._normalize_planner_card_name("Ability: Promising Vein") == "Promising Vein"


def test_strips_activate_ability_prefix():
    autopilot = _load_autopilot()
    assert autopilot._normalize_planner_card_name("Activate Ability: Promising Vein") == "Promising Vein"


def test_strips_play_land_prefix():
    autopilot = _load_autopilot()
    assert autopilot._normalize_planner_card_name("Play Land: Forest") == "Forest"


def test_strips_cast_prefix():
    autopilot = _load_autopilot()
    assert autopilot._normalize_planner_card_name("Cast: Lightning Bolt") == "Lightning Bolt"


def test_strip_is_case_insensitive():
    autopilot = _load_autopilot()
    assert autopilot._normalize_planner_card_name("ABILITY: Promising Vein") == "Promising Vein"
    assert autopilot._normalize_planner_card_name("ability: promising vein") == "promising vein"


def test_clean_name_passes_through():
    autopilot = _load_autopilot()
    # No prefix → returned trimmed but otherwise unchanged. Idempotent.
    assert autopilot._normalize_planner_card_name("Promising Vein") == "Promising Vein"
    assert autopilot._normalize_planner_card_name("  Lightning Bolt  ") == "Lightning Bolt"


def test_empty_passes_through():
    autopilot = _load_autopilot()
    assert autopilot._normalize_planner_card_name("") == ""
    assert (
        autopilot._normalize_planner_card_name(None) is None
        or autopilot._normalize_planner_card_name(None) == ""
    )


def test_only_strips_one_prefix():
    """Pathological input — strip only the outermost prefix, leave inner names alone."""
    autopilot = _load_autopilot()
    # If a card were literally named "Ability: Foo", we'd strip the prefix
    # once. That's accepted — the alternative (greedy strip) breaks names
    # that legitimately start with "Cast" / "Play" etc.
    assert autopilot._normalize_planner_card_name("Ability: Ability: Foo") == "Ability: Foo"
