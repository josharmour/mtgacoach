#!/usr/bin/env python3
"""Refuse to emit a play-decision corpus built against a half-loaded card DB.

Why this file exists
--------------------
``arenamcp.mtgjson.get_mtgjson()`` loads in a background daemon thread. A
builder that started rendering prompts immediately raced that load, and the
records it produced *looked* perfectly well-formed:

    Mana: 0
    YOUR BOARD:
      Plains
    HAND:
      Sheltered by Ghosts (AURA) {1}{W} [S,NEED:2+{W}]

There is no error in that prompt. It is simply false — the Plains resolved to
a permanent with no type line, ``RulesEngine._get_mana_pool``'s ``"land" in
type_line`` test missed it, so the board produced no mana and every
affordability tag downstream was computed against an empty pool. 20.4% of the
206 on-disk gate test prompts were corrupted this way, undetected, until a
second builder happened to render the same decisions with the database loaded
and the two disagreed.

That is the worst failure mode a *gate* can have: a model fails those prompts
for reasons that have nothing to do with play skill, and the training verdict
becomes unreadable. Three prior runs died of measurement bugs of exactly this
shape.

So the corpus is checked for the bug's fingerprints before it is allowed to be
written, and a corpus carrying any of them is rejected rather than repaired —
a degraded record is not recoverable after the fact, because the card facts it
needed are gone.

What it looks for
-----------------
``unresolved_permanent``   a grpId on the battlefield or in hand that the card
                           database could not resolve at all
``numeric_type_code``      a type line that is MTGA's internal numeric type
                           encoding ("5", "1,2") rather than a type line
``missing_type_line``      a resolved permanent with an empty type line — the
                           direct cause of the 0-mana rendering
``land_yields_no_mana``    untapped lands on your battlefield but a mana pool
                           of 0 — the direct observable consequence

What it deliberately does NOT check
-----------------------------------
The prompt's rendered ``Mana: N`` line is **not** compared against the pool the
``[OK]`` tags were computed from. Those are two different accountings and they
legitimately disagree: ``RulesEngine._get_mana_pool`` counts a colourless land
in ``total`` but in no colour bucket, and misses mana creatures entirely
(its regex looks for ``{T}``/``{C}`` while MTGA's oracle text writes ``{oT}``/
``{oC}``), whereas the coach formatter's line handles the ``{o…}`` style. An
equality check between them fires on 34/206 healthy records. That divergence is
a real inconsistency worth fixing in the coach, but it is a separate defect
from this one, and a validator that cries wolf is how the next silent
corruption gets waved through.

Usage
-----
Wired into ``tools.training.gate_play_decisions build`` (nothing is written if
it fails). Also runnable over already-built corpora::

    python -m tools.training.corpus_selfcheck \\
        tools/training/data/gate_play_decisions_test.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from arenamcp.card_db import is_bogus_type_line, is_hidden_card_id  # noqa: E402

# Key added to every corpus record's meta so an already-written corpus can be
# re-checked without the card database that built it.
AUDIT_KEY = "card_facts_audit"


@dataclass(frozen=True)
class Finding:
    """One reason a record is not trustworthy."""

    record_id: str
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.record_id}: {self.kind} — {self.detail}"


def _cards(game_state: dict) -> Iterable[tuple[str, dict]]:
    for zone in ("battlefield", "hand"):
        for card in game_state.get(zone) or []:
            yield zone, card


def audit_game_state(game_state: dict, *, local_seat: int, mana_total: int) -> dict:
    """Summarise the card facts a decision was built from.

    Returned verbatim into the record's meta so the corpus is self-describing:
    a later reader can verify it without re-resolving 900 grpIds.
    """
    unresolved: list[int] = []
    numeric: list[str] = []
    missing: list[int] = []
    hidden: list[int] = []
    untapped_lands = 0

    for _zone, card in _cards(game_state):
        grp_id = card.get("grp_id")
        type_line = (card.get("type_line") or "").strip()
        if grp_id is None:
            continue
        if is_hidden_card_id(grp_id):
            # A face-down permanent. MTGA does not tell anyone what it is, so
            # "unresolved" is the truthful answer, not a defect. Counted, not
            # flagged — the whole point of this module is to keep "hidden by
            # the rules" and "our card database wasn't loaded" apart.
            hidden.append(int(grp_id))
            continue
        if is_bogus_type_line(type_line):
            numeric.append(f"{grp_id}:{type_line}")
        elif not type_line:
            missing.append(int(grp_id))
        name = card.get("name") or ""
        if isinstance(name, str) and name.startswith("#"):
            unresolved.append(int(grp_id))

    for card in game_state.get("battlefield") or []:
        if card.get("controller_seat_id") != local_seat:
            continue
        if card.get("is_tapped"):
            continue
        if "land" in (card.get("type_line") or "").lower():
            untapped_lands += 1

    return {
        "unresolved_grp_ids": sorted(set(unresolved)),
        "numeric_type_codes": sorted(set(numeric)),
        "missing_type_line_grp_ids": sorted(set(missing)),
        # Face-down / obscured permanents: legitimately unknowable, reported
        # so a reviewer can see they were considered rather than overlooked.
        "hidden_card_grp_ids": sorted(set(hidden)),
        "untapped_local_lands": untapped_lands,
        "mana_total": int(mana_total),
    }


def findings_from_audit(record_id: str, audit: dict) -> list[Finding]:
    """Turn an audit dict into hard findings. Empty list == trustworthy."""
    out: list[Finding] = []

    for grp_id in audit.get("unresolved_grp_ids") or []:
        out.append(Finding(record_id, "unresolved_permanent", f"grpId {grp_id} did not resolve to a card"))
    for entry in audit.get("numeric_type_codes") or []:
        out.append(Finding(record_id, "numeric_type_code", f"type line {entry!r} is a numeric type code"))
    for grp_id in audit.get("missing_type_line_grp_ids") or []:
        out.append(Finding(record_id, "missing_type_line", f"grpId {grp_id} resolved but has no type line"))

    lands = int(audit.get("untapped_local_lands") or 0)
    mana = int(audit.get("mana_total") or 0)
    if lands > 0 and mana == 0:
        out.append(
            Finding(
                record_id,
                "land_yields_no_mana",
                f"{lands} untapped land(s) on your battlefield but the mana pool is 0",
            )
        )

    return out


def check_decision(
    record_id: str, game_state: dict, *, local_seat: int, mana_total: int
) -> tuple[dict, list[Finding]]:
    """Build-time check. Returns ``(audit, findings)``."""
    audit = audit_game_state(game_state, local_seat=local_seat, mana_total=mana_total)
    return audit, findings_from_audit(record_id, audit)


def check_corpus_record(record: dict) -> list[Finding]:
    """Post-hoc check of one built corpus record.

    Requires the ``card_facts_audit`` provenance block. A corpus without it
    predates this check and cannot be verified — that is reported as a finding
    rather than passed, because "cannot verify" and "verified clean" are the
    exact two things this whole exercise exists to stop conflating.
    """
    record_id = str(record.get("id") or "?")
    audit = (record.get("meta") or {}).get(AUDIT_KEY)
    if not isinstance(audit, dict):
        return [
            Finding(
                record_id,
                "no_provenance",
                f"record has no meta.{AUDIT_KEY} block — built before the self-check existed, "
                "so its card facts cannot be verified; rebuild the corpus",
            )
        ]

    return findings_from_audit(record_id, audit)


def scan_corpus_file(path: Path) -> list[Finding]:
    """Check every record in a built corpus JSONL."""
    findings: list[Finding] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                findings.extend(check_corpus_record(json.loads(line)))
    return findings


def summarise(findings: list[Finding]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    records: set[str] = set()
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
        records.add(f.record_id)
    return {"findings": len(findings), "records_affected": len(records), "by_kind": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("corpora", nargs="+", type=Path, help="built corpus JSONL files")
    parser.add_argument("--max-examples", type=int, default=10)
    args = parser.parse_args(argv)

    failed = False
    for path in args.corpora:
        if not path.exists():
            print(f"MISSING: {path}", file=sys.stderr)
            failed = True
            continue
        findings = scan_corpus_file(path)
        if findings:
            failed = True
            print(f"REJECTED {path}: {json.dumps(summarise(findings))}", file=sys.stderr)
            for f in findings[: args.max_examples]:
                print(f"    {f}", file=sys.stderr)
            if len(findings) > args.max_examples:
                print(f"    ... and {len(findings) - args.max_examples} more", file=sys.stderr)
        else:
            print(f"OK {path}")

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
