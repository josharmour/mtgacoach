"""Guardrail: deck-membership sanity checks over the real MageZero log.

Asserts three invariants on battlefield_self and battlefield_opp for every
parsed decision row.  Designed to catch regressions in the permanents-line
assignment logic (esp. the 1-line "opponent board is empty" case).

Uses the real 201 MB smoke log at /Users/joshu/wp3/evidence/mz_train_smoke.log
and the deck files under /Volumes/repos/magezero/xmage/decks/.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "tools" / "training"))

from parse_magezero_log import parse_log  # noqa: E402

import pytest

LOG = Path("/Users/joshu/wp3/evidence/mz_train_smoke.log")
DECKS_DIR = Path("/Volumes/repos/magezero/xmage/decks")

# ── Deck card-set parser ────────────────────────────────────────────
RE_DCK = re.compile(r"^\d+\s+\[[^\]]*\]\s+(.+)$")
SESSION_OPP_RE = re.compile(r"session\d+_UWTempo_vs_Standard-Mono([RGBWU])")


def _parse_dck(path: Path) -> set[str]:
    cards: set[str] = set()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_DCK.match(line)
            if m:
                cards.add(m.group(1).strip())
    return cards


def _load_decks() -> tuple[set[str], dict[str, set[str]]]:
    """Return (uwtempo_cards, monster_cards) where monster_cards[name] = card_set."""
    dck_files: dict[str, set[str]] = {}
    for p in sorted(DECKS_DIR.glob("*.dck")):
        dck_files[p.stem] = _parse_dck(p)

    uwtempo = dck_files.get("UWTempo", set())

    mono = {}
    for name in ["Standard-MonoR", "Standard-MonoG", "Standard-MonoB",
                  "Standard-MonoW", "Standard-MonoU"]:
        cards = dck_files.get(name, set())
        if cards:
            mono[name] = cards

    return uwtempo, mono


@pytest.fixture(scope="module")
def parsed_data():
    decisions, sessions = parse_log(str(LOG))
    return decisions, sessions


@pytest.fixture(scope="module")
def deck_data():
    return _load_decks()


def _card_name(p) -> str:
    if isinstance(p, dict):
        return p.get("name", "")
    return str(p)


# ════════════════════════════════════════════════════════════════════
# Deck-sanity assertions
# ════════════════════════════════════════════════════════════════════

class TestDeckSanity:
    """Three invariants every parsed decision row must satisfy."""

    def test_all_zero_violations(self, parsed_data, deck_data):
        """Assert all three counts are zero.

        This is the summary test — it computes all three counts and asserts
        them all at once so we see the complete picture in a single failure.
        """
        decisions, sessions = parsed_data
        uwtempo_cards, mono_decks = deck_data

        # Precompute card-sets per opponent
        opp_only: dict[str, set[str]] = {}
        uw_only: dict[str, set[str]] = {}
        shared_cards: dict[str, set[str]] = {}

        for name, cards in mono_decks.items():
            opp_only[name] = cards - uwtempo_cards
            uw_only[name] = uwtempo_cards - cards
            shared_cards[name] = uwtempo_cards & cards

        v_self_opp_only = 0   # assertion 1
        v_opp_uw_only = 0     # assertion 2
        v_dual_board = 0      # assertion 3

        for d in decisions:
            sm = SESSION_OPP_RE.match(d.get("session", ""))
            if not sm:
                continue
            color_key = f"Standard-Mono{sm.group(1)}"

            bself_names = {_card_name(p) for p in d.get("battlefield_self", []) if _card_name(p)}
            bopp_names = {_card_name(p) for p in d.get("battlefield_opp", []) if _card_name(p)}

            # Assertion 1: self board never has opponent-only cards
            if bself_names & opp_only.get(color_key, set()):
                v_self_opp_only += 1

            # Assertion 2: opp board never has UWTempo-only cards
            if bopp_names & uw_only.get(color_key, set()):
                v_opp_uw_only += 1

            # Assertion 3: no card on both boards unless both decks run it
            both = bself_names & bopp_names
            both -= shared_cards.get(color_key, set())
            if both:
                v_dual_board += 1

        # Report all three
        msg = (
            f"\nAssertion 1 (self → opp-only cards): {v_self_opp_only}\n"
            f"Assertion 2 (opp → UW-only cards):    {v_opp_uw_only}\n"
            f"Assertion 3 (both boards same card):  {v_dual_board}"
        )
        assert v_self_opp_only == 0 and v_opp_uw_only == 0 and v_dual_board == 0, msg

    def test_total_decisions(self, parsed_data):
        """Sanity check — make sure we parsed the expected number of rows."""
        decisions, _ = parsed_data
        # 10 sessions × 6 threads, total parsed rows is stable at 9789
        assert len(decisions) == 9789, f"Expected 9789, got {len(decisions)}"

    def test_empty_opp_board(self, parsed_data):
        """At least some rows have an empty opponent board (1-line case)."""
        decisions, _ = parsed_data
        empty_opp = sum(
            1 for d in decisions
            if d.get("battlefield_opp") is not None and len(d["battlefield_opp"]) == 0
        )
        assert empty_opp > 0, "Expected at least one row with empty opponent board"
