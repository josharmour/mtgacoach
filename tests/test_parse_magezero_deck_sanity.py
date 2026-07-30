"""Deck-sanity guardrail: assert battlefield_self / battlefield_opp correctness.

Each assertion runs over the real mz_train_smoke.log (591 games, 9,789 decisions)
to catch board-assignment regressions.

Check 1: battlefield_self never holds an opponent-exclusive basic.
         UWTempo plays Island + Plains; opponent-exclusive = {Mountain, Forest, Swamp}.

Check 2: battlefield_opp never holds UW-only nonbasic lands on mono-colour opponents.
         UW dual lands {Adarkar Wastes, Seachrome Coast, Meticulous Archive,
         Floodfarm Verge} are unique to the UW Tempo shell.
         Soulstone Sanctuary is excluded: it is a colourless land that appears
         legitimately in several opponent decklists.

Check 3: no non-basic card name appears in BOTH boards of the same row.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent
PARSER = REPO / "tools" / "training" / "parse_magezero_log.py"
SMOKE_LOG = Path("/Users/joshu/wp3/evidence/mz_train_smoke.log")

# UWTempo-specific nonbasic lands — never played by mono-colour opponents.
# Soulstone Sanctuary is excluded: it is a colourless land that appears
# legitimately in several opponent decklists.
UW_ONLY_NONBASICS = frozenset({
    "Adarkar Wastes",
    "Seachrome Coast",
    "Meticulous Archive",
    "Floodfarm Verge",
})

# Opponent-exclusive basics — basics owned by the opponent mono deck that
# the UWTempo deck never plays.  UWTempo has Island + Plains; everything
# else is opponent-exclusive.
OPP_EXCLUSIVE_BASICS = frozenset({"Mountain", "Forest", "Swamp"})

BASICS = frozenset({"Plains", "Island", "Swamp", "Mountain", "Forest"})


def _parse_smoke_log() -> list[dict]:
    """Parse the real smoke log and return decisions."""
    if not SMOKE_LOG.exists():
        raise FileNotFoundError(f"Smoke log not found: {SMOKE_LOG}")

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        out_path = tmp.name

    result = subprocess.run(
        [
            sys.executable, str(PARSER),
            "--log", str(SMOKE_LOG),
            "--out", out_path,
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Parser failed (exit {result.returncode}):\n"
            f"{result.stderr[:2000]}"
        )

    decisions: list[dict] = []
    with open(out_path) as f:
        for line in f:
            line = line.strip()
            if line:
                decisions.append(json.loads(line))
    Path(out_path).unlink()
    return decisions


def _session_opponent_colour(session: str) -> str | None:
    """Return the opponent mono colour from a session name, or None."""
    if "vs_Standard-Mono" not in session:
        return None
    return session.split("Standard-Mono")[1][0]  # R, G, B, W, or U


# ── Module-level decisions fixture (parse once per session) ──────────────

_decisions: list[dict] | None = None


def _get_decisions() -> list[dict]:
    global _decisions
    if _decisions is None:
        _decisions = _parse_smoke_log()
    return _decisions


# ── Tests ────────────────────────────────────────────────────────────────


def test_battlefield_self_no_opp_exclusive_basics() -> None:
    """Check 1: battlefield_self never holds an opponent-exclusive basic."""
    decisions = _get_decisions()
    bad: list[str] = []
    for d in decisions:
        bf = [p["name"] for p in (d.get("battlefield_self") or [])]
        found = {c for c in bf if c in OPP_EXCLUSIVE_BASICS}
        if found:
            bad.append(f"  {d.get('session', '?')} gid={d.get('game_id', '?')} basics={found}")
    assert not bad, (
        "battlefield_self has opponent-exclusive basics:\n"
        + "\n".join(bad[:10])
    )


def test_battlefield_opp_no_uw_nonbasics() -> None:
    """Check 2: battlefield_opp never holds UW-only nonbasics on mono opponents."""
    decisions = _get_decisions()
    bad: list[str] = []
    for d in decisions:
        session = d.get("session", "")
        opp_colour = _session_opponent_colour(session)
        if opp_colour is None or opp_colour == "U":
            continue  # MonoU shares Island with UWTempo, so skip
        bf = [p["name"] for p in (d.get("battlefield_opp") or [])]
        found = [c for c in bf if c in UW_ONLY_NONBASICS]
        if found:
            bad.append(f"  {session} gid={d.get('game_id', '?')} cards={found}")
    assert not bad, (
        "battlefield_opp has UW-only nonbasics:\n"
        + "\n".join(bad[:10])
    )


def test_no_card_in_both_boards() -> None:
    """Check 3: no non-basic card name appears in BOTH boards of the same row."""
    decisions = _get_decisions()
    bad: list[str] = []
    for d in decisions:
        self_set = {p["name"] for p in (d.get("battlefield_self") or [])}
        opp_set = {p["name"] for p in (d.get("battlefield_opp") or [])}
        shared = (self_set & opp_set) - BASICS
        if shared:
            bad.append(f"  {d.get('session', '?')} gid={d.get('game_id', '?')} shared={shared}")
    assert not bad, (
        "Same non-basic card in both boards:\n"
        + "\n".join(bad[:10])
    )
