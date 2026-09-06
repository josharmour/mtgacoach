#!/usr/bin/env python3
"""Fail-closed holdout-deck validator for MageZero run configs.

THE CONTRACT (rl-pipeline-fix.md MORNING PRIORITY, 2026-07-30): the unseen-deck
gate slice is only honest if its decks appear in NO training corpus, ever. The
permanent holdout decks are:

    HighNoonControl, BGRoots, BWBats

This script FAILS (exit 2) if any run config lists a holdout deck as the
primary ``deck:`` or as any ``opponents[].deck``. It fails CLOSED: a config it
cannot read, cannot parse, or whose deck fields it cannot positively identify
is a violation, never a skip — a typo'd config must not slip a holdout deck
into training because the validator shrugged.

Matching is normalized (case-insensitive, whitespace-stripped, optional
``.dck`` suffix ignored) so ``highnooncontrol.dck`` cannot dodge the check.

Usage:
    validate_run_config.py <config.yml> [<config2.yml> ...]
    validate_run_config.py --configs-dir /home/joshu/repos/magezero/configs

With no arguments it validates the tracked configs in this directory
(``next_run_diverse.yml``, ``run_baseline.yml``).

Exit codes: 0 = all configs clean; 2 = violation or unreadable/ambiguous
config (fail closed).

Wired as a test in tests/test_validate_run_config.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# The permanent holdout set. Extend only by owner decision, and only ever GROW
# it — removing a deck from here invalidates every unseen-deck gate number
# measured while it was held out.
HOLDOUT_DECKS = frozenset({"HighNoonControl", "BGRoots", "BWBats"})

_WP3_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIGS = [
    _WP3_DIR / "next_run_diverse.yml",
    _WP3_DIR / "run_baseline.yml",
]

# Config filenames that are not run configs (no deck/opponents keys) and are
# therefore out of scope when scanning a --configs-dir wholesale.
NON_RUN_CONFIG_NAMES = frozenset({"curriculum.yml", "game.yml", "game_smoke.yml"})


def _norm(name: object) -> str:
    """Normalize a deck name for holdout comparison."""
    s = str(name).strip().lower()
    if s.endswith(".dck"):
        s = s[: -len(".dck")]
    return s


_HOLDOUT_NORM = {_norm(d): d for d in HOLDOUT_DECKS}


def validate_config(path: Path) -> list[str]:
    """Return a list of violation strings for one config file (empty = clean).

    Fail closed: any inability to positively determine the deck fields is
    itself a violation.
    """
    violations: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"{path}: unreadable ({e}) — fail closed"]
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"{path}: YAML parse error ({e}) — fail closed"]

    if not isinstance(cfg, dict):
        return [f"{path}: top level is {type(cfg).__name__}, expected mapping — fail closed"]

    # Primary deck — must exist and must not be a holdout.
    primary = cfg.get("deck")
    if primary is None:
        violations.append(f"{path}: missing 'deck' key — cannot prove primary is not a holdout — fail closed")
    elif _norm(primary) in _HOLDOUT_NORM:
        violations.append(
            f"{path}: primary deck {primary!r} is permanent holdout "
            f"{_HOLDOUT_NORM[_norm(primary)]!r} — holdouts must appear in NO training config"
        )

    # Opponents — the list must be well-formed and each entry checkable.
    opponents = cfg.get("opponents")
    if opponents is None:
        violations.append(f"{path}: missing 'opponents' key — fail closed")
    elif not isinstance(opponents, list) or not opponents:
        violations.append(f"{path}: 'opponents' is not a non-empty list — fail closed")
    else:
        for i, opp in enumerate(opponents):
            if not isinstance(opp, dict) or "deck" not in opp:
                violations.append(f"{path}: opponents[{i}] has no 'deck' field — fail closed")
                continue
            name = opp["deck"]
            if _norm(name) in _HOLDOUT_NORM:
                violations.append(
                    f"{path}: opponents[{i}] deck {name!r} is permanent holdout "
                    f"{_HOLDOUT_NORM[_norm(name)]!r} — holdouts must appear in NO training config"
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("configs", nargs="*", type=Path, help="run config YAML files to validate")
    ap.add_argument(
        "--configs-dir",
        type=Path,
        default=None,
        help="also validate every *.yml in this directory (known non-run configs skipped by name)",
    )
    args = ap.parse_args(argv)

    targets: list[Path] = list(args.configs)
    if args.configs_dir is not None:
        if not args.configs_dir.is_dir():
            print(f"FAIL: --configs-dir {args.configs_dir} is not a directory — fail closed")
            return 2
        targets.extend(
            p
            for p in sorted(args.configs_dir.glob("*.yml"))
            if p.name not in NON_RUN_CONFIG_NAMES and not p.name.startswith("run.yml.bak")
        )
    if not targets:
        targets = list(DEFAULT_CONFIGS)

    all_violations: list[str] = []
    for path in targets:
        vs = validate_config(path)
        all_violations.extend(vs)
        status = "FAIL" if vs else "ok"
        print(f"[{status}] {path}")
        for v in vs:
            print(f"    {v}")

    if all_violations:
        print(f"\nFAIL: {len(all_violations)} violation(s) across {len(targets)} config(s).")
        print("Permanent holdout decks (unseen-deck gate contract): " + ", ".join(sorted(HOLDOUT_DECKS)))
        return 2
    print(f"\nOK: {len(targets)} config(s) clean of holdout decks ({', '.join(sorted(HOLDOUT_DECKS))}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
