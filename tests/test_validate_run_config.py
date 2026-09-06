"""Tests for tools/training/wp3/validate_run_config.py — the permanent-holdout
contract that keeps the unseen-deck gate slice honest.

The contract: HighNoonControl, BGRoots, BWBats appear in NO training config,
ever — not as primary, not as opponent. The validator must fail CLOSED on
configs it cannot positively clear.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

pytest.importorskip("yaml", reason="pyyaml required for run-config validation")

from tools.training.wp3 import validate_run_config as V  # noqa: E402

WP3 = REPO / "tools" / "training" / "wp3"

CLEAN_CONFIG = """\
deck: UWTempo
opponents:
  - deck: Standard-MonoR
    mode: minimax
  - deck: Oathbreaker_UR
    mode: minimax
"""


def _write(tmp_path: Path, text: str, name: str = "cfg.yml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The contract itself
# ---------------------------------------------------------------------------


def test_holdout_set_is_exactly_the_three_pinned_decks():
    # Growing this set is an owner decision; shrinking it invalidates every
    # unseen-deck gate number measured while the deck was held out.
    assert {"HighNoonControl", "BGRoots", "BWBats"} == V.HOLDOUT_DECKS


def test_clean_config_passes(tmp_path):
    p = _write(tmp_path, CLEAN_CONFIG)
    assert V.validate_config(p) == []


@pytest.mark.parametrize("holdout", sorted(V.HOLDOUT_DECKS))
def test_holdout_as_opponent_fails(tmp_path, holdout):
    p = _write(
        tmp_path,
        f"deck: UWTempo\nopponents:\n  - deck: {holdout}\n    mode: minimax\n",
    )
    violations = V.validate_config(p)
    assert violations, f"{holdout} as opponent must be a violation"
    assert holdout in "\n".join(violations)


@pytest.mark.parametrize("holdout", sorted(V.HOLDOUT_DECKS))
def test_holdout_as_primary_fails(tmp_path, holdout):
    p = _write(
        tmp_path,
        f"deck: {holdout}\nopponents:\n  - deck: Standard-MonoR\n    mode: minimax\n",
    )
    violations = V.validate_config(p)
    assert violations, f"{holdout} as primary must be a violation"


def test_holdout_matching_is_normalized(tmp_path):
    # Case / whitespace / .dck-suffix variants must not dodge the check.
    for variant in ("highnooncontrol", "HIGHNOONCONTROL", " HighNoonControl ", "HighNoonControl.dck"):
        p = _write(tmp_path, f"deck: UWTempo\nopponents:\n  - deck: '{variant}'\n")
        assert V.validate_config(p), f"variant {variant!r} must still be caught"


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


def test_missing_file_fails_closed(tmp_path):
    assert V.validate_config(tmp_path / "does_not_exist.yml")


def test_unparseable_yaml_fails_closed(tmp_path):
    p = _write(tmp_path, "deck: [unclosed\n")
    assert V.validate_config(p)


def test_missing_deck_key_fails_closed(tmp_path):
    p = _write(tmp_path, "opponents:\n  - deck: Standard-MonoR\n")
    assert V.validate_config(p)


def test_missing_opponents_fails_closed(tmp_path):
    p = _write(tmp_path, "deck: UWTempo\n")
    assert V.validate_config(p)


def test_opponent_entry_without_deck_field_fails_closed(tmp_path):
    p = _write(tmp_path, "deck: UWTempo\nopponents:\n  - mode: minimax\n")
    assert V.validate_config(p)


def test_non_mapping_top_level_fails_closed(tmp_path):
    p = _write(tmp_path, "- just\n- a\n- list\n")
    assert V.validate_config(p)


# ---------------------------------------------------------------------------
# CLI exit codes + the tracked configs stay clean
# ---------------------------------------------------------------------------


def test_main_exit_2_on_violation(tmp_path, capsys):
    p = _write(tmp_path, "deck: BGRoots\nopponents:\n  - deck: BWBats\n")
    assert V.main([str(p)]) == 2
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_main_exit_0_on_clean(tmp_path, capsys):
    p = _write(tmp_path, CLEAN_CONFIG)
    assert V.main([str(p)]) == 0


def test_tracked_next_run_diverse_config_is_clean():
    # THE deliverable config for the next curriculum run must never regress.
    cfg = WP3 / "next_run_diverse.yml"
    assert cfg.is_file(), "tracked next-run config missing"
    assert V.validate_config(cfg) == []


def test_default_targets_all_clean():
    # No-args invocation validates every tracked run config in wp3/.
    assert V.main([]) == 0


def test_next_run_diverse_has_438_opponents_and_scale():
    import yaml

    cfg = yaml.safe_load((WP3 / "next_run_diverse.yml").read_text(encoding="utf-8"))
    opponent_decks = {o["deck"] for o in cfg["opponents"]}
    # The #438 greedy-optimal five, exact .dck basenames per deck_inventory.json.
    assert {
        "Oathbreaker_UR",
        "GBLegends",
        "EVG_Elves",
        "EVG_Goblins",
        "Mind(MindvsMight)",
    } <= opponent_decks
    assert cfg["games_per_gen"] == 550
    assert cfg["generations"] == 6
