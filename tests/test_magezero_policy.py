"""Unit tests for MageZero policy prior mapping and normalization."""

from __future__ import annotations

import math

from arenamcp.magezero_policy import (
    PINNED,
    action_index,
    compute_legal_action_priors,
    java_string_hash_code,
    map_action_to_xmage_text,
)


def test_pinned_indices():
    assert action_index("Pass") == 0
    assert action_index("{T}: Add {B}.") == 1
    assert action_index("{T}: Add {G}.") == 2
    assert action_index("{T}: Add {R}.") == 3
    assert action_index("{T}: Add {U}.") == 4
    assert action_index("{T}: Add {W}.") == 5
    assert action_index("{T}: Add {C}.") == 6


def test_action_collision():
    # As identified in review: Cast Skrelv and Cast Negate share slot 103
    assert action_index("Cast Skrelv, Defector Mite") == 103
    assert action_index("Cast Negate") == 103


def test_map_action_to_xmage_text():
    assert map_action_to_xmage_text("Pass Priority", "pass") == "Pass"
    assert map_action_to_xmage_text("Play Land: Island", "land") == "Play Island"
    assert (
        map_action_to_xmage_text("Cast: Malcolm, Alluring Scoundrel [Cost: {1}{U}]", "cast")
        == "Cast Malcolm, Alluring Scoundrel"
    )


def test_compute_legal_action_priors_normalization():
    candidates = [
        ("pass", "Pass"),
        ("land", "Play Island"),
        ("cast", "Cast Malcolm, Alluring Scoundrel"),
    ]
    # Synthetic logits
    logits = [0.0] * 128
    logits[action_index("Pass")] = 1.0
    logits[action_index("Play Island")] = 2.0
    logits[action_index("Cast Malcolm, Alluring Scoundrel")] = 3.0

    priors = compute_legal_action_priors(candidates, logits, temperature=1.5)
    assert len(priors) == 3
    assert abs(sum(priors.values()) - 1.0) < 0.01

    # Malcolm is a non-mana, non-pass spell so it gets the exploration bonus and highest prior
    assert priors["cast"] > priors["land"] > priors["pass"]
