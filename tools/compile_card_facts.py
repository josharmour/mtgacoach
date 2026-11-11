"""Compile card-facts records for named cards or Arena IDs.

Offline companion to the coach: one model call per card, the answer validated
against the printed card (Scryfall text) and written as one JSON file per card
under --out, named by rules_hash. See arenamcp/card_facts/compiler.py.

    python tools/compile_card_facts.py --names "Fury,Chrome Mox" --out ~/.arenamcp/card_facts
    python tools/compile_card_facts.py --names-file cards.txt --out out/
    python tools/compile_card_facts.py --ids 12345 --out out/

Run from the repo root with the package on the path (PYTHONPATH=src, or an
installed arenamcp).

Cards whose rules_hash already has a file in --out are skipped without a model
call, so the tool is safe to re-run. Prints the run summary as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from arenamcp.card_facts.compiler import compile_cards


def _read_list_file(path: str) -> list[str]:
    entries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def _split_names(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--names", help="comma-separated card names")
    parser.add_argument("--names-file", help="file with one card name per line")
    parser.add_argument("--ids", help="comma-separated Arena card IDs")
    parser.add_argument("--ids-file", help="file with one Arena card ID per line")
    parser.add_argument("--out", required=True, help="directory for the JSON records")
    args = parser.parse_args(argv)

    identifiers: list[int | str] = []
    if args.names:
        identifiers.extend(_split_names(args.names))
    if args.names_file:
        identifiers.extend(_read_list_file(args.names_file))
    if args.ids:
        identifiers.extend(int(part) for part in _split_names(args.ids))
    if args.ids_file:
        identifiers.extend(int(entry) for entry in _read_list_file(args.ids_file))
    if not identifiers:
        parser.error("nothing to compile: pass --names/--names-file/--ids/--ids-file")

    summary = compile_cards(identifiers, args.out)
    print(json.dumps(summary, indent=2))
    return 0 if summary["compiled"] + summary["skipped"] else 1


if __name__ == "__main__":
    sys.exit(main())
