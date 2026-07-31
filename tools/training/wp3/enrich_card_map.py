#!/usr/bin/env python3
"""Enrich magezero_card_map.json with oracle text from the LOCAL Scryfall bulk.

The MageZero corpus renders card names without oracle text because the card
map (built by resolve_cards.py) never carried it. This script adds, from the
local Scryfall bulk cache ONLY (no network — fail closed):

  - ``oracle_text``  (all faces joined with " // ", reminder text kept: the
                      production formatter strips it at render time)
  - ``power`` / ``toughness``  (printed; stored for future use, NOT rendered)

and adds NEW entries for names that appear in decisions JSONL files but are
missing from the map (tokens, DFC faces). Names that resolve nowhere locally
are counted and reported — never fabricated.

Usage
-----
    python3 tools/training/wp3/enrich_card_map.py \
        --names-from tools/training/data/wp3_v2_combat/decisions.jsonl \
        --names-from tools/training/data/wp3_v2_combat/combat_decisions.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MAP_PATH = REPO / "tools" / "training" / "magezero_card_map.json"
BULK_PATH = Path.home() / ".arenamcp" / "cache" / "scryfall" / "default_cards.json"

_COMBAT_SUFFIXES = (",attacking", ",blocking")


def _oracle_from(card: dict) -> str:
    text = card.get("oracle_text")
    if text:
        return text
    faces = card.get("card_faces") or []
    joined = " // ".join(f.get("oracle_text") or "" for f in faces).strip(" /")
    return joined


def _pt_from(card: dict) -> tuple[str | None, str | None]:
    p, t = card.get("power"), card.get("toughness")
    if p is None or t is None:
        for f in card.get("card_faces") or []:
            if f.get("power") is not None:
                return f.get("power"), f.get("toughness")
    return p, t


def load_bulk_indexes(bulk_path: Path) -> tuple[dict, dict, dict]:
    """Return (by_name, by_face_name, tokens_by_name), all lowercase-keyed.

    Non-token printings win name collisions; the first token printing wins
    within tokens_by_name.
    """
    with open(bulk_path, encoding="utf-8") as f:
        cards = json.load(f)
    by_name: dict[str, dict] = {}
    by_face: dict[str, dict] = {}
    tokens: dict[str, dict] = {}
    for c in cards:
        name = (c.get("name") or "").lower()
        if not name:
            continue
        layout = c.get("layout") or ""
        is_token = "token" in layout
        if is_token:
            tokens.setdefault(name, c)
            # Double-faced tokens ("Incubator // Phyrexian"): XMage names one
            # face ("Incubator Token"), so register faces too.
            for f in c.get("card_faces") or []:
                fname = (f.get("name") or "").lower()
                if fname:
                    tokens.setdefault(fname, c)
        else:
            by_name.setdefault(name, c)
            for f in c.get("card_faces") or []:
                fname = (f.get("name") or "").lower()
                if fname:
                    by_face.setdefault(fname, c)
    return by_name, by_face, tokens


import re as _re


def _clean(n: str) -> str:
    """Apply the same log-noise strips the parsers now apply at source
    (defensive: decisions files built by older parser code carry them)."""
    for suf in _COMBAT_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)]
    return _re.sub(r":\d+$", "", n).strip()


def collect_names(paths: list[Path]) -> set[str]:
    names: set[str] = set()
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                for n in row.get("hand", []) or []:
                    names.add(_clean(n))
                for side in ("battlefield_self", "battlefield_opp"):
                    for c in row.get(side, []) or []:
                        n = c.get("name", "") if isinstance(c, dict) else str(c)
                        names.add(_clean(n))
                if row.get("creature"):
                    names.add(_clean(row["creature"]))
    names.discard("")
    return names


def resolve(name: str, entry: dict | None, by_name: dict, by_face: dict, tokens: dict) -> tuple[dict | None, str]:
    """Return (bulk_card, how) for a map name, or (None, 'miss')."""
    low = name.lower()
    # Token-suffixed XMage names ("Map Token") -> Scryfall token "Map"
    if low.endswith(" token"):
        base = low[: -len(" token")].strip()
        if base in tokens:
            return tokens[base], "token"
        if base in by_name:
            return by_name[base], "token_as_card"
    if entry:
        canon = (entry.get("scryfall") or "").lower()
        if canon and canon in by_name:
            return by_name[canon], "canonical"
    if low in by_name:
        return by_name[low], "exact"
    if low in by_face:
        return by_face[low], "face"
    if low in tokens:
        return tokens[low], "token_exact"
    return None, "miss"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--names-from", action="append", type=Path, default=[],
                    help="decisions JSONL to scan for card names missing from the map")
    ap.add_argument("--map", type=Path, default=MAP_PATH)
    ap.add_argument("--bulk", type=Path, default=BULK_PATH)
    args = ap.parse_args(argv)

    if not args.bulk.exists():
        print(f"FAIL CLOSED: local Scryfall bulk not found at {args.bulk}; no network fallback.",
              file=sys.stderr)
        return 2

    card_map: dict[str, dict] = json.loads(args.map.read_text(encoding="utf-8"))
    print(f"card map: {len(card_map)} entries")

    extra = collect_names(args.names_from)
    new_names = sorted(n for n in extra if n not in card_map)
    print(f"decisions scan: {len(extra)} distinct names, {len(new_names)} not in map: {new_names}")

    print(f"loading local bulk {args.bulk} ...")
    by_name, by_face, tokens = load_bulk_indexes(args.bulk)
    print(f"bulk: {len(by_name)} names, {len(by_face)} faces, {len(tokens)} tokens")

    enriched = 0
    misses: list[str] = []
    how_hist: dict[str, int] = {}
    for name in sorted(set(card_map) | set(new_names)):
        entry = card_map.get(name)
        card, how = resolve(name, entry, by_name, by_face, tokens)
        how_hist[how] = how_hist.get(how, 0) + 1
        if card is None:
            misses.append(name)
            if entry is None:
                card_map[name] = {
                    "exact": False,
                    "found": False,
                    "scryfall": name,
                    "type_line": "",
                    "mana_cost": "",
                    "oracle_source": "none_local",
                }
            else:
                entry["oracle_source"] = "none_local"
            continue
        if entry is None:
            entry = {
                "exact": False,
                "found": True,
                "scryfall": card.get("name", name),
                "type_line": card.get("type_line", ""),
                "mana_cost": card.get("mana_cost", ""),
            }
            card_map[name] = entry
        entry["oracle_text"] = _oracle_from(card)
        p, t = _pt_from(card)
        if p is not None and t is not None:
            entry["power"], entry["toughness"] = p, t
        entry["oracle_source"] = f"local_bulk:{how}"
        enriched += 1

    args.map.write_text(json.dumps(card_map, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"wrote {args.map}: {len(card_map)} entries, {enriched} enriched, "
          f"{len(misses)} unresolved locally")
    print(f"resolution histogram: {dict(sorted(how_hist.items()))}")
    if misses:
        print("UNRESOLVED (counted, not fabricated):")
        for m in misses:
            print(f"  - {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
