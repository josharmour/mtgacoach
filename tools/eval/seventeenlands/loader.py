"""Streaming column-projecting CSV loader for 17lands replay data.

The replay CSVs are 2,625 columns × millions of rows (~2-3 GB uncompressed).
We never want all of that in memory. ``iter_rows`` streams the gzipped CSV
and yields one dict per row, including only the columns the caller asks for.
"""

from __future__ import annotations

import csv
import gzip
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Optional


def iter_rows(
    csv_gz_path: Path,
    columns: Iterable[str],
    *,
    limit: Optional[int] = None,
    where: Optional[dict] = None,
) -> Iterator[dict]:
    """Yield one dict per row containing only the requested columns.

    Args:
        csv_gz_path: Path to the gzipped CSV file.
        columns: Column names to extract per row.
        limit: Stop after this many rows (post-filter).
        where: Equality filter dict applied before yielding (e.g.
               ``{"event_type": "PremierDraft", "won": "True"}``). Compares as
               strings (CSV values are unparsed strings).
    """
    columns = list(columns)
    where = where or {}
    # Always read filter columns even if the caller didn't ask for them, then
    # project at yield time.
    read_cols = list({*columns, *where.keys()})

    with gzip.open(csv_gz_path, mode="rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        col_idx = {name: i for i, name in enumerate(header)}

        # Validate requested columns exist (fail fast, friendly error).
        missing = [c for c in read_cols if c not in col_idx]
        if missing:
            raise KeyError(
                f"columns missing from CSV header: {missing[:5]}"
                + (f" (and {len(missing) - 5} more)" if len(missing) > 5 else "")
            )

        proj_idx = [(c, col_idx[c]) for c in columns]
        filt_idx = [(c, col_idx[c], v) for c, v in where.items()]

        yielded = 0
        for row in reader:
            if not row:
                continue
            # Apply filter
            if filt_idx and any(row[i] != v for _, i, v in filt_idx):
                continue
            yield {c: row[i] for c, i in proj_idx}
            yielded += 1
            if limit is not None and yielded >= limit:
                return


# -- Helpers for the shape 17lands uses --------------------------------------


def parse_grpid_list(value: str) -> list[int]:
    """Parse a pipe-separated grpid list (e.g. '96628|96823|96804') into ints.

    Empty / blank fields return an empty list.
    """
    if not value:
        return []
    out: list[int] = []
    for tok in value.split("|"):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


def parse_bool(value: str) -> Optional[bool]:
    """Parse 17lands' string boolean. Returns None for empty/unknown."""
    v = (value or "").strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return None


# Standard rank tiers in 17lands data, ordered worst -> best. We keep the
# string form because that's how the dataset stores them.
RANK_ORDER = ("bronze", "silver", "gold", "platinum", "diamond", "mythic")


def rank_tier(rank: str, *, min_tier: str = "diamond") -> bool:
    """True if ``rank`` is at least ``min_tier`` (defaults to diamond+).

    Used to filter the eval to higher-skill samples — at low ranks the
    "actually played" decision is too noisy to be ground truth.
    """
    rank = (rank or "").strip().lower()
    if rank not in RANK_ORDER:
        return False
    return RANK_ORDER.index(rank) >= RANK_ORDER.index(min_tier)
