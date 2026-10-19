"""Regression test for the activate_ability "Ability:" prefix bug (issue #186).

The bridge type+name match path used strict lowercase equality between
the planner's card_name and the bridge's grpId-resolved card name. When
the LLM kept the leading legal-action label ("Ability: Promising Vein")
instead of stripping it to just "Promising Vein", the equality failed,
no bridge action matched, and autopilot flipped to MANUAL REQUIRED.

This pins the normalization helper the looser comparison relies on.
"""

from __future__ import annotations

import pytest

from arenamcp.autopilot import _normalize_planner_card_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ability: Promising Vein", "Promising Vein"),
        ("Activate Ability: Promising Vein", "Promising Vein"),
        ("Play Land: Forest", "Forest"),
        ("Cast: Lightning Bolt", "Lightning Bolt"),
        # Case-insensitive prefix; the name itself keeps its case.
        ("ABILITY: Promising Vein", "Promising Vein"),
        ("ability: promising vein", "promising vein"),
        # No prefix: trimmed but otherwise unchanged (idempotent).
        ("Promising Vein", "Promising Vein"),
        ("  Lightning Bolt  ", "Lightning Bolt"),
        ("", ""),
        (None, None),
        # Only the outermost prefix goes: a greedy strip would break names
        # that legitimately start with "Cast" / "Play" etc.
        ("Ability: Ability: Foo", "Ability: Foo"),
    ],
)
def test_normalize_planner_card_name(raw, expected):
    assert _normalize_planner_card_name(raw) == expected
